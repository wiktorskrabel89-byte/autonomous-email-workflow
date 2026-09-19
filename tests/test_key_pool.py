"""Several API keys at once - one per account - and the two ways they work.

Split: each key takes a different email, so a full inbox finishes in a fraction
of the time. Team: two keys work on the same email, one writing the reply and
another checking it before it can be sent.

The thing these tests really have to protect is the counting. A free tier is per
account, so two keys must be believed to be two allowances only when they really
are two - and the workers that share one audit log must not lose each other's
entries.
"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from email_workflow.core.errors import AIProviderError
from email_workflow.core.pipeline import WorkflowPipeline
from email_workflow.core.throttle import RateLimiter
from email_workflow.models.analysis import ReplyReviewOutput
from email_workflow.models.config import AppConfig
from email_workflow.models.email import (
    DecisionOption,
    EmailCategory,
    EmailMessage,
    SenderInfo,
)
from email_workflow.providers.email_provider import MockEmailProvider
from email_workflow.providers.fake_ai import FakeAIProvider
from email_workflow.providers.fallback_ai import ProviderLink
from email_workflow.providers.key_pool import (
    KeyLane,
    KeyPoolProvider,
    discover_key_envs,
    parallel_lanes,
)


# --- finding the keys -------------------------------------------------------

@pytest.fixture()
def clean_env(monkeypatch):
    for name in list(__import__("os").environ):
        if "API_KEY" in name:
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_a_key_from_a_second_account_is_found_with_no_configuration(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "from-account-one")
    clean_env.setenv("GEMINI_API_KEY_2", "from-account-two")
    clean_env.setenv("GEMINI_API_KEY_3", "from-account-three")

    assert discover_key_envs("GEMINI_API_KEY") == [
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
    ]


def test_keys_are_ordered_the_way_a_person_reads_them(clean_env):
    for n in ("", "_2", "_10"):
        clean_env.setenv("GEMINI_API_KEY" + n, "key" + n)
    assert discover_key_envs("GEMINI_API_KEY") == [
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_10",
    ]


def test_a_key_named_after_the_account_works_too(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "one")
    clean_env.setenv("GEMINI_API_KEY_WORK", "two")
    assert discover_key_envs("GEMINI_API_KEY") == ["GEMINI_API_KEY", "GEMINI_API_KEY_WORK"]


def test_the_same_key_under_two_names_counts_once(clean_env):
    """Two names for one account is still one free tier.

    Believing it was two would let the pool send twice the allowance at a
    single account, which is the 429 this whole thing exists to avoid.
    """
    clean_env.setenv("GEMINI_API_KEY", "the-only-key")
    clean_env.setenv("GEMINI_API_KEY_COPY", "the-only-key")
    assert discover_key_envs("GEMINI_API_KEY") == ["GEMINI_API_KEY"]


def test_another_provider_key_does_not_join_this_pool(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "gem")
    clean_env.setenv("GROQ_API_KEY", "groq")
    assert discover_key_envs("GEMINI_API_KEY") == ["GEMINI_API_KEY"]


def test_an_empty_key_is_not_a_key(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "real")
    clean_env.setenv("GEMINI_API_KEY_2", "   ")
    assert discover_key_envs("GEMINI_API_KEY") == ["GEMINI_API_KEY"]


# --- the pool itself --------------------------------------------------------

class Recorder(FakeAIProvider):
    """One key. Remembers what it was asked and can be made to give out."""

    def __init__(self, name, fail=None, review=None):
        self.name = name
        self.asked = []
        self.fail = fail
        self.review_result = review
        self.limiter = RateLimiter(0)

    def describe(self):
        return ("fake", self.name)

    def classify_email(self, message, thread=None, known_facts=""):
        self._note("classify")
        return super().classify_email(message, thread, known_facts)

    def generate_reply(self, message, thread=None, known_facts=""):
        self._note("reply")
        return super().generate_reply(message, thread, known_facts)

    def review_reply(self, message, reply, thread=None, known_facts=""):
        self._note("review")
        if isinstance(self.review_result, Exception):
            raise self.review_result
        return self.review_result or ReplyReviewOutput(approved=True)

    def _note(self, what):
        self.asked.append(what)
        if self.fail:
            raise self.fail


def pool_of(*recorders) -> KeyPoolProvider:
    return KeyPoolProvider(
        [
            KeyLane(ProviderLink(f"Fake [{r.name}]", "m", r), r.name)
            for r in recorders
        ]
    )


def an_email(message_id="m1", subject="Meeting Tuesday", body="Can we meet Tuesday at 2 PM?"):
    return EmailMessage(
        message_id=message_id,
        thread_id="t1",
        sender=SenderInfo(name="Ann", email="ann@example.com", known_contact=True),
        subject=subject,
        body=body,
        received_at="2026-09-18T10:00:00Z",
    )


def test_three_keys_are_three_lanes():
    pool = pool_of(Recorder("k1"), Recorder("k2"), Recorder("k3"))
    assert pool.size == 3
    assert parallel_lanes(pool) == 3


def test_one_key_is_not_parallelism():
    assert parallel_lanes(Recorder("only")) == 1


def test_work_is_spread_over_the_keys():
    """Otherwise the extra accounts are just decoration."""
    a, b, c = Recorder("k1"), Recorder("k2"), Recorder("k3")
    pool = pool_of(a, b, c)
    for _ in range(3):
        pool.classify_email(an_email())
    assert [len(r.asked) for r in (a, b, c)] == [1, 1, 1]


def test_a_spent_key_is_stepped_over():
    dead = Recorder("dead", fail=AIProviderError("daily quota gone", kind="quota"))
    alive = Recorder("alive")
    pool = pool_of(dead, alive)

    pool.classify_email(an_email())

    assert alive.asked, "the work should have moved to the key that still had quota"
    assert dead.exhausted if hasattr(dead, "exhausted") else True
    assert len(pool.live_lanes) == 1


def test_a_mistake_of_ours_is_not_failed_over():
    """A malformed request fails the same way on every key. Trying them all
    just burns the rest of the quota for nothing."""
    broken = Recorder("k1", fail=AIProviderError("bad request", kind="other"))
    spare = Recorder("k2")
    pool = pool_of(broken, spare)

    with pytest.raises(AIProviderError):
        pool.classify_email(an_email())
    assert not spare.asked, "the second key should not have been touched"


def test_when_every_key_is_spent_it_says_so():
    out = AIProviderError("quota", kind="quota")
    pool = pool_of(Recorder("k1", fail=out), Recorder("k2", fail=out))

    with pytest.raises(AIProviderError) as caught:
        pool.classify_email(an_email())
    assert "Every API key failed" in caught.value.message
    assert "k1" in caught.value.hint and "k2" in caught.value.hint


# --- team mode: one writes, another checks ----------------------------------

def test_the_reply_is_checked_by_a_different_key():
    """A model marking its own homework is not a second opinion.

    The keys are deliberately given uneven allowances. The pool normally picks
    whichever key has the most left this minute, so with one key far ahead it
    would pick that same key twice - writing the reply and then reviewing its
    own work. Only the rule "never the key that just answered" breaks that,
    which is what this pins. With evenly matched keys the plain round-robin
    would hide the bug.
    """
    roomy, busy = Recorder("roomy"), Recorder("busy")
    roomy.limiter = RateLimiter(15)
    busy.limiter = RateLimiter(15)
    for _ in range(14):
        busy.limiter.wait()          # busy now has 1 left, roomy has 15
    assert roomy.limiter.available() > busy.limiter.available()

    pool = pool_of(roomy, busy)
    reply = pool.generate_reply(an_email())
    pool.review_reply(an_email(), reply)

    assert "reply" in roomy.asked, "the key with room should have written it"
    assert "review" in busy.asked, (
        "the review went back to the key that wrote the reply - that is the "
        "same model marking its own homework, not a second opinion"
    )
    assert "review" not in roomy.asked


def test_with_one_key_the_review_still_happens_on_that_key():
    """Avoiding the last key must not mean skipping the work entirely."""
    only = Recorder("k1")
    pool = pool_of(only)
    reply = pool.generate_reply(an_email())
    pool.review_reply(an_email(), reply)
    assert only.asked == ["reply", "review"]


# --- team mode through the pipeline -----------------------------------------

class WantsToSend(Recorder):
    """Clears every deterministic gate, so the second key is the only thing
    left that can stop the send. Without this the reply would be held back
    anyway and the test would prove nothing about the review."""

    def classify_email(self, message, thread=None, known_facts=""):
        analysis = super().classify_email(message, thread, known_facts)
        analysis.recommended_decision = DecisionOption.AUTOMATICALLY_REPLY
        analysis.safe_to_automate = True
        analysis.confidence = 0.99
        analysis.commitments_implied = []
        analysis.missing_information = []
        analysis.category = EmailCategory.WORK
        return analysis

    def generate_reply(self, message, thread=None, known_facts=""):
        reply = super().generate_reply(message, thread, known_facts)
        reply.complete = True
        reply.placeholders_used = []
        reply.commitments_made = []
        return reply


def build_team(tmp_path, review):
    facts = tmp_path / "known_facts.txt"
    facts.write_text("- Work hours: 9-17", encoding="utf-8")
    # Both lanes give the same verdict on purpose. Which key ends up doing the
    # review is the pool's business (and its own test); what is under test here
    # is what the pipeline does with the answer.
    writer = WantsToSend("writer", review=review)
    checker = WantsToSend("checker", review=review)
    pool = pool_of(writer, checker)

    config = AppConfig()
    config.email.allow_send = True
    # The send gate only lets a trusted category out. Work mail is what this
    # scenario is about, so it is trusted here.
    config.automation.trusted_categories = ["work"]

    mailbox = MockEmailProvider()
    flow = WorkflowPipeline(
        config=config,
        ai_provider=pool,
        email_provider=mailbox,
        store_path=str(tmp_path / "state.json"),
        audit_path=str(tmp_path / "audit.jsonl"),
        idempotency_path=str(tmp_path / "idempotency.json"),
        known_facts_path=str(facts),
    )
    return mailbox, flow, tmp_path / "audit.jsonl"


def audit_details(path):
    return [
        json.loads(line)["detail"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_an_approved_reply_is_allowed_out(tmp_path):
    mailbox, flow, audit = build_team(tmp_path, ReplyReviewOutput(approved=True))
    flow.process_email(an_email())
    assert mailbox.sent, "nothing objected, so the reply should have gone"
    assert any("second key approved" in d for d in audit_details(audit))


def test_an_objection_holds_the_reply_back(tmp_path):
    """The whole point of the second key: it can stop a send."""
    mailbox, flow, audit = build_team(
        tmp_path,
        ReplyReviewOutput(approved=False, concerns=["invented a price we never quoted"]),
    )
    flow.process_email(an_email())

    assert not mailbox.sent, "a reply the second key objected to must not be sent"
    assert mailbox.drafts, "the written reply should be kept, not thrown away"
    details = audit_details(audit)
    assert any("invented a price" in d for d in details)


def test_a_broken_reviewer_does_not_lose_the_reply(tmp_path):
    """If the checker itself fails, the reply that was already written stands
    and the ordinary safety gate still applies."""
    mailbox, flow, audit = build_team(tmp_path, RuntimeError("reviewer exploded"))
    flow.process_email(an_email())
    assert mailbox.sent or mailbox.drafts, "the reply was lost entirely"
    assert any("could not be obtained" in d for d in audit_details(audit))


def test_one_key_is_never_asked_for_a_second_opinion(tmp_path):
    facts = tmp_path / "known_facts.txt"
    facts.write_text("- Work hours: 9-17", encoding="utf-8")
    only = WantsToSend("only")
    config = AppConfig()
    config.email.allow_send = True
    config.automation.trusted_categories = ["work"]
    flow = WorkflowPipeline(
        config=config,
        ai_provider=only,
        email_provider=MockEmailProvider(),
        store_path=str(tmp_path / "state.json"),
        audit_path=str(tmp_path / "audit.jsonl"),
        idempotency_path=str(tmp_path / "idempotency.json"),
        known_facts_path=str(facts),
    )
    flow.process_email(an_email())
    assert "review" not in only.asked, (
        "with a single key a review is the same model marking its own homework"
    )


def test_the_review_can_be_switched_off(tmp_path):
    mailbox, flow, audit = build_team(tmp_path, ReplyReviewOutput(approved=False))
    flow.config.ai.keys.review = False
    flow.process_email(an_email())
    assert not any("second key" in d for d in audit_details(audit))


# --- several emails at once -------------------------------------------------

def test_workers_never_outnumber_the_keys():
    from email_workflow.cli.cli import _worker_count

    config = AppConfig()
    pool = pool_of(Recorder("k1"), Recorder("k2"), Recorder("k3"))

    assert _worker_count(config, pool, email_count=10) == 3, "one worker per key"
    assert _worker_count(config, pool, email_count=2) == 2, "no worker without an email"
    assert _worker_count(config, Recorder("solo"), email_count=10) == 1

    config.ai.keys.parallel = False
    assert _worker_count(config, pool, email_count=10) == 1

    config.ai.keys.parallel = True
    config.ai.keys.max_workers = 2
    assert _worker_count(config, pool, email_count=10) == 2


def test_workers_sharing_one_run_do_not_lose_each_others_records(tmp_path):
    """The audit log, the thread store and the idempotency file are one file
    each, shared by every worker. A read-modify-write that overlaps loses an
    update - which would show as a missing audit entry or an email handled
    twice, the exact things this app is supposed to be able to prove.

    Measured: with the store locks removed this fails about two runs in five.
    So a single green run is not proof the locking is still there - if you are
    checking that on purpose, run it several times.
    """
    facts = tmp_path / "known_facts.txt"
    facts.write_text("- Work hours: 9-17", encoding="utf-8")
    mailbox = MockEmailProvider()
    flow = WorkflowPipeline(
        config=AppConfig(),
        ai_provider=FakeAIProvider(),
        email_provider=mailbox,
        store_path=str(tmp_path / "state.json"),
        audit_path=str(tmp_path / "audit.jsonl"),
        idempotency_path=str(tmp_path / "idempotency.json"),
        known_facts_path=str(facts),
    )

    emails = [
        an_email(f"n{i}", "Weekly newsletter", "Our weekly roundup. Unsubscribe.")
        for i in range(12)
    ]
    for i, email in enumerate(emails):
        email.thread_id = f"t{i}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(flow.process_email, emails))

    received = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    got = {e["message_id"] for e in received if e["event_type"] == "received"}
    assert got == {f"n{i}" for i in range(12)}, "an audit entry was lost"

    idem = json.loads((tmp_path / "idempotency.json").read_text(encoding="utf-8"))
    recorded = json.dumps(idem)
    for i in range(12):
        assert f"n{i}" in recorded, f"n{i} never made it into the idempotency file"

    assert len(mailbox.archived) == 12, "an archive was lost between workers"
