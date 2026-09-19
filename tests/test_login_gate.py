"""Tests for the CLI login gate itself.

Driven by faking the prompts, because a masked password prompt needs a real
terminal and cannot be fed from a pipe.
"""

import pytest
import typer

from email_workflow.cli import cli as cli_module
from email_workflow.core.auth import AuthManager


class Answers:
    """Feeds scripted answers to Prompt.ask / Confirm.ask."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions = []

    def ask(self, question, *args, **kwargs):
        self.questions.append(str(question))
        if not self.answers:
            raise AssertionError(f"ran out of scripted answers at: {question}")
        return self.answers.pop(0)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Point the whole CLI at a temp project folder.

    Both modules must be patched: cli.py and auth.py each import
    resolve_project_file by value, so patching one still lets the other write
    into the real project directory.
    """
    (tmp_path / "config.yaml").write_text(
        "security:\n"
        "  require_login: true\n"
        "  max_login_attempts: 3\n"
        "  store: auth.json\n",
        encoding="utf-8",
    )
    resolve = lambda name: tmp_path / str(name)
    monkeypatch.setattr(cli_module, "resolve_project_file", resolve)
    monkeypatch.setattr(
        "email_workflow.core.auth.resolve_project_file", resolve
    )
    return tmp_path


def test_the_sandbox_really_is_isolated(sandbox):
    """Guard: a bug in this fixture must not write into the real project."""
    AuthManager(store_path="auth.json").set_credentials("x", "passwordpassword")
    assert (sandbox / "auth.json").exists()


def script(monkeypatch, *answers) -> Answers:
    answers_obj = Answers(*answers)
    monkeypatch.setattr(cli_module.Prompt, "ask", answers_obj.ask)
    return answers_obj


# --- first run --------------------------------------------------------------

def test_first_run_creates_a_login(sandbox, monkeypatch):
    script(monkeypatch, "testuser", "hunter2hunter", "hunter2hunter")
    cli_module._require_login()

    auth = AuthManager(store_path=str(sandbox / "auth.json"))
    assert auth.is_configured()
    assert auth.verify("testuser", "hunter2hunter")


def test_mismatched_passwords_are_asked_again(sandbox, monkeypatch):
    answers = script(
        monkeypatch,
        "testuser", "firstpassword", "different",   # mismatch -> retry
        "testuser", "secondpassword", "secondpassword",
    )
    cli_module._require_login()

    auth = AuthManager(store_path=str(sandbox / "auth.json"))
    assert auth.verify("testuser", "secondpassword")
    assert len(answers.questions) == 6, "it should have asked a second time"


def test_a_too_short_password_is_asked_again(sandbox, monkeypatch):
    script(
        monkeypatch,
        "testuser", "short", "short",               # too short -> retry
        "testuser", "longenoughpassword", "longenoughpassword",
    )
    cli_module._require_login()

    auth = AuthManager(store_path=str(sandbox / "auth.json"))
    assert auth.verify("testuser", "longenoughpassword")


# --- returning user ---------------------------------------------------------

def test_correct_login_is_let_in(sandbox, monkeypatch):
    AuthManager(store_path=str(sandbox / "auth.json")).set_credentials(
        "testuser", "hunter2hunter"
    )
    script(monkeypatch, "testuser", "hunter2hunter")
    cli_module._require_login()  # must not raise


def test_wrong_password_is_retried_then_allowed(sandbox, monkeypatch):
    AuthManager(store_path=str(sandbox / "auth.json")).set_credentials(
        "testuser", "hunter2hunter"
    )
    script(monkeypatch, "testuser", "nope", "testuser", "hunter2hunter")
    cli_module._require_login()  # second attempt succeeds


def test_too_many_wrong_attempts_closes_the_app(sandbox, monkeypatch):
    AuthManager(store_path=str(sandbox / "auth.json")).set_credentials(
        "testuser", "hunter2hunter"
    )
    script(
        monkeypatch,
        "testuser", "nope1",
        "testuser", "nope2",
        "testuser", "nope3",
    )
    with pytest.raises(typer.Exit) as excinfo:
        cli_module._require_login()
    assert excinfo.value.exit_code == 1


