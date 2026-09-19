from enum import Enum
from typing import Optional, List, Dict
from pydantic import BaseModel, Field

class MessageStatus(str, Enum):
    PENDING = "pending"
    ANALYZED = "analyzed"
    REPLIED = "replied"
    SUPERSEDED = "superseded"
    STALE = "stale"

class ActionState(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class ProcessingStage(str, Enum):
    RECEIVED = "received"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    DECIDED = "decided"
    DRAFTING = "drafting"
    DRAFTED = "drafted"
    AWAITING_APPROVAL = "awaiting_approval"
    SENDING = "sending"
    SENT = "sent"
    ARCHIVED = "archived"
    ESCALATED = "escalated"
    SUPERSEDED = "superseded"
    FAILED = "failed"

class ActiveAction(BaseModel):
    action_type: str  # e.g., draft, auto_reply, approval_wait
    target_message_id: str
    created_at: str
    state: ActionState = ActionState.IN_PROGRESS

class ThreadMessage(BaseModel):
    message_id: str
    in_reply_to: Optional[str] = None
    received_at: str
    status: MessageStatus = MessageStatus.PENDING

class ThreadState(BaseModel):
    thread_id: str
    canonical_subject: str
    participants: List[str] = Field(default_factory=list)
    messages: List[ThreadMessage] = Field(default_factory=list)
    active_action: Optional[ActiveAction] = None
    last_agent_reply_id: Optional[str] = None

class MessageRecord(BaseModel):
    message_id: str
    thread_id: str
    current_stage: ProcessingStage
    updated_at: str
    history: List[Dict[str, str]] = Field(default_factory=list)
