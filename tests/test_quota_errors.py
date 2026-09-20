"""Reading a 429 for what it actually says.

A provider says "429" for two completely different things: "not this second"
and "not until tomorrow". Treating both as the second one is what ended a run
halfway through the inbox - the model that was only busy for a moment was
retired, and so was the next one, until there was nothing left.

These tests use the real shapes Google and Groq send back.
"""

import httpx
import pytest
from openai import InternalServerError, RateLimitError

from email_workflow.models.config import APIConfig
from email_workflow.providers.api_providers import OpenAICompatibleProvider


def provider(monkeypatch) -> OpenAICompatibleProvider:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return OpenAICompatibleProvider(
        "gemini",
        APIConfig(
            provider="gemini",
            model="gemini-3.1-flash-lite",
            api_key_env="GEMINI_API_KEY",
            requests_per_minute=0,
        ),
    )


def rate_limit_error(message: str, headers=None) -> RateLimitError:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://example.invalid/chat"),
        headers=headers or {},
    )
    return RateLimitError(
        "Error code: 429", response=response, body={"error": {"message": message}}
    )


PER_MINUTE = (
    "You exceeded your current quota. quota_metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
    "quota_id: GenerateRequestsPerMinutePerProjectPerModel-FreeTier. "
    'retryDelay: "17s"'
)

PER_DAY = (
    "You exceeded your current quota. quota_id: "
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
)


# --- which kind of 429 is it -----------------------------------------------

def test_a_per_minute_refusal_is_a_pause_not_the_end(monkeypatch):
    failure = provider(monkeypatch)._translate(rate_limit_error(PER_MINUTE))
    assert failure.kind == "rate_limit"
    assert failure.is_transient
    assert failure.retry_after == 17.0, "the wait the server asked for"


def test_a_daily_quota_is_the_end_for_today(monkeypatch):
    failure = provider(monkeypatch)._translate(rate_limit_error(PER_DAY))
    assert failure.kind == "quota"
    assert not failure.is_transient
    assert "today" in failure.message


def test_a_retry_after_header_is_honoured(monkeypatch):
    failure = provider(monkeypatch)._translate(
        rate_limit_error("Too Many Requests", headers={"retry-after": "9"})
    )
    assert failure.retry_after == 9.0


def test_a_429_that_explains_nothing_is_given_the_benefit_of_the_doubt(monkeypatch):
    """Unclear has to mean "wait and see". Guessing "spent" retires a model
    that would have worked again in seconds, and that is how a run dies."""
    failure = provider(monkeypatch)._translate(rate_limit_error("Too Many Requests"))
    assert failure.kind == "rate_limit"


# OpenAI's real "you have no money on this account" 429. It says nothing about
# a minute or a day, so the benefit of the doubt above would have waited it out
# over and over - and told the user nothing was broken, about the one failure
# that needs them to go and add a payment method.
OPENAI_NO_CREDIT = (
    "You exceeded your current quota, please check your plan and billing "
    "details. For more information on this error, read the docs."
)


def test_no_credit_on_the_account_is_never_waited_out(monkeypatch):
    failure = provider(monkeypatch)._translate(rate_limit_error(OPENAI_NO_CREDIT))
    assert failure.kind == "quota"
    assert not failure.is_transient


def test_and_it_does_not_promise_that_it_clears_by_itself(monkeypatch):
    """The message has to tell the truth about who has to do something."""
    failure = provider(monkeypatch)._translate(rate_limit_error(OPENAI_NO_CREDIT))
    assert "Waiting will not fix this" in failure.hint
    assert "today" not in failure.message, (
        "this one does not come back tomorrow either"
    )


def test_insufficient_quota_by_name_is_also_final(monkeypatch):
    failure = provider(monkeypatch)._translate(
        rate_limit_error("Error: insufficient_quota")
    )
    assert failure.kind == "quota"


# --- an overloaded model is a failover, not the end of the run --------------

# Word for word what ended his run at 21 of 145.
HIGH_DEMAND = (
    "This model is currently experiencing high demand. Spikes in demand are "
    "usually temporary. Please try again later."
)


def busy_error(message: str = HIGH_DEMAND, status: int = 503) -> InternalServerError:
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://example.invalid/chat")
    )
    return InternalServerError(
        f"Error code: {status}",
        response=response,
        body={"error": {"code": status, "message": message, "status": "UNAVAILABLE"}},
    )


def test_a_model_under_high_demand_moves_to_the_next_one(monkeypatch):
    """It fell through to the catch-all, which does not fail over, so one
    overloaded model ended a 145-email run after 21."""
    failure = provider(monkeypatch)._translate(busy_error())
    assert failure.kind == "unavailable"
    assert failure.can_failover, "this has to move to another model"
    assert failure.is_transient, "and the model comes back later in the run"


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
def test_the_whole_5xx_family_is_treated_the_same(monkeypatch, status):
    assert provider(monkeypatch)._translate(busy_error(status=status)).can_failover


