"""Tests for running with no internet connection.

Two separate questions:

1. The demo must make no network calls at all - that is what makes it safe to
   hand to someone with no API key.
2. The local (Ollama) mode must do real work against a model on this machine,
   with no cloud provider involved.

The mailbox itself is the part that cannot be offline: reading Gmail needs the
internet by definition. Offline means the AI runs locally, not that email
arrives by magic.
"""

import json

import pytest

from email_workflow.core.errors import AIProviderError
from email_workflow.models.analysis import EmailAnalysis
from email_workflow.models.config import AppConfig, AIMode, LocalConfig
from email_workflow.models.email import EmailMessage, SenderInfo
from email_workflow.providers.ai_factory import get_ai_provider
from email_workflow.providers.fake_ai import FakeAIProvider
from email_workflow.providers.local_ai import OllamaProvider

VERDICT = {
    "category": "work",
    "importance": "medium",
    "urgency": "medium",
    "action_required": True,
    "response_required": True,
    "safe_to_automate": False,
    "confidence": 0.8,
    "missing_information": [],
    "commitments_implied": [],
    "recommended_decision": "create_draft",
    "reasoning": "asks for a meeting",
}


def an_email():
    return EmailMessage(
        message_id="<m1@example.com>",
        thread_id="t1",
        sender=SenderInfo(name="Ann", email="ann@example.com", known_contact=True),
        subject="Can we meet?",
        body="Are you free Thursday?",
        received_at="2026-09-18T10:00:00Z",
    )


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")


@pytest.fixture()
def ollama(monkeypatch):
    """A stand-in for a model running on this machine."""
    calls = {"posts": [], "gets": []}

    def fake_post(url, json=None, timeout=None):
        calls["posts"].append((url, json))
        return FakeResponse({"response": __import__("json").dumps(VERDICT)})

    def fake_get(url, timeout=None):
        calls["gets"].append(url)
        return FakeResponse({"models": []})

    monkeypatch.setattr("email_workflow.providers.local_ai.httpx.post", fake_post)
    monkeypatch.setattr("email_workflow.providers.local_ai.httpx.get", fake_get)
    return calls


# --- the demo touches nothing ----------------------------------------------

def test_the_fake_provider_makes_no_network_calls(monkeypatch):
    """Guard: if the demo ever grew a network call, this fails."""
    def forbidden(*args, **kwargs):
        raise AssertionError("the demo must not touch the network")

    for target in ("httpx.post", "httpx.get"):
        monkeypatch.setattr(f"email_workflow.providers.local_ai.{target}", forbidden)

    analysis = FakeAIProvider().classify_email(an_email())
    assert isinstance(analysis, EmailAnalysis)


def test_the_fake_provider_needs_no_api_key(monkeypatch):
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    config = AppConfig()
    config.ai.api.provider = "fake"
    provider = get_ai_provider(config)

    assert isinstance(provider, FakeAIProvider)
    assert provider.describe() == ("fake", "demo-deterministic")


# --- local mode does real work ---------------------------------------------

def test_local_mode_classifies_through_ollama(ollama):
    provider = OllamaProvider(LocalConfig())
    analysis = provider.classify_email(an_email(), None, "Work hours: 9-5")

    assert isinstance(analysis, EmailAnalysis)
    assert analysis.category.value == "work"
    assert ollama["posts"], "it should have asked the local model"


def test_local_mode_reaches_localhost_only(ollama):
    OllamaProvider(LocalConfig()).classify_email(an_email())
    url, _ = ollama["posts"][0]
    assert url.startswith("http://localhost:11434"), (
        "local mode must not leave the machine"
    )


def test_known_facts_reach_the_local_model(ollama):
    OllamaProvider(LocalConfig()).classify_email(an_email(), None, "Meetings: Thursdays")
    _, payload = ollama["posts"][0]
    assert "Meetings: Thursdays" in payload["prompt"]


def test_local_mode_merges_metadata_the_model_never_sees(ollama):
    """Same contract as the cloud providers: ids are ours, not the model's."""
    email = an_email()
    analysis = OllamaProvider(LocalConfig()).classify_email(email)

    assert analysis.message_id == email.message_id
    assert analysis.thread_id == email.thread_id
    assert analysis.subject == email.subject
    assert analysis.sender.email == email.sender.email


def test_local_mode_reports_itself_for_the_audit_log(ollama):
    assert OllamaProvider(LocalConfig()).describe() == ("ollama", "llama3.2")


def test_local_mode_needs_no_api_key(monkeypatch, ollama):
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    config = AppConfig()
    config.ai.mode = AIMode.LOCAL
    provider = get_ai_provider(config)

    assert isinstance(provider, OllamaProvider)


def test_a_stopped_ollama_is_explained_not_crashed(monkeypatch):
    def refuse(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("email_workflow.providers.local_ai.httpx.get", refuse)

    config = AppConfig()
    config.ai.mode = AIMode.LOCAL
    with pytest.raises(ValueError, match="Ollama"):
        get_ai_provider(config)


def test_a_local_model_that_breaks_mid_answer_is_explained(monkeypatch, ollama):
    def broken_post(url, json=None, timeout=None):
        return FakeResponse({"response": "this is not json"})

    monkeypatch.setattr("email_workflow.providers.local_ai.httpx.post", broken_post)

    with pytest.raises((AIProviderError, ValueError, json.JSONDecodeError)):
        OllamaProvider(LocalConfig()).classify_email(an_email())
