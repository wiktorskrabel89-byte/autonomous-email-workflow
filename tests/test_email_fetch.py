"""Tests for how mail is fetched from a real mailbox.

Two real bugs are pinned here:
  - FETCH (RFC822) sets the \\Seen flag as a side effect, so a crash halfway
    through a run would leave emails marked read and never processed again.
    BODY.PEEK[] must be used instead.
  - The IMAP SINCE date must be English (18-Sep-2026). strftime("%b") follows
    the machine locale, so on a Polish Windows it would emit "wrz" and the
    server would reject the search.
"""

from datetime import datetime, timedelta

import pytest

from email_workflow.core.errors import EmailProviderError
from email_workflow.models.config import EmailConfig
from email_workflow.providers.email_provider import (
    GmailProvider,
    _imap_date,
    get_email_provider,
)

RAW = (
    b"From: Ann Lee <ann@example.com>\r\n"
    b"Subject: Can we meet?\r\n"
    b"Message-ID: <m1@example.com>\r\n"
    b"Date: Thu, 18 Sep 2026 10:00:00 +0000\r\n"
    b"\r\n"
    b"Are you free Thursday?\r\n"
)


class FakeIMAP:
    """Records what the provider asks the server to do."""

    instances = []

    def __init__(self, host):
        self.host = host
        self.searches = []
        self.fetches = []
        self.selected = None
        self.message_ids = [b"1", b"2", b"3"]
        FakeIMAP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        return ("OK", [b"ok"])

    def select(self, mailbox):
        self.selected = mailbox
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        self.searches.append(criteria)
        return ("OK", [b" ".join(self.message_ids)])

    def fetch(self, msg_id, parts):
        self.fetches.append((msg_id, parts))
        return ("OK", [(b"1 (BODY[] {10}", RAW), b")"])


@pytest.fixture(autouse=True)
def fake_imap(monkeypatch):
    FakeIMAP.instances.clear()
    monkeypatch.setattr(
        "email_workflow.providers.email_provider.imaplib.IMAP4_SSL", FakeIMAP
    )
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    yield


def provider_with(**kwargs) -> GmailProvider:
    return GmailProvider(EmailConfig(provider="gmail", **kwargs))


# --- the unread + recent window ---------------------------------------------

def test_search_asks_for_unread_only():
    provider_with().fetch_unprocessed_emails()
    criteria = FakeIMAP.instances[0].searches[0]
    assert "UNSEEN" in criteria


def test_search_limits_to_the_configured_window():
    provider_with(max_age_days=7).fetch_unprocessed_emails()
    criteria = FakeIMAP.instances[0].searches[0]

    assert "SINCE" in criteria
    since = criteria[criteria.index("SINCE") + 1]
    expected = _imap_date(datetime.now() - timedelta(days=7))
    assert since == expected


def test_a_longer_window_asks_for_an_earlier_date():
    provider_with(max_age_days=30).fetch_unprocessed_emails()
    since = FakeIMAP.instances[0].searches[0][-1]
    assert since == _imap_date(datetime.now() - timedelta(days=30))


def test_zero_or_negative_age_is_clamped_to_one_day():
    provider_with(max_age_days=0).fetch_unprocessed_emails()
    since = FakeIMAP.instances[0].searches[0][-1]
    assert since == _imap_date(datetime.now() - timedelta(days=1))


# --- how many get processed -------------------------------------------------

def test_no_limit_by_default_processes_everything_in_the_window():
    messages = provider_with().fetch_unprocessed_emails()
    assert len(FakeIMAP.instances[0].fetches) == 3
    assert len(messages) == 3


def test_an_explicit_cap_keeps_only_the_newest():
    provider_with(max_emails_per_run=2).fetch_unprocessed_emails()
    fetched = [msg_id for msg_id, _ in FakeIMAP.instances[0].fetches]
    assert fetched == [b"2", b"3"], "a cap should keep the newest, not the oldest"


def test_zero_means_unlimited():
    provider_with(max_emails_per_run=0).fetch_unprocessed_emails()
    assert len(FakeIMAP.instances[0].fetches) == 3


# --- the read-flag bug ------------------------------------------------------

def test_fetch_uses_peek_so_mail_is_not_marked_read():
    provider_with().fetch_unprocessed_emails()
    for _, parts in FakeIMAP.instances[0].fetches:
        assert "PEEK" in parts, (
            "fetching with RFC822 sets the Seen flag; a crash would then lose "
            "these emails permanently"
        )
        assert "RFC822" not in parts


# --- parsing ----------------------------------------------------------------

def test_sender_name_and_address_are_split():
    messages = provider_with().fetch_unprocessed_emails()
    assert messages[0].sender.name == "Ann Lee"
    assert messages[0].sender.email == "ann@example.com"


def test_subject_and_body_survive():
    messages = provider_with().fetch_unprocessed_emails()
    assert messages[0].subject == "Can we meet?"
    assert "Thursday" in messages[0].body


def test_empty_mailbox_returns_nothing(monkeypatch):
    class EmptyIMAP(FakeIMAP):
        def search(self, charset, *criteria):
            self.searches.append(criteria)
            return ("OK", [b""])

    monkeypatch.setattr(
        "email_workflow.providers.email_provider.imaplib.IMAP4_SSL", EmptyIMAP
    )
    assert provider_with().fetch_unprocessed_emails() == []


def test_missing_credentials_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("NOTIFICATION_SENDER_PASSWORD", raising=False)
    provider = GmailProvider(EmailConfig(provider="gmail", account_ref=""))
    monkeypatch.setenv("GMAIL_ADDRESS", "")
    provider.address = ""
    provider.password = ""
    # An EmailProviderError, not a bare ValueError: the CLI knows how to show
    # one of those as advice, and it carries the app-password instructions.
    with pytest.raises(EmailProviderError, match="credentials") as failure:
        provider.fetch_unprocessed_emails()

    assert "2-Step Verification" in failure.value.hint, (
        "an app password cannot even be created until that is on, and Google's "
        "own page never says so"
    )


# --- date helper ------------------------------------------------------------

@pytest.mark.parametrize(
    "when,expected",
    [
        (datetime(2026, 1, 1), "01-Jan-2026"),
        (datetime(2026, 9, 18), "18-Sep-2026"),
        (datetime(2026, 12, 31), "31-Dec-2026"),
        (datetime(2026, 2, 5), "05-Feb-2026"),
    ],
)
def test_imap_dates_are_always_english(when, expected):
    assert _imap_date(when) == expected


# --- provider selection -----------------------------------------------------

@pytest.mark.parametrize(
    "name,imap_host",
    [
        ("gmail", "imap.gmail.com"),
        ("outlook", "outlook.office365.com"),
        ("imap_generic", "imap.mail.com"),
    ],
)
def test_each_provider_gets_its_own_server(name, imap_host, monkeypatch):
    monkeypatch.delenv("IMAP_SERVER", raising=False)
    provider = get_email_provider(EmailConfig(provider=name))
    assert provider.imap_server == imap_host


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="Unknown email provider"):
        get_email_provider(EmailConfig(provider="carrier-pigeon"))