def test_the_message_says_it_is_the_model_not_you(monkeypatch):
    failure = provider(monkeypatch)._translate(busy_error())
    assert "busy" in failure.message
    assert "high demand" in failure.message, "keep what the provider said"
    assert "not anything you did" in failure.hint


def test_an_overloaded_model_is_retried_once_then_handed_on(monkeypatch):
    """Waiting is the wrong answer when another model is free: "high demand"
    has no window to wait out, and the next model usually answers at once."""
    ai = provider(monkeypatch)
    waits = []
    ai.sleep = waits.append
    monkeypatch.setattr(ai, "_get_client", lambda: object())

    tries = []

    def always_busy(client, kwargs):
        tries.append(1)
        raise ai._translate(busy_error())

    monkeypatch.setattr(ai, "_attempt", always_busy)

    with pytest.raises(Exception) as failure:
        ai._chat([{"role": "user", "content": "hi"}])

    assert failure.value.kind == "unavailable"
    assert len(tries) == 2, "one quick retry, then let the chain switch models"
    assert waits == [5.0], "and a short wait, not a per-minute one"


# --- and what is done about it ---------------------------------------------

class FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()
        self.finish_reason = "stop"


class FakeResponse:
    def __init__(self, content='{"ok": true}'):
        self.choices = [FakeChoice(content)]
        self.usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()


def test_a_busy_minute_is_waited_out_on_the_same_model(monkeypatch):
    """The request is retried where it was, instead of failing over.

    This is the cheapest of the fixes and the one that catches the common
    case: the model is fine, this minute is full.
    """
    ai = provider(monkeypatch)
    waits = []
    ai.sleep = waits.append
    monkeypatch.setattr(ai, "_get_client", lambda: object())

    tries = []

    def flaky(client, kwargs):
        tries.append(1)
        if len(tries) == 1:
            raise ai._translate(rate_limit_error(PER_MINUTE))
        return FakeResponse(), {}

    monkeypatch.setattr(ai, "_attempt", flaky)

    assert ai._chat([{"role": "user", "content": "hi"}]) == '{"ok": true}'
    assert len(tries) == 2, "it should have tried the same model again"
    assert waits == [17.0], "and waited exactly as long as it was asked to"


def test_the_pause_is_said_out_loud(monkeypatch):
    ai = provider(monkeypatch)
    ai.sleep = lambda seconds: None
    said = []
    ai.on_server_pause = lambda seconds, who: said.append((seconds, who))
    monkeypatch.setattr(ai, "_get_client", lambda: object())

    tries = []

    def flaky(client, kwargs):
        tries.append(1)
        if len(tries) == 1:
            raise ai._translate(rate_limit_error(PER_MINUTE))
        return FakeResponse(), {}

    monkeypatch.setattr(ai, "_attempt", flaky)
    ai._chat([{"role": "user", "content": "hi"}])

    assert said and said[0][0] == 17.0
    assert "gemini" in said[0][1].lower()


def test_a_daily_quota_is_not_waited_out(monkeypatch):
    """Sitting on a quota that resets tomorrow is not a pause, it is a hang."""
    ai = provider(monkeypatch)
    waits = []
    ai.sleep = waits.append
    monkeypatch.setattr(ai, "_get_client", lambda: object())

    def spent(client, kwargs):
        raise ai._translate(rate_limit_error(PER_DAY))

    monkeypatch.setattr(ai, "_attempt", spent)

    with pytest.raises(Exception) as failure:
        ai._chat([{"role": "user", "content": "hi"}])
    assert failure.value.kind == "quota"
    assert waits == []


def test_it_stops_retrying_rather_than_hammering_the_provider(monkeypatch):
    ai = provider(monkeypatch)
    waits = []
    ai.sleep = waits.append
    monkeypatch.setattr(ai, "_get_client", lambda: object())

    tries = []

    def always_busy(client, kwargs):
        tries.append(1)
        raise ai._translate(rate_limit_error(PER_MINUTE))

    monkeypatch.setattr(ai, "_attempt", always_busy)

    with pytest.raises(Exception):
        ai._chat([{"role": "user", "content": "hi"}])
    assert len(tries) == 3, "one try plus two retries, then hand it on"


def test_a_wait_the_server_asks_for_is_capped(monkeypatch):
    """A refusal that asks for an hour is a failover, not a pause."""
    ai = provider(monkeypatch)
    waits = []
    ai.sleep = waits.append
    monkeypatch.setattr(ai, "_get_client", lambda: object())

    tries = []

    def long_wait(client, kwargs):
        tries.append(1)
        if len(tries) == 1:
            raise ai._translate(
                rate_limit_error("Too many requests. retryDelay: \"3600s\"")
            )
        return FakeResponse(), {}

    monkeypatch.setattr(ai, "_attempt", long_wait)
    ai._chat([{"role": "user", "content": "hi"}])

    assert waits == [90.0], "capped, not slept for an hour"
