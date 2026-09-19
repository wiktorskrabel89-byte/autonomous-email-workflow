import os
import re
import json
from typing import Optional, Type, TypeVar, List
from pydantic import BaseModel, ValidationError
from openai import (
    OpenAI,
    APIConnectionError,
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
        "default_model": "gemini-3.6-flash",
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

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


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
            return AIProviderError(
                f"{self.provider_name} rate limit or free-tier quota reached for '{model}'.",
                hint="Wait a minute and run it again, or switch to a smaller model "
                "in config.yaml. Free tiers usually reset within a minute or a day.",
                kind="quota",
            )
        if isinstance(e, (APITimeoutError, APIConnectionError)):
            return AIProviderError(
                f"Could not reach {self.provider_name} (timeout or no connection).",
                hint="Check your internet connection and try again.",
                kind="network",
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
        # Space requests out before sending, not after being refused.
        self.limiter.wait()
        """One raw request, so rate-limit headers can be read off the response.

        Groq and OpenAI report what is left in x-ratelimit-* headers. Gemini
        sends none, which is why usage is also counted locally.
        """
        raw = client.chat.completions.with_raw_response.create(**kwargs)
        return raw.parse(), dict(raw.headers)

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
        try:
            response, headers = self._send(client, kwargs)
        except BadRequestError as e:
            # Not every model on every provider supports response_format.
            # Fall back once rather than failing the whole run.
            if self._supports_json_mode and "response_format" in str(e).lower():
                self._supports_json_mode = False
                kwargs.pop("response_format", None)
                try:
                    response, headers = self._send(client, kwargs)
                except Exception as inner:
                    failure = self._translate(inner)
                    self._record(status="error", kind=failure.kind)
                    raise failure from None
            else:
                failure = self._translate(e)
                self._record(status="error", kind=failure.kind)
                raise failure from None
        except Exception as e:
            failure = self._translate(e)
            # A quota refusal is the single most useful thing to have on record.
            self._record(status="error", kind=failure.kind)
            raise failure from None

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
