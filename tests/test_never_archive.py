"""Mail that matters must not be filed away with the newsletters.

What he saw: he is job hunting, and the app archived an employer saying they
had read his application, along with job offers and recruitment mail. The
reasoning was not wrong on its own terms - to a model, "Pracuj.pl: an employer
viewed your application" is a routine low-importance notification that needs no
reply - and the decision engine archived anything routine that needed no reply,
without ever looking at whether it was addressed to HIM or whether he cared
about the subject.

Category says what a message IS. Neither of these questions is about that.
"""

import pytest

from email_workflow.core.decision import evaluate_decision
from email_workflow.models.analysis import EmailAnalysis
from email_workflow.models.config import AutomationConfig
from email_workflow.models.email import DecisionOption, SenderInfo


def analysis(**overrides) -> EmailAnalysis:
    """A routine notification that needs no reply - the archive case."""
    base = dict(
        message_id="m1",
        thread_id="t1",
        sender=SenderInfo(name="Pracuj.pl", email="noreply@aplikacje.pracuj.pl"),
        subject="An employer has read your application",
        received_at="2026-09-20T10:00:00Z",
        category="notification",
        importance="low",
        urgency="low",
        action_required=False,
        response_required=False,
        safe_to_automate=True,
        confidence=1.0,
        recommended_decision="archive",
        reasoning="Informational.",
    )
    base.update(overrides)
    return EmailAnalysis(**base)


def decide(analysis_obj, **automation):
    return evaluate_decision(analysis_obj, AutomationConfig(**automation))


# --- the behaviour that was right, and has to stay right --------------------

def test_a_plain_newsletter_is_still_archived():
    """The feature exists because filing bulk mail away is the whole point."""
    decision, _ = decide(analysis(category="newsletter"))
    assert decision == DecisionOption.ARCHIVE


def test_an_advert_is_still_archived():
    decision, _ = decide(analysis(category="marketing"))
    assert decision == DecisionOption.ARCHIVE


# --- written to you, about something of yours -------------------------------

def test_an_employer_reading_your_application_is_not_archived():
    """His exact email. Category notification, no reply needed, low importance -
    every old rule said archive."""
    decision, reason = decide(analysis(personally_addressed=True))
    assert decision == DecisionOption.NOTIFY_ME, (
        "it should be starred and left in the inbox, not filed away"
    )
    assert "Written to you" in reason


def test_a_bulk_job_alert_is_not_covered_by_that_alone():
    """"Jobs you might like" goes to thousands of people. Personally addressed
    has to mean something, or it means nothing."""
    decision, _ = decide(analysis(category="marketing", personally_addressed=False))
    assert decision == DecisionOption.ARCHIVE


# --- subjects you said you never want filed away ----------------------------

def test_a_subject_you_protected_is_never_archived():
    decision, reason = decide(
        analysis(
            category="marketing",
            protected_topic="job offers and anything about my applications",
        )
    )
    assert decision == DecisionOption.NOTIFY_ME
    assert "job offers" in reason, "the reason should quote your own words back"


def test_the_topic_is_what_decides_it_not_the_category():
    """Even spam, if it is about the thing you said you care about."""
    decision, _ = decide(analysis(category="spam", protected_topic="my landlord"))
    assert decision == DecisionOption.NOTIFY_ME


# --- the hole that was there regardless of any of this ----------------------

def test_a_high_importance_email_is_not_archived_just_because_it_needs_no_reply():
    """The archive branch never looked at importance at all: the model could
    call something high importance and it was filed away anyway."""
    decision, reason = decide(analysis(importance="high"))
    assert decision == DecisionOption.NOTIFY_ME
    assert "importance" in reason


def test_high_urgency_counts_too():
    decision, _ = decide(analysis(urgency="high"))
    assert decision == DecisionOption.NOTIFY_ME


def test_medium_importance_is_still_archived():
    """A line has to be somewhere, and medium is where most routine mail sits -
    treating that as "keep" would archive nothing at all."""
    decision, _ = decide(analysis(importance="medium", urgency="medium"))
    assert decision == DecisionOption.ARCHIVE


# --- the prompt has to actually ask ----------------------------------------

def test_the_prompt_asks_both_questions():
    from email_workflow.providers.base_ai import CLASSIFICATION_PROMPT_TEMPLATE

    assert "personally_addressed" in CLASSIFICATION_PROMPT_TEMPLATE
    assert "protected_topic" in CLASSIFICATION_PROMPT_TEMPLATE
    assert "{protected_topics}" in CLASSIFICATION_PROMPT_TEMPLATE


def test_your_topics_reach_the_prompt_of_whichever_provider_answers():
    """Including the one the chain falls over to - a switch must not quietly
    drop the thing that keeps your mail out of the archive."""
    from email_workflow.models.config import AppConfig
    from email_workflow.providers.ai_factory import get_ai_provider
    from email_workflow.providers.key_pool import leaf_providers

    config = AppConfig()
    config.ai.api.provider = "fake"
    config.automation.never_archive_about = ["job offers"]

    provider = get_ai_provider(config)
    assert "job offers" in provider.protected_topics_block()


def test_no_topics_means_the_question_is_not_asked():
    from email_workflow.providers.fake_ai import FakeAIProvider

    block = FakeAIProvider().protected_topics_block()
    assert "has not named any" in block
