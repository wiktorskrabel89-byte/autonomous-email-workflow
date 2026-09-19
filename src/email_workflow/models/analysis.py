from typing import Optional, List
from pydantic import BaseModel, Field
from email_workflow.models.email import (
    SenderInfo,
    EmailCategory,
    ImportanceLevel,
    UrgencyLevel,
    DecisionOption,
)

class ClassificationVerdict(BaseModel):
    """Only the fields the model actually has to judge.

    Message id, thread id, sender and subject are known locally and are merged
    back in by the provider, so the model cannot corrupt them and we do not pay
    output tokens to have them repeated.
    """

    category: EmailCategory
    importance: ImportanceLevel
    urgency: UrgencyLevel
    action_required: bool
    response_required: bool
    safe_to_automate: bool
    confidence: float = Field(ge=0.0, le=1.0)
    missing_information: List[str] = Field(default_factory=list)
    commitments_implied: List[str] = Field(default_factory=list)
    recommended_decision: DecisionOption
    reasoning: str

class EmailAnalysis(BaseModel):
    message_id: str
    thread_id: str
    in_reply_to: Optional[str] = None
    sender: SenderInfo
    subject: str
    received_at: str
    category: EmailCategory
    importance: ImportanceLevel
    urgency: UrgencyLevel
    action_required: bool
    response_required: bool
    safe_to_automate: bool
    confidence: float = Field(ge=0.0, le=1.0)
    missing_information: List[str] = Field(default_factory=list)
    commitments_implied: List[str] = Field(default_factory=list)
    recommended_decision: DecisionOption
    reasoning: str

class DecisionSupportOutput(BaseModel):
    missing_information: List[str] = Field(default_factory=list)
    commitments_implied: List[str] = Field(default_factory=list)
    would_require_invented_facts: bool = False
    analysis_summary: str

class ReplyGenerationOutput(BaseModel):
    reply_subject: str
    reply_body: str
    complete: bool = True
    placeholders_used: List[str] = Field(default_factory=list)
    commitments_made: List[str] = Field(default_factory=list)

class ReplyReviewOutput(BaseModel):
    """A second key's opinion on a reply another key wrote.

    Defaults are the permissive ones on purpose: a review that comes back
    malformed, or from a provider that cannot review at all, must not silently
    block a reply that was fine.
    """

    approved: bool = True
    concerns: List[str] = Field(default_factory=list)
    invented_details: List[str] = Field(default_factory=list)
    confidence: float = 1.0

class FinalValidationResult(BaseModel):
    valid: bool
    failure_reasons: List[str] = Field(default_factory=list)
    fallback_decision: DecisionOption = DecisionOption.CREATE_DRAFT
