import json
import httpx
from typing import Optional
from email_workflow.core.errors import AIProviderError
from email_workflow.models.config import LocalConfig
from email_workflow.models.email import EmailMessage
from email_workflow.models.analysis import (
    ClassificationVerdict,
    EmailAnalysis,
    DecisionSupportOutput,
    ReplyGenerationOutput,
)
from email_workflow.models.state import ThreadState
from email_workflow.providers.base_ai import (
    AIProvider,
    CLASSIFICATION_PROMPT_TEMPLATE,
    DECISION_SUPPORT_PROMPT_TEMPLATE,
    REPLY_GENERATION_PROMPT_TEMPLATE,
)

# A local model on modest hardware is slower than a cloud one, and it may
# have to load into memory first.
LOCAL_TIMEOUT = 180.0


class OllamaProvider(AIProvider):
    def __init__(self, config: LocalConfig):
        self.config = config
        self.endpoint = config.endpoint.rstrip("/")

    def describe(self) -> tuple:
        return (self.config.runtime, self.config.model)

    def validate_setup(self) -> None:
        try:
            resp = httpx.get(f"{self.endpoint}/api/tags", timeout=3.0)
            if resp.status_code != 200:
                raise ValueError(
                    f"Ollama server at '{self.endpoint}' returned status code {resp.status_code}."
                )
        except Exception as e:
            raise ValueError(
                f"Could not connect to local Ollama server at '{self.endpoint}': {e}. "
                f"Please verify Ollama is running."
            )

    def _generate_json(self, prompt: str) -> dict:
        """Ask the local model, reporting failures the way the cloud ones do.

        Raising AIProviderError rather than a bare httpx error matters because
        this provider can sit at the end of a fallback chain: the chain only
        knows how to move on from an AIProviderError.
        """
        url = f"{self.endpoint}/api/generate"
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {"temperature": self.config.temperature},
        }

        try:
            resp = httpx.post(url, json=payload, timeout=LOCAL_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 404:
                raise AIProviderError(
                    f"Ollama does not have the model '{self.config.model}'.",
                    hint=f"Run 'ollama pull {self.config.model}', or set another "
                    f"model under ai.local.model in config.yaml.",
                    kind="model_gone",
                ) from None
            raise AIProviderError(
                f"Ollama returned status {status} for '{self.config.model}'.",
                kind="network",
            ) from None
        except Exception as e:
            raise AIProviderError(
                f"Could not reach Ollama at {self.endpoint}: {e}",
                hint="Start it with 'ollama serve', or switch ai.mode back to "
                "'api' in config.yaml.",
                kind="network",
            ) from None

        response_text = data.get("response", "")
        try:
            return json.loads(response_text)
        except json.JSONDecodeError as e:
            raise AIProviderError(
                f"The local model '{self.config.model}' did not return valid JSON.",
                hint="Small models often cannot hold an output format. Try a "
                "larger one, for example 'ollama pull qwen3:4b'. "
                f"Error: {e}",
                kind="bad_output",
            ) from None

    def classify_email(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> EmailAnalysis:
        thread_context = ""
        if thread and thread.messages:
            thread_context = "\n".join(
                [f"- Msg {m.message_id} ({m.status})" for m in thread.messages]
            )
        else:
            thread_context = "No previous messages in thread."

        prompt = CLASSIFICATION_PROMPT_TEMPLATE.format(
            subject=message.subject,
            sender_name=message.sender.name,
            sender_email=message.sender.email,
            known_contact="yes" if message.sender.known_contact else "no",
            received_at=message.received_at,
            body=message.body,
            thread_context=thread_context,
            known_facts=known_facts or "None provided.",
        )
        res_dict = self._generate_json(prompt)
        verdict = ClassificationVerdict.model_validate(res_dict)

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
        res_dict = self._generate_json(prompt)
        return DecisionSupportOutput.model_validate(res_dict)

    def generate_reply(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> ReplyGenerationOutput:
        thread_context = ""
        if thread and thread.messages:
            thread_context = "\n".join(
                [f"- Msg {m.message_id} ({m.received_at})" for m in thread.messages]
            )
        else:
            thread_context = "Single message thread."

        prompt = REPLY_GENERATION_PROMPT_TEMPLATE.format(
            subject=message.subject,
            sender_name=message.sender.name,
            sender_email=message.sender.email,
            body=message.body,
            thread_context=thread_context,
            known_facts=known_facts or "None provided.",
        )
        res_dict = self._generate_json(prompt)
        return ReplyGenerationOutput.model_validate(res_dict)
