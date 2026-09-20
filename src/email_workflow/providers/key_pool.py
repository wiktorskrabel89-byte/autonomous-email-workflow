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

import math
import os
import threading
import time
from typing import Callable, List, Optional, Tuple

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
from email_workflow.providers.fallback_ai import ProviderLink


# The longest the pool will wait for a resting key before giving up on the
# request. A per-minute window is 60 seconds wide, so this covers one full
# window plus the time it takes to notice. Deliberately under the chain's own
# budget: when this pool sits inside a fallback chain, the chain measures wall
# clock, so time spent waiting here is time the chain no longer has.
MAX_KEY_WAIT = 120.0

# Pauses in a row before a key is taken as finished rather than busy. Reset
# whenever it answers, so a genuinely busy key is never retired for being busy.
MAX_TRANSIENT_STRIKES = 3


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

    def __init__(self, link: ProviderLink, key_env: str, clock=time.monotonic):
        self.link = link
        self.key_env = key_env
        # When this key may be used again. 0 = now, inf = not again this run.
        # A key that hit this minute's limit is resting, not spent: retiring it
        # for the whole run over a pause that clears in seconds threw away an
        # account's whole allowance, and with one key it ended the run.
        self.resume_at = 0.0
        # Pauses in a row with no answer in between.
        self.strikes = 0
        self._clock = clock
        # Held for the whole of this lane's rate-limit wait. Two callers on one
        # key have to take turns; callers on other keys are unaffected.
        self.gate = threading.Lock()

    @property
    def exhausted(self) -> bool:
        """Kept as a name because it reads well; it means "not usable now"."""
        return self.resume_at > self._clock()

    def stand_down(self, error) -> None:
        if not getattr(error, "is_transient", False):
            self.resume_at = math.inf
            return
        # A key that has not answered once between its pauses is not busy.
        # Waiting for it again on every email would cost the rest of the run.
        self.strikes += 1
        self.resume_at = (
            math.inf
            if self.strikes >= MAX_TRANSIENT_STRIKES
            else self._clock() + error.cooldown_seconds
        )

    def answered(self) -> None:
        """It worked, so its pauses were real pauses."""
        self.strikes = 0

    def waiting_for(self) -> Optional[float]:
        """Seconds until this key is usable again, if it is coming back."""
        if self.resume_at == math.inf:
            return None
        left = self.resume_at - self._clock()
        return left if left > 0 else None

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
        on_pause: Optional[Callable[[float, str], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not lanes:
            raise ValueError("KeyPoolProvider needs at least one key.")
        self.lanes = lanes
        self.on_switch = on_switch
        # Said out loud before waiting for a resting key, so the pause is
        # explained while it happens.
        self.on_pause = on_pause
        self._sleep = sleep
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
        with self._lock:
            lane = self._last_used
        return lane.link.provider.describe()

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
        """Stand a key down and name its replacement, for the message.

        Down for a moment if the server only asked for a pause, down for the
        run if the key is rejected or its day's allowance is spent.
        """
        with self._lock:
            lane.stand_down(error)
            replacement = next((other for other in self.lanes if not other.exhausted), None)
        if replacement is not None and self.on_switch:
            try:
                self.on_switch(lane.link, replacement.link, error)
            except Exception:
                pass
        return replacement

    def _soonest_return(self) -> Optional[float]:
        """Seconds until the first resting key is usable again."""
        with self._lock:
            waits = [w for w in (lane.waiting_for() for lane in self.lanes) if w]
        return min(waits) if waits else None

    def _run(self, method_name: str, *args, avoid_last: bool = False, **kwargs):
        last_error: Optional[AIProviderError] = None
        waited = 0.0

        while True:
            with self._lock:
                avoid = self._last_used if avoid_last else None
            lane = self._claim(avoid=avoid)
            if lane is None:
                # Every key is resting rather than spent: wait for the first
                # one back. With a single key this is the whole difference
                # between a run that pauses and a run that stops.
                pause = self._soonest_return()
                if pause is None or waited + pause > MAX_KEY_WAIT:
                    break
                pause = max(pause, 1.0)
                if self.on_pause:
                    try:
                        self.on_pause(pause, self.describe_pool())
                    except Exception:
                        pass
                self._sleep(pause)
                waited += pause
                continue

            try:
                # The gate covers this key's rate-limit wait and its request.
                # Other keys are free to work the whole time.
                with lane.gate:
                    result = getattr(lane.link.provider, method_name)(*args, **kwargs)
                with self._lock:
                    self._last_used = lane
                    lane.answered()
                return result
            except AIProviderError as e:
                last_error = e
                if not e.can_failover:
                    raise
                self._retire(lane, e)

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

    # These two sat below find_pool, indented, so they belonged to that
    # function and not to this class - which quietly left the pool with the
    # base class's dumb "just append it" version of both. With two keys or
    # more, adding a fact stopped being folded in by the AI at all.
    def organise_facts(self, existing: str, addition: str) -> KnownFactsMerge:
        return self._run("organise_facts", existing, addition)

    def suggest_facts(self, emails: str, existing: str = "") -> KnownFactsMerge:
        return self._run("suggest_facts", emails, existing)


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


def leaf_providers(provider) -> List:
    """Every provider that really talks to an API behind a pool or a chain."""
    lanes = getattr(provider, "lanes", None)
    if lanes:
        return [lane.link.provider for lane in lanes]
    links = getattr(provider, "links", None)
    if links:
        found = []
        for link in links:
            found.extend(leaf_providers(link.provider))
        return found
    return [provider]
