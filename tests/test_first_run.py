"""The first run opens the wizard, and the wizard asks about you.

Before this, a fresh copy dropped you at a menu where every option failed for
want of a key and a mailbox, and you had to know to pick option 3. And the
wizard set up WHO it talks to without ever asking the two things that decide
whether it is any use: what to always tell you about, and what it may say
about you. With both empty it archives mail that mattered and answers every
question with "[NEEDS INPUT]".
"""

import pytest

from email_workflow.cli import cli as cli_module
from email_workflow.cli.cli import nothing_is_set_up
from email_workflow.models.config import AppConfig


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    yield


def gemini_config() -> AppConfig:
    config = AppConfig()
    config.ai.api.provider = "gemini"
    config.ai.api.api_key_env = "GEMINI_API_KEY"
    config.email.provider = "gmail"
    return config


# --- when the wizard should open on its own ---------------------------------

def test_a_fresh_copy_counts_as_not_set_up():
    assert nothing_is_set_up(gemini_config())


def test_a_key_on_its_own_is_not_enough(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "a-key")
    assert nothing_is_set_up(gemini_config()), "it still has no mailbox to read"


def test_a_mailbox_on_its_own_is_not_enough(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    assert nothing_is_set_up(gemini_config())


def test_with_both_it_never_interrupts_you_again(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "a-key")
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    assert not nothing_is_set_up(gemini_config())


def test_an_empty_key_does_not_count(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    assert nothing_is_set_up(gemini_config())


def test_someone_testing_on_purpose_is_left_alone():
    """A mock inbox or the fake provider needs no key and no mailbox, and
    opening a setup wizard over the top of that would be wrong."""
    config = gemini_config()
    config.ai.api.provider = "fake"
    assert not nothing_is_set_up(config)

    config = gemini_config()
    config.email.provider = "mock"
    assert not nothing_is_set_up(config)


# --- the two questions at the end of the wizard -----------------------------

class Answers:
    """Stands in for the person typing. Empty string = Enter = stop."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.asked = []

    def __call__(self, question, **kwargs):
        self.asked.append(str(question))
        return self.replies.pop(0) if self.replies else ""


def run_about_you(monkeypatch, tmp_path, replies):
    import yaml

    config_path = tmp_path / "config.yaml"
    config = AppConfig()
    answers = Answers(replies)
    monkeypatch.setattr(cli_module.Prompt, "ask", answers)

    facts_file = tmp_path / "known_facts.txt"
    facts_file.write_text("- Work Hours: 9 to 5\n", encoding="utf-8")

    class Facts:
        def load_facts(self):
            return facts_file.read_text(encoding="utf-8")

        def save_facts(self, text):
            facts_file.write_text(text, encoding="utf-8")

    monkeypatch.setattr(cli_module, "KnownFactsManager", lambda *a, **k: Facts())
    cli_module._ask_about_you(config, config_path)

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    return config, saved, facts_file.read_text(encoding="utf-8"), answers


def test_what_to_tell_you_about_is_saved_to_the_config(monkeypatch, tmp_path):
    config, saved, _, _ = run_about_you(
        monkeypatch, tmp_path,
        ["job offers and anything about my applications", "", ""],
    )
    assert config.automation.never_archive_about == [
        "job offers and anything about my applications"
    ]
    assert saved["automation"]["never_archive_about"] == [
        "job offers and anything about my applications"
    ], "it has to reach config.yaml, not just the object in memory"


def test_several_topics_are_kept(monkeypatch, tmp_path):
    config, _, _, _ = run_about_you(
        monkeypatch, tmp_path, ["job offers", "my landlord", "", ""],
    )
    assert config.automation.never_archive_about == ["job offers", "my landlord"]


def test_facts_are_added_without_touching_what_is_already_there(monkeypatch, tmp_path):
    """This file is a knowledge base someone builds up. Writing over the top of
    it has eaten one before."""
    _, _, facts, _ = run_about_you(
        monkeypatch, tmp_path, ["", "I am a freelance designer", ""],
    )
    assert "Work Hours: 9 to 5" in facts, "the existing fact must survive"
    assert "freelance designer" in facts


def test_pressing_enter_through_both_changes_nothing(monkeypatch, tmp_path):
    config, saved, facts, _ = run_about_you(monkeypatch, tmp_path, ["", ""])
    assert config.automation.never_archive_about == []
    assert facts == "- Work Hours: 9 to 5\n"
    assert saved == {}, "nothing to save means config.yaml is not rewritten"


def test_it_stops_asking_after_a_few(monkeypatch, tmp_path):
    """A prompt that loops until you give up is a trap in a first-run wizard."""
    # Real-looking answers, not single letters: "b" means "take me back" at
    # every question in the app now, so using it as filler here was testing
    # the wrong thing.
    _, _, _, answers = run_about_you(
        monkeypatch, tmp_path,
        ["job offers", "my landlord", "school", "I work 9 to 5",
         "I am a designer", "I live in Krakow", "extra", "more"],
    )
    assert len(answers.asked) == 6, "three topics and three facts, then move on"
