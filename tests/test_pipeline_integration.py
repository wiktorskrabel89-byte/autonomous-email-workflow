"""End-to-end tests for the pipeline, using the offline fake AI.

These run the real WorkflowPipeline against temporary state files, so they
cover the wiring between classification, the decision engine, idempotency and
the audit log without touching any API.
"""

import json

import pytest

from email_workflow.core.errors import AIProviderError
from email_workflow.core.pipeline import WorkflowPipeline
from email_workflow.models.analysis import EmailAnalysis
from email_workflow.models.config import AppConfig
from email_workflow.models.email import EmailMessage, SenderInfo
from email_workflow.providers.email_provider import MockEmailProvider
from email_workflow.providers.fake_ai import FakeAIProvider

FACTS = "- Work hours: 9:00 to 17:00\n- Meetings: Thursdays only"


def an_email(message_id="m1", subject="Can we meet?", body="Are you free Thursday?"):
    return EmailMessage(
        message_id=message_id,
        thread_id="thread-1",
        sender=SenderInfo(name="Ann", email="ann@example.com", known_contact=True),
        subject=subject,
        body=body,
        received_at="2026-09-18T10:00:00Z",
    )


@pytest.fixture()
def build(tmp_path):
    """A pipeline writing all of its state into a temp folder."""

    def _build(ai_provider=None, facts=FACTS):
        facts_file = tmp_path / "known_facts.txt"
        facts_file.write_text(facts, encoding="utf-8")
        return WorkflowPipeline(
            config=AppConfig(),
            ai_provider=ai_provider or FakeAIProvider(),
            email_provider=MockEmailProvider(),
            store_path=str(tmp_path / "state.json"),
            audit_path=str(tmp_path / "audit.jsonl"),
            idempotency_path=str(tmp_path / "idempotency.json"),
            known_facts_path=str(facts_file),
        )

    return _build


class RecordingAI(FakeAIProvider):
    def __init__(self):
        super().__init__()
        self.classify_calls = []

    def classify_email(self, message, thread=None, known_facts=""):
        self.classify_calls.append(known_facts)
        return super().classify_email(message, thread, known_facts)


class BrokenAI(FakeAIProvider):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def classify_email(self, message, thread=None, known_facts=""):
        raise self.error


# --- known facts reach the model -------------------------------------------

def test_known_facts_are_passed_into_classification(build):
    ai = RecordingAI()
    pipeline = build(ai_provider=ai)
    pipeline.process_email(an_email())

    assert ai.classify_calls, "classify_email was never called"
    assert ai.classify_calls[0] == FACTS, (
        "the classification prompt asks the model to use Known Facts, so they "
        "must actually be handed to it"
    )


def test_explicitly_supplied_facts_win_over_the_file(build):
    ai = RecordingAI()
    pipeline = build(ai_provider=ai)
    pipeline.process_email(an_email(), known_facts="- Only fact that counts")
    assert ai.classify_calls[0] == "- Only fact that counts"


# --- idempotency ------------------------------------------------------------

def test_the_same_email_is_not_processed_twice(build):
    pipeline = build()
    first = pipeline.process_email(an_email("m1"))
    second = pipeline.process_email(an_email("m1"))

    assert first.get("status") != "skipped"
    assert second["status"] == "skipped", "a message already handled must be skipped"


def test_a_different_email_is_still_processed(build):
    pipeline = build()
    pipeline.process_email(an_email("m1"))
    result = pipeline.process_email(an_email("m2"))
    assert result.get("status") != "skipped"


def test_idempotency_survives_a_restart(build, tmp_path):
    build().process_email(an_email("m1"))
    # A brand new pipeline object, same files on disk.
    again = build().process_email(an_email("m1"))
    assert again["status"] == "skipped"


# --- audit trail ------------------------------------------------------------

def test_every_run_is_written_to_the_audit_log(build, tmp_path):
    pipeline = build()
    pipeline.process_email(an_email())

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    kinds = {e["event_type"] for e in events}

    assert "received" in kinds
    assert "classified" in kinds
    assert all(e["message_id"] for e in events), "every event needs its message id"


def test_audit_records_which_model_made_the_call(build, tmp_path):
    pipeline = build()
    pipeline.process_email(an_email())

    events = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    classified = next(e for e in events if e["event_type"] == "classified")
    assert classified["run_metadata"]["model"], "the audit must say which model decided"


# --- safety -----------------------------------------------------------------

def test_security_email_is_never_auto_replied(build):
    pipeline = build()
    result = pipeline.process_email(
        an_email(
            "sec1",
            subject="Urgent: confirm your password",
            body="Your account was locked. Confirm your password and bank details.",
        )
    )
    assert result.get("decision") != "automatically_reply"


# --- failures surface as advice, not tracebacks -----------------------------

def test_a_provider_failure_propagates_as_a_workflow_error(build):
    error = AIProviderError("quota gone", hint="wait a bit", kind="quota")
    pipeline = build(ai_provider=BrokenAI(error))

    with pytest.raises(AIProviderError) as excinfo:
        pipeline.process_email(an_email())

    assert excinfo.value.hint == "wait a bit"
    assert excinfo.value.can_failover


def test_the_email_is_not_marked_done_when_classification_fails(build, tmp_path):
    pipeline = build(ai_provider=BrokenAI(AIProviderError("boom", kind="quota")))
    with pytest.raises(AIProviderError):
        pipeline.process_email(an_email("m1"))

    # A second, working run must still pick the message up.
    result = build().process_email(an_email("m1"))
    assert result.get("status") != "skipped", (
        "an email that failed mid-classification must not be treated as handled"
    )
    assert result.get("decision"), "the retry should actually reach a decision"


# --- results ----------------------------------------------------------------

def test_a_processed_email_reports_a_decision(build):
    result = build().process_email(an_email())
    assert result["message_id"] == "m1"
    assert result.get("decision"), "the result should say what was decided"


# --- a result with nothing decided must still render -------------------------

def test_an_already_handled_email_renders_without_crashing():
    """An escalated email stays unread, so it comes back next run and is
    skipped - with no decision attached. The table used to crash on that."""
    from email_workflow.cli.formatter import render_stage_result

    render_stage_result(1, {"message_id": "m1", "status": "skipped", "stage": "escalated"})


def test_a_skipped_result_with_no_stage_still_renders():
    from email_workflow.cli.formatter import render_stage_result

    render_stage_result(1, {"message_id": "m1", "status": "skipped"})


def test_a_result_missing_a_decision_entirely_still_renders():
    from email_workflow.cli.formatter import render_stage_result

    render_stage_result(1, {"message_id": "m1"})


def test_an_escalated_email_is_skipped_the_second_time(build):
    """Escalating does not mark mail read, so the same email returns next run."""
    pipeline = build()
    first = pipeline.process_email(
        an_email("sec1", subject="Confirm your password", body="Bank details needed.")
    )
    second = pipeline.process_email(
        an_email("sec1", subject="Confirm your password", body="Bank details needed.")
    )

    assert first.get("decision")
    assert second["status"] == "skipped"
    assert second.get("decision") is None, (
        "a skipped result carries no decision - the renderer must cope"
    )
