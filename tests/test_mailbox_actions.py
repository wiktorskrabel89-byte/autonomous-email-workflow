"""Tests for what the app does to the mailbox itself.

Archiving used to only mark mail as read, which left everything sitting in the
inbox - nothing visibly happened. And nothing was ever starred, so the mail that
actually needed a person looked the same as everything else.
"""

import pytest

from email_workflow.core.pipeline import WorkflowPipeline
from email_workflow.models.config import AppConfig, EmailConfig
from email_workflow.models.email import (
    DecisionOption,
    EmailCategory,
    EmailMessage,
    SenderInfo,
)
from email_workflow.providers.email_provider import (
    GmailProvider,
    MockEmailProvider,
    OutlookProvider,
)
from email_workflow.providers.fake_ai import FakeAIProvider


class FakeIMAP:
    instances = []

    def __init__(self, host):
        self.host = host
        self.stored = []          # (msg_num, mode, value)
        self.uid_calls = []       # (command, args)
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
        self.stored.append((num, mode, value))
        return ("OK", [b""])

    def uid(self, command, *args):
        self.uid_calls.append((command.upper(), args))
        if command.upper() == "SEARCH":
            return ("OK", [b"9001"])
        return ("OK", [b""])

    def expunge(self):
        self.uid_calls.append(("EXPUNGE", ()))
        return ("OK", [b""])


@pytest.fixture(autouse=True)
def imap(monkeypatch):
    FakeIMAP.instances.clear()
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr(
        "email_workflow.providers.email_provider.imaplib.IMAP4_SSL", FakeIMAP
    )
    yield


def gmail(**kwargs) -> GmailProvider:
    """A Gmail account with the mailbox actions switched on.

    They ship OFF, so a fresh install touches nobody's inbox until asked. These
    tests are about what those actions DO, so they turn them on and say so -
    rather than leaning on a default that could quietly change under them.
    """
    kwargs.setdefault("archive_unimportant", True)
    kwargs.setdefault("star_important", True)
    return GmailProvider(EmailConfig(provider="gmail", **kwargs))


def what_was_stored():
    return FakeIMAP.instances[0].stored


# --- archiving really archives ---------------------------------------------

def uid_calls():
    return FakeIMAP.instances[0].uid_calls


def test_archiving_marks_it_read_and_deleted():
    gmail().archive_email("<m1@x>")
    stores = [args for cmd, args in uid_calls() if cmd == "STORE"]
    assert stores, "nothing was stored"
    flags = stores[0][-1]
    assert "\\Seen" in flags
    assert "\\Deleted" in flags


def test_archiving_expunges_so_it_really_leaves_the_inbox():
    """Verified against a real Gmail account: this is what archives a message.

    Removing the \\Inbox label with -X-GM-LABELS answers OK and changes
    nothing - the message stays put. Deleting it from INBOX is what Gmail
    turns into an archive; it lands in All Mail, not Trash.
    """
    gmail().archive_email("<m1@x>")
    assert "EXPUNGE" in [cmd for cmd, _ in uid_calls()], (
        "without an expunge the mail never leaves the inbox"
    )
    assert "-X-GM-LABELS" not in [mode for _, mode, _ in what_was_stored()], (
        "that mechanism does not work and should no longer be used"
    )


def test_it_expunges_only_this_one_message():
    """UID EXPUNGE names the message, so nothing else marked deleted goes too.

    This checks the command that is sent, not the server's behaviour: the
    fake mailbox keeps no per-message deleted state to check against.
    """
    gmail().archive_email("<m1@x>")
    expunges = [args for cmd, args in uid_calls() if cmd == "EXPUNGE"]
    assert expunges and expunges[0], "UID EXPUNGE must name the message"


def test_a_refused_uid_expunge_falls_back_to_the_plain_one():
    """UID EXPUNGE needs the UIDPLUS extension. Without it the mail must still
    leave the inbox, through the plain EXPUNGE.
    """
    class NoUidPlus(FakeIMAP):
        def uid(self, command, *args):
            self.uid_calls.append((command.upper(), args))
            if command.upper() == "SEARCH":
                return ("OK", [b"9001"])
            if command.upper() == "EXPUNGE":
                return ("NO", [b"UIDPLUS not supported"])
            return ("OK", [b""])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "email_workflow.providers.email_provider.imaplib.IMAP4_SSL", NoUidPlus
        )
        provider = gmail()
        provider.archive_email("<m1@x>")

    calls = FakeIMAP.instances[0].uid_calls
    plain = [args for cmd, args in calls if cmd == "EXPUNGE" and not args]
    assert plain, "the plain EXPUNGE fallback never ran, so the mail stayed put"
    assert provider.last_archive_error == "", (
        "the message did leave the inbox, so nothing should be reported"
    )


