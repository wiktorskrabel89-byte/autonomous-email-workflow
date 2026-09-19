from typing import List, Optional
from email_workflow.models.email import EmailMessage, EmailCategory, DecisionOption
from email_workflow.models.analysis import (
    EmailAnalysis,
    ReplyGenerationOutput,
    FinalValidationResult,
)
from email_workflow.models.config import AutomationConfig
from email_workflow.models.state import ThreadState

def validate_send_gate(
    message: EmailMessage,
    analysis: EmailAnalysis,
    reply: ReplyGenerationOutput,
    thread: ThreadState,
    automation: AutomationConfig,
) -> FinalValidationResult:
    """
    Deterministic validation gate before sending any automated email reply.
    If ANY check fails, returns valid=False with reasons and fallback_decision=CREATE_DRAFT.
    """
    reasons: List[str] = []

    # 1. No fabricated facts / incomplete response
    if not reply.complete:
        reasons.append("Reply is incomplete and contains unresolved placeholders.")

    if reply.placeholders_used and len(reply.placeholders_used) > 0:
        reasons.append(f"Reply contains unresolved placeholders: {reply.placeholders_used}")

    # 2. No new unauthorized commitments
    if reply.commitments_made and len(reply.commitments_made) > 0:
        reasons.append(f"Reply attempts unauthorized commitments: {reply.commitments_made}")

    # 3. Category allowed at send time
    allowed_categories = [c.lower() for c in automation.trusted_categories]
    if automation.autonomous_rules:
        allowed_categories.extend([r.category.lower() for r in automation.autonomous_rules])

    if analysis.category.value.lower() not in allowed_categories:
        reasons.append(
            f"Category '{analysis.category.value}' is not permitted in allowed send list at send time."
        )

    # 4. Confidence holds
    if analysis.confidence < automation.confidence_threshold_auto_reply:
        reasons.append(
            f"Confidence score {analysis.confidence:.2f} is below auto-reply threshold {automation.confidence_threshold_auto_reply:.2f} at send time."
        )

    # 5. Thread hasn't gotten a newer message since analysis started
    latest_msg_id = thread.messages[-1].message_id if thread.messages else None
    if latest_msg_id and latest_msg_id != message.message_id:
        reasons.append(
            f"Thread {message.thread_id} received a newer message ({latest_msg_id}) since analysis started."
        )

    if reasons:
        return FinalValidationResult(
            valid=False,
            failure_reasons=reasons,
            fallback_decision=DecisionOption.CREATE_DRAFT,
        )

    return FinalValidationResult(
        valid=True,
        failure_reasons=[],
        fallback_decision=DecisionOption.AUTOMATICALLY_REPLY,
    )
