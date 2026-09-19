"""Tests for actually sending and drafting mail.

These pin the worst class of bug this app can have: reporting that a reply was
sent when nothing left the machine. `send_message` had been commented out, so
send_email returned a success id while the audit log, the digest and the user
were all told a reply had gone out.
"""

import pytest

from email_workflow.core.errors import EmailProviderError
from email_workflow.models.config import AppConfig, EmailConfig
from email_workflow.models.email import EmailMessage, SenderInfo
from email_workflow.providers.email_provider import GmailProvider


class FakeSMTP:
    """Records what actually reached the transport."""

    instances = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sent = []
        self.logged_in = False
        self.tls = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.logged_in = True

    def send_message(self, msg):
        self.sent.append(msg)


class FakeIMAPAppend:
    instances = []

    def __init__(self, host, append_status="OK", explode=False):
        self.host = host
        self.appended = []
        self.append_status = append_status
        self.explode = explode
        FakeIMAPAppend.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        return ("OK", [b"ok"])

    def append(self, folder, flags, date, message):
        if self.explode:
            raise OSError("connection reset")
        self.appended.append((folder, message))
        return (self.append_status, [b""])


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    FakeSMTP.instances.clear()
    FakeIMAPAppend.instances.clear()
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr(
        "email_workflow.providers.email_provider.smtplib.SMTP", FakeSMTP
    )
    yield


def provider() -> GmailProvider:
    return GmailProvider(EmailConfig(provider="gmail"))


# --- the blocker: it must really send --------------------------------------

def test_send_email_actually_hands_the_message_to_smtp():
    sent_id = provider().send_email(
        "<m1@example.com>", "Re: hello", "Here you go.", to_address="ann@example.com"
    )

    assert len(FakeSMTP.instances) == 1
    transport = FakeSMTP.instances[0]
    assert len(transport.sent) == 1, (
        "send_email returned an id but nothing reached SMTP - this is exactly "
        "the bug where the audit log claimed a reply had been sent"
    )
    assert sent_id.startswith("sent_gmail_")


def test_the_sent_message_has_a_recipient():
    provider().send_email("<m1@x>", "Re: hello", "Body", to_address="ann@example.com")
    msg = FakeSMTP.instances[0].sent[0]
    assert msg["To"] == "ann@example.com", "a message with no To header reaches nobody"
    assert msg["From"] == "me@example.com"
    assert msg["Subject"] == "Re: hello"


def test_the_reply_is_threaded_to_the_original():
    provider().send_email("<m1@x>", "Re: hello", "Body", to_address="ann@example.com")
    msg = FakeSMTP.instances[0].sent[0]
    assert msg["In-Reply-To"] == "<m1@x>"
    assert msg["References"] == "<m1@x>"


def test_the_body_survives():
    provider().send_email("<m1@x>", "Re: hi", "Thursday works.", to_address="ann@example.com")
    msg = FakeSMTP.instances[0].sent[0]
    assert "Thursday works." in msg.get_payload(decode=True).decode("utf-8")


def test_it_connects_securely_and_authenticates():
    provider().send_email("<m1@x>", "Re: hi", "Body", to_address="ann@example.com")
    transport = FakeSMTP.instances[0]
    assert transport.tls, "STARTTLS must run before credentials are sent"
    assert transport.logged_in


def test_sending_without_a_recipient_is_refused():
    with pytest.raises(EmailProviderError, match="no recipient"):
        provider().send_email("<m1@x>", "Re: hi", "Body")
    assert not FakeSMTP.instances, "nothing should reach SMTP without a recipient"


def test_missing_credentials_raise_before_connecting(monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("NOTIFICATION_SENDER_PASSWORD", raising=False)
    p = GmailProvider(EmailConfig(provider="gmail"))
    p.password = ""
    with pytest.raises(EmailProviderError):
        p.send_email("<m1@x>", "Re: hi", "Body", to_address="ann@example.com")
    assert not FakeSMTP.instances


def test_an_smtp_failure_is_reported_not_swallowed(monkeypatch):
    class BrokenSMTP(FakeSMTP):
        def send_message(self, msg):
            raise OSError("mailbox unavailable")

    monkeypatch.setattr(
        "email_workflow.providers.email_provider.smtplib.SMTP", BrokenSMTP
    )
    with pytest.raises(EmailProviderError, match="Could not send"):
        provider().send_email("<m1@x>", "Re: hi", "Body", to_address="ann@example.com")


# --- drafts must not fake success ------------------------------------------

def _use_imap(monkeypatch, **kwargs):
    monkeypatch.setattr(
        "email_workflow.providers.email_provider.imaplib.IMAP4_SSL",
        lambda host: FakeIMAPAppend(host, **kwargs),
    )


def test_a_draft_is_really_appended(monkeypatch):
    _use_imap(monkeypatch)
    draft_id = provider().create_draft(
        "<m1@x>", "Re: hi", "Body", to_address="ann@example.com"
    )
    assert draft_id.startswith("draft_gmail_")
    assert len(FakeIMAPAppend.instances[0].appended) == 1


def test_a_refused_append_is_an_error_not_a_fake_id(monkeypatch):
    _use_imap(monkeypatch, append_status="NO")
    with pytest.raises(EmailProviderError, match="refused to save the draft"):
        provider().create_draft("<m1@x>", "Re: hi", "Body", to_address="ann@example.com")


def test_a_broken_connection_while_drafting_is_an_error(monkeypatch):
    _use_imap(monkeypatch, explode=True)
    with pytest.raises(EmailProviderError, match="Could not save the draft"):
        provider().create_draft("<m1@x>", "Re: hi", "Body", to_address="ann@example.com")


def test_the_drafts_folder_can_be_overridden(monkeypatch):
    """A non-English Gmail does not have a folder called [Gmail]/Drafts."""
    monkeypatch.setenv("IMAP_DRAFTS_FOLDER", '"[Gmail]/Wersje robocze"')
    _use_imap(monkeypatch)
    provider().create_draft("<m1@x>", "Re: hi", "Body", to_address="ann@example.com")
    folder, _ = FakeIMAPAppend.instances[0].appended[0]
    assert folder == '"[Gmail]/Wersje robocze"'


# --- archiving stays non-fatal but stops hiding the reason ------------------

def test_a_failed_archive_does_not_stop_the_run(monkeypatch):
    def explode(host):
        raise OSError("imap down")

    monkeypatch.setattr(
        "email_workflow.providers.email_provider.imaplib.IMAP4_SSL", explode
    )
    p = provider()
    p.archive_email("<m1@x>")  # must not raise
    assert "imap down" in p.last_archive_error
    assert "next run" in p.last_archive_error, (
        "the message should say the email is untouched and will come back"
    )


# --- sending is opt-in ------------------------------------------------------

def test_sending_is_off_by_default():
    assert AppConfig().email.allow_send is False, (
        "sending email on someone's behalf cannot be undone, so it must be "
        "switched on deliberately"
    )
