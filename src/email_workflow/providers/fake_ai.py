from typing import Optional
from email_workflow.models.email import (
    EmailMessage,
    EmailCategory,
    ImportanceLevel,
    UrgencyLevel,
    DecisionOption,
    SenderInfo,
)
from email_workflow.models.analysis import (
    EmailAnalysis,
    DecisionSupportOutput,
    ReplyGenerationOutput,
)
from email_workflow.models.state import ThreadState
from email_workflow.providers.base_ai import AIProvider

class FakeAIProvider(AIProvider):
    """Deterministic Fake AI Provider for demo execution (zero network, zero API keys)."""

    def describe(self) -> tuple:
        return ("fake", "demo-deterministic")

    def validate_setup(self) -> None:
        pass  # Always valid offline

    def classify_email(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> EmailAnalysis:
        subj = message.subject.lower()
        body = message.body.lower()

        # Case 1: Newsletter
        if "newsletter" in subj or "weekly digest" in subj or "newsletter" in body:
            return EmailAnalysis(
                message_id=message.message_id,
                thread_id=message.thread_id,
                in_reply_to=message.in_reply_to,
                sender=message.sender,
                subject=message.subject,
                received_at=message.received_at,
                category=EmailCategory.NEWSLETTER,
                importance=ImportanceLevel.LOW,
                urgency=ImportanceLevel.LOW,
                action_required=False,
                response_required=False,
                safe_to_automate=True,
                confidence=0.98,
                missing_information=[],
                commitments_implied=[],
                recommended_decision=DecisionOption.ARCHIVE,
                reasoning="Routine informational newsletter. No action or response required.",
            )

        # Case 2a: Meeting Tuesday
        elif "tuesday" in subj or "meet tuesday" in body:
            return EmailAnalysis(
                message_id=message.message_id,
                thread_id=message.thread_id,
                in_reply_to=message.in_reply_to,
                sender=message.sender,
                subject=message.subject,
                received_at=message.received_at,
                category=EmailCategory.CALENDAR,
                importance=ImportanceLevel.MEDIUM,
                urgency=ImportanceLevel.MEDIUM,
                action_required=True,
                response_required=True,
                safe_to_automate=False,
                confidence=0.92,
                missing_information=[],
                commitments_implied=["Proposed meeting on Tuesday at 2 PM"],
                recommended_decision=DecisionOption.CREATE_DRAFT,
                reasoning="Calendar meeting invitation. Requires response; calendar is not a trusted auto-reply category.",
            )

        # Case 2b: Meeting Wednesday (update on same thread)
        elif "wednesday" in subj or "wednesday works better" in body:
            return EmailAnalysis(
                message_id=message.message_id,
                thread_id=message.thread_id,
                in_reply_to=message.in_reply_to,
                sender=message.sender,
                subject=message.subject,
                received_at=message.received_at,
                category=EmailCategory.CALENDAR,
                importance=ImportanceLevel.MEDIUM,
                urgency=ImportanceLevel.HIGH,
                action_required=True,
                response_required=True,
                safe_to_automate=False,
                confidence=0.91,
                missing_information=[],
                commitments_implied=["Proposed rescheduled meeting on Wednesday"],
                recommended_decision=DecisionOption.WAIT_FOR_APPROVAL,
                reasoning="Updated meeting time proposed on existing thread. Requires user approval prior to confirming.",
            )

        # Case 3: Vendor invoice dispute
        elif "invoice" in subj or "dispute" in body:
            return EmailAnalysis(
                message_id=message.message_id,
                thread_id=message.thread_id,
                in_reply_to=message.in_reply_to,
                sender=message.sender,
                subject=message.subject,
                received_at=message.received_at,
                category=EmailCategory.FINANCIAL,
                importance=ImportanceLevel.HIGH,
                urgency=ImportanceLevel.HIGH,
                action_required=True,
                response_required=True,
                safe_to_automate=False,
                confidence=0.88,
                missing_information=["Corrected billing line items", "Approved discount code"],
                commitments_implied=["Financial credit adjustment request"],
                recommended_decision=DecisionOption.ESCALATE,
                reasoning="Vendor invoice dispute involving financial adjustments and missing corrected billing amount.",
            )

        # Case 4: Fake bank password phishing
        elif "password" in subj or "confirm your password" in body or "bank" in subj:
            return EmailAnalysis(
                message_id=message.message_id,
                thread_id=message.thread_id,
                in_reply_to=message.in_reply_to,
                sender=message.sender,
                subject=message.subject,
                received_at=message.received_at,
                category=EmailCategory.SECURITY,
                importance=ImportanceLevel.HIGH,
                urgency=ImportanceLevel.HIGH,
                action_required=True,
                response_required=False,
                safe_to_automate=False,
                confidence=0.99,
                missing_information=["Verified sender authentication (DKIM/SPF failed)"],
                commitments_implied=[],
                recommended_decision=DecisionOption.ESCALATE,
                reasoning="Suspicious security alert requesting password confirmation. Potential phishing attack.",
            )

        # Default fallback classification
        else:
            return EmailAnalysis(
                message_id=message.message_id,
                thread_id=message.thread_id,
                in_reply_to=message.in_reply_to,
                sender=message.sender,
                subject=message.subject,
                received_at=message.received_at,
                category=EmailCategory.OTHER,
                importance=ImportanceLevel.MEDIUM,
                urgency=ImportanceLevel.LOW,
                action_required=False,
                response_required=False,
                safe_to_automate=False,
                confidence=0.80,
                missing_information=[],
                commitments_implied=[],
                recommended_decision=DecisionOption.NOTIFY_ME,
                reasoning="General message.",
            )

    def evaluate_decision_support(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> DecisionSupportOutput:
        analysis = self.classify_email(message, thread)
        would_invent = len(analysis.missing_information) > 0
        return DecisionSupportOutput(
            missing_information=analysis.missing_information,
            commitments_implied=analysis.commitments_implied,
            would_require_invented_facts=would_invent,
            analysis_summary=f"Evaluated risks for {message.message_id}: {analysis.reasoning}",
        )

    def generate_reply(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> ReplyGenerationOutput:
        subj = message.subject
        if "tuesday" in subj.lower():
            return ReplyGenerationOutput(
                reply_subject=f"Re: {message.subject}",
                reply_body="Hi,\n\nThanks for reaching out! Tuesday at 2 PM works for me.\n\nBest regards,\nUser",
                complete=True,
                placeholders_used=[],
                commitments_made=["Meeting on Tuesday at 2 PM"],
            )
        elif "wednesday" in subj.lower():
            return ReplyGenerationOutput(
                reply_subject=f"Re: {message.subject}",
                reply_body="Hi,\n\nWednesday sounds great as well. Let's do 2 PM on Wednesday.\n\nBest regards,\nUser",
                complete=True,
                placeholders_used=[],
                commitments_made=["Meeting on Wednesday at 2 PM"],
            )
        elif "invoice" in subj.lower():
            return ReplyGenerationOutput(
                reply_subject=f"Re: {message.subject}",
                reply_body="Hi,\n\nI received your notice regarding invoice #1042. Could you please provide [NEEDS INPUT: Corrected invoice amount and documentation]?\n\nThanks,",
                complete=False,
                placeholders_used=["[NEEDS INPUT: Corrected invoice amount and documentation]"],
                commitments_made=[],
            )
        else:
            return ReplyGenerationOutput(
                reply_subject=f"Re: {message.subject}",
                reply_body="Thank you for your email. I have received it.",
                complete=True,
                placeholders_used=[],
                commitments_made=[],
            )
