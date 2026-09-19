"""Running on a schedule: on this computer, or on GitHub's.

The two things that must not go wrong here are the clock and the secrets. A
cron line an hour out means the report arrives at the wrong time every day; a
crontab rewritten carelessly destroys the user's other jobs; and a secret list
that sweeps up unrelated variables uploads things to GitHub that were never
meant to leave the machine.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from email_workflow.core.scheduling import (
    TASK_NAME,
    describe_drift,
    git_is_clean_of_secrets,
    install_hint,
    local_schedule_plan,
    merge_crontab,
    parse_time,
    repo_name_suggestion,
    secrets_from_env_file,
    utc_cron_for_local_time,
    workflow_with_cron,
)


# --- reading the time the user typed ----------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("7", (7, 0)),
    ("07", (7, 0)),
    ("7:30", (7, 30)),
    ("07:30", (7, 30)),
    ("23:59", (23, 59)),
    ("  8:05  ", (8, 5)),
    ("8.05", (8, 5)),        # people type a dot
    ("0:00", (0, 0)),
])
def test_times_people_actually_type(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("bad", ["", "   ", "25:00", "7:60", "half past six", "-1", "7:30:15"])
def test_a_time_that_is_not_a_time_is_refused(bad):
    with pytest.raises(ValueError):
        parse_time(bad)


# --- local wall clock -> UTC cron -------------------------------------------

def at_offset(hours):
    """Noon on a fixed day, at a fixed offset from UTC."""
    return datetime(2026, 9, 19, 12, 0, tzinfo=timezone(timedelta(hours=hours)))


def test_a_summer_morning_in_poland_becomes_the_right_utc_cron():
    """07:30 local at UTC+2 is 05:30 UTC. Getting this wrong by an hour is the
    single most likely bug here, and it would be invisible for a whole day."""
    assert utc_cron_for_local_time(7, 30, now=at_offset(2)) == "30 5 * * *"


def test_winter_offset_gives_a_different_cron():
    assert utc_cron_for_local_time(7, 30, now=at_offset(1)) == "30 6 * * *"


def test_utc_itself_needs_no_shifting():
    assert utc_cron_for_local_time(7, 30, now=at_offset(0)) == "30 7 * * *"


def test_an_early_hour_wraps_to_the_day_before():
    """00:30 at UTC+2 is 22:30 UTC. The hour must wrap, not go negative."""
    assert utc_cron_for_local_time(0, 30, now=at_offset(2)) == "30 22 * * *"


def test_a_late_hour_wraps_forward():
    assert utc_cron_for_local_time(23, 30, now=at_offset(-5)) == "30 4 * * *"


def test_a_naive_clock_is_still_handled():
    """datetime.now() with no tzinfo must not crash the whole command."""
    cron = utc_cron_for_local_time(9, 0, now=datetime(2026, 9, 19, 12, 0))
    assert cron.endswith("* * *") and len(cron.split()) == 5


def test_the_daylight_saving_shift_is_admitted_not_hidden():
    warning = describe_drift(7, 30, now=at_offset(2))
    assert "UTC+2" in warning
    assert "clocks change" in warning


def test_no_warning_when_there_is_nothing_to_warn_about():
    assert describe_drift(7, 30, now=at_offset(0)) == ""


# --- not destroying the user's other cron jobs ------------------------------

def test_our_line_is_added_without_touching_anything_else():
    existing = "0 3 * * * /usr/bin/backup.sh\n@reboot /usr/bin/thing\n"
    merged = merge_crontab(existing, "30 7 * * * run-me")
    assert "/usr/bin/backup.sh" in merged, "someone else's cron job was destroyed"
    assert "@reboot /usr/bin/thing" in merged
    assert "30 7 * * * run-me" in merged
    assert f"# {TASK_NAME}" in merged


def test_scheduling_twice_replaces_our_line_instead_of_stacking_it():
    first = merge_crontab("0 3 * * * /usr/bin/backup.sh\n", "30 7 * * * run-me")
    second = merge_crontab(first, "0 9 * * * run-me")
    assert second.count("run-me") == 1, "the old schedule was left behind"
    assert "0 9 * * * run-me" in second
    assert "30 7 * * * run-me" not in second
    assert "/usr/bin/backup.sh" in second


def test_an_empty_crontab_is_fine():
    merged = merge_crontab("", "30 7 * * * run-me")
    assert merged.strip().splitlines() == [f"# {TASK_NAME}", "30 7 * * * run-me"]


# --- what gets uploaded to GitHub -------------------------------------------

def test_only_the_apps_own_variables_are_uploaded(tmp_path):
    """A .env is a junk drawer. Uploading whatever is in it would send things
    to GitHub that were never meant to leave the machine."""
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "GEMINI_API_KEY=abc123\n"
        "GEMINI_API_KEY_2=def456\n"
        "GMAIL_APP_PASSWORD=app pass here\n"
        "MY_BANK_PIN=1234\n"
        "AWS_SECRET_ACCESS_KEY=nothing-to-do-with-this-app\n"
        "OPENAI_API_KEY=\n"
        'DISCORD_WEBHOOK_URL="https://hook"\n',
        encoding="utf-8",
    )
    found = secrets_from_env_file(env)

    assert found["GEMINI_API_KEY"] == "abc123"
    assert found["GEMINI_API_KEY_2"] == "def456"
    assert found["GMAIL_APP_PASSWORD"] == "app pass here", "spaces must survive"
    assert found["DISCORD_WEBHOOK_URL"] == "https://hook", "quotes must be stripped"
    assert "MY_BANK_PIN" not in found, "an unrelated secret was about to be uploaded"
    assert "AWS_SECRET_ACCESS_KEY" not in found
    assert "OPENAI_API_KEY" not in found, "an empty value is not a key"


def test_a_missing_env_file_is_not_an_error(tmp_path):
    assert secrets_from_env_file(tmp_path / "nope.env") == {}


# --- the workflow file ------------------------------------------------------

def test_the_chosen_time_replaces_the_cron_in_the_workflow():
    template = (
        "on:\n  schedule:\n"
        '    # a comment about timezones\n'
        '    - cron: "0 6 * * *"\n'
        "  workflow_dispatch:\n"
    )
    out = workflow_with_cron(template, "30 5 * * *")
    assert '- cron: "30 5 * * *"' in out
    assert '"0 6 * * *"' not in out
    assert "workflow_dispatch" in out, "the rest of the file must be untouched"
    assert "a comment about timezones" in out


def test_the_real_workflow_file_can_be_retimed():
    """Guards against the shipped file drifting away from what the regex expects."""
    path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "email-workflow.yml"
    if not path.exists():
        pytest.skip("no workflow file in this checkout")
    out = workflow_with_cron(path.read_text(encoding="utf-8"), "15 4 * * *")
    assert '- cron: "15 4 * * *"' in out


# --- per-machine plumbing ---------------------------------------------------

@pytest.mark.parametrize("system,kind", [
    ("windows", "schtasks"),
    ("macos", "launchd"),
    ("linux", "cron"),
])
def test_every_operating_system_gets_a_plan(system, kind):
    plan = local_schedule_plan(7, 30, "email-workflow run", Path("/projects/app"), system=system)
    assert plan["kind"] == kind
    assert plan["argv"], "a plan with nothing to run is not a plan"
    assert "07:30" in plan["explain"]
    assert "only runs while" in plan["explain"], (
        "the user has to be told this misses the run when the machine is off"
    )


def test_the_mac_plan_writes_a_plist_before_loading_it():
    plan = local_schedule_plan(7, 30, "email-workflow run", Path("/p"), system="macos")
    assert plan["files"], "launchd needs the plist on disk first"
    path, contents = plan["files"][0]
    assert str(path).endswith(".plist")
    assert "<integer>7</integer>" in contents and "<integer>30</integer>" in contents


def test_the_linux_plan_carries_the_line_for_the_crontab():
    plan = local_schedule_plan(7, 30, "email-workflow run", Path("/p"), system="linux")
    assert plan["stdin_line"].startswith("30 7 * * *")


@pytest.mark.parametrize("system", ["windows", "macos", "linux"])
def test_install_hints_exist_for_every_system(system):
    for tool in ("git", "gh"):
        hint = install_hint(tool, system)
        assert hint and "try again" not in hint, f"no {tool} hint for {system}"


def test_a_repo_name_github_will_accept():
    assert repo_name_suggestion(Path("/x/email-workflow-lab")) == "email-workflow-lab"
    assert " " not in repo_name_suggestion(Path("/x/my mail app"))
    assert repo_name_suggestion(Path("/x/!!!")) == "email-workflow-lab"


def test_secrets_sitting_untracked_are_reported(tmp_path):
    """Not a git repo at all: every secret file counts as exposed, because
    nothing is ignoring it yet."""
    (tmp_path / ".env").write_text("GEMINI_API_KEY=x", encoding="utf-8")
    (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
    exposed = git_is_clean_of_secrets(tmp_path)
    assert ".env" in exposed and "auth.json" in exposed
