"""Tests that drive the real Typer app.

The other suites call functions directly. These go through actual command
dispatch, which is where wiring mistakes live - a callback that does not run, a
command that was never registered, a gate that only guards the menu.
"""

import pytest
from typer.testing import CliRunner

from email_workflow.cli import cli as cli_module
from email_workflow.core.auth import AuthManager

runner = CliRunner()


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """Point every module that touches project files at a temp folder."""
    resolve = lambda name: tmp_path / str(name)
    for module in (
        "email_workflow.cli.cli",
        "email_workflow.core.auth",
        "email_workflow.core.usage",
    ):
        monkeypatch.setattr(f"{module}.resolve_project_file", resolve, raising=False)
    return tmp_path


def write_config(path, require_login=True):
    (path / "config.yaml").write_text(
        f"security:\n"
        f"  require_login: {str(require_login).lower()}\n"
        f"  max_login_attempts: 3\n"
        f"  store: auth.json\n",
        encoding="utf-8",
    )


# --- every command is registered -------------------------------------------

def test_help_lists_the_commands_we_added():
    result = runner.invoke(cli_module.app, ["--help"])
    assert result.exit_code == 0
    for command in ("models", "usage", "passwd", "reset", "run", "demo"):
        assert command in result.output, f"{command} is not registered"


# --- the gate guards real subcommands, not just the menu -------------------

def test_a_subcommand_is_blocked_without_a_login(project):
    write_config(project, require_login=True)
    AuthManager(store_path="auth.json").set_credentials("testuser", "hunter2hunter")

    result = runner.invoke(cli_module.app, ["providers"], input="")
    assert result.exit_code != 0, "the login gate must guard subcommands too"
    assert "Google Gemini" not in result.output, "no command output before logging in"


def test_a_subcommand_runs_after_a_correct_login(project):
    write_config(project, require_login=True)
    AuthManager(store_path="auth.json").set_credentials("testuser", "hunter2hunter")

    result = runner.invoke(
        cli_module.app, ["providers"], input="testuser\nhunter2hunter\n"
    )
    assert result.exit_code == 0
    assert "Welcome back" in result.output


def test_a_wrong_password_three_times_stops_the_command(project):
    write_config(project, require_login=True)
    AuthManager(store_path="auth.json").set_credentials("testuser", "hunter2hunter")

    result = runner.invoke(
        cli_module.app,
        ["providers"],
        input="testuser\nwrong1\ntestuser\nwrong2\ntestuser\nwrong3\n",
    )
    assert result.exit_code == 1
    assert "Too many failed attempts" in result.output


def test_no_login_is_asked_for_when_it_is_turned_off(project):
    write_config(project, require_login=False)
    result = runner.invoke(cli_module.app, ["providers"], input="")
    assert result.exit_code == 0
    assert "Password" not in result.output


# --- reset ------------------------------------------------------------------

def test_reset_removes_personal_files_but_not_config(project):
    write_config(project, require_login=False)
    for name in ("auth.json", "known_facts.txt", "state.json", "usage.jsonl"):
        (project / name).write_text("private", encoding="utf-8")

    result = runner.invoke(cli_module.app, ["reset", "--yes"])

    assert result.exit_code == 0
    for name in ("auth.json", "known_facts.txt", "state.json", "usage.jsonl"):
        assert not (project / name).exists(), f"{name} should have been removed"
    assert (project / "config.yaml").exists(), "config.yaml must be left alone"


def test_reset_deletes_nothing_when_declined(project):
    write_config(project, require_login=False)
    (project / "known_facts.txt").write_text("private", encoding="utf-8")

    result = runner.invoke(cli_module.app, ["reset"], input="n\n")

    assert result.exit_code == 0
    assert (project / "known_facts.txt").exists(), "a declined reset must delete nothing"


def test_reset_on_a_clean_folder_says_so(project):
    write_config(project, require_login=False)
    result = runner.invoke(cli_module.app, ["reset", "--yes"])
    assert result.exit_code == 0
    assert "already clean" in result.output


# --- usage ------------------------------------------------------------------

def test_usage_runs_with_no_history(project):
    write_config(project, require_login=False)
    result = runner.invoke(cli_module.app, ["usage"])
    assert result.exit_code == 0
    assert "Nothing recorded today" in result.output


def test_usage_shows_what_was_recorded(project):
    write_config(project, require_login=False)
    from email_workflow.core.usage import UsageTracker

    tracker = UsageTracker(store_path="usage.jsonl")
    for _ in range(3):
        tracker.record("gemini", "gemini-3.1-flash-lite", 500, 100)
    tracker.record("gemini", "gemini-3.1-flash-lite", 0, 0, status="error", kind="quota")

    result = runner.invoke(cli_module.app, ["usage"])

    assert result.exit_code == 0
    assert "gemini" in result.output
    assert "1,800" in result.output or "1800" in result.output, "token total should show"


def test_usage_reset_needs_confirmation(project):
    write_config(project, require_login=False)
    from email_workflow.core.usage import UsageTracker

    tracker = UsageTracker(store_path="usage.jsonl")
    tracker.record("gemini", "m", 10, 10)

    result = runner.invoke(cli_module.app, ["usage", "--reset"], input="n\n")

    assert result.exit_code == 0
    assert (project / "usage.jsonl").exists(), "declining must keep the history"


# --- models -----------------------------------------------------------------

def test_models_rejects_an_unknown_provider(project):
    write_config(project, require_login=False)
    result = runner.invoke(cli_module.app, ["models", "--provider", "not-a-provider"])
    assert result.exit_code == 1
    assert "Unknown provider" in result.output