def test_attempt_limit_is_taken_from_config(sandbox, monkeypatch):
    (sandbox / "config.yaml").write_text(
        "security:\n  require_login: true\n  max_login_attempts: 1\n  store: auth.json\n",
        encoding="utf-8",
    )
    AuthManager(store_path=str(sandbox / "auth.json")).set_credentials(
        "testuser", "hunter2hunter"
    )
    answers = script(monkeypatch, "testuser", "wrong")

    with pytest.raises(typer.Exit):
        cli_module._require_login()
    assert len(answers.questions) == 2, "only one attempt should have been offered"


# --- the off switch ---------------------------------------------------------

def test_login_can_be_turned_off_in_config(sandbox, monkeypatch):
    (sandbox / "config.yaml").write_text(
        "security:\n  require_login: false\n", encoding="utf-8"
    )

    def explode(*args, **kwargs):
        raise AssertionError("must not prompt when the login is disabled")

    monkeypatch.setattr(cli_module.Prompt, "ask", explode)
    cli_module._require_login()


def test_a_broken_config_still_asks_for_a_login(sandbox, monkeypatch):
    """Failing open would be the wrong default for a mailbox tool."""
    (sandbox / "config.yaml").write_text("this: is: not: valid: yaml:", encoding="utf-8")
    script(monkeypatch, "testuser", "hunter2hunter", "hunter2hunter")
    cli_module._require_login()

    assert AuthManager(store_path=str(sandbox / "auth.json")).is_configured()


# --- seeing what you type ---------------------------------------------------

class RecordingPrompt:
    """Captures whether each prompt was asked with masking on."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def ask(self, question, *args, **kwargs):
        self.asked.append((str(question), kwargs.get("password", False)))
        return self.answers.pop(0)


def record(monkeypatch, *answers) -> RecordingPrompt:
    prompt = RecordingPrompt(*answers)
    monkeypatch.setattr(cli_module.Prompt, "ask", prompt.ask)
    return prompt


def masking_of(prompt, needle):
    return [hidden for question, hidden in prompt.asked if needle in question]


def test_the_password_is_visible_while_creating_a_login(sandbox, monkeypatch):
    (sandbox / "config.yaml").write_text(
        "security:\n  require_login: true\n  store: auth.json\n"
        "  show_password_while_typing: true\n",
        encoding="utf-8",
    )
    prompt = record(monkeypatch, "testuser", "hunter2hunter", "hunter2hunter")
    cli_module._require_login()

    assert masking_of(prompt, "password") == [False, False], (
        "the password prompts should not be masked when the setting is on"
    )


def test_the_password_is_visible_when_logging_back_in(sandbox, monkeypatch):
    (sandbox / "config.yaml").write_text(
        "security:\n  require_login: true\n  store: auth.json\n"
        "  show_password_while_typing: true\n",
        encoding="utf-8",
    )
    AuthManager(store_path=str(sandbox / "auth.json")).set_credentials(
        "testuser", "hunter2hunter"
    )
    prompt = record(monkeypatch, "testuser", "hunter2hunter")
    cli_module._require_login()

    assert masking_of(prompt, "Password") == [False]


def test_it_can_still_be_hidden(sandbox, monkeypatch):
    (sandbox / "config.yaml").write_text(
        "security:\n  require_login: true\n  store: auth.json\n"
        "  show_password_while_typing: false\n",
        encoding="utf-8",
    )
    prompt = record(monkeypatch, "testuser", "hunter2hunter", "hunter2hunter")
    cli_module._require_login()

    assert masking_of(prompt, "password") == [True, True], (
        "turning the setting off must mask the prompts again"
    )


def test_the_username_is_never_masked(sandbox, monkeypatch):
    (sandbox / "config.yaml").write_text(
        "security:\n  require_login: true\n  store: auth.json\n"
        "  show_password_while_typing: false\n",
        encoding="utf-8",
    )
    prompt = record(monkeypatch, "testuser", "hunter2hunter", "hunter2hunter")
    cli_module._require_login()

    assert masking_of(prompt, "username") == [False]


def test_visible_by_default():
    from email_workflow.models.config import SecurityConfig

    assert SecurityConfig().show_password_while_typing is True
