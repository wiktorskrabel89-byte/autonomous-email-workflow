"""Run a chain of AI providers, moving to the next when one gives out.

The point is the free tier: Gemini's daily quota runs out mid-run, and instead
of the run dying, the work continues on Groq, then OpenAI, then whatever else
has a key. Only failures another provider could plausibly survive cause a
switch - a quota wall, a dead key, a retired model, a network drop. A malformed
request would fail identically everywhere, so it is raised immediately.
"""

from typing import Callable, List, Optional

from email_workflow.core.errors import AIProviderError
from email_workflow.models.analysis import (
    DecisionSupportOutput,
    EmailAnalysis,
    ReplyGenerationOutput,
    ReplyReviewOutput,
)
from email_workflow.models.email import EmailMessage
from email_workflow.models.state import ThreadState
from email_workflow.providers.base_ai import AIProvider


class ProviderLink:
    """One provider in the chain, plus how to describe it to the user."""

    def __init__(self, label: str, model: str, provider: AIProvider):
        self.label = label
        self.model = model
        self.provider = provider

    def __str__(self) -> str:
        return f"{self.label} / {self.model}"


class FallbackAIProvider(AIProvider):
    def __init__(
        self,
        links: List[ProviderLink],
        on_switch: Optional[Callable[[ProviderLink, ProviderLink, AIProviderError], None]] = None,
    ):
        if not links:
            raise ValueError("FallbackAIProvider needs at least one provider.")
        self.links = links
        self.on_switch = on_switch
        self.active_index = 0
        # Providers that already hit a wall this run; not retried until restart.
        self._exhausted: set = set()

    @property
    def active(self) -> ProviderLink:
        return self.links[self.active_index]

    @property
    def label(self) -> str:
        return str(self.active)

    def describe_chain(self) -> str:
        return "  ->  ".join(str(link) for link in self.links)

    def describe(self) -> tuple:
        """Whichever link is currently live - not the configured primary."""
        return self.active.provider.describe()

    def validate_setup(self) -> None:
        """Valid if at least one link in the chain is usable."""
        problems = []
        for link in self.links:
            try:
                link.provider.validate_setup()
                return
            except Exception as e:
                problems.append(f"  - {link}: {e}")
        raise ValueError(
            "No usable AI provider. Every provider in the chain failed:\n"
            + "\n".join(problems)
        )

    def _run(self, method_name: str, *args, **kwargs):
        """Call method_name on the active provider, advancing the chain on failure."""
        last_error: Optional[AIProviderError] = None

        for index in range(self.active_index, len(self.links)):
            link = self.links[index]
            if index in self._exhausted:
                continue

            try:
                result = getattr(link.provider, method_name)(*args, **kwargs)
                self.active_index = index
                return result
            except AIProviderError as e:
                last_error = e
                if not e.can_failover:
                    raise
                self._exhausted.add(index)

                next_index = next(
                    (i for i in range(index + 1, len(self.links)) if i not in self._exhausted),
                    None,
                )
                if next_index is None:
                    break
                if self.on_switch:
                    self.on_switch(link, self.links[next_index], e)
                self.active_index = next_index

        tried = ", ".join(str(link) for link in self.links)
        raise AIProviderError(
            f"Every AI provider failed. Last error: "
            f"{last_error.message if last_error else 'unknown'}",
            hint=(
                f"Tried: {tried}. "
                + (last_error.hint if last_error and last_error.hint else "")
            ).strip(),
            kind=last_error.kind if last_error else "other",
        )

    def classify_email(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> EmailAnalysis:
        return self._run("classify_email", message, thread, known_facts)

    def evaluate_decision_support(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> DecisionSupportOutput:
        return self._run("evaluate_decision_support", message, thread, known_facts)

    def generate_reply(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> ReplyGenerationOutput:
        return self._run("generate_reply", message, thread, known_facts)

    def review_reply(
        self,
        message: EmailMessage,
        reply: ReplyGenerationOutput,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> ReplyReviewOutput:
        return self._run("review_reply", message, reply, thread, known_facts)
