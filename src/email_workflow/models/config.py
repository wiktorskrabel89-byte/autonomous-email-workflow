from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Union
from pydantic import BaseModel, Field
import yaml
from dotenv import load_dotenv
from email_workflow.core.paths import resolve_project_file, find_env_file

# Load secrets from .env. find_env_file checks the project folder, then the
# current folder, then the home directory, so it works wherever it lives.
load_dotenv(find_env_file())

class AIMode(str, Enum):
    API = "api"
    LOCAL = "local"

class APIProviderName(str, Enum):
    OPENAI = "openai"
    GEMINI = "gemini"
    GROQ = "groq"
    OPENROUTER = "openrouter"
    FAKE = "fake"

class EmailProviderName(str, Enum):
    GMAIL = "gmail"
    OUTLOOK = "outlook"
    IMAP_GENERIC = "imap_generic"
    MOCK = "mock"

class AutomationLevel(str, Enum):
    MANUAL = "manual"
    DRAFT = "draft"
    TRUSTED = "trusted"
    AUTONOMOUS = "autonomous"

class AutonomousRule(BaseModel):
    category: str
    sender_pattern: Optional[str] = None
    max_importance: Optional[str] = None
    min_confidence: Optional[float] = 0.85

class APIConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key_env: str = "OPENAI_API_KEY"
    temperature: float = 0.1
    max_output_tokens: int = 4000
    # Free tiers cap requests per minute (Gemini's is commonly 15). Requests
    # are spaced out to stay underneath it - note that ONE email costs up to
    # three requests, so this counts requests, not emails. 0 = no throttling.
    requests_per_minute: int = 15

class LocalConfig(BaseModel):
    runtime: str = "ollama"
    model: str = "llama3.2"
    endpoint: str = "http://localhost:11434"
    temperature: float = 0.1

class FallbackLink(BaseModel):
    """One provider to fall back to. Model and key variable are optional:
    left out, the provider's defaults are used."""

    provider: str
    model: Optional[str] = None
    api_key_env: Optional[str] = None

class FallbackConfig(BaseModel):
    """What to do when the main provider hits a quota wall or dies.

    With auto_detect on and no explicit chain, every other supported provider
    that has an API key in the environment is used, in `auto_order`. That is
    what makes "I have several keys" work without any configuration.
    """

    enabled: bool = True
    auto_detect: bool = True
    # After every cloud provider has failed, try the local model. Cloud
    # providers all fail together when the connection drops, and free
    # quotas run out on the same day; a local model has neither problem.
    use_local_last: bool = True
    chain: List[FallbackLink] = Field(default_factory=list)
    auto_order: List[str] = Field(
        default_factory=lambda: ["gemini", "groq", "openai", "openrouter"]
    )

class ProviderLimit(BaseModel):
    """Your own daily allowance for one provider, for the usage report.

    These cannot be read from the API: Gemini sends no rate-limit headers and
    has no usage endpoint for an API key. So they are numbers you copy from
    your provider's dashboard. 0 means "I don't know", and the report then
    just shows how much you used without a percentage.
    """

    requests_per_day: int = 0
    tokens_per_day: int = 0

class KeysConfig(BaseModel):
    """Several API keys at once, including keys from different accounts.

    A free tier is per account. Three Gemini keys from three Google accounts
    are three separate allowances, so the app can work three emails at a time
    instead of queueing them all behind one key's per-minute limit.

    With auto_detect on, any variable named after the provider's own key
    variable is picked up - GEMINI_API_KEY_2, GEMINI_API_KEY_3,
    GEMINI_API_KEY_WORK - so a second account needs nothing but a line in .env.
    """

    auto_detect: bool = True
    # Keys whose variable is NOT named after the provider's, per provider:
    #   extra: {gemini: [MY_OTHER_GEMINI_KEY]}
    # Listed per provider because a bare list could not say which pool a name
    # belongs to, and putting a Groq key in the Gemini pool would be silent.
    extra: Dict[str, List[str]] = Field(default_factory=dict)
    # Work on several emails at the same time, one key each. With a single key
    # this changes nothing: the per-minute limit would just make the extra
    # workers queue.
    parallel: bool = True
    # 0 = one worker per key found. Capped, because every worker also opens its
    # own mailbox connection.
    max_workers: int = 0
    # Two ways to use several keys, and they are not alternatives - parallel
    # splits the inbox between keys, review puts two keys on the same email.
    # With review on, a second key checks a reply before it can be sent: one
    # writes, another marks it. Ignored when there is only one key, where it
    # would be the same model marking its own homework.
    review: bool = True

class UsageConfig(BaseModel):
    store: str = "usage.jsonl"
    # Warn once usage passes this share of a declared limit.
    warn_at_percent: int = 80
    limits: Dict[str, ProviderLimit] = Field(default_factory=dict)

class AIConfig(BaseModel):
    mode: AIMode = AIMode.API
    api: APIConfig = Field(default_factory=APIConfig)
    local: LocalConfig = Field(default_factory=LocalConfig)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)
    keys: KeysConfig = Field(default_factory=KeysConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)