def test_models_explains_a_missing_key_without_a_traceback(project, monkeypatch):
    write_config(project, require_login=False)
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    result = runner.invoke(cli_module.app, ["models", "--provider", "groq"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "GROQ_API_KEY" in result.output


# --- unattended runs --------------------------------------------------------

def test_scheduled_runs_can_skip_the_login(project, monkeypatch):
    """A cron job or GitHub Action has nobody to type a password."""
    write_config(project, require_login=True)
    AuthManager(store_path="auth.json").set_credentials("testuser", "hunter2hunter")
    monkeypatch.setenv("EMAIL_WORKFLOW_DISABLE_LOGIN", "1")

    result = runner.invoke(cli_module.app, ["providers"], input="")

    assert result.exit_code == 0
    assert "Password" not in result.output


def test_the_login_still_applies_without_that_variable(project, monkeypatch):
    write_config(project, require_login=True)
    AuthManager(store_path="auth.json").set_credentials("testuser", "hunter2hunter")
    monkeypatch.delenv("EMAIL_WORKFLOW_DISABLE_LOGIN", raising=False)

    result = runner.invoke(cli_module.app, ["providers"], input="")
    assert result.exit_code != 0


@pytest.mark.parametrize("value", ["0", "no", "", "maybe"])
def test_only_an_explicit_yes_disables_the_login(project, monkeypatch, value):
    write_config(project, require_login=True)
    AuthManager(store_path="auth.json").set_credentials("testuser", "hunter2hunter")
    monkeypatch.setenv("EMAIL_WORKFLOW_DISABLE_LOGIN", value)

    result = runner.invoke(cli_module.app, ["providers"], input="")
    assert result.exit_code != 0, f"{value!r} must not switch the login off"


# --- scheduling a daily run -------------------------------------------------

def no_shell(monkeypatch):
    """Record external commands instead of running them."""
    calls = []
    monkeypatch.setattr(cli_module, "_shell",
                        lambda argv, **kw: calls.append(list(argv)) or (True, ""))
    return calls


def test_schedule_is_registered():
    names = {c.name or c.callback.__name__ for c in cli_module.app.registered_commands}
    assert "schedule" in names


def test_the_github_path_stops_when_the_tool_is_missing(project, monkeypatch):
    write_config(project, require_login=False)
    calls = no_shell(monkeypatch)
    monkeypatch.setattr(cli_module, "tool_available", lambda name: False)

    result = runner.invoke(cli_module.app, ["schedule", "--where", "github", "--at", "07:30"])

    assert result.exit_code == 1
    assert "not installed" in result.output
    assert not calls, "nothing should have been run without the tools present"


def test_the_github_path_refuses_to_upload_unprotected_secrets(project, monkeypatch):
    """The one that really matters: a private repo is still a copy of your
    files on someone else's computer, and one click can make it public."""
    write_config(project, require_login=False)
    calls = no_shell(monkeypatch)
    monkeypatch.setattr(cli_module, "tool_available", lambda name: True)
    monkeypatch.setattr(cli_module, "git_is_clean_of_secrets", lambda root: [".env", "auth.json"])
    (project / ".git").mkdir()

    result = runner.invoke(cli_module.app, ["schedule", "--where", "github", "--at", "07:30"])

    assert result.exit_code == 1
    assert ".env" in result.output
    pushed = [c for c in calls if "push" in c or "create" in c]
    assert not pushed, "it was about to publish unprotected secrets"


def test_nothing_happens_on_this_computer_until_you_say_yes(project, monkeypatch):
    write_config(project, require_login=False)
    calls = no_shell(monkeypatch)

    result = runner.invoke(
        cli_module.app, ["schedule", "--where", "computer", "--at", "07:30"], input="n\n"
    )

    assert result.exit_code == 0
    # rich wraps the panel, so match a fragment short enough to survive it
    assert "misses the run" in result.output, "the user was not warned"
    assert "Nothing changed" in result.output
    assert not calls, "the task was created without being confirmed"


def test_a_time_that_is_not_a_time_is_refused(project, monkeypatch):
    write_config(project, require_login=False)
    no_shell(monkeypatch)
    result = runner.invoke(
        cli_module.app, ["schedule", "--where", "computer", "--at", "half past six"]
    )
    assert result.exit_code == 1


def test_an_unknown_destination_is_refused(project, monkeypatch):
    write_config(project, require_login=False)
    no_shell(monkeypatch)
    result = runner.invoke(
        cli_module.app, ["schedule", "--where", "the moon", "--at", "07:30"]
    )
    assert result.exit_code == 1


def test_menu_option_two_opens_the_real_scheduler(project, monkeypatch):
    """It used to block the window until the chosen hour and run once - close
    the terminal and nothing happened. The menu must reach the scheduler that
    actually registers a daily task."""
    write_config(project, require_login=False)
    opened = []
    monkeypatch.setattr(cli_module, "schedule",
                        lambda at=None, where=None: opened.append((at, where)))

    result = runner.invoke(cli_module.app, [], input="2\n\n12\n")

    assert opened == [(None, None)], "option 2 did not open the scheduler"
    assert "Set Up a Daily Run" in result.output


def test_the_menu_no_longer_offers_the_blocking_wait(project):
    write_config(project, require_login=False)
    result = runner.invoke(cli_module.app, [], input="12\n")
    assert "Schedule Daily Run at Specific Time" not in result.output, (
        "the old label promised a schedule it did not deliver"
    )
