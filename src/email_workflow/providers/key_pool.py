"""Use several API keys at once, including keys from different accounts.

Why this exists: a free tier is per account, not per person. Three Gemini keys
from three Google accounts are three separate allowances - three times the
requests per minute and three times the daily quota. The fallback chain already
moved between *providers* when one gave out; this moves between *keys*, which
is the case where the model and the prompts stay exactly the same and only the
billing account differs.

Two things come out of that:

* More headroom. Each key carries its own rate limiter, so the pool as a whole
  allows the sum of the per-key limits rather than the smallest one.
* Real parallelism. Several emails can be in flight at once, each on a
  different key, which is what makes a full inbox finish in a fraction of the
  time. One key alone gains nothing from that - the limiter would just make the
  extra workers queue - so the worker count follows the number of keys.

A lane is only ever handed to one caller at a time for its rate-limit wait: the
allowance belongs to the key, so two threads sharing a key must still take
turns. Threads on *different* keys never wait for each other.
"""

import os
import threading
from typing import Callable, List, Optional, Tuple

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
from email_workflow.providers.fallback_ai import ProviderLink


def _sort_key(name: str) -> Tuple[int, str]:
    """Order GEMINI_API_KEY, _2, _3, _10 the way a person would read them."""
    tail = name.rsplit("_", 1)[-1]
    return (int(tail), "") if tail.isdigit() else (10**6, name)


def discover_key_envs(
    base_env: str,
    auto_detect: bool = True,
    extra: Optional[List[str]] = None,
) -> List[str]:
    """Environment variable names that hold a usable key for one provider.

    The base variable first, then anything named after it - GEMINI_API_KEY_2,
    GEMINI_API_KEY_3, GEMINI_API_KEY_WORK. Naming them after the base variable
    is what makes a second account work with no configuration at all.

    The same key pasted under two names counts once. Believing it was two keys
    would double the assumed allowance of a single account and walk straight
    into the 429 this is meant to avoid.
    """
    wanted: List[str] = [base_env]

    if auto_detect:
        prefix = base_env + "_"
        wanted += sorted(
            (name for name in os.environ if name.startswith(prefix)),
            key=_sort_key,
        )

    for name in extra or []:
        if name and name not in wanted:
            wanted.append(name)

    found: List[str] = []
    seen_values = set()
    for name in wanted:
        value = (os.getenv(name) or "").strip()
        if not value or value in seen_values:
            continue
        seen_values.add(value)
        found.append(name)
    return found


class KeyLane:
    """One API key: a provider bound to it, and that key's own allowance."""

    def __init__(self, link: ProviderLink, key_env: str):
        self.link = link
        self.key_env = key_env
        self.exhausted = False
        # Held for the whole of this lane's rate-limit wait. Two callers on one
        # key have to take turns; callers on other keys are unaffected.
        self.gate = threading.Lock()

    @property
    def limiter(self):
        return getattr(self.link.provider, "limiter", None)

    def headroom(self) -> int:
        """Requests this key could make right now without waiting."""
        limiter = self.limiter
        if limiter is None or not limiter.enabled:
            return 10**6  # no limit configured: never the reason to pick another
        return limiter.available()

    def __str__(self) -> str:
        return str(self.link)


