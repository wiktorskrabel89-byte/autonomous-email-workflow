"""User-facing error types.

The CLI catches WorkflowError and prints the message as a clean panel, so an
expected failure (retired model, bad key, quota exhausted, no network) never
reaches the user as a Python traceback.
"""

# Failure kinds that another provider could plausibly succeed at, so they are
# worth failing over for. A bad request or malformed output is our fault and
# would fail identically everywhere, so it is not in this set.
FAILOVER_KINDS = frozenset({"quota", "auth", "model_gone", "network", "no_access"})


class WorkflowError(Exception):
    """An error we can explain to the user, with a suggested next step."""

    def __init__(self, message: str, hint: str = "", kind: str = "other"):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.kind = kind

    @property
    def can_failover(self) -> bool:
        return self.kind in FAILOVER_KINDS


class AIProviderError(WorkflowError):
    """The AI provider rejected the request or was unreachable."""


class EmailProviderError(WorkflowError):
    """The mailbox could not be reached."""
