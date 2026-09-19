import pytest
from email_workflow.models.config import AutomationConfig, AutomationLevel, AutonomousRule
from email_workflow.models.email import EmailCategory, ImportanceLevel, UrgencyLevel, DecisionOption, SenderInfo
from email_workflow.models.analysis import EmailAnalysis
from email_workflow.core.decision import evaluate_decision

def make_analysis(
    category=EmailCategory.WORK,
    importance=ImportanceLevel.MEDIUM,
    urgency=UrgencyLevel.MEDIUM,
    action_required=True,
    response_required=True,
    safe_to_automate=True,
    confidence=0.95,
    missing_info=None,
    commitments_implied=None,
    recommended_decision=DecisionOption.CREATE_DRAFT,
) -> EmailAnalysis:
    return EmailAnalysis(
        message_id="msg_test",
        thread_id="thread_test",
        sender=SenderInfo(name="Test Sender", email="test@example.com", known_contact=True),
        subject="Test Subject",
        received_at="2026-09-18T10:00:00Z",
        category=category,
        importance=importance,
        urgency=urgency,
        action_required=action_required,
        response_required=response_required,
        safe_to_automate=safe_to_automate,
        confidence=confidence,
        missing_information=missing_info or [],
        commitments_implied=commitments_implied or [],
        recommended_decision=recommended_decision,
        reasoning="Test analysis reasoning",
    )

def test_hard_safety_financial():
    automation = AutomationConfig()
    analysis = make_analysis(category=EmailCategory.FINANCIAL)
    decision, reason = evaluate_decision(analysis, automation)
    assert decision == DecisionOption.ESCALATE
    assert "Hard safety rule triggered" in reason

def test_hard_safety_password_keyword():
    automation = AutomationConfig()
    analysis = make_analysis(category=EmailCategory.WORK)
    decision, reason = evaluate_decision(analysis, automation, subject="Please update your password")
    assert decision == DecisionOption.CREATE_DRAFT
    assert "password" in reason.lower()

def test_hard_safety_missing_information():
    automation = AutomationConfig()
    analysis = make_analysis(missing_info=["Missing account balance"])
    decision, reason = evaluate_decision(analysis, automation)
    assert decision == DecisionOption.ESCALATE
    assert "Missing required information" in reason

def test_response_not_required_newsletter():
    automation = AutomationConfig()
    analysis = make_analysis(category=EmailCategory.NEWSLETTER, response_required=False, action_required=False)
    decision, reason = evaluate_decision(analysis, automation)
    assert decision == DecisionOption.ARCHIVE

def test_automation_level_manual_blocks_auto_reply():
    automation = AutomationConfig(level=AutomationLevel.MANUAL)
    analysis = make_analysis(category=EmailCategory.WORK, confidence=0.99)
    decision, reason = evaluate_decision(analysis, automation)
    assert decision == DecisionOption.CREATE_DRAFT
    assert "restricts auto-replies" in reason

def test_trusted_automation_level_allowed_vs_disallowed():
    automation = AutomationConfig(
        level=AutomationLevel.TRUSTED,
        trusted_categories=["newsletter", "receipt"]
    )
    # Allowed category
    analysis_receipt = make_analysis(category=EmailCategory.RECEIPT, confidence=0.95)
    decision, _ = evaluate_decision(analysis_receipt, automation)
    assert decision == DecisionOption.AUTOMATICALLY_REPLY

    # Disallowed category for trusted level
    analysis_work = make_analysis(category=EmailCategory.WORK, confidence=0.95)
    decision, reason = evaluate_decision(analysis_work, automation)
    assert decision == DecisionOption.CREATE_DRAFT
    assert "not in trusted_categories" in reason

def test_autonomous_level_auto_reply():
    automation = AutomationConfig(
        level=AutomationLevel.AUTONOMOUS,
        autonomous_rules=[
            AutonomousRule(category="work", min_confidence=0.85)
        ],
        confidence_threshold_auto_reply=0.90
    )
    analysis = make_analysis(category=EmailCategory.WORK, confidence=0.95)
    decision, _ = evaluate_decision(analysis, automation)
    assert decision == DecisionOption.AUTOMATICALLY_REPLY
