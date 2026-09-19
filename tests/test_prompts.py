"""Tests that the prompts and the code agree with each other.

The bug these exist to prevent: the classification prompt used to instruct the
model to reason about "Known Authorized Facts" that the caller never actually
passed in, so the model was told to use information it could not see. A
placeholder that nobody fills, or a schema key the parser does not expect, is
invisible until a real run fails - so it is checked here instead.
"""

import json
from string import Formatter

import pytest

from email_workflow.core.known_facts import KnownFactsManager

from email_workflow.models.analysis import (
    ClassificationVerdict,
    DecisionSupportOutput,
    EmailAnalysis,
    ReplyGenerationOutput,
)
from email_workflow.models.config import APIConfig
from email_workflow.models.email import EmailMessage, SenderInfo
from email_workflow.providers.api_providers import OpenAICompatibleProvider
from email_workflow.providers.base_ai import (
    CLASSIFICATION_PROMPT_TEMPLATE,
    DECISION_SUPPORT_PROMPT_TEMPLATE,
    REPLY_GENERATION_PROMPT_TEMPLATE,
)

TEMPLATES = {
    "classification": CLASSIFICATION_PROMPT_TEMPLATE,
    "decision_support": DECISION_SUPPORT_PROMPT_TEMPLATE,
    "reply_generation": REPLY_GENERATION_PROMPT_TEMPLATE,
}

SCHEMA_FOR = {
    "classification": ClassificationVerdict,
    "decision_support": DecisionSupportOutput,
    "reply_generation": ReplyGenerationOutput,
}


def placeholders(template: str) -> set:
    return {f for _, f, _, _ in Formatter().parse(template) if f}


def output_schema_of(template: str) -> dict:
    """The JSON skeleton at the end of a prompt, parsed."""
    rendered = template.replace("{{", "{").replace("}}", "}")
    start = rendered.rindex("{\n")
    end = rendered.rindex("}")
    return json.loads(rendered[start : end + 1])


@pytest.fixture()
def email():
    return EmailMessage(
        message_id="<m1@example.com>",
        thread_id="t1",
        in_reply_to=None,
        sender=SenderInfo(name="Ann Lee", email="ann@example.com", known_contact=True),
        subject='Re: budget "Q4" <urgent>',
        body="Can we meet Thursday?",
        received_at="2026-09-18T10:00:00Z",
    )


@pytest.fixture()
def provider():
    return OpenAICompatibleProvider(
        "gemini",
        APIConfig(provider="gemini", model="gemini-2.5-flash", api_key_env="GEMINI_API_KEY"),
    )


class RecordingProvider(OpenAICompatibleProvider):
    """Captures the prompt instead of calling an API."""

    def __init__(self, *args, response, **kwargs):
        super().__init__(*args, **kwargs)
        self.prompts = []
        self._response = response

    def _call_model_with_json_retry(self, prompt, schema_class):
        self.prompts.append(prompt)
        return schema_class.model_validate(self._response)


VERDICT = {
    "category": "work",
    "importance": "high",
    "urgency": "medium",
    "action_required": True,
    "response_required": True,
    "safe_to_automate": False,
    "confidence": 0.82,
    "missing_information": [],
    "commitments_implied": [],
    "recommended_decision": "create_draft",
    "reasoning": "asks for a meeting",
}


# --- the prompt output block matches the model we parse into ----------------

@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_prompt_output_block_matches_its_schema(name):
    declared = set(output_schema_of(TEMPLATES[name]))
    expected = set(SCHEMA_FOR[name].model_fields)
    assert declared == expected, (
        f"{name}: the JSON block in the prompt and {SCHEMA_FOR[name].__name__} "
        f"have drifted apart. Only in prompt: {declared - expected}. "
        f"Only in model: {expected - declared}."
    )


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_prompt_output_block_is_valid_json(name):
    assert isinstance(output_schema_of(TEMPLATES[name]), dict)


# --- every placeholder is actually supplied ---------------------------------