class EmailLabel(BaseModel):
    """One label of your own, and what belongs in it.

    `about` is the whole point: it is what the AI is shown, in your words, so
    it can tell a -10% code from Modivo apart from a delivery notice. Without
    it a label name like "Rabaty" means nothing to a model reading Polish
    marketing mail.
    """

    name: str
    about: str = ""
    # Mail filed here stays in your inbox instead of being archived.
    #
    # Deterministic on purpose. Asking the AI a SECOND question - "is this one
    # of the subjects he never wants archived?" - gave a different answer once
    # the prompt also had labels in it: a job advert was filed under
    # Newslettery and archived, when the same mail had been kept the day
    # before. One question ("which label?") and one rule ("this label is
    # kept") cannot disagree with each other.
    keep_in_inbox: bool = False

class EmailConfig(BaseModel):
    provider: str = "mock"
    account_ref: str = "user@example.com"
    mailbox: str = "INBOX"
    sync_mode: str = "poll"
    poll_interval_seconds: int = 60
    # Only look at unread mail this recent. Without a window, the first run on
    # an old mailbox would process years of unread backlog in one go.
    max_age_days: int = 7
    # How many emails one run may process. 0 means no limit: every unread email
    # inside the age window is handled.
    max_emails_per_run: int = 0
    # Out of the box it writes drafts and stars what needs you, and does
    # nothing you cannot undo: it does not send, and it does not move mail out
    # of your inbox. Both of those are switched on deliberately, once you have
    # seen what it decides. A default that sends email or empties an inbox
    # before anyone has been asked is a surprise, not a feature.
    #
    # Actually send automatic replies. Off by default: sending email on
    # someone's behalf is not undoable. While off, a reply that passed every
    # check is saved as a draft instead, and the audit log says exactly that.
    allow_send: bool = False
    # Unimportant mail really leaves the inbox (on Gmail: the Archive
    # button removes the Inbox label; the mail stays in All Mail).
    archive_unimportant: bool = False
    # Save a draft when a reply is not sent. Turn this off to skip drafts
    # entirely: a reply that cannot be sent is then escalated to you
    # instead, and nothing is left sitting in your Drafts folder.
    create_drafts: bool = True
    # Mail that matters gets a star and a label, so it is easy to find.
    star_important: bool = True
    # NOT "Important": that is one of Gmail's own labels, and asking Gmail to
    # put a user label of that name on a message is refused with BAD. Naming it
    # something Gmail does not already own also keeps it obvious which label
    # this app put there. A name that does collide is moved out of the way
    # automatically (see gmail_label), but the shipped default should not need
    # rescuing.
    important_label: str = "AI/Important"
    # Labels of your own, and what belongs in each. An email that matches one
    # gets that Gmail label BEFORE anything else happens to it - so a discount
    # code still leaves your inbox, but it lands under "Rabaty" instead of
    # disappearing into All Mail with everything else. Empty means the app
    # applies no labels of its own beyond important_label.
    #
    # Gmail makes a label the first time one is used, and a "/" in the name
    # makes it a sub-label: "Zakupy/Rabaty" nests under Zakupy.
    labels: List[EmailLabel] = Field(default_factory=list)

class AutomationConfig(BaseModel):
    level: AutomationLevel = AutomationLevel.AUTONOMOUS
    trusted_categories: List[str] = Field(
        default_factory=lambda: ["newsletter", "receipt", "notification"]
    )
    autonomous_rules: List[AutonomousRule] = Field(default_factory=list)
    confidence_threshold_auto_reply: float = 0.90
    confidence_threshold_draft: float = 0.60
    # Let the AI suggest facts about you from your own incoming mail. Off by
    # default, and it only ever SUGGESTS: nothing reaches your knowledge base
    # without you picking it, because a wrong "fact" would then be stated to
    # real people as if it were true.
    learn_facts_from_email: bool = False
    # Subjects you never want filed away, in your own words - "job offers and
    # anything about my applications", "anything about my landlord". Mail that
    # is about one of these is starred and left in the inbox instead of being
    # archived, however routine it looks.
    #
    # This exists because "important to you" is not something an AI can work
    # out from the email alone. A job-board status update is, to a model, an
    # ordinary low-importance notification - and it was archived as one, along
    # with the mail from an employer who had actually read an application.
    never_archive_about: List[str] = Field(default_factory=list)

class NotificationsConfig(BaseModel):
    channel: str = "terminal"
    digest_mode: str = "immediate"
    # The per-message list at the bottom of the run digest. Off by default: it
    # repeats what the run already printed, and the raw message ids render as
    # mailto: links in Discord.
    show_message_breakdown: bool = False

class StateConfig(BaseModel):
    store: str = "state.json"

class SecurityConfig(BaseModel):
    """The app can read a mailbox and send email, so it asks who you are first."""

    require_login: bool = True
    max_login_attempts: int = 3
    store: str = "auth.json"
    # Show the password on screen while it is typed. A hidden prompt gives no
    # feedback at all - not even dots - so a typo is only discovered after the
    # fact. On your own machine that trade is usually worth it. Turn it off
    # when someone can see your screen.
    show_password_while_typing: bool = True

class UpdateConfig(BaseModel):
    """Where new versions of the app come from, and whether to look for them.

    Checking is automatic; applying never is. Code that replaces itself behind
    your back, on a machine that sends email as you, is not a convenience.
    """

    # Empty means the project this app came from. A fork points elsewhere.
    source: str = ""
    check_automatically: bool = True

class AppConfig(BaseModel):
    ai: AIConfig = Field(default_factory=AIConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    update: UpdateConfig = Field(default_factory=UpdateConfig)

    @classmethod
    def load_from_file(cls, path: Union[str, Path]) -> "AppConfig":
        file_path = resolve_project_file(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)