class KeyPoolProvider(AIProvider):
    """Spread requests over several keys, and step over one that gives out."""

    def __init__(
        self,
        lanes: List[KeyLane],
        on_switch: Optional[Callable] = None,
    ):
        if not lanes:
            raise ValueError("KeyPoolProvider needs at least one key.")
        self.lanes = lanes
        self.on_switch = on_switch
        self._cursor = 0
        self._lock = threading.Lock()
        self._last_used = lanes[0]

    # --- description ------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self.lanes)

    @property
    def live_lanes(self) -> List[KeyLane]:
        return [lane for lane in self.lanes if not lane.exhausted]

    def describe(self) -> tuple:
        return self._last_used.link.provider.describe()

    def describe_pool(self) -> str:
        return "  +  ".join(str(lane) for lane in self.lanes)

    def validate_setup(self) -> None:
        problems = []
        for lane in self.lanes:
            try:
                lane.link.provider.validate_setup()
                return
            except Exception as e:
                problems.append(f"  - {lane}: {e}")
        raise ValueError(
            "No usable API key. Every key in the pool failed:\n" + "\n".join(problems)
        )

    # --- choosing a key ---------------------------------------------------

    def _claim(self, avoid: Optional[KeyLane] = None) -> Optional[KeyLane]:
        """The key with the most left this minute; round-robin breaks ties.

        Picking by headroom is what keeps a run moving: a key that has just
        been used heavily is passed over while another still has allowance,
        so nobody sits in a rate-limit wait while a fresh key is idle.
        """
        with self._lock:
            live = self.live_lanes
            if not live:
                return None
            # For a second opinion, anything but the key that just answered.
            # Only if that leaves something: one key left is better than none.
            if avoid is not None and len(live) > 1:
                live = [lane for lane in live if lane is not avoid] or live
            best = max(
                range(len(live)),
                key=lambda i: (live[i].headroom(), -((i - self._cursor) % len(live))),
            )
            self._cursor = (best + 1) % len(live)
            return live[best]

    def _retire(self, lane: KeyLane, error: AIProviderError) -> Optional[KeyLane]:
        """Mark a key as spent and name its replacement, for the message."""
        with self._lock:
            lane.exhausted = True
            replacement = next((other for other in self.lanes if not other.exhausted), None)
        if replacement is not None and self.on_switch:
            try:
                self.on_switch(lane.link, replacement.link, error)
            except Exception:
                pass
        return replacement

    def _run(self, method_name: str, *args, avoid_last: bool = False, **kwargs):
        last_error: Optional[AIProviderError] = None

        while True:
            lane = self._claim(avoid=self._last_used if avoid_last else None)
            if lane is None:
                break

            try:
                # The gate covers this key's rate-limit wait and its request.
                # Other keys are free to work the whole time.
                with lane.gate:
                    result = getattr(lane.link.provider, method_name)(*args, **kwargs)
                self._last_used = lane
                return result
            except AIProviderError as e:
                last_error = e
                if not e.can_failover:
                    raise
                if self._retire(lane, e) is None:
                    break

        tried = self.describe_pool()
        raise AIProviderError(
            f"Every API key failed. Last error: "
            f"{last_error.message if last_error else 'unknown'}",
            hint=(
                f"Tried: {tried}. "
                + (last_error.hint if last_error and last_error.hint else "")
            ).strip(),
            kind=last_error.kind if last_error else "other",
        )

    # --- the work ---------------------------------------------------------

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
        """A different key checks the reply.
        A model marking its own homework is worth very little, and the whole
        point of holding keys on separate accounts is that there is another
        one to ask - so this deliberately avoids the key that just answered.
        """
        return self._run(
            "review_reply", message, reply, thread, known_facts, avoid_last=True
        )



def all_limiters(provider) -> List:
    """Every rate limiter behind a provider, whether it is a pool or one key."""
    lanes = getattr(provider, "lanes", None)
    if lanes:
        return [lane.limiter for lane in lanes if lane.limiter is not None]
    limiter = getattr(provider, "limiter", None)
    return [limiter] if limiter is not None else []


def parallel_lanes(provider) -> int:
    """How many requests this provider can genuinely have in flight at once.

    Only a key pool gives real parallelism. A fallback chain is sequential by
    design - it holds one provider live and moves on only when that one gives
    out - so its width is the width of whatever is currently in front.
    """
    if isinstance(provider, KeyPoolProvider):
        return provider.size
    links = getattr(provider, "links", None)
    if links:
        return parallel_lanes(links[0].provider)
    return 1


def find_pool(provider) -> Optional["KeyPoolProvider"]:
    """The key pool in front, if there is one - for showing it to the user."""
    if isinstance(provider, KeyPoolProvider):
        return provider
    for link in getattr(provider, "links", None) or []:
        found = find_pool(link.provider)
        if found is not None:
            return found
    return None
