"""A second provider, with its own key and its own model.

His complaint: "when I save a model for the Groq key, it is gone when I leave
the terminal". It was never saved, and it could not have been. The wizard
configured ONE provider, and every other one is found by auto-detect - those
entries carry model=None, so whatever you typed was dropped and the built-in
default used instead. There was nowhere to put it.
"""

import pytest

from email_workflow.models.config import AppConfig, FallbackLink
from email_workflow.providers.ai_factory import build_provider_chain
from email_workflow.providers.api_providers import PROVIDER_METADATA


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    yield


def models_in_chain(config) -> list:
    return [(link.label, link.model) for link in build_provider_chain(config)]


def test_a_model_chosen_for_the_backup_is_the_one_used():
    """The whole point. Before this, the chain used Groq's built-in default
    however carefully you had typed something else."""
    config = AppConfig()
    config.ai.api.provider = "gemini"
    config.ai.api.model = "gemini-3.1-flash-lite"
    config.ai.fallback.chain = [
        FallbackLink(provider="groq", model="qwen/qwen3.8-27b",
                     api_key_env="GROQ_API_KEY")
    ]

    chosen = dict(models_in_chain(config))
    assert chosen.get("Groq") == "qwen/qwen3.8-27b"


def test_without_a_model_it_falls_back_to_the_default():
    config = AppConfig()
    config.ai.api.provider = "gemini"
    config.ai.fallback.chain = [FallbackLink(provider="groq")]

    chosen = dict(models_in_chain(config))
    assert chosen.get("Groq") == PROVIDER_METADATA["groq"]["default_model"]


def test_the_groq_default_is_the_one_he_asked_for():
    assert PROVIDER_METADATA["groq"]["default_model"] == "qwen/qwen3.8-27b"


def test_it_survives_being_written_and_read_back(tmp_path, monkeypatch):
    """"It is not saved when I leave the terminal" - so this writes it, reads
    it back, and checks the model is still there."""
    import yaml
    from email_workflow.models import config as config_module

    config = AppConfig()
    config.ai.fallback.chain = [
        FallbackLink(provider="groq", model="qwen/qwen3.8-27b",
                     api_key_env="GROQ_API_KEY")
    ]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")

    monkeypatch.setattr(config_module, "resolve_project_file",
                        lambda p: tmp_path / str(p))
    reloaded = config_module.AppConfig.load_from_file("config.yaml")

    groq = [l for l in reloaded.ai.fallback.chain if l.provider == "groq"]
    assert groq and groq[0].model == "qwen/qwen3.8-27b"
    assert groq[0].api_key_env == "GROQ_API_KEY"


# --- the wizard actually asks for both --------------------------------------

def test_the_wizard_asks_for_a_model_for_whichever_you_pick(monkeypatch):
    from email_workflow.cli import cli as cli_module

    answers = iter(["3", "qwen/qwen3.8-27b"])      # Groq, then its model
    monkeypatch.setattr(cli_module.Prompt, "ask",
                        lambda *a, **k: next(answers))
    monkeypatch.setattr(cli_module.Confirm, "ask", lambda *a, **k: True)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_already_there")

    prov, env, model = cli_module._pick_provider("Which one?")
    assert (prov, env, model) == ("groq", "GROQ_API_KEY", "qwen/qwen3.8-27b")


def test_the_backup_list_never_offers_the_one_you_already_chose(monkeypatch):
    from email_workflow.cli import cli as cli_module

    shown = []
    monkeypatch.setattr(cli_module.console, "print",
                        lambda *a, **k: shown.append(str(a[0]) if a else ""))
    # It asks for a choice and then a model; anything after that is the
    # default being accepted.
    answers = iter(["1", "gemini-3.1-flash-lite"])
    monkeypatch.setattr(cli_module.Prompt, "ask",
                        lambda *a, **k: next(answers, ""))
    monkeypatch.setattr(cli_module.Confirm, "ask", lambda *a, **k: True)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")

    cli_module._pick_provider("Backup?", skip="groq")
    assert not any("Groq" in line for line in shown), (
        "offering the same provider as its own backup is not a backup"
    )
