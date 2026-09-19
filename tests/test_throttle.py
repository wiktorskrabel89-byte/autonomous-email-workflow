"""Tests for staying under a free tier's requests-per-minute cap.

Time is injected, so these assert real spacing without ever sleeping.

The limiter is a sliding window: an idle allowance may be spent in a burst, and
waiting only starts once it runs out. The property that must hold is not "every
call is 4 seconds apart" but "no rolling 60-second window contains more than 15
calls" - which is what the provider actually enforces.
"""

import pytest

from email_workflow.core.throttle import RateLimiter
from email_workflow.models.config import APIConfig


class FakeClock:
    """A clock that only moves when something sleeps or the test advances it."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0, "sleeping for nothing is a bug"
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock():
    return FakeClock()


def limiter(clock, rpm=15):
    return RateLimiter(rpm, sleep=clock.sleep, clock=clock.time)


def busiest_minute(timestamps) -> int:
    """Most requests found in any 60-second window."""
    return max(
        sum(1 for other in timestamps if start <= other < start + 60.0)
        for start in timestamps
    )


# --- the cap is never exceeded ---------------------------------------------

def test_no_minute_ever_holds_more_than_the_allowance(clock):
    """The scenario that matters: a full inbox processed back to back."""
    rl = limiter(clock, 15)
    stamps = []
    for _ in range(60):          # 20 emails x 3 calls each
        rl.wait()
        stamps.append(clock.now)

    assert busiest_minute(stamps) <= 15


def test_the_cap_holds_for_a_very_long_run(clock):
    rl = limiter(clock, 15)
    stamps = []
    for _ in range(200):
        rl.wait()
        stamps.append(clock.now)

    assert busiest_minute(stamps) <= 15


def test_the_cap_holds_when_work_arrives_in_clumps(clock):
    """Bursts separated by idle time must still respect the rolling window."""
    rl = limiter(clock, 15)
    stamps = []
    for _ in range(4):
        for _ in range(10):
            rl.wait()
            stamps.append(clock.now)
        clock.advance(20.0)      # a pause between batches

    assert busiest_minute(stamps) <= 15


# --- bursts are allowed -----------------------------------------------------

def test_one_email_runs_without_waiting(clock):
    """Three calls for a single email should not cost 8 seconds of staring."""
    rl = limiter(clock, 15)
    started = clock.now
    for _ in range(3):
        assert rl.wait() == 0.0
    assert clock.now == started
    assert clock.slept == []


def test_a_full_allowance_can_be_spent_at_once(clock):
    rl = limiter(clock, 15)
    waited = [rl.wait() for _ in range(15)]
    assert waited == [0.0] * 15
    assert clock.slept == []


def test_waiting_starts_only_once_the_allowance_is_gone(clock):
    rl = limiter(clock, 15)
    assert [rl.wait() for _ in range(15)] == [0.0] * 15
    assert rl.wait() > 0


def test_idle_time_earns_the_allowance_back(clock):
    rl = limiter(clock, 15)
    for _ in range(15):
        rl.wait()

    clock.advance(60.0)          # the whole window ages out
    waited = [rl.wait() for _ in range(15)]

    assert waited == [0.0] * 15
    assert rl.wait() > 0, "the sixteenth should have to wait again"


def test_the_allowance_never_grows_past_the_cap(clock):
    rl = limiter(clock, 15)
    clock.advance(3600.0)        # idle for an hour
    assert rl.available() == 15
    waited = [rl.wait() for _ in range(15)]
    assert waited == [0.0] * 15
    assert rl.wait() > 0, "an hour of idling must not buy a 16th free request"


def test_slow_calls_are_never_slowed_further(clock):
    """If the API is slower than the limit, throttling must add nothing."""
    rl = limiter(clock, 15)
    for _ in range(30):
        clock.advance(6.0)
        assert rl.wait() == 0.0
    assert clock.slept == []


# --- arithmetic -------------------------------------------------------------

def test_the_allowance_is_the_configured_rate(clock):
    assert limiter(clock, 15).available() == 15


def test_a_full_window_waits_until_the_oldest_ages_out(clock):
    rl = limiter(clock, 15)
    for _ in range(15):
        rl.wait()
    assert rl.wait() == pytest.approx(60.0)


def test_a_higher_allowance_lets_more_through_first(clock):
    rl = limiter(clock, 60)
    assert rl.available() == 60
    assert [rl.wait() for _ in range(60)] == [0.0] * 60


# --- switching it off ------------------------------------------------------

@pytest.mark.parametrize("rpm", [0, -1, None])
def test_zero_or_missing_disables_throttling(clock, rpm):
    rl = RateLimiter(rpm, sleep=clock.sleep, clock=clock.time)
    assert not rl.enabled
    for _ in range(50):
        assert rl.wait() == 0.0
    assert clock.slept == []


# --- the default matches the free tier -------------------------------------

def test_default_is_fifteen_requests_per_minute():
    assert APIConfig().requests_per_minute == 15, (
        "Gemini's free tier is commonly 15 requests per minute"
    )
