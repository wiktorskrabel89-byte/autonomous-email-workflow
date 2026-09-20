import os
import re
import json
import time
from typing import Callable, Optional, Type, TypeVar, List
from pydantic import BaseModel, ValidationError
from openai import (
    OpenAI,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from email_workflow.core.errors import AIProviderError
from email_workflow.core.throttle import RateLimiter
from email_workflow.core.usage import UsageTracker
from email_workflow.models.config import APIConfig
from email_workflow.models.email import EmailMessage
from email_workflow.models.analysis import (
    ClassificationVerdict,
    EmailAnalysis,
    DecisionSupportOutput,
    ReplyGenerationOutput,
    ReplyReviewOutput,
    KnownFactsMerge,
)
from email_workflow.models.state import ThreadState
from email_workflow.providers.base_ai import (
    AIProvider,
    CLASSIFICATION_PROMPT_TEMPLATE,
    DECISION_SUPPORT_PROMPT_TEMPLATE,
    REPLY_GENERATION_PROMPT_TEMPLATE,
    REPLY_REVIEW_PROMPT_TEMPLATE,
)

T = TypeVar("T", bound=BaseModel)

PROVIDER_METADATA = {
    "openai": {
        "name": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "base_url": None,
        "keys_url": "https://platform.openai.com/api-keys",
        "default_model": "gpt-4o-mini",
    },
    "gemini": {
        "name": "Google Gemini",
        "env_var": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "keys_url": "https://aistudio.google.com/apikey",
        # The free tier's workhorse: the cheapest Gemini per request, so the
        # daily allowance stretches furthest over a full inbox.
        "default_model": "gemini-3.1-flash-lite",
    },
    "groq": {
        "name": "Groq",
        "env_var": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "keys_url": "https://console.groq.com/keys",
        "default_model": "llama-3.3-70b-versatile",
    },
    "openrouter": {
        "name": "OpenRouter",
        "env_var": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "keys_url": "https://openrouter.ai/keys",
        "default_model": "meta-llama/llama-3.3-70b-instruct",
    },
}

# Request timeout in seconds. Without this the CLI can hang forever on a stalled
# connection with no output at all, which reads to the user as a freeze.
REQUEST_TIMEOUT = 60.0

# A 429 that is only this minute's limit is waited out here, on the same model,
# rather than being treated as the end of that model. Two tries is enough: a
# per-minute window is 60 seconds wide, so anything still refused after two
# waits is not a per-minute problem.
RATE_LIMIT_RETRIES = 2
# Never sit on one refusal longer than this, whatever the server asks for. A
# daily quota can come back with a wait of hours, and that is a failover, not
# a pause.
MAX_RATE_LIMIT_WAIT = 90.0
# When the server refuses without saying how long to wait. Gemini's window is
# a rolling minute, so this is long enough to clear most of one.
DEFAULT_RATE_LIMIT_WAIT = 20.0

# A model that is overloaded (HTTP 5xx). Unlike a per-minute limit, waiting is
# the WORSE answer here: another model on the same key will usually answer
# immediately, and "high demand" has no published window to wait out. So it
# gets one quick retry in case the spike was momentary, then the chain moves
# to the next model.
_BUSY_STATUSES = frozenset({500, 502, 503, 504, 529})
BUSY_RETRIES = 1
BUSY_WAIT = 5.0

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# "retryDelay": "17s" / retry in 17.2s / Retry-After: 17
# Tolerant about what sits between the phrase and the number: the same body
# arrives as retryDelay: "17s", retryDelay=17s, or - once it has been through
# a JSON dump - retryDelay: \"17s\".
_RETRY_DELAY = re.compile(
    r"(?:retry[_\-]?delay|retry in|try again in)[\s:=\"'\\]*([0-9]+(?:\.[0-9]+)?)\s*s",
    re.IGNORECASE,
)
# Google names the exact allowance that was hit, e.g.
# "GenerateRequestsPerMinutePerProjectPerModel-FreeTier" or "...PerDay...".
_PER_MINUTE = re.compile(r"per[\s_\-]?minute|requests?[\s_\-]?per[\s_\-]?min|\brpm\b", re.IGNORECASE)
_PER_DAY = re.compile(r"per[\s_\-]?day|daily|\brpd\b|per[\s_\-]?month", re.IGNORECASE)
# A 429 that will never clear on its own: the allowance is used up or there is
# no billing behind it. OpenAI's is the important one - "insufficient_quota"
# mentions neither a minute nor a day, so without this it would read as a busy
# minute and be waited out over and over, while telling the user "nothing is
# broken" about something only a payment method can fix.
_SPENT_FOR_GOOD = re.compile(
    r"insufficient[_\s\-]?quota"
    r"|check your plan and billing"
    r"|billing details"
    r"|exceeded your (?:current )?quota.*billing"
    r"|out of credits?"
    r"|credit balance",
    re.IGNORECASE,
)


def _extract_json(content: str) -> str:
    """Pull the JSON object out of a model response.

    Models wrap JSON in markdown fences or add a sentence of preamble often
    enough that failing on it wastes a whole retry round-trip.
    """
    if not content:
        return ""
    text = content.strip()

    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    if text.startswith("{"):
        return text

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _describe_error(e: Exception) -> str:
    """Everything the provider said about a failure, as one searchable string.

    A 429 says which allowance ran out and how long to wait, but it says it in
    a different place for every provider: in the message, in the JSON body, or
    in a header. Reading a 429 wrongly is expensive in both directions - waiting
    out a quota that is spent for the day wastes a run, and retiring a model
    over this minute's limit ends one - so all of it is looked at.
    """
    parts = [str(getattr(e, "message", "") or ""), str(e)]
    body = getattr(e, "body", None)
    # The provider's own sentence, before json.dumps escapes the quotes in it.
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("message"):
            parts.append(str(error["message"]))
    if body is not None:
        try:
            parts.append(json.dumps(body, default=str))
        except Exception:
            parts.append(str(body))
    response = getattr(e, "response", None)
    if response is not None:
        try:
            parts.append(json.dumps(dict(response.headers), default=str))
        except Exception:
            pass
    return " ".join(part for part in parts if part)


class OpenAICompatibleProvider(AIProvider):
    def __init__(self, provider_id: str, config: APIConfig):
        self.provider_id = provider_id.lower()
        self.config = config
        meta = PROVIDER_METADATA.get(self.provider_id, {})
        self.provider_name = meta.get("name", provider_id)
        self.api_key_env = config.api_key_env or meta.get("env_var", f"{provider_id.upper()}_API_KEY")
        self.base_url = meta.get("base_url")
        self.keys_url = meta.get("keys_url", "")
        self._supports_json_mode = True
        self.usage = UsageTracker()
        self.limiter = RateLimiter(getattr(config, "requests_per_minute", 0))
        # Called before waiting out a refusal the server sent us, so the pause
        # is explained while it happens instead of looking like a freeze.
        self.on_server_pause: Optional[Callable[[float, str], None]] = None
        # Swappable so a test can prove the waiting without sitting through it.
        self.sleep: Callable[[float], None] = time.sleep

    def describe(self) -> tuple:
        return (self.provider_id, self.config.model)

    def validate_setup(self) -> None:
        key = os.getenv(self.api_key_env)
        if not key or not key.strip():
            hint = f"Get a key at {self.keys_url}" if self.keys_url else ""
            raise ValueError(
                f"No API key for {self.provider_name}. "
                f"Set '{self.api_key_env}' in your .env file or environment. {hint}".strip()
            )

    def _get_client(self) -> OpenAI:
        self.validate_setup()
        key = os.getenv(self.api_key_env)
        if self.base_url:
            return OpenAI(api_key=key, base_url=self.base_url, timeout=REQUEST_TIMEOUT)
        return OpenAI(api_key=key, timeout=REQUEST_TIMEOUT)

    def list_models(self) -> List[str]:
        """Model ids this key can actually use, newest-looking last.

        Listing models does not consume generation quota, so this is safe to
        call on a free tier when a configured model has stopped working.
        """
        try:
            client = self._get_client()
            return sorted(m.id for m in client.models.list())
        except Exception as e:
            raise self._translate(e) from None

    @staticmethod
    def _provider_message(e: Exception) -> str:
        """The provider's own explanation, without the wrapper noise.

        Worth surfacing: Google's 404 says whether a model was retired outright
        or is merely closed to new keys, and names the replacement. Our own
        guess at the cause is never that specific.
        """
        body = getattr(e, "body", None)
        if isinstance(body, list) and body:
            body = body[0]
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"]).strip()
        message = getattr(e, "message", "")
        return str(message or e).strip()

    @staticmethod
    def _retry_after_seconds(e: Exception) -> float:
        """How long the server asked us to wait, in seconds. 0 if it did not.

        Google puts it in the error body ("retryDelay": "17s"), most other
        providers in a Retry-After header. Honouring it is the difference
        between one short pause and a stream of refusals.
        """
        response = getattr(e, "response", None)
        headers = getattr(response, "headers", None) or {}
        for name in ("retry-after", "Retry-After", "x-ratelimit-reset-requests"):
            raw = headers.get(name) if hasattr(headers, "get") else None
            if raw:
                match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)", str(raw))
                if match:
                    return float(match.group(1))

        found = _RETRY_DELAY.search(_describe_error(e))
        return float(found.group(1)) if found else 0.0

    @classmethod
    def _is_per_minute_limit(cls, e: Exception) -> bool:
        """Whether a 429 is this minute's limit rather than a spent allowance.

        Final, and moved on from at once: a per-DAY allowance, and a refusal
        that names billing or an empty balance - OpenAI's "insufficient_quota"
        says neither "minute" nor "day", and waiting that one out would be
        waiting for a payment method to appear by itself.

        Everything else is given the benefit of the doubt and waited out,
        because retiring a model that would have worked again in 17 seconds is
        what used to end a run halfway through the inbox.
        """
        text = _describe_error(e)
        if _SPENT_FOR_GOOD.search(text):
            return False
        if _PER_DAY.search(text) and not _PER_MINUTE.search(text):
            return False
        return True

    def _translate(self, e: Exception) -> AIProviderError:
        """Turn a provider exception into something a human can act on."""
        model = self.config.model

        if isinstance(e, NotFoundError):
            detail = self._provider_message(e)
            return AIProviderError(
                f"{self.provider_name} will not serve the model '{model}'."
                + (f"\n\n{self.provider_name} says: {detail}" if detail else ""),
                hint="Run 'email-workflow models' to see the models your key can use, "
                "then put one of them in config.yaml under ai.api.model. "
                "Note that a model can be listed there and still be closed to new "
                "keys, in which case the message above names the replacement.",
                kind="model_gone",
            )
        if isinstance(e, AuthenticationError):
            return AIProviderError(
                f"{self.provider_name} rejected your API key.",
                hint=f"Check '{self.api_key_env}' in your .env file."
                + (f" You can create a new key at {self.keys_url}." if self.keys_url else ""),
                kind="auth",
            )
        if isinstance(e, PermissionDeniedError):
            return AIProviderError(
                f"Your {self.provider_name} key is not allowed to use '{model}'.",
                hint="Run 'email-workflow models' to see what this key may use.",
                kind="no_access",
            )
        if isinstance(e, RateLimitError):
            wait = self._retry_after_seconds(e)
            if self._is_per_minute_limit(e):
                return AIProviderError(
                    f"{self.provider_name} is refusing requests for a moment: "
                    f"the per-minute limit for '{model}' is full.",
                    hint="It clears by itself within a minute. Nothing is broken "
                    "and nothing is lost.",
                    kind="rate_limit",
                    retry_after=wait,
                )
            detail = self._provider_message(e)
            for_good = bool(_SPENT_FOR_GOOD.search(_describe_error(e)))
            return AIProviderError(
                (
                    f"{self.provider_name} has no quota left for '{model}'."
                    if for_good
                    else f"{self.provider_name} has no free quota left today "
                    f"for '{model}'."
                )
                + (f"\n\n{self.provider_name} says: {detail}" if detail else ""),
                hint=(
                    "Waiting will not fix this one - the allowance is used up, "
                    "or the account needs a payment method. Use a key from "
                    "another account, or put a different model in config.yaml "
                    "under ai.api.model."
                    if for_good
                    else "A daily free allowance resets the next day. Add "
                    "another key from a second account, or put a different "
                    "model in config.yaml under ai.api.model."
                ),
                kind="quota",
                retry_after=wait,
            )
        if isinstance(e, (APITimeoutError, APIConnectionError)):
            return AIProviderError(
                f"Could not reach {self.provider_name} (timeout or no connection).",
                hint="Check your internet connection and try again.",
                kind="network",
            )
        # 503 "this model is currently experiencing high demand", and the rest
        # of the 5xx family. Nothing is wrong with the request, the key or the
        # quota - that model is busy this minute. It has to be a failover:
        # another model on the same key will usually answer at once. Falling
        # through to the generic case below made it kind="other", which does
        # not fail over, so one busy model ended the whole run.
        if isinstance(e, APIStatusError) and e.status_code in _BUSY_STATUSES:
            detail = self._provider_message(e)
            return AIProviderError(
                f"{self.provider_name} says '{model}' is busy right now "
                f"(HTTP {e.status_code})."
                + (f"\n\n{self.provider_name} says: {detail}" if detail else ""),
                hint="This is the model being overloaded, not anything you did. "
                "It moves to the next model in the chain and carries on.",
                kind="unavailable",
                retry_after=self._retry_after_seconds(e),
            )
        if isinstance(e, BadRequestError):
            return AIProviderError(
                f"{self.provider_name} rejected the request for '{model}': {e}",
                hint="The model may not support JSON mode. Try a different model "
                "from 'email-workflow models'.",
                kind="bad_request",
            )
        return AIProviderError(f"{self.provider_name} request failed: {e}")

    def _send(self, client: OpenAI, kwargs: dict):
        """One raw request, so rate-limit headers can be read off the response.

        Groq and OpenAI report what is left in x-ratelimit-* headers. Gemini
        sends none, which is why usage is also counted locally.
        """
        # Space requests out before sending, not after being refused.
        self.limiter.wait()
        raw = client.chat.completions.with_raw_response.create(**kwargs)
        return raw.parse(), dict(raw.headers)

    def _attempt(self, client: OpenAI, kwargs: dict):
        """One request, JSON mode dropped if the model cannot do it.

        Always raises AIProviderError, never a provider exception: the caller
        decides what to do from the kind, and every refusal is recorded.
        """
        try:
            return self._send(client, kwargs)
        except BadRequestError as e:
            # Not every model on every provider supports response_format.
            # Fall back once rather than failing the whole run.
            if self._supports_json_mode and "response_format" in str(e).lower():
                self._supports_json_mode = False
                kwargs.pop("response_format", None)
                try:
                    return self._send(client, kwargs)
                except Exception as inner:
                    failure = self._translate(inner)
                    self._record(status="error", kind=failure.kind)
                    raise failure from None
            failure = self._translate(e)
            self._record(status="error", kind=failure.kind)
            raise failure from None
        except Exception as e:
            failure = self._translate(e)
            # A quota refusal is the single most useful thing to have on record.
            self._record(status="error", kind=failure.kind)
            raise failure from None

    def _record(self, response=None, headers=None, status="ok", kind="") -> None:
        """Bookkeeping for the usage report. Never raises."""
        try:
            usage = getattr(response, "usage", None)
            self.usage.record(
                provider=self.provider_id,
                model=self.config.model,
                # Which key, so a pool of keys from different accounts can be
                # read per account - one account's free tier says nothing
                # about another's.
                key_env=self.api_key_env,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                status=status,
                kind=kind,
                headers=headers,
            )
        except Exception:
            pass

    def _chat(self, messages: list) -> str:
        """One chat completion, with JSON mode dropped if the model lacks it."""
        client = self._get_client()
        kwargs = dict(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_output_tokens,
        )
        if self._supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        headers = None
        tries = {"rate_limit": RATE_LIMIT_RETRIES, "unavailable": BUSY_RETRIES}
        used = {"rate_limit": 0, "unavailable": 0}
        while True:
            try:
                response, headers = self._attempt(client, kwargs)
                break
            except AIProviderError as failure:
                # This minute's limit is not the end of a model. Waiting it out
                # on the same model is the whole fix: treating it as "this
                # provider is finished" is what walked the chain down, one
                # model at a time, to a local Ollama that was not running - and
                # ended the run halfway through the inbox.
                #
                # An overloaded model gets ONE quick retry and is then handed
                # on: waiting is the wrong answer when another model is free.
                left = tries.get(failure.kind, 0) - used.get(failure.kind, 0)
                if left <= 0:
                    raise
                used[failure.kind] += 1
                if failure.kind == "unavailable":
                    delay = min(failure.retry_after or BUSY_WAIT, MAX_RATE_LIMIT_WAIT)
                else:
                    delay = min(
                        failure.retry_after or DEFAULT_RATE_LIMIT_WAIT,
                        MAX_RATE_LIMIT_WAIT,
                    )
                if self.on_server_pause:
                    try:
                        self.on_server_pause(delay, f"{self.provider_name} / {self.config.model}")
                    except Exception:
                        pass
                self.sleep(delay)

        self._record(response=response, headers=headers)

        if not response.choices:
            raise AIProviderError(
                f"{self.provider_name} returned an empty response for '{self.config.model}'.",
                hint="Try running it again, or use a different model.",
            )

        choice = response.choices[0]

        # A cut-off answer is invalid JSON, but "your JSON is broken" would send
        # the user hunting for the wrong problem. The real cause is the token
        # budget, and reasoning models spend part of it before they write.
        if choice.finish_reason == "length":
            raise AIProviderError(
                f"{self.provider_name} ran out of room while answering with "
                f"'{self.config.model}', so the reply was cut off.",
                hint=(
                    f"Raise [ai.api.max_output_tokens] in config.yaml - it is "
                    f"currently {self.config.max_output_tokens}. Try 2000 or more. "
                    f"Newer models think before they answer, and that thinking "
                    f"comes out of the same budget."
                ),
                kind="truncated",
            )

        return choice.message.content or ""

    def _call_model_with_json_retry(self, prompt: str, schema_class: Type[T]) -> T:
        system = (
            "You output a single JSON object and nothing else. "
            "No explanation, no markdown fence."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        content = self._chat(messages)
        try:
            return schema_class.model_validate_json(_extract_json(content))
        except (ValidationError, json.JSONDecodeError) as first_error:
            retry_messages = [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\n"
                        f"Your previous answer was not valid for the schema.\n"
                        f"Error: {first_error}\n"
                        f"You returned:\n{content}\n\n"
                        f"Return the corrected JSON object only."
                    ),
                },
            ]
            retry_content = self._chat(retry_messages)
            try:
                return schema_class.model_validate_json(_extract_json(retry_content))
            except (ValidationError, json.JSONDecodeError) as second_error:
                raise AIProviderError(
                    f"{self.provider_name} did not return valid JSON for "
                    f"'{self.config.model}', even after one retry.",
                    hint="Smaller models sometimes cannot hold the output format. "
                    "Try a larger model from 'email-workflow models'. "
                    f"Last error: {second_error}",
                ) from None

    @staticmethod
    def _format_thread(thread: Optional[ThreadState], empty: str) -> str:
        if thread and thread.messages:
            return "\n".join(
                f"- {m.message_id} (status: {m.status})" for m in thread.messages
            )
        return empty

    def classify_email(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> EmailAnalysis:
        prompt = CLASSIFICATION_PROMPT_TEMPLATE.format(
            subject=message.subject,
            sender_name=message.sender.name,
            sender_email=message.sender.email,
            known_contact="yes" if message.sender.known_contact else "no",
            received_at=message.received_at,
            body=message.body,
            thread_context=self._format_thread(thread, "No earlier messages in this thread."),
            known_facts=known_facts or "None provided.",
            protected_topics=self.protected_topics_block(),
        )
        verdict = self._call_model_with_json_retry(prompt, ClassificationVerdict)

        # Metadata is ours, not the model's.
        return EmailAnalysis(
            message_id=message.message_id,
            thread_id=message.thread_id,
            in_reply_to=message.in_reply_to,
            sender=message.sender,
            subject=message.subject,
            received_at=message.received_at,
            **verdict.model_dump(),
        )

    def evaluate_decision_support(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> DecisionSupportOutput:
        prompt = DECISION_SUPPORT_PROMPT_TEMPLATE.format(
            subject=message.subject,
            body=message.body,
            known_facts=known_facts or "None provided.",
        )
        return self._call_model_with_json_retry(prompt, DecisionSupportOutput)

    def generate_reply(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> ReplyGenerationOutput:
        prompt = REPLY_GENERATION_PROMPT_TEMPLATE.format(
            subject=message.subject,
            sender_name=message.sender.name,
            sender_email=message.sender.email,
            body=message.body,
            thread_context=self._format_thread(thread, "This is the first message."),
            known_facts=known_facts or "None provided.",
        )
        return self._call_model_with_json_retry(prompt, ReplyGenerationOutput)

    def organise_facts(self, existing: str, addition: str) -> KnownFactsMerge:
        from email_workflow.core.known_facts import FACTS_MERGE_PROMPT_TEMPLATE
        prompt = FACTS_MERGE_PROMPT_TEMPLATE.format(
            existing=existing or "(nothing yet)",
            addition=addition,
        )
        return self._call_model_with_json_retry(prompt, KnownFactsMerge)

    def suggest_facts(self, emails: str, existing: str = "") -> KnownFactsMerge:
        from email_workflow.core.known_facts import FACTS_FROM_EMAIL_PROMPT_TEMPLATE
        prompt = FACTS_FROM_EMAIL_PROMPT_TEMPLATE.format(
            existing=existing or "(nothing yet)", emails=emails,
        )
        return self._call_model_with_json_retry(prompt, KnownFactsMerge)

    def review_reply(
        self,
        message: EmailMessage,
        reply: ReplyGenerationOutput,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> ReplyReviewOutput:
        prompt = REPLY_REVIEW_PROMPT_TEMPLATE.format(
            subject=message.subject,
            sender_name=message.sender.name,
            sender_email=message.sender.email,
            body=message.body,
            thread_context=self._format_thread(thread, "This is the first message."),
            known_facts=known_facts or "None provided.",
            reply_subject=reply.reply_subject,
            reply_body=reply.reply_body,
        )
        return self._call_model_with_json_retry(prompt, ReplyReviewOutput)
