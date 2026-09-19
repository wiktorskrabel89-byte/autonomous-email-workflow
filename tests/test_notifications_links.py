"""Tests for the Gmail deep link in reports, and the throttle message."""

import pytest

from email_workflow.core.notifications import gmail_link
from email_workflow.core.throttle import RateLimiter


# --- the link ---------------------------------------------------------------

def test_a_real_message_id_becomes_a_gmail_link():
    link = gmail_link("<ead7c2dd@google.com>")
    assert link == (
        "https://mail.google.com/mail/u/0/#search/rfc822msgid:ead7c2dd%40google.com"
    )


def test_the_angle_brackets_are_stripped():
    assert "<" not in gmail_link("<abc@example.com>")
    assert ">" not in gmail_link("<abc@example.com>")


def test_the_id_is_url_encoded():
    """A raw + or # in an id would break the search without encoding."""
    link = gmail_link("<a+b#c@example.com>")
    assert "%2B" in link and "%23" in link


def test_a_long_real_world_id_works():
    raw = "<ead7c2dd3e0f6778fb90815036566f8169ec3239-10044049-111213542@google.com>"
    link = gmail_link(raw)
    assert link.startswith("https://mail.google.com/mail/u/0/#search/rfc822msgid:")
    assert "10044049-111213542%40google.com" in link


@pytest.mark.parametrize("fake", ["msg_004_phishing", "thread_sec_01", "", "   "])
def test_demo_ids_get_no_link(fake):
    """A fixture id belongs to no mailbox; a dead link is worse than none."""
    assert gmail_link(fake) is None


def test_an_id_with_a_space_is_rejected():
    assert gmail_link("<not a real id@x.com>") is None


# --- the throttle message ---------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_it_says_when_the_limit_was_hit():
    clock = Clock()
    told = []
    rl = RateLimiter(
        15, sleep=clock.sleep, clock=clock.time,
        on_wait=lambda seconds, rpm: told.append((seconds, rpm)),
    )

    for _ in range(15):
        rl.wait()
    assert told == [], "nothing to announce while inside the allowance"

    rl.wait()
    assert len(told) == 1, "hitting the limit should be announced"
    seconds, rpm = told[0]
    assert rpm == 15
    assert seconds == pytest.approx(60.0)


def test_it_is_announced_before_the_wait_not_after():
    """The message has to appear while you are waiting, or it is pointless."""
    clock = Clock()
    order = []
    rl = RateLimiter(
        1,
        sleep=lambda s: (order.append("slept"), clock.sleep(s)),
        clock=clock.time,
        on_wait=lambda s, rpm: order.append("announced"),
    )
    rl.wait()
    rl.wait()

    assert order == ["announced", "slept"]


def test_a_broken_announcer_never_breaks_the_run():
    clock = Clock()

    def explode(seconds, rpm):
        raise RuntimeError("the console is on fire")

    rl = RateLimiter(1, sleep=clock.sleep, clock=clock.time, on_wait=explode)
    rl.wait()
    assert rl.wait() > 0, "a failing message must not stop the work"


def test_nothing_is_announced_when_throttling_is_off():
    clock = Clock()
    told = []
    rl = RateLimiter(0, sleep=clock.sleep, clock=clock.time,
                     on_wait=lambda s, rpm: told.append(s))
    for _ in range(20):
        rl.wait()
    assert told == []


# --- a sent reply must never be called a draft ------------------------------

from email_workflow.core.notifications import NotificationDispatcher, reply_outcome
from email_workflow.models.config import NotificationsConfig
from email_workflow.models.email import DecisionOption


def test_a_reply_that_was_sent_is_reported_as_sent():
    """The bug: the id was announced as "Draft Created" whichever path made it,
    so a reply that had already gone out was reported as a draft still waiting
    for approval. A sent email cannot be unsent - the report has to say so.
    """
    out = reply_outcome(DecisionOption.AUTOMATICALLY_REPLY, "sent_gmail_<abc@x>")
    assert "SENT" in out
    assert "Draft" not in out, "a reply that was sent must not be called a draft"
    assert "sent_gmail_<abc@x>" in out


def test_a_draft_is_still_reported_as_a_draft():
    out = reply_outcome(DecisionOption.CREATE_DRAFT, "draft_gmail_<abc@x>")
    assert "Draft Created" in out
    assert "SENT" not in out


def test_waiting_for_approval_is_a_draft_not_a_send():
    out = reply_outcome(DecisionOption.WAIT_FOR_APPROVAL, "draft_gmail_<abc@x>")
    assert "Draft Created" in out and "SENT" not in out


def test_no_reply_at_all_says_so():
    assert reply_outcome(DecisionOption.ESCALATE, None) == "No draft created"
    assert reply_outcome(DecisionOption.AUTOMATICALLY_REPLY, "") == "No draft created"


# --- the per-message breakdown is a setting ---------------------------------

def a_run_result():
    return [
        {"message_id": "<m1@x>", "decision": DecisionOption.ARCHIVE, "summary": "Archived"},
        {"message_id": "<m2@x>", "decision": DecisionOption.CREATE_DRAFT, "summary": "Draft made"},
    ]


class RecordingConsole:
    """The digest prints its panel straight to the module console, so that is
    what has to be intercepted - not _send_terminal, which it never calls."""

    def __init__(self):
        self.printed = []

    def print(self, *args, **kwargs):
        for a in args:
            self.printed.append(str(getattr(a, "renderable", a)))

    def __getattr__(self, name):
        return lambda *a, **k: None


def digest_text(monkeypatch, **cfg):
    rec = RecordingConsole()
    monkeypatch.setattr("email_workflow.core.notifications.console", rec)
    dispatcher = NotificationDispatcher(NotificationsConfig(channel="terminal", **cfg))
    dispatcher.send_run_digest(a_run_result(), "gemini", "flash")
    return "\n".join(rec.printed)


def test_the_per_message_breakdown_is_off_by_default(monkeypatch):
    text = digest_text(monkeypatch)
    assert "Detailed Breakdown by Message" not in text
    assert "<m1@x>" not in text, "raw ids turn into mailto: links in Discord"
    assert "Archived / Ignored" in text, "the totals must still be there"


def test_the_per_message_breakdown_can_be_switched_back_on(monkeypatch):
    text = digest_text(monkeypatch, show_message_breakdown=True)
    assert "Detailed Breakdown by Message" in text
    assert "<m1@x>" in text and "<m2@x>" in text
