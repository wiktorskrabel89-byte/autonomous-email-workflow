"""Run a chain of AI providers, moving to the next when one gives out.

The point is the free tier: Gemini's daily quota runs out mid-run, and instead
of the run dying, the work continues on Groq, then OpenAI, then whatever else
has a key. Only failures another provider could plausibly survive cause a
switch - a quota wall, a dead key, a retired model, a network drop. A malformed
request would fail identically everywhere, so it is raised immediately.

Standing a provider down is not the same as retiring it. A per-minute limit
clears in seconds, so a provider that hits one is put aside for a moment and
picked up again; only a spent daily quota, a rejected key or a retired model is
final. Reading every refusal as final is what used to end a run halfway through
an inbox: the first model was retired over one busy minute, the second over the
next, and the chain fell through to a local model that was not even installed.

When every provider is merely resting, the chain waits for the first one to
come back rather than giving up - waiting a minute beats losing the run.
"""

import math
import threading
import time
from typing import Callable, List, Optional

from email_workflow.core.errors import AIProviderError
from email_workflow.models.analysis import (
    DecisionSupportOutput,
    EmailAnalysis,
    ReplyGenerationOutput,
    ReplyReviewOutput,
    KnownFactsMerge,
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


# The longest one request may spend waiting before the chain gives up on it.
# Longer than any per-minute window, short enough that nobody watches a still
# screen wondering whether it died. Measured as wall clock over the whole call,
# so a pool of keys doing its own waiting inside a link counts towards this
# rather than adding a second budget on top.
MAX_TOTAL_WAIT = 150.0

# How many pauses in a row a provider gets before it is taken as finished.
# Reset the moment it answers, so a genuinely busy provider is never retired
# for being busy - only one that has not worked once in between.
MAX_TRANSIENT_STRIKES = 3


class FallbackAIProvider(AIProvider):
    def __init__(
        self,
        links: List[ProviderLink],
        on_switch: Optional[Callable[[ProviderLink, ProviderLink, AIProviderError], None]] = None,
        on_pause: Optional[Callable[[float, str], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not links:
            raise ValueError("FallbackAIProvider needs at least one provider.")
        self.links = links
        self.on_switch = on_switch
        # Called before waiting for a resting provider, so a pause is explained
        # while it happens rather than looking like a freeze.
        self.on_pause = on_pause
        self._sleep = sleep
        # monotonic: a clock change mid-run must not strand a provider.
        self._clock = clock
        self.active_index = 0
        # index -> the time it may be tried again. math.inf means never: the
        # key was rejected, the model is gone, or the day's quota is spent.
        self._down_until: dict = {}
        # Whether a link has been checked for being usable at all, and the
        # answer. A local model that is not running is found out here, once,
        # instead of being announced as the rescue and then failing.
        self._checked: dict = {}
        # index -> why it cannot be used, so the final message can say
        # "Ollama is not running" instead of only naming it as tried.
        self._why_unusable: dict = {}
        # index -> pauses in a row with no answer in between.
        self._strikes: dict = {}
        # Several workers share one chain when a key pool runs them in
        # parallel, and they all write this bookkeeping.
        self._lock = threading.Lock()

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

    # --- who is available -------------------------------------------------

    def _stand_down(self, index: int, error: AIProviderError) -> None:
        """Put a provider aside - for a moment, or for the rest of the run."""
        with self._lock:
            if not error.is_transient:
                self._down_until[index] = math.inf
                return

            # "Wait and try again" is only worth believing so many times. A
            # provider that has never once answered between its pauses is not
            # busy, it is broken - or its refusal was misread as temporary -
            # and waiting for it again on every single email would quietly
            # cost minutes per email for the rest of the run.
            strikes = self._strikes.get(index, 0) + 1
            self._strikes[index] = strikes
            if strikes >= MAX_TRANSIENT_STRIKES:
                self._down_until[index] = math.inf
            else:
                self._down_until[index] = self._clock() + error.cooldown_seconds

    def _ready(self, index: int) -> bool:
        with self._lock:
            return self._down_until.get(index, 0.0) <= self._clock()

    def _usable(self, index: int) -> bool:
        """Whether this provider could work at all - asked once, then remembered.

        A local model that is not running, or a provider whose key has gone
        missing, is found out here. Without this the chain announces "switching
        to the local model and carrying on" and then dies on the next line,
        which is a promise it never had any way of keeping.
        """
        with self._lock:
            known = self._checked.get(index)
        if known is not None:
            return known

        try:
            self.links[index].provider.validate_setup()
            ok, problem = True, ""
        except Exception as e:
            ok, problem = False, str(e)

        with self._lock:
            self._checked[index] = ok
            if not ok:
                self._down_until[index] = math.inf
                self._why_unusable[index] = problem
        return ok

    def _try_order(self) -> List[int]:
        """The provider doing the work first, then the chain from the top."""
        active = self.active_index
        return [active] + [i for i in range(len(self.links)) if i != active]

    def _next_ready(self, exclude: Optional[int] = None) -> Optional[int]:
        for index in self._try_order():
            if index != exclude and self._ready(index) and self._usable(index):
                return index
        return None

    def _soonest_return(self) -> Optional[float]:
        """Seconds until the first resting provider is due back. None = nobody."""
        with self._lock:
            now = self._clock()
            waits = [
                until - now
                for index, until in self._down_until.items()
                if until != math.inf and self._checked.get(index, True)
            ]
        future = [w for w in waits if w > 0]
        return min(future) if future else None

    # --- the call ---------------------------------------------------------

    def _run(self, method_name: str, *args, **kwargs):
        """Call method_name on the active provider, advancing the chain on failure."""
        last_error: Optional[AIProviderError] = None
        # Wall clock, not a running total of this method's own sleeps: a link
        # in the chain can itself be a pool of keys, and that pool does its own
        # waiting before it gives up. Counting only our sleeps let one email
        # stall for the pool's budget AND then ours - twice the ceiling either
        # comment claimed.
        started = self._clock()

        while True:
            index = self._next_ready()
            if index is not None:
                link = self.links[index]
                try:
                    result = getattr(link.provider, method_name)(*args, **kwargs)
                    with self._lock:
                        self.active_index = index
                        # It answered: its pauses were real pauses.
                        self._strikes[index] = 0
                    return result
                except AIProviderError as e:
                    last_error = e
                    if not e.can_failover:
                        raise
                    self._stand_down(index, e)

                    replacement = self._next_ready(exclude=index)
                    if replacement is not None:
                        if self.on_switch:
                            self.on_switch(link, self.links[replacement], e)
                        with self._lock:
                            self.active_index = replacement
                    continue

            # Everybody is either finished or resting. Resting is worth waiting
            # for: a minute of waiting is cheaper than losing the rest of the
            # inbox, which is what happened when this gave up here.
            pause = self._soonest_return()
            if pause is None or (self._clock() - started) + pause > MAX_TOTAL_WAIT:
                break
            pause = max(pause, 1.0)
            if self.on_pause:
                try:
                    self.on_pause(pause, "every provider")
                except Exception:
                    pass
            self._sleep(pause)

        tried = ", ".join(self._describe_state(i) for i in range(len(self.links)))
        if last_error is None:
            raise AIProviderError(
                "No AI provider was available to do the work.",
                hint=f"Tried: {tried}.",
                kind="other",
            )
        raise AIProviderError(
            f"Every AI provider failed. Last error: "
            f"{last_error.message if last_error else 'unknown'}",
            hint=(
                f"Tried: {tried}. "
                + (last_error.hint if last_error and last_error.hint else "")
            ).strip(),
            kind=last_error.kind if last_error else "other",
        )

    def _describe_state(self, index: int) -> str:
        """One provider and why it is not being used, for the final message."""
        problem = self._why_unusable.get(index)
        if problem:
            return f"{self.links[index]} (not available: {problem})"
        return str(self.links[index])

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

    def organise_facts(self, existing: str, addition: str) -> KnownFactsMerge:
        return self._run("organise_facts", existing, addition)

    def suggest_facts(self, emails: str, existing: str = "") -> KnownFactsMerge:
        return self._run("suggest_facts", emails, existing)