def test_an_expunge_that_raises_puts_the_email_back():
    """The dangerous case: the delete flags are already set when the expunge
    fails. imaplib RAISES on a BAD reply rather than returning a status, so
    this is the path an exception takes.

    Left as it was, the email would sit in the inbox marked read - and unread
    is the only thing that brings it back, so it would never be looked at
    again. It has to be put back.
    """
    import imaplib as _imaplib

    class ExpungeExplodes(FakeIMAP):
        def uid(self, command, *args):
            self.uid_calls.append((command.upper(), args))
            if command.upper() == "SEARCH":
                return ("OK", [b"9001"])
            if command.upper() == "EXPUNGE":
                raise _imaplib.IMAP4.error("BAD unrecognised command")
            return ("OK", [b""])

        def expunge(self):
            self.uid_calls.append(("EXPUNGE", ()))
            raise _imaplib.IMAP4.error("BAD")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "email_workflow.providers.email_provider.imaplib.IMAP4_SSL", ExpungeExplodes
        )
        provider = gmail()
        provider.archive_email("<m1@x>")

    stores = [args for cmd, args in FakeIMAP.instances[0].uid_calls if cmd == "STORE"]
    undo = [a for a in stores if a[1] == "-FLAGS"]
    assert undo, "the email was left marked read and deleted - it can never come back"
    assert "\\Seen" in undo[0][-1], "unread is what brings it back next run"
    assert "\\Deleted" in undo[0][-1]
    assert "tried again next run" in provider.last_archive_error, (
        "the owner must be told, and told the truth about what happened"
    )


def test_a_message_that_cannot_be_found_is_reported(monkeypatch):
    class NotFound(FakeIMAP):
        def uid(self, command, *args):
            self.uid_calls.append((command.upper(), args))
            return ("OK", [b""])

    monkeypatch.setattr(
        "email_workflow.providers.email_provider.imaplib.IMAP4_SSL", NotFound
    )
    provider = gmail()
    provider.archive_email("<gone@x>")
    assert "Could not find" in provider.last_archive_error


def test_archiving_can_be_reduced_to_just_marking_read():
    gmail(archive_unimportant=False).archive_email("<m1@x>")
    modes = [mode for _, mode, _ in what_was_stored()]
    assert "+FLAGS" in modes
    assert "EXPUNGE" not in [cmd for cmd, _ in FakeIMAP.instances[0].uid_calls], (
        "with archiving off the mail must stay in the inbox"
    )


# --- important mail gets starred and labelled -------------------------------

def test_flagging_stars_the_email():
    gmail().flag_email("<m1@x>")
    starred = [v for _, mode, v in what_was_stored() if mode == "+FLAGS"]
    assert any("\\Flagged" in v for v in starred), "a star is how Gmail shows it"


def test_flagging_applies_a_label():
    gmail().flag_email("<m1@x>")
    labels = [v for _, mode, v in what_was_stored() if mode == "+X-GM-LABELS"]
    assert labels and "Important" in labels[0]


def test_the_label_name_is_configurable():
    gmail(important_label="Wazne").flag_email("<m1@x>")
    labels = [v for _, mode, v in what_was_stored() if mode == "+X-GM-LABELS"]
    assert "Wazne" in labels[0]


def test_an_explicit_label_wins():
    gmail().flag_email("<m1@x>", label="Money")
    labels = [v for _, mode, v in what_was_stored() if mode == "+X-GM-LABELS"]
    assert "Money" in labels[0]


def test_starring_can_be_switched_off():
    gmail(star_important=False).flag_email("<m1@x>")
    assert not FakeIMAP.instances, "nothing should have been touched"


# --- servers that are not Gmail ---------------------------------------------

def test_outlook_gets_no_gmail_labels():
    """X-GM-LABELS is a Gmail extension; other servers reject it."""
    OutlookProvider(EmailConfig(provider="outlook")).flag_email("<m1@x>")
    modes = [mode for _, mode, _ in what_was_stored()]
    assert "+FLAGS" in modes, "starring still works everywhere"
    assert "+X-GM-LABELS" not in modes


def test_outlook_archive_only_marks_read():
    """On a non-Gmail server, delete + expunge really deletes. Never do it."""
    OutlookProvider(EmailConfig(provider="outlook")).archive_email("<m1@x>")
    stored = what_was_stored()
    assert any(m == "+FLAGS" and "\\Seen" in v for _, m, v in stored)
    assert not any("\\Deleted" in v for _, m, v in stored if m == "+FLAGS"), (
        "deleting on a non-Gmail server would destroy the email"
    )
    assert "EXPUNGE" not in [cmd for cmd, _ in FakeIMAP.instances[0].uid_calls]


# --- a mailbox failure never stops the run ----------------------------------

