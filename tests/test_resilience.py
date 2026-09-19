"""Tests for the failure paths: provider failover, error translation, IMAP fetch rules."""

from datetime import datetime

import pytest

from email_workflow.core.errors import AIProviderError, WorkflowError
from email_workflow.models.analysis import EmailAnalysis
from email_workflow.models.config import AppConfig, EmailConfig
from email_workflow.models.email import EmailMessage, SenderInfo
from email_workflow.providers.api_providers import _extract_json
from email_workflow.providers.email_provider import _imap_date
from email_workflow.providers.fallback_ai import FallbackAIProvider, ProviderLink


def make_email(message_id: str = "m1") -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        thread_id="t1",
        sender=SenderInfo(name="Ann", email="ann@example.com"),
        subject="When are you free?",
        body="Could we meet this week?",
        received_at="2026-09-18T10:00:00Z",
    )


ANALYSIS_KWARGS = dict(
    message_id="m1",
    thread_id="t1",
    sender=SenderInfo(name="Ann", email="ann@example.com"),
    subject="When are you free?",
    received_at="2026-09-18T10:00:00Z",
    category="work",
    importance="medium",
    urgency="medium",
    action_required=True,
    response_required=True,
    safe_to_automate=False,
    confidence=0.8,
    recommended_decision="create_draft",
    reasoning="test",
)


class StubProvider:
    """An AI provider that fails a set number of times, then succeeds."""

    def __init__(self, name, error=None):
        self.name = name
        self.error = error
        self.calls = 0
        self.seen_facts = None

    def validate_setup(self):
        return None

    def classify_email(self, message, thread=None, known_facts=""):
        self.calls += 1
        self.seen_facts = known_facts
        if self.error:
            raise self.error
        return EmailAnalysis(**ANALYSIS_KWARGS)


def chain_of(*providers):
    return [
        ProviderLink(label=p.name, model="m", provider=p) for p in providers
    ]


# --- error translation ------------------------------------------------------

def test_quota_error_can_failover():
    assert AIProviderError("x", kind="quota").can_failover


def test_bad_request_does_not_failover():
    assert not AIProviderError("x", kind="bad_request").can_failover


# --- failover ---------------------------------------------------------------

def test_switches_to_next_provider_when_quota_runs_out():
    dead = StubProvider("gemini", AIProviderError("quota gone", kind="quota"))
    alive = StubProvider("groq")
    switches = []

    chain = FallbackAIProvider(
        chain_of(dead, alive),
        on_switch=lambda a, b, e: switches.append((a.label, b.label)),
    )
    result = chain.classify_email(make_email())

    assert result.category.value == "work"
    assert dead.calls == 1 and alive.calls == 1
    assert switches == [("gemini", "groq")]
    assert chain.active.label == "groq"


def test_walks_the_whole_chain():
    a = StubProvider("gemini", AIProviderError("quota", kind="quota"))
    b = StubProvider("groq", AIProviderError("bad key", kind="auth"))
    c = StubProvider("openai")

    chain = FallbackAIProvider(chain_of(a, b, c))
    chain.classify_email(make_email())

    assert (a.calls, b.calls, c.calls) == (1, 1, 1)
    assert chain.active.label == "openai"


def test_does_not_failover_on_our_own_bad_request():
    a = StubProvider("gemini", AIProviderError("malformed", kind="bad_request"))
    b = StubProvider("groq")

    chain = FallbackAIProvider(chain_of(a, b))
    with pytest.raises(AIProviderError):
        chain.classify_email(make_email())

    assert b.calls == 0, "a request that is wrong everywhere must not be retried"


def test_exhausted_provider_is_not_retried_on_the_next_email():
    dead = StubProvider("gemini", AIProviderError("quota", kind="quota"))
    alive = StubProvider("groq")

    chain = FallbackAIProvider(chain_of(dead, alive))
    chain.classify_email(make_email("m1"))
    chain.classify_email(make_email("m2"))

    assert dead.calls == 1, "a provider that ran out should not be asked again"
    assert alive.calls == 2


def test_all_providers_failing_raises_one_clear_error():
    a = StubProvider("gemini", AIProviderError("quota", kind="quota"))
    b = StubProvider("groq", AIProviderError("quota", kind="quota"))

    chain = FallbackAIProvider(chain_of(a, b))
    with pytest.raises(AIProviderError) as excinfo:
        chain.classify_email(make_email())

    assert isinstance(excinfo.value, WorkflowError)
    assert "gemini" in excinfo.value.hint and "groq" in excinfo.value.hint


def test_known_facts_reach_the_provider():
    p = StubProvider("gemini")
    chain = FallbackAIProvider(chain_of(p))
    chain.classify_email(make_email(), None, "Work hours: 9-5")
    assert p.seen_facts == "Work hours: 9-5"


# --- JSON extraction --------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'Here is the JSON you asked for:\n{"a": 1}',
    ],
)
def test_extract_json_survives_wrapping(raw):
    assert _extract_json(raw) == '{"a": 1}'


# --- IMAP -------------------------------------------------------------------

def test_imap_date_is_english_regardless_of_locale():
    assert _imap_date(datetime(2026, 9, 18)) == "18-Sep-2026"
    assert _imap_date(datetime(2026, 1, 1)) == "01-Jan-2026"


def test_email_defaults_are_unlimited_within_a_week():
    cfg = EmailConfig()
    assert cfg.max_age_days == 7
    assert cfg.max_emails_per_run == 0, "0 means no cap on how many are processed"


# --- provider chain building ------------------------------------------------

def test_chain_skips_providers_with_no_key(monkeypatch):
    from email_workflow.providers.ai_factory import build_provider_chain

    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "y")

    config = AppConfig()
    config.ai.api.provider = "gemini"
    config.ai.api.api_key_env = "GEMINI_API_KEY"
    config.ai.fallback.use_local_last = False   # cloud chain only, for this test

    labels = [link.label for link in build_provider_chain(config)]
    assert labels == ["Google Gemini", "Groq"], "only providers with a key belong in the chain"


def test_chain_is_just_the_primary_when_it_is_the_only_key(monkeypatch):
    from email_workflow.providers.ai_factory import build_provider_chain

    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")

    config = AppConfig()
    config.ai.api.provider = "gemini"
    config.ai.api.api_key_env = "GEMINI_API_KEY"
    config.ai.fallback.use_local_last = False

    assert len(build_provider_chain(config)) == 1


def test_the_local_model_is_the_last_resort(monkeypatch):
    """When the connection or every free quota dies, the machine still has a model."""
    from email_workflow.providers.ai_factory import build_provider_chain
    from email_workflow.providers.local_ai import OllamaProvider

    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")

    config = AppConfig()
    config.ai.api.provider = "gemini"
    config.ai.api.api_key_env = "GEMINI_API_KEY"

    chain = build_provider_chain(config)
    assert isinstance(chain[-1].provider, OllamaProvider), "local must come last"
    assert len(chain) == 2


def test_the_local_last_resort_can_be_switched_off(monkeypatch):
    from email_workflow.providers.ai_factory import build_provider_chain
    from email_workflow.providers.local_ai import OllamaProvider

    monkeypatch.setenv("GEMINI_API_KEY", "x")
    config = AppConfig()
    config.ai.api.provider = "gemini"
    config.ai.api.api_key_env = "GEMINI_API_KEY"
    config.ai.fallback.use_local_last = False

    chain = build_provider_chain(config)
    assert not any(isinstance(link.provider, OllamaProvider) for link in chain)
