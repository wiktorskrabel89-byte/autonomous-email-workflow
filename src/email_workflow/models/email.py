from enum import Enum
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field

class EmailCategory(str, Enum):
    PERSONAL = "personal"
    WORK = "work"
    SUPPORT = "support"
    FINANCIAL = "financial"
    SHOPPING = "shopping"
    RECEIPT = "receipt"
    NEWSLETTER = "newsletter"
    MARKETING = "marketing"
    NOTIFICATION = "notification"
    SECURITY = "security"
    ACCOUNT = "account"
    TRAVEL = "travel"
    CALENDAR = "calendar"
    AUTOMATED = "automated"
    SPAM = "spam"
    OTHER = "other"

class ImportanceLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class UrgencyLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class DecisionOption(str, Enum):
    IGNORE = "ignore"
    ARCHIVE = "archive"
    NOTIFY_ME = "notify_me"
    CREATE_DRAFT = "create_draft"
    WAIT_FOR_APPROVAL = "wait_for_approval"
    AUTOMATICALLY_REPLY = "automatically_reply"
    ESCALATE = "escalate"

class SenderInfo(BaseModel):
    name: str
    email: str
    known_contact: bool = False

class EmailMessage(BaseModel):
    message_id: str
    thread_id: str
    in_reply_to: Optional[str] = None
    sender: SenderInfo
    subject: str
    body: str
    received_at: str  # ISO string or formatted datetime string
    headers: Optional[dict] = Field(default_factory=dict)