def test_a_failure_while_archiving_is_reported_not_raised(monkeypatch):
    def explode(host):
        raise OSError("imap down")

    monkeypatch.setattr(
        "email_workflow.providers.email_provider.imaplib.IMAP4_SSL", explode
    )
    provider = gmail()
    provider.archive_email("<m1@x>")          # must not raise
    assert "imap down" in provider.last_archive_error


# --- the pipeline actually uses them ----------------------------------------

def an_email(message_id, subject, body):
    return EmailMessage(
        message_id=message_id,
        thread_id="t1",
        sender=SenderInfo(name="Ann", email="ann@example.com", known_contact=True),
        subject=subject,
        body=body,
        received_at="2026-09-18T10:00:00Z",
    )


@pytest.fixture()
def pipeline(tmp_path):
    mailbox = MockEmailProvider()
    facts = tmp_path / "known_facts.txt"
    facts.write_text("- Work hours: 9-17", encoding="utf-8")
    config = AppConfig()
    # Shipped off; these tests are about what happens when they are on.
    config.email.archive_unimportant = True
    config.email.star_important = True
    config.email.create_drafts = True
    return mailbox, WorkflowPipeline(
        config=config,
        ai_provider=FakeAIProvider(),
        email_provider=mailbox,
        store_path=str(tmp_path / "state.json"),
        audit_path=str(tmp_path / "audit.jsonl"),
        idempotency_path=str(tmp_path / "idempotency.json"),
        known_facts_path=str(facts),
    )


def test_an_escalated_email_gets_starred(pipeline):
    mailbox, flow = pipeline
    flow.process_email(
        an_email("sec1", "Confirm your password", "Bank details and wire transfer.")
    )
    assert mailbox.flagged, "mail that needed a person should be easy to find later"
    assert mailbox.flagged[0][0] == "sec1"


def test_an_unimportant_email_gets_archived(pipeline):
    mailbox, flow = pipeline
    flow.process_email(
        an_email("n1", "Weekly newsletter", "Here is our weekly roundup. Unsubscribe.")
    )
    assert mailbox.archived, "routine mail should leave the inbox"


def test_archived_mail_is_not_also_starred(pipeline):
    mailbox, flow = pipeline
    flow.process_email(
        an_email("n1", "Weekly newsletter", "Here is our weekly roundup. Unsubscribe.")
    )
    assert not mailbox.flagged


# --- a draft is mail still waiting on you, so it gets starred too -----------

def test_creating_a_draft_also_stars_the_email(pipeline):
    """The reported bug: work and financial mail got a star, but a draft did
    not - so the one email actually waiting on a reply was the hardest to find.
    """
    mailbox, flow = pipeline
    flow.process_email(
        an_email("cal1", "Meeting Tuesday", "Can we meet Tuesday at 2 PM?")
    )
    assert mailbox.drafts, "this email should have produced a draft"
    assert mailbox.flagged, "a draft leaves mail waiting on you - it must be starred"
    assert mailbox.flagged[0][0] == "cal1"


class _WantsToSendAI(FakeAIProvider):
    """Asks to send straight away, so the send gate refuses and the written
    reply is kept as a draft instead - the second way a draft appears.
    """

    def classify_email(self, message, thread=None, known_facts=""):
        analysis = super().classify_email(message, thread, known_facts)
        analysis.recommended_decision = DecisionOption.AUTOMATICALLY_REPLY
        analysis.safe_to_automate = True
        analysis.confidence = 0.99
        # The engine only clears a send when nothing is outstanding and the
        # category is a trusted one. Without all four of these it downgrades to
        # waiting for approval, and this test would silently cover the same
        # branch as the one above instead of the send-gate fallback.
        analysis.commitments_implied = []
        analysis.missing_information = []
        analysis.category = EmailCategory.WORK
        return analysis


def test_a_draft_kept_after_a_refused_send_is_starred_too(tmp_path):
    mailbox = MockEmailProvider()
    facts = tmp_path / "known_facts.txt"
    facts.write_text("- Work hours: 9-17", encoding="utf-8")
    flow = WorkflowPipeline(
        config=AppConfig(),
        ai_provider=_WantsToSendAI(),
        email_provider=mailbox,
        store_path=str(tmp_path / "state.json"),
        audit_path=str(tmp_path / "audit.jsonl"),
        idempotency_path=str(tmp_path / "idempotency.json"),
        known_facts_path=str(facts),
    )
    flow.process_email(
        an_email("cal2", "Meeting Tuesday", "Can we meet Tuesday at 2 PM?")
    )
    assert mailbox.drafts, "sending is off, so the reply must be kept as a draft"
    assert mailbox.flagged, "that draft is still waiting on you - star it"
    assert mailbox.flagged[0][0] == "cal2"
