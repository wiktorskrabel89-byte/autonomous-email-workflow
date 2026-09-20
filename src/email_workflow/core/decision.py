from typing import Tuple, Optional
from email_workflow.models.config import AutomationConfig, AutomationLevel, AutonomousRule
from email_workflow.models.email import EmailCategory, DecisionOption, ImportanceLevel
from email_workflow.models.analysis import EmailAnalysis

HARD_SAFETY_CATEGORIES = {
    EmailCategory.FINANCIAL,
    EmailCategory.SECURITY,
    EmailCategory.ACCOUNT,
}

HARD_SAFETY_KEYWORDS = [
    "password", "credential", "phishing", "contract", "legal",
    "wire transfer", "social security", "credit card", "bank login"
]

def _reason_to_keep(analysis: EmailAnalysis, kept_label: str = "") -> Optional[str]:
    """Why this must NOT be archived, or None if filing it away is fine.

    Three separate ways an email earns its place in the inbox. Before this,
    "no reply needed" plus a routine-looking category was enough to archive
    anything - which is how an employer saying they had read a job application
    ended up filed with the supermarket newsletters. Category says what a
    message IS; none of these are about that.
    """
    if kept_label:
        return (
            f"Filed under '{kept_label}', which you asked to keep in your "
            f"inbox. Left where you will see it."
        )
    if analysis.protected_topic:
        return (
            f"You asked never to archive anything about "
            f"'{analysis.protected_topic}'. Starred and left in your inbox."
        )
    if analysis.personally_addressed:
        return (
            "Written to you about something of yours, not sent to a list. "
            "Starred and left in your inbox."
        )
    # The archive branch used to ignore importance completely, so a message the
    # model itself had called high-importance was archived anyway if it needed
    # no reply.
    if ImportanceLevel.HIGH in (analysis.importance, analysis.urgency):
        return (
            f"No reply needed, but this is {analysis.importance.value} importance "
            f"and {analysis.urgency.value} urgency. Starred and left in your inbox."
        )
    return None


def evaluate_decision(
    analysis: EmailAnalysis,
    automation: AutomationConfig,
    subject: str = "",
    body: str = "",
    kept_label: str = "",
) -> Tuple[DecisionOption, str]:
    """
    Deterministic 4-step decision engine.
    Returns (final_decision, reason_description).
    """

    # -------------------------------------------------------------
    # 1. HARD SAFETY RULES (Override everything, never auto-reply)
    # -------------------------------------------------------------
    hard_rule_reasons = []

    # Category checks
    if analysis.category in HARD_SAFETY_CATEGORIES:
        hard_rule_reasons.append(f"Hard safety category '{analysis.category.value}'")

    # Subject/body security/legal keywords
    text_content = f"{subject} {body}".lower()
    for kw in HARD_SAFETY_KEYWORDS:
        if kw in text_content:
            hard_rule_reasons.append(f"Hard safety keyword '{kw}' detected")
            break

    # Confidence check below draft threshold
    if analysis.confidence < automation.confidence_threshold_draft:
        hard_rule_reasons.append(
            f"Confidence ({analysis.confidence:.2f}) below draft threshold ({automation.confidence_threshold_draft:.2f})"
        )

    # Missing information check
    if analysis.missing_information and len(analysis.missing_information) > 0:
        hard_rule_reasons.append(
            f"Missing required information: {', '.join(analysis.missing_information)}"
        )

    # If any hard safety rule triggered
    if hard_rule_reasons:
        reason_str = "Hard safety rule triggered: " + "; ".join(hard_rule_reasons)
        if analysis.category in (EmailCategory.FINANCIAL, EmailCategory.SECURITY):
            return DecisionOption.ESCALATE, reason_str
        elif analysis.missing_information:
            return DecisionOption.ESCALATE, reason_str
        else:
            return DecisionOption.CREATE_DRAFT, reason_str

    # -------------------------------------------------------------
    # 2. RESPONSE REQUIRED CHECK
    # -------------------------------------------------------------
    if not analysis.response_required:
        if analysis.category in (
            EmailCategory.NEWSLETTER,
            EmailCategory.RECEIPT,
            EmailCategory.NOTIFICATION,
            EmailCategory.MARKETING,
            EmailCategory.AUTOMATED,
            EmailCategory.SPAM,
        ):
            keep = _reason_to_keep(analysis, kept_label)
            if keep:
                return DecisionOption.NOTIFY_ME, keep
            return DecisionOption.ARCHIVE, "No response required for informational email. Archiving."
        else:
            return DecisionOption.IGNORE, "No response required. Ignoring."

    # -------------------------------------------------------------
    # 3. AUTOMATION LEVEL GATING
    # -------------------------------------------------------------
    level = automation.level

    if level in (AutomationLevel.MANUAL, AutomationLevel.DRAFT):
        return (
            DecisionOption.CREATE_DRAFT,
            f"Automation level '{level.value}' restricts auto-replies. Creating draft.",
        )

    # Trusted automation level check
    if level == AutomationLevel.TRUSTED:
        allowed_trusted = [c.lower() for c in automation.trusted_categories]
        if analysis.category.value.lower() not in allowed_trusted:
            return (
                DecisionOption.CREATE_DRAFT,
                f"Category '{analysis.category.value}' is not in trusted_categories for level 'trusted'. Creating draft.",
            )

    # Autonomous automation level check
    if level == AutomationLevel.AUTONOMOUS:
        # Check if category matches any autonomous rule
        rule_matched = False
        if automation.autonomous_rules:
            for rule in automation.autonomous_rules:
                if rule.category.lower() == analysis.category.value.lower():
                    if rule.min_confidence and analysis.confidence < rule.min_confidence:
                        continue
                    rule_matched = True
                    break
        else:
            # If rules list empty, allow default categories
            rule_matched = analysis.category.value.lower() in [c.lower() for c in automation.trusted_categories]

        if not rule_matched and analysis.category not in (EmailCategory.WORK, EmailCategory.SUPPORT, EmailCategory.PERSONAL):
            return (
                DecisionOption.WAIT_FOR_APPROVAL,
                f"Category '{analysis.category.value}' is not matched by autonomous rules. Waiting for approval.",
            )

    # -------------------------------------------------------------
    # 4. CONFIDENCE & COMMITMENT EVALUATION
    # -------------------------------------------------------------
    has_implied_commitments = bool(analysis.commitments_implied and len(analysis.commitments_implied) > 0)

    if (
        analysis.confidence >= automation.confidence_threshold_auto_reply
        and not analysis.missing_information
        and not has_implied_commitments
        and analysis.safe_to_automate
    ):
        return (
            DecisionOption.AUTOMATICALLY_REPLY,
            f"Confidence ({analysis.confidence:.2f}) meets auto-reply threshold ({automation.confidence_threshold_auto_reply:.2f}) with no missing info or implied commitments.",
        )
    elif analysis.confidence >= automation.confidence_threshold_draft:
        if has_implied_commitments or not analysis.safe_to_automate:
            return (
                DecisionOption.WAIT_FOR_APPROVAL,
                f"Confidence ({analysis.confidence:.2f}) sufficient, but email involves commitments or requires approval. Waiting for approval.",
            )
        return (
            DecisionOption.CREATE_DRAFT,
            f"Confidence ({analysis.confidence:.2f}) meets draft threshold ({automation.confidence_threshold_draft:.2f}). Creating draft.",
        )
    else:
        return (
            DecisionOption.NOTIFY_ME,
            f"Confidence ({analysis.confidence:.2f}) is low. Requesting user intervention.",
        )