def test_classification_prompt_is_fully_filled(email, provider):
    recorder = RecordingProvider(
        "gemini",
        APIConfig(provider="gemini", model="m", api_key_env="GEMINI_API_KEY"),
        response=VERDICT,
    )
    recorder.classify_email(email, None, "Work hours: 9-5")
    rendered = recorder.prompts[0]

    assert "{" not in rendered.split("=== OUTPUT ===")[0], "an unfilled placeholder remains"
    assert "Work hours: 9-5" in rendered, "known facts must reach the model"
    assert email.subject in rendered
    assert email.body in rendered
    assert "ann@example.com" in rendered


def test_classification_prompt_declares_every_placeholder_the_caller_passes(email):
    """No placeholder in the template goes unfilled, and none is passed in vain."""
    supplied = {
        "subject", "sender_name", "sender_email", "known_contact",
        "received_at", "body", "thread_context", "known_facts",
    }
    assert placeholders(CLASSIFICATION_PROMPT_TEMPLATE) == supplied


def test_known_facts_is_a_placeholder_in_every_template_that_mentions_it():
    for name, template in TEMPLATES.items():
        if "Known Facts" in template or "known facts" in template:
            assert "known_facts" in placeholders(template), (
                f"{name} talks about Known Facts but never receives them"
            )


def test_reply_prompt_is_fully_filled(email):
    recorder = RecordingProvider(
        "gemini",
        APIConfig(provider="gemini", model="m", api_key_env="GEMINI_API_KEY"),
        response={
            "reply_subject": "Re: x",
            "reply_body": "Sure.",
            "complete": True,
            "placeholders_used": [],
            "commitments_made": [],
        },
    )
    recorder.generate_reply(email, None, "Available Thursdays")
    rendered = recorder.prompts[0]
    assert "Available Thursdays" in rendered
    assert "{" not in rendered.split("=== OUTPUT ===")[0]


def test_decision_support_prompt_is_fully_filled(email):
    recorder = RecordingProvider(
        "gemini",
        APIConfig(provider="gemini", model="m", api_key_env="GEMINI_API_KEY"),
        response={
            "missing_information": [],
            "commitments_implied": [],
            "would_require_invented_facts": False,
            "analysis_summary": "fine",
        },
    )
    recorder.evaluate_decision_support(email, None, "Net-30 terms")
    assert "Net-30 terms" in recorder.prompts[0]


# --- metadata is ours, not the model's --------------------------------------

def test_model_cannot_change_the_message_identity(email):
    """The model returns a judgement only; ids and subject are merged in locally."""
    hostile = dict(VERDICT)
    hostile["message_id"] = "<attacker@evil.com>"
    hostile["subject"] = "something else entirely"

    recorder = RecordingProvider(
        "gemini",
        APIConfig(provider="gemini", model="m", api_key_env="GEMINI_API_KEY"),
        response=hostile,
    )
    analysis = recorder.classify_email(email)

    assert analysis.message_id == email.message_id
    assert analysis.thread_id == email.thread_id
    assert analysis.subject == email.subject
    assert analysis.sender.email == email.sender.email
    assert analysis.received_at == email.received_at


def test_classification_returns_a_full_email_analysis(email):
    recorder = RecordingProvider(
        "gemini",
        APIConfig(provider="gemini", model="m", api_key_env="GEMINI_API_KEY"),
        response=VERDICT,
    )
    analysis = recorder.classify_email(email)
    assert isinstance(analysis, EmailAnalysis)
    assert analysis.category.value == "work"
    assert analysis.confidence == pytest.approx(0.82)


def test_verdict_rejects_an_out_of_range_confidence():
    bad = dict(VERDICT, confidence=1.7)
    with pytest.raises(Exception):
        ClassificationVerdict.model_validate(bad)


def test_verdict_rejects_an_unknown_category():
    bad = dict(VERDICT, category="not-a-category")
    with pytest.raises(Exception):
        ClassificationVerdict.model_validate(bad)


def test_verdict_rejects_an_unknown_decision():
    bad = dict(VERDICT, recommended_decision="delete_everything")
    with pytest.raises(Exception):
        ClassificationVerdict.model_validate(bad)


# --- the safety instructions are actually present ---------------------------

def test_classification_prompt_forbids_automating_security_and_financial_mail():
    text = CLASSIFICATION_PROMPT_TEMPLATE
    assert "safe_to_automate MUST be false" in text
    assert "escalate" in text


def test_reply_prompt_forbids_inventing_facts():
    assert "NEEDS INPUT" in REPLY_GENERATION_PROMPT_TEMPLATE


