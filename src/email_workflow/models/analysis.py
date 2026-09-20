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
    # Written TO this person about something of theirs - their application,
    # their order, their ticket - rather than sent to a mailing list. It is a
    # different question from the category: "an employer has read your
    # application" is a notification AND personal, and filing that away with
    # the newsletters is how a reply to something you did gets lost.
    # Defaults false so an older model that omits it changes nothing.
    personally_addressed: bool = False
    # Which of the user's own never-archive topics this is about, copied
    # verbatim from the list it was given. Empty when none of them fit.
    protected_topic: str = ""
    # Which of the user's own labels this belongs under, copied verbatim from
    # the list it was given. Empty when none of them fit - a label nobody asked
    # for is worse than no label.
    suggested_label: str = ""

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
    personally_addressed: bool = False
    protected_topic: str = ""
    suggested_label: str = ""

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

class KnownFactsMerge(BaseModel):
    """A knowledge base with one new piece of information folded into it."""

    facts: List[str] = Field(default_factory=list)
    what_changed: str = ""
    # Old lines the new information genuinely supersedes. Anything else that
    # goes missing is a mistake, not an edit, and the caller checks for it.
    replaced: List[str] = Field(default_factory=list)

class FinalValidationResult(BaseModel):
    valid: bool
    failure_reasons: List[str] = Field(default_factory=list)
    fallback_decision: DecisionOption = DecisionOption.CREATE_DRAFT
