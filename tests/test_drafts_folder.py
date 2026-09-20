"""Finding the Drafts folder, whatever language the mailbox is in.

"[Gmail]/Drafts" is only its English name. On a Polish account it is
"[Gmail]/Wersje robocze", on a German one "[Gmail]/Entwurfe" - so a hardcoded
name made every single draft fail with a bare "the mail server refused to save
the draft (status NO)", with nothing in the message to say what was wrong.

The server knows the answer: IMAP LIST marks the folder with the special-use
flag \\Drafts whatever it is called. These tests pin that it is asked.
"""

import pytest

from email_workflow.models.config import EmailConfig
from email_workflow.providers.email_provider import GmailProvider, _quote_mailbox
from email_workflow.core.errors import EmailProviderError

POLISH_MAILBOX = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
    b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Wersje robocze"',
    b'(\\HasNoChildren \\All) "/" "[Gmail]/Wszystkie"',
]

ENGLISH_MAILBOX = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
]

# An older server that sends no special-use flags at all.
NO_FLAGS_MAILBOX = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasNoChildren) "/" "INBOX.Drafts"',
]

# The same Polish mailbox, but with the folder name sent as an IMAP literal -
# which is exactly how a server may send a name with non-ASCII characters in
# it. imaplib hands that back as a tuple, not a line of bytes.
LITERAL_MAILBOX = [
    b'(\\HasNoChildren) "/" "INBOX"',
    (b'(\\HasNoChildren \\Drafts) "/" {14}', b"Wersje robocze"),
    b")",
]


class FakeIMAP:
    instances = []
    folders = POLISH_MAILBOX
    append_status = "OK"

    def __init__(self, host):
        self.host = host
        self.appended = []
        FakeIMAP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        return ("OK", [b"ok"])

    def list(self, directory='""', pattern="*"):
        return ("OK", list(FakeIMAP.folders))

    def append(self, mailbox, flags, date_time, message):
        self.appended.append(mailbox)
        return (FakeIMAP.append_status, [b""])


@pytest.fixture(autouse=True)
def imap(monkeypatch):
    FakeIMAP.instances.clear()
    FakeIMAP.folders = POLISH_MAILBOX
    FakeIMAP.append_status = "OK"
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.delenv("IMAP_DRAFTS_FOLDER", raising=False)
    monkeypatch.setattr(
        "email_workflow.providers.email_provider.imaplib.IMAP4_SSL", FakeIMAP
    )
    yield


def gmail() -> GmailProvider:
    return GmailProvider(EmailConfig(provider="gmail", create_drafts=True))


def draft_went_to() -> str:
    return FakeIMAP.instances[0].appended[0]


# --- where the draft is written --------------------------------------------

def test_a_polish_mailbox_gets_its_own_drafts_folder():
    """The failure he actually saw: status NO, because the folder is called
    "Wersje robocze" and the app asked for "[Gmail]/Drafts"."""
    gmail().create_draft("<m1@x>", "Re: hello", "body", "ann@example.com")
    assert "Wersje robocze" in draft_went_to()


def test_an_english_mailbox_still_works():
    FakeIMAP.folders = ENGLISH_MAILBOX
    gmail().create_draft("<m1@x>", "Re: hello", "body", "ann@example.com")
    assert "[Gmail]/Drafts" in draft_went_to()


def test_a_server_with_no_special_use_flags_falls_back_to_a_known_name():
    FakeIMAP.folders = NO_FLAGS_MAILBOX
    gmail().create_draft("<m1@x>", "Re: hello", "body", "ann@example.com")
    assert "INBOX.Drafts" in draft_went_to()


def test_a_folder_name_sent_as_a_literal_is_still_found():
    """imaplib returns a tuple for those, not a line of bytes. Reading it as a
    string threw, the error was swallowed, and the lookup quietly fell back to
    guessing English names - on exactly the kind of mailbox this is for."""
    FakeIMAP.folders = LITERAL_MAILBOX
    gmail().create_draft("<m1@x>", "Re: hello", "body", "ann@example.com")
    assert "Wersje robocze" in draft_went_to()
    assert "{14}" not in draft_went_to(), "the literal's byte count is not part of the name"


def test_your_own_setting_wins_over_the_server(monkeypatch):
    monkeypatch.setenv("IMAP_DRAFTS_FOLDER", "Moje wersje")
    gmail().create_draft("<m1@x>", "Re: hello", "body", "ann@example.com")
    assert "Moje wersje" in draft_went_to()


def test_the_folder_name_is_quoted_so_a_space_does_not_split_it():
    """An unquoted name with a space is a different command to the server,
    which is how a folder that exists comes back as "no such folder"."""
    gmail().create_draft("<m1@x>", "Re: hello", "body", "ann@example.com")
    assert draft_went_to().startswith('"') and draft_went_to().endswith('"')


def test_a_name_that_is_already_quoted_is_not_quoted_twice():
    assert _quote_mailbox('"[Gmail]/Drafts"') == '"[Gmail]/Drafts"'
    assert _quote_mailbox("[Gmail]/Drafts") == '"[Gmail]/Drafts"'


# --- when it still will not take it ----------------------------------------

def test_a_refused_draft_names_the_folder_and_lists_what_the_mailbox_has():
    """The old message named a folder the app had invented and stopped there.
    Seeing the real folder names is what makes this fixable by the person
    reading it."""
    FakeIMAP.append_status = "NO"
    with pytest.raises(EmailProviderError) as failure:
        gmail().create_draft("<m1@x>", "Re: hello", "body", "ann@example.com")

    assert "Wersje robocze" in failure.value.message
    assert "IMAP_DRAFTS_FOLDER" in failure.value.hint
    assert "[Gmail]/Spam" in failure.value.hint, "it should show the real folders"