def test_prompts_do_not_ask_the_model_to_echo_identifiers():
    """Echoed ids cost output tokens and can come back corrupted."""
    for name, template in TEMPLATES.items():
        schema = output_schema_of(template)
        assert "message_id" not in schema, f"{name} should not ask for message_id back"
        assert "thread_id" not in schema, f"{name} should not ask for thread_id back"


# --- a settled receipt is not "financial" -----------------------------------

def test_the_prompt_separates_a_settled_receipt_from_money_that_wants_something():
    """A payment confirmation was escalated instead of archived.

    Not the model's fault: step 4 rule 1 listed "invoices" with no qualifier and
    said it overrides everything below, while rule 6 said receipts are filed
    away. Rule 1 always won, so every receipt that named an invoice number came
    back starred and waiting for a person - burying the ones that really did
    need one. Verified against the live model after the wording changed: the
    same email now classifies as receipt and archives, while a wire transfer and
    an unpaid invoice still escalate.
    """
    prompt = CLASSIFICATION_PROMPT_TEMPLATE

    assert "payment details, bank accounts, invoices or wire transfers" not in prompt, (
        "the unqualified word 'invoices' is what escalated settled receipts"
    )
    assert "an invoice still awaiting payment" in prompt, (
        "only an invoice that still wants paying should reach the escalate rule"
    )
    assert "money already settled that needs nothing" in prompt, (
        "the prompt has to say what a receipt is, or the model cannot tell them apart"
    )


def test_the_escalate_rule_still_covers_what_it_must():
    """Narrowing rule 1 must not have narrowed the dangerous cases away."""
    prompt = CLASSIFICATION_PROMPT_TEMPLATE
    for must_stay in ("passwords", "login codes", "payment details",
                      "bank accounts", "wire transfer", "safe_to_automate MUST be false"):
        assert must_stay in prompt, f"the escalate rule lost '{must_stay}'"


# --- the facts can come from the environment --------------------------------

def test_the_file_is_used_when_you_have_one(tmp_path, monkeypatch):
    facts = tmp_path / "known_facts.txt"
    facts.write_text("- User Name: From the file", encoding="utf-8")
    monkeypatch.setenv("KNOWN_FACTS", "- User Name: From the environment")

    mgr = KnownFactsManager(file_path=str(facts))
    assert "From the file" in mgr.load_facts(), (
        "a stale variable in a shell must not override what you just edited"
    )


def test_the_environment_is_used_when_there_is_no_file(tmp_path, monkeypatch):
    """This is what makes a scheduled cloud run know anything about you:
    known_facts.txt is gitignored, so it never reaches GitHub."""
    monkeypatch.setenv("KNOWN_FACTS", "- User Name: From the environment")
    mgr = KnownFactsManager(file_path=str(tmp_path / "absent.txt"))

    assert "From the environment" in mgr.load_facts()
    assert not (tmp_path / "absent.txt").exists(), (
        "nothing should be written to disk when the facts came from the environment"
    )


def test_an_empty_file_falls_through_to_the_environment(tmp_path, monkeypatch):
    facts = tmp_path / "known_facts.txt"
    facts.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("KNOWN_FACTS", "- User Name: From the environment")
    assert "From the environment" in KnownFactsManager(file_path=str(facts)).load_facts()


def test_with_neither_you_get_the_template(tmp_path, monkeypatch):
    monkeypatch.delenv("KNOWN_FACTS", raising=False)
    target = tmp_path / "known_facts.txt"
    text = KnownFactsManager(file_path=str(target)).load_facts()
    assert "[your name]" in text, "the shipped template must not name a person"
    assert target.exists(), "a first run should leave a file to edit"


def test_the_template_names_nobody():
    """The shipped template must be a blank form, not somebody's details.

    Deliberately checked by looking for the placeholder rather than by listing
    real names: a test that spells out the name it is guarding against would
    put that name back into the repository it is trying to keep clean.
    """
    from email_workflow.core.known_facts import DEFAULT_KNOWN_FACTS
    assert "[your name]" in DEFAULT_KNOWN_FACTS
    assert "@" not in DEFAULT_KNOWN_FACTS, "no address belongs in the template"
