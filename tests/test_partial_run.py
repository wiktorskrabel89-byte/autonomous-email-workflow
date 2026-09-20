"""A run that cannot finish still keeps - and reports - what it did.

The old behaviour: the AI gave out on email 16 of 158, the error went all the
way up, and the whole pass was thrown away. No digest, no summary, nothing to
show for the fifteen emails that had already been filed. From the outside that
is "it randomly stops working and I lose the run".

The work itself was never lost - the mailbox changes had already happened and
the unread ones are picked up next time - but nobody could tell that from the
screen. Now the report goes out for what was finished, and the message says
how many were left and that they are still waiting.
"""

import pytest

from email_workflow.cli import cli as cli_module
from email_workflow.core.errors import AIProviderError
from email_workflow.models.config import AppConfig
from email_workflow.models.email import EmailMessage, SenderInfo


def email_at(n: int) -> EmailMessage:
    return EmailMessage(
        message_id=f"m{n}",
        thread_id=f"t{n}",
        sender=SenderInfo(name="Ann", email="ann@example.com"),
        subject=f"Message {n}",
        body="hello",
        received_at="2026-09-20T10:00:00Z",
    )


class FakeNotifier:
    def __init__(self):
        self.digests = []

    def send_run_digest(self, results, provider_name="", model_name=""):
        self.digests.append(list(results))


class FakePipeline:
    """Works through emails, then hits a wall on the nth one."""

    def __init__(self, fail_on: int, error=None):
        self.fail_on = fail_on
        self.error = error or AIProviderError(
            "Every AI provider failed.", hint="Tried: everything.", kind="quota"
        )
        self.seen = 0
        self.notifier = FakeNotifier()
        self.ai = self

    def describe(self):
        return ("gemini", "gemini-3.1-flash-lite")

    def process_email(self, email, known_facts=""):
        self.seen += 1
        if self.seen == self.fail_on:
            raise self.error
        return {
            "message_id": email.message_id,
            "decision": "archive",
            "summary": "Archived",
            "analysis": None,
        }


class FakeMailbox:
    def __init__(self, count):
        self.emails = [email_at(n) for n in range(1, count + 1)]
        self.last_archive_error = ""

    def fetch_unprocessed_emails(self):
        return self.emails


@pytest.fixture
def quiet(monkeypatch):
    """No rendering: these tests are about what is kept, not how it looks."""
    monkeypatch.setattr(cli_module, "render_stage_result", lambda *a, **k: None)
    yield


def test_the_emails_already_done_are_not_thrown_away(quiet):
    pipeline = FakePipeline(fail_on=4)
    results, stopped = cli_module._process_one_at_a_time(
        pipeline, [email_at(n) for n in range(1, 11)], "gemini", "flash"
    )

    assert len(results) == 3, "the three that finished are real work"
    assert stopped is not None
    assert stopped.kind == "quota"


def test_the_report_still_goes_out_for_what_was_finished(quiet):
    config = AppConfig()
    config.email.provider = "mock"
    config.ai.keys.parallel = False
    pipeline = FakePipeline(fail_on=4)

    results, stopped = cli_module._process_inbox(
        pipeline, FakeMailbox(10), config
    )

    assert stopped is not None
    assert pipeline.notifier.digests, "the digest must still be sent"
    assert len(pipeline.notifier.digests[0]) == 3


def test_a_run_that_finishes_reports_no_problem(quiet):
    config = AppConfig()
    config.email.provider = "mock"
    config.ai.keys.parallel = False
    pipeline = FakePipeline(fail_on=0)  # never fails

    results, stopped = cli_module._process_inbox(pipeline, FakeMailbox(5), config)

    assert stopped is None
    assert len(results) == 5
    assert len(pipeline.notifier.digests[0]) == 5


# --- the same thing, with several keys working at once ---------------------

class FakeParallelPipeline(FakePipeline):
    """Fails on the emails whose number is in `fail_these`, whenever they come.

    With several workers the finishing order is not the inbox order, so a
    pipeline that counts calls would fail a different email on every run.
    """

    def __init__(self, fail_these):
        super().__init__(fail_on=-1)
        self.fail_these = set(fail_these)

    def process_email(self, email, known_facts=""):
        if email.message_id in self.fail_these:
            raise self.error
        return {
            "message_id": email.message_id,
            "decision": "archive",
            "summary": "Archived",
            "analysis": None,
        }


def test_one_email_failing_does_not_take_the_others_with_it(quiet):
    """The parallel path: the others are in flight on other keys, and may well
    succeed - so one failure must not discard them."""
    pipeline = FakeParallelPipeline(fail_these={"m3"})
    emails = [email_at(n) for n in range(1, 7)]

    results, stopped = cli_module._process_together(pipeline, emails, workers=3)

    assert stopped is not None, "the failure still has to be reported"
    assert len(results) == 5, "the other five are finished work"
    assert "m3" not in [r["message_id"] for r in results]
    assert None not in results, "a gap must never reach the digest"


def test_the_first_failure_is_the_one_reported(quiet):
    pipeline = FakeParallelPipeline(fail_these={"m2", "m5"})
    emails = [email_at(n) for n in range(1, 7)]

    results, stopped = cli_module._process_together(pipeline, emails, workers=3)

    assert len(results) == 4
    assert stopped is pipeline.error


def test_nothing_is_lost_when_they_all_succeed(quiet):
    pipeline = FakeParallelPipeline(fail_these=set())
    emails = [email_at(n) for n in range(1, 7)]

    results, stopped = cli_module._process_together(pipeline, emails, workers=3)

    assert stopped is None
    assert sorted(r["message_id"] for r in results) == sorted(
        e.message_id for e in emails
    ), "every email, exactly once"


def test_an_empty_inbox_is_not_a_failure(quiet):
    config = AppConfig()
    config.email.provider = "mock"
    results, stopped = cli_module._process_inbox(
        FakePipeline(fail_on=0), FakeMailbox(0), config
    )
    assert (results, stopped) == ([], None)
