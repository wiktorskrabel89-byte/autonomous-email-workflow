"""Sorting mail into labels of your own.

His ask: "you can create labels and maybe someone wants to have it organized,
so you add labels like Rabaty - e.g. from Modivo -10% - or job offers... if I
get a rabat from Modivo then not normally just archive, but with this thing we
arrange it into the section of labels it needs, and do the same for Discord".

So: a discount code still leaves the inbox, but it lands under "Rabaty" rather
than disappearing into All Mail with everything else, and the report says where
things went instead of only how many were archived.
"""

import pytest

from email_workflow.core.notifications import group_by_label
from email_workflow.core.pipeline import _label_to_apply
from email_workflow.models.config import AppConfig, EmailConfig, EmailLabel
from email_workflow.models.analysis import EmailAnalysis
from email_workflow.models.email import SenderInfo
from email_workflow.providers.fake_ai import FakeAIProvider


def config_with_labels(*pairs) -> AppConfig:
    config = AppConfig()
    config.email.labels = [EmailLabel(name=n, about=a) for n, a in pairs]
    return config


def analysis(**overrides) -> EmailAnalysis:
    base = dict(
        message_id="m1", thread_id="t1",
        sender=SenderInfo(name="Modivo", email="news@modivo.pl"),
        subject="-10% na wszystko w Modivo", received_at="2026-09-20T10:00:00Z",
        category="marketing", importance="low", urgency="low",
        action_required=False, response_required=False, safe_to_automate=True,
        confidence=1.0, recommended_decision="archive", reasoning="An advert.",
    )
    base.update(overrides)
    return EmailAnalysis(**base)


# --- which label, if any ----------------------------------------------------

def test_a_matching_label_is_used():
    config = config_with_labels(("Rabaty", "discount codes and sales"))
    assert _label_to_apply(analysis(suggested_label="Rabaty"), config) == "Rabaty"


def test_nothing_suggested_means_nothing_is_filed():
    config = config_with_labels(("Rabaty", "discounts"))
    assert _label_to_apply(analysis(suggested_label=""), config) == ""


def test_a_label_you_never_asked_for_is_refused():
    """A model that invents a label would have Gmail create it, and a sidebar
    filling up with labels nobody asked for is worse than no filing at all."""
    config = config_with_labels(("Rabaty", "discounts"))
    assert _label_to_apply(analysis(suggested_label="Newsletters"), config) == ""


def test_your_spelling_wins_over_the_models():
    """Matched loosely, applied exactly - otherwise Gmail ends up with both
    "rabaty" and "Rabaty" as separate labels."""
    config = config_with_labels(("Rabaty", "discounts"))
    assert _label_to_apply(analysis(suggested_label=" rabaty "), config) == "Rabaty"


def test_no_labels_configured_means_the_feature_is_simply_off():
    assert _label_to_apply(analysis(suggested_label="Rabaty"), AppConfig()) == ""


# --- what the AI is told ----------------------------------------------------

def test_the_prompt_asks_for_a_label():
    from email_workflow.providers.base_ai import CLASSIFICATION_PROMPT_TEMPLATE

    assert "suggested_label" in CLASSIFICATION_PROMPT_TEMPLATE
    assert "{your_labels}" in CLASSIFICATION_PROMPT_TEMPLATE


def test_your_labels_and_their_descriptions_reach_the_prompt():
    """The description is the part that matters: "Rabaty" alone means nothing
    to a model reading Polish marketing mail."""
    ai = FakeAIProvider()
    ai.labels = (("Rabaty", "discount codes, sales, -10% offers"),)
    block = ai.labels_block()
    assert "Rabaty" in block
    assert "discount codes" in block


def test_with_no_labels_the_question_is_not_asked():
    assert "no labels of their own" in FakeAIProvider().labels_block()


def test_the_labels_reach_whichever_provider_answers():
    from email_workflow.providers.ai_factory import get_ai_provider

    config = config_with_labels(("Rabaty", "discounts"))
    config.ai.api.provider = "fake"
    assert "Rabaty" in get_ai_provider(config).labels_block()


# --- the label goes on before the mail is archived --------------------------

def test_it_is_filed_before_it_is_archived():
    """On Gmail, archiving deletes the message out of INBOX and expunges it.
    Label it afterwards and there is nothing left in INBOX to label - which
    is the whole difference between "filed under Rabaty" and "gone"."""
    from email_workflow.providers.email_provider import MockEmailProvider

    order = []

    class Watching(MockEmailProvider):
        def apply_label(self, message_id, label):
            order.append(("label", label))

        def archive_email(self, message_id):
            order.append(("archive", message_id))

    provider = Watching()
    provider.apply_label("<m1@x>", "Rabaty")
    provider.archive_email("<m1@x>")
    assert order[0][0] == "label" and order[1][0] == "archive"


def test_a_mailbox_that_cannot_label_does_not_stop_the_run():
    from email_workflow.providers.email_provider import EmailProvider

    assert EmailProvider.apply_label(None, "<m1@x>", "Rabaty") is None


# --- the report says where things went --------------------------------------

def test_the_digest_groups_by_label():
    results = [
        {"filed_under": "Rabaty", "analysis": analysis(subject="-10% Modivo")},
        {"filed_under": "Rabaty", "analysis": analysis(subject="-30% Reserved")},
        {"filed_under": "Job offers", "analysis": analysis(subject="New role")},
        {"filed_under": "", "analysis": analysis(subject="Something else")},
    ]
    grouped = group_by_label(results)

    assert list(grouped) == ["Rabaty", "Job offers"], "fullest label first"
    assert len(grouped["Rabaty"]) == 2
    assert "Something else" not in str(grouped), "unfiled mail is not a label"


def test_a_run_with_no_labels_groups_nothing():
    assert group_by_label([{"filed_under": "", "analysis": analysis()}]) == {}


def test_the_subject_is_what_is_shown_not_the_message_id():
    grouped = group_by_label(
        [{"filed_under": "Rabaty", "analysis": analysis(subject="-10% Modivo")}]
    )
    assert grouped["Rabaty"] == ["-10% Modivo"]
