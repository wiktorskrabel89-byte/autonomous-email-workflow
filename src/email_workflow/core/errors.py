"""User-facing error types.

The CLI catches WorkflowError and prints the message as a clean panel, so an
expected failure (retired model, bad key, quota exhausted, no network) never
reaches the user as a Python traceback.
"""

# Failure kinds that another provider could plausibly succeed at, so they are
# worth failing over for. A bad request or malformed output is our fault and
# would fail identically everywhere, so it is not in this set.
FAILOVER_KINDS = frozenset(
    {"quota", "rate_limit", "unavailable", "auth", "model_gone", "network",
     "no_access",
     # The same request fits a model with a bigger context window, so this is
     # worth handing on. A genuinely malformed request is not.
     "too_long"}
)

# Failures that pass on their own. A per-minute rate limit clears in seconds
# and a dropped connection usually comes back, so a provider that hits one is
# stood down for a moment - not retired for the rest of the run.
#
# This distinction is the whole reason a run used to die halfway: every 429 was
# read as "this model is finished", so one busy minute retired the model, then
# the next one, and the chain fell through to a local Ollama that was not
# running. A quota that is really spent (a daily allowance) is still permanent.
TRANSIENT_KINDS = frozenset({"rate_limit", "unavailable", "network"})

# How long a transient failure stands a provider down for, when the server does
# not say. Long enough for a per-minute window to roll over.
DEFAULT_COOLDOWN_SECONDS = 60.0


class WorkflowError(Exception):
    """An error we can explain to the user, with a suggested next step."""

    def __init__(
        self,
        message: str,
        hint: str = "",
        kind: str = "other",
        retry_after: float = 0.0,
    ):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.kind = kind
        # Seconds the server asked us to wait, when it said. 0 = it did not.
        self.retry_after = max(0.0, float(retry_after or 0.0))

    @property
    def can_failover(self) -> bool:
        return self.kind in FAILOVER_KINDS

    @property
    def is_transient(self) -> bool:
        """True when waiting is likely to fix it, so nothing should be retired."""
        return self.kind in TRANSIENT_KINDS

    @property
    def cooldown_seconds(self) -> float:
        """How long to leave this provider alone before trying it again."""
        if not self.is_transient:
            return 0.0
        return self.retry_after or DEFAULT_COOLDOWN_SECONDS


class AIProviderError(WorkflowError):
    """The AI provider rejected the request or was unreachable."""


class EmailProviderError(WorkflowError):
    """The mailbox could not be reached."""
