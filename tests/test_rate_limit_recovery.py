"""A busy minute must not end the run.

What this is about, in plain words: the app was reading every "429 - too many
requests" as "this model is finished for today". One busy minute retired the
main model, the next one retired the fallback, and the chain fell through to a
local Ollama that was not even running - so a run stopped halfway through the
inbox with "Every AI provider failed".

A per-minute limit clears by itself in seconds. These tests pin that it is now
waited out instead: on the same model first, then as a short stand-down that
the provider comes back from. A quota that is genuinely spent for the day is
still permanent - moving on from that one is the whole point of the chain.
"""

import pytest

from email_workflow.core.errors import AIProviderError
from email_workflow.models.analysis import EmailAnalysis
from email_workflow.providers.fallback_ai import FallbackAIProvider, ProviderLink
from email_workflow.providers.key_pool import KeyLane, KeyPoolProvider

from tests.test_resilience import ANALYSIS_KWARGS, StubProvider, chain_of, make_email


class FakeClock:
    """A clock the test moves on its own, so nothing really sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Flaky(StubProvider):
    """Fails with a given error for the first N calls, then works."""

    def __init__(self, name, error, failures):
        super().__init__(name, error)
        self.failures = failures

    def classify_email(self, message, thread=None, known_facts=""):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return EmailAnalysis(**ANALYSIS_KWARGS)


def rate_limited(seconds: float = 20.0) -> AIProviderError:
    return AIProviderError(
        "busy", kind="rate_limit", retry_after=seconds
    )


# --- the error itself -------------------------------------------------------

def test_a_per_minute_limit_is_transient_and_a_daily_quota_is_not():
    assert rate_limited().is_transient
    assert not AIProviderError("spent", kind="quota").is_transient


def test_the_cooldown_follows_what_the_server_asked_for():
    assert rate_limited(17.0).cooldown_seconds == 17.0
    # No number from the server: long enough for a per-minute window to roll.
    assert rate_limited(0).cooldown_seconds == 60.0


# --- the chain stands a provider down, it does not retire it ----------------

def test_a_busy_provider_is_used_again_once_its_pause_is_over():
    clock = FakeClock()
    busy = Flaky("gemini", rate_limited(20.0), failures=1)

    chain = FallbackAIProvider(
        chain_of(busy), sleep=clock.sleep, clock=clock
    )
    chain.classify_email(make_email("m1"))

    assert busy.calls == 2, "the same provider should be tried again"
    assert clock.slept, "it should have waited for the pause to pass"


def test_a_daily_quota_still_retires_the_provider_for_the_run():
    """The distinction this whole change rests on: spent is not the same as busy."""
    clock = FakeClock()
    spent = StubProvider("gemini", AIProviderError("no quota", kind="quota"))
    other = StubProvider("groq")

    chain = FallbackAIProvider(
        chain_of(spent, other), sleep=clock.sleep, clock=clock
    )
    chain.classify_email(make_email("m1"))
    chain.classify_email(make_email("m2"))

    assert spent.calls == 1, "a spent daily quota must not be asked again"
    assert other.calls == 2
    assert not clock.slept, "nothing should wait for a quota that is gone"


def test_the_run_does_not_die_when_everyone_is_merely_busy():
    """The exact failure he hit: this used to raise 'Every AI provider failed'."""
    clock = FakeClock()
    first = Flaky("gemini-lite", rate_limited(15.0), failures=1)
    second = Flaky("gemini-preview", rate_limited(15.0), failures=1)

    chain = FallbackAIProvider(
        chain_of(first, second), sleep=clock.sleep, clock=clock
    )
    result = chain.classify_email(make_email("m1"))

    assert result.category.value == "work"
    assert clock.slept, "it waited rather than giving up"


def test_a_provider_that_never_answers_stops_being_waited_for():
    """Three pauses with no answer in between means it is not busy, it is
    broken - or its refusal was misread as temporary. Either way, waiting for
    it again on every email would cost minutes per email for the rest of the
    run, which is the cost of getting the "wait and see" default wrong.
    """
    clock = FakeClock()
    never_works = StubProvider("gemini", rate_limited(20.0))

    chain = FallbackAIProvider(
        chain_of(never_works), sleep=clock.sleep, clock=clock
    )
    for n in range(5):
        with pytest.raises(AIProviderError):
            chain.classify_email(make_email(f"m{n}"))

    assert never_works.calls == 3, (
        "after three pauses in a row with nothing to show for them, stop asking"
    )
    # And - the point of the rule - emails 2 to 5 cost nothing at all. Without
    # it each of them would have waited the full cooldown again, which over a
    # 158-email inbox is most of an afternoon spent waiting for a provider
    # that was never coming back.
    assert len(clock.slept) == 2, "only the first email should ever wait"


def test_but_a_provider_that_answers_in_between_is_never_retired():
    """The other half of the same rule: busy is not broken. This is the bug
    the whole change is about, and the strike count must not bring it back."""
    clock = FakeClock()

    class BusyEveryOtherTime(StubProvider):
        def classify_email(self, message, thread=None, known_facts=""):
            self.calls += 1
            if self.calls % 2:
                raise rate_limited(10.0)
            return EmailAnalysis(**ANALYSIS_KWARGS)

    flaky = BusyEveryOtherTime("gemini")
    chain = FallbackAIProvider(chain_of(flaky), sleep=clock.sleep, clock=clock)
    for n in range(6):
        chain.classify_email(make_email(f"m{n}"))

    assert flaky.calls == 12, "it should still be in use after all six emails"


def test_it_gives_up_eventually_rather_than_waiting_for_ever():
    clock = FakeClock()
    always_busy = StubProvider("gemini", rate_limited(60.0))

    chain = FallbackAIProvider(
        chain_of(always_busy), sleep=clock.sleep, clock=clock
    )
    with pytest.raises(AIProviderError):
        chain.classify_email(make_email("m1"))

    assert sum(clock.slept) <= 150.0, "a pause must not become a hang"


def test_an_overloaded_model_hands_over_to_the_next_one():
    """His 503: "this model is currently experiencing high demand". Another
    model on the same key answers at once, so switching beats waiting."""
    clock = FakeClock()
    busy = StubProvider(
        "gemini-3.1-flash-lite",
        AIProviderError("busy", kind="unavailable"),
    )
    other = StubProvider("gemini-3-flash-preview")
    switches = []

    chain = FallbackAIProvider(
        chain_of(busy, other),
        on_switch=lambda a, b, e: switches.append((a.label, b.label, e.kind)),
        sleep=clock.sleep,
        clock=clock,
    )
    result = chain.classify_email(make_email("m1"))

    assert result.category.value == "work"
    assert switches == [
        ("gemini-3.1-flash-lite", "gemini-3-flash-preview", "unavailable")
    ]
    assert not clock.slept, "there is nothing to wait for - just use the other one"


def test_but_the_busy_model_is_not_written_off_for_the_run():
    """A spike is temporary. Once its cooldown passes it is used again, so a
    momentary 503 does not cost the good model for the whole inbox."""
    clock = FakeClock()
    busy_once = Flaky("gemini", AIProviderError("busy", kind="unavailable"), failures=1)

    chain = FallbackAIProvider(chain_of(busy_once), sleep=clock.sleep, clock=clock)
    chain.classify_email(make_email("m1"))

    assert busy_once.calls == 2, "it comes back once the spike is over"


# --- a provider that cannot work is not announced as the rescue -------------

class NotInstalled(StubProvider):
    """Stands in for Ollama when it is not running."""

    def validate_setup(self):
        raise ValueError("Could not connect to local Ollama server")

    def classify_email(self, message, thread=None, known_facts=""):
        raise AssertionError("must never be called - it cannot work")


def test_a_local_model_that_is_not_running_is_never_announced_as_the_rescue():
    """It used to say "Switching to local ollama and carrying on", then die.

    Promising a rescue it has no way of delivering is worse than saying
    plainly that there is nothing left.
    """
    clock = FakeClock()
    spent = StubProvider("gemini", AIProviderError("no quota", kind="quota"))
    switches = []

    chain = FallbackAIProvider(
        chain_of(spent, NotInstalled("local ollama")),
        on_switch=lambda a, b, e: switches.append((a.label, b.label)),
        sleep=clock.sleep,
        clock=clock,
    )
    with pytest.raises(AIProviderError) as failure:
        chain.classify_email(make_email("m1"))

    assert switches == [], "nothing should be announced as taking over"
    assert "ollama" in failure.value.hint.lower()
    assert "not available" in failure.value.hint.lower(), (
        "the message has to say the local model could not be used, and why"
    )


# --- the same rule for a pool of keys ---------------------------------------

def lane_of(provider, env="GEMINI_API_KEY", clock=None):
    link = ProviderLink(label=provider.name, model="m", provider=provider)
    return KeyLane(link, env, clock=clock) if clock else KeyLane(link, env)


def test_one_busy_key_waits_instead_of_ending_the_run():
    """With a single key there is nothing to move to, so waiting is the only
    thing that keeps the run alive - and it used to give up here."""
    clock = FakeClock()
    busy = Flaky("gemini", rate_limited(10.0), failures=1)

    pool = KeyPoolProvider(
        [lane_of(busy, clock=clock)], sleep=clock.sleep
    )
    result = pool.classify_email(make_email("m1"))

    assert result.category.value == "work"
    assert busy.calls == 2
    assert clock.slept


def test_a_key_with_no_quota_left_is_still_retired():
    clock = FakeClock()
    spent = StubProvider("gemini-1", AIProviderError("no quota", kind="quota"))
    other = StubProvider("gemini-2")

    pool = KeyPoolProvider(
        [
            lane_of(spent, "GEMINI_API_KEY", clock=clock),
            lane_of(other, "GEMINI_API_KEY_2", clock=clock),
        ],
        sleep=clock.sleep,
    )
    pool.classify_email(make_email("m1"))
    pool.classify_email(make_email("m2"))

    assert spent.calls == 1
    assert other.calls == 2


# --- the pool really does the AI merge of facts now -------------------------

def test_a_pool_of_keys_folds_facts_in_with_the_model():
    """Both methods sat below find_pool, indented, so they belonged to that
    function instead of the class - and a pool quietly fell back to the base
    class's "just append it", which is the bug that once ate his facts file.
    """
    assert "organise_facts" in KeyPoolProvider.__dict__
    assert "suggest_facts" in KeyPoolProvider.__dict__
