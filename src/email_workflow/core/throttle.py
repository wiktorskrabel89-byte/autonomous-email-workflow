"""Keep under a provider's requests-per-minute limit.

Free tiers cap requests per minute (Gemini's is commonly 15). One email is not
one request: classifying it, checking it for risk and writing a reply are up to
three separate calls. So a sleep between emails would still overrun the limit on
a busy inbox - the wait has to sit in front of every request instead.

Why a sliding window and not the two obvious alternatives:

* Fixed pacing (always 4s apart) never overruns, but makes a single email cost
  8 seconds of pure waiting on an otherwise idle key. Painful to sit and watch.
* A token bucket starting full allows a burst, but does not actually hold the
  line: 15 tokens spent instantly, then one refilling every 4 seconds, puts 29
  requests inside the first 60 seconds. The provider counts a rolling minute,
  so that earns a 429.

Keeping the times of recent requests gives both properties. An idle allowance
can be spent at once, and a request only waits when it would genuinely be the
16th within the last minute - at which point it waits exactly until the oldest
one ages out. No rolling 60-second window ever holds more than the limit.

Requests are spaced before they are sent rather than after being refused: a 429
already costs a round trip, and on some tiers repeated ones lengthen the cooldown.
"""

import threading
import time
from collections import deque
from typing import Callable, Optional

WINDOW_SECONDS = 60.0


class RateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        window: float = WINDOW_SECONDS,
        on_wait: Optional[Callable[[float, int], None]] = None,
    ):
        # 0 or less means no throttling at all.
        self.requests_per_minute = max(0, int(requests_per_minute or 0))
        self.window = window
        self._sleep = sleep
        # monotonic, so a system clock change mid-run cannot make this hang.
        self._clock = clock
        self._recent: deque = deque()
        # Several workers can share one key, and the allowance belongs to
        # the key. Their waits have to be taken in turn or the window
        # bookkeeping races and the limit is overrun. Workers on other
        # keys hold a different limiter and never touch this one.
        self._turn = threading.Lock()
        # A second, short-lived lock for the deque itself. available() is
        # asked by the key pool while choosing a key, from a thread that is
        # NOT holding the turn lock, and it drops old entries as it counts.
        # Two threads inside that loop could pop the same entry twice and hit
        # an empty deque - or, worse, read stale headroom and let the pool
        # overrun the key's limit, which is the 429 this class exists to stop.
        # Never held across a sleep, so asking never blocks behind a waiter.
        self._book = threading.Lock()
        # Called just before a wait starts. Without it the app goes silent for
        # up to a minute and looks like it has frozen.
        self.on_wait = on_wait

    @property
    def enabled(self) -> bool:
        return self.requests_per_minute > 0

    def _forget_old(self, now: float) -> None:
        """Drop requests that have aged out. Call while holding self._book."""
        cutoff = now - self.window
        while self._recent and self._recent[0] <= cutoff:
            self._recent.popleft()

    def available(self) -> int:
        """How many requests could be made right now without waiting."""
        if not self.enabled:
            return 0
        with self._book:
            self._forget_old(self._clock())
            return max(0, self.requests_per_minute - len(self._recent))

    def wait(self) -> float:
        """Block until one request is allowed. Returns seconds waited."""
        if not self.enabled:
            return 0.0
        with self._turn:
            return self._wait_my_turn()

    def _wait_my_turn(self) -> float:
        now = self._clock()
        with self._book:
            self._forget_old(now)
            if len(self._recent) < self.requests_per_minute:
                self._recent.append(now)
                return 0.0
            # The window is full. Wait exactly until the oldest request ages
            # out; any less and this would be the 16th inside the same minute.
            delay = self._recent[0] + self.window - now

        if delay > 0:
            if self.on_wait:
                # Say so before sleeping, not after: the point is to explain
                # the pause while it is happening.
                try:
                    self.on_wait(delay, self.requests_per_minute)
                except Exception:
                    pass
            # Deliberately outside self._book: holding it here would make
            # every headroom question in the pool block behind this sleep.
            self._sleep(delay)
            now = self._clock()
        else:
            delay = 0.0

        with self._book:
            self._forget_old(now)
            self._recent.append(now)
        return delay
