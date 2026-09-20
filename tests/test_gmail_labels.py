"""Starring mail that matters, and saying truthfully when it did not work.

What he saw:

    Could not star <...@linkedin.com> on imap.gmail.com:
    STORE command error: BAD .

Two bugs in one line. "Important" is one of Gmail's OWN labels - his mailbox
lists it as (\\HasNoChildren \\Important) "[Gmail]/Wazne" - so asking Gmail to
put a user label of that name on a message is refused with BAD. And the star
itself had already been applied by then: the label and the flag shared one try
block, so one refusal threw away the report of the other and blamed the part
that had actually worked.
"""

import imaplib

import pytest

from email_workflow.models.config import EmailConfig
from email_workflow.providers.email_provider import GmailProvider, gmail_label


class FakeIMAP:
    instances = []
    refuse_labels = False       # Gmail's BAD on a reserved label
    refuse_flags = False

    def __init__(self, host):
        self.host = host
        self.stored = []
        FakeIMAP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        return ("OK", [b"ok"])

    def select(self, mailbox):
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        return ("OK", [b"7"])

    def store(self, num, mode, value):
        if "X-GM-LABELS" in mode and FakeIMAP.refuse_labels:
            # Exactly how imaplib surfaces it: raised, not returned.
            raise imaplib.IMAP4.error("STORE command error: BAD .")
        if mode == "+FLAGS" and FakeIMAP.refuse_flags:
            raise imaplib.IMAP4.error("STORE command error: BAD .")
        self.stored.append((num, mode, value))
        return ("OK", [b""])


@pytest.fixture(autouse=True)
def imap(monkeypatch):
    FakeIMAP.instances.clear()
    FakeIMAP.refuse_labels = False
    FakeIMAP.refuse_flags = False
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr(
        "email_workflow.providers.email_provider.imaplib.IMAP4_SSL", FakeIMAP
    )
    yield


def gmail(**kwargs) -> GmailProvider:
    kwargs.setdefault("star_important", True)
    return GmailProvider(EmailConfig(provider="gmail", **kwargs))


def stored():
    return FakeIMAP.instances[0].stored


# --- the reserved name itself -----------------------------------------------

def test_gmails_own_label_names_are_moved_out_of_the_way():
    """His mailbox really does list \\Important as a system label."""
    assert gmail_label("Important") == '"AI/Important"'
    assert gmail_label("starred") == '"AI/starred"'
    assert gmail_label("Trash") == '"AI/Trash"'


def test_a_label_of_your_own_is_left_alone():
    assert gmail_label("AI/Important") == '"AI/Important"'
    assert gmail_label("Job hunt") == '"Job hunt"'


def test_a_gmail_system_label_can_still_be_asked_for_on_purpose():
    """Written the way IMAP writes them, with the backslash, and unquoted."""
    assert gmail_label("\\Important") == "\\Important"
    assert gmail_label("\\Starred") == "\\Starred"


def test_a_quote_in_a_label_cannot_break_the_command():
    assert gmail_label('we"ird') == '"we\\"ird"'


def test_the_shipped_default_is_not_one_of_gmails():
    """The default was changed to "Important" and every star broke."""
    assert EmailConfig().important_label.lower() not in {
        "important", "starred", "inbox", "spam", "trash"
    }


# --- what actually reaches the server ---------------------------------------

def test_a_reserved_label_in_the_config_is_still_applied_safely():
    gmail(important_label="Important").flag_email("<m1@x>")
    labels = [value for _, mode, value in stored() if "X-GM-LABELS" in mode]
    assert labels == ['"AI/Important"'], (
        "the bare name is what Gmail refuses with BAD"
    )


def test_the_star_goes_on_first_and_is_not_lost_if_the_label_is_refused():
    """His exact failure. The star had already been applied when the label was
    refused, and the refusal threw away the fact."""
    FakeIMAP.refuse_labels = True
    provider = gmail(important_label="Important")
    provider.flag_email("<m1@x>")

    flags = [value for _, mode, value in stored() if mode == "+FLAGS"]
    assert flags == ["\\Flagged"], "the star must still be applied"


def test_a_refused_label_is_reported_as_a_label_not_as_a_star():
    """It said "Could not star", which was not the part that failed."""
    FakeIMAP.refuse_labels = True
    provider = gmail(important_label="Important")
    provider.flag_email("<m1@x>")

    problem = provider.last_archive_error
    assert "label" in problem.lower()
    assert "could not star" not in problem.lower(), (
        "starring worked - saying otherwise sends him looking for the wrong thing"
    )


def test_a_refused_star_is_reported_as_a_star():
    FakeIMAP.refuse_flags = True
    provider = gmail(important_label="AI/Important")
    provider.flag_email("<m1@x>")
    assert "star" in provider.last_archive_error.lower()


def test_both_failing_is_reported_as_both():
    """One overwriting the other hides half of what happened."""
    FakeIMAP.refuse_flags = True
    FakeIMAP.refuse_labels = True
    provider = gmail(important_label="AI/Important")
    provider.flag_email("<m1@x>")

    problem = provider.last_archive_error.lower()
    assert "star" in problem and "label" in problem


def test_nothing_is_reported_when_it_all_worked():
    provider = gmail(important_label="AI/Important")
    provider.flag_email("<m1@x>")
    assert provider.last_archive_error == ""


def test_starring_can_still_be_switched_off():
    provider = gmail(star_important=False)
    provider.flag_email("<m1@x>")
    assert stored() == [] if FakeIMAP.instances else True
