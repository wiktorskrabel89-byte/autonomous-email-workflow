"""Updating the code without touching what makes this copy yours.

A copy of this app is code plus your keys, your knowledge base, your settings,
the hour you chose and the record of what has already been handled. `config.yaml`
is in the repository, so a plain `git pull` would quietly put `allow_send` back
to false, reset your trusted categories and change your schedule. These tests
exist so that can never happen.
"""

import json
from pathlib import Path

import pytest

from email_workflow.core import updates
from email_workflow.core.updates import (
    PROTECTED,
    apply_update,
    changed_files,
    read_state,
    update_available,
    write_state,
)


def build_tree(root: Path, files: dict):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture()
def project(tmp_path):
    """A copy someone has been using: their settings, their data, old code."""
    return build_tree(tmp_path / "mine", {
        "config.yaml": "email:\n  allow_send: true\n  account_ref: me@example.com\n",
        ".env": "GEMINI_API_KEY=my-real-key\n",
        "known_facts.txt": "- User Name: Me\n- Work Hours: 9 to 5\n",
        "auth.json": '{"username": "me"}',
        "state.json": '{"threads": 12}',
        "audit.jsonl": '{"event": "old"}\n',
        "usage.jsonl": '{"calls": 40}\n',
        "idempotency.json": '{"done": true}',
        "src/email_workflow/core/pipeline.py": "# the old version\n",
        "README.md": "old readme\n",
    })


@pytest.fixture()
def upstream(tmp_path):
    """What the project looks like now: new code, and the shipped defaults."""
    return build_tree(tmp_path / "new", {
        "config.yaml": "email:\n  allow_send: false\n  account_ref: user@example.com\n",
        "known_facts.txt": "- User Name: [your name]\n",
        "src/email_workflow/core/pipeline.py": "# the new version\n",
        "src/email_workflow/core/updates.py": "# brand new file\n",
        "README.md": "new readme\n",
        ".git/config": "should never be copied\n",
        "__pycache__/x.pyc": "junk\n",
    })


# --- what an update would touch ---------------------------------------------

def test_your_settings_are_never_listed_as_changing(project, upstream):
    listed = " ".join(changed_files(upstream, project))
    assert "config.yaml" not in listed, "an update was about to reset your settings"
    assert "known_facts.txt" not in listed, "it was about to reset your knowledge base"


def test_new_and_changed_code_is_listed(project, upstream):
    listed = changed_files(upstream, project)
    assert any("pipeline.py" in line for line in listed)
    assert any("updates.py" in line and "(new)" in line for line in listed)
    assert any("README.md" in line for line in listed)


def test_an_identical_file_is_not_listed(project, upstream):
    (project / "README.md").write_text("new readme\n", encoding="utf-8")
    assert not any("README.md" in line for line in changed_files(upstream, project))


def test_git_and_build_junk_is_never_listed(project, upstream):
    listed = " ".join(changed_files(upstream, project))
    assert ".git" not in listed
    assert "__pycache__" not in listed


# --- what an update actually does -------------------------------------------

def test_the_code_is_updated(project, upstream):
    apply_update(upstream, project)
    assert "new version" in (project / "src/email_workflow/core/pipeline.py").read_text(encoding="utf-8")
    assert (project / "src/email_workflow/core/updates.py").exists(), "a new file was not delivered"


def test_everything_of_yours_survives(project, upstream):
    """The test this whole module exists for."""
    apply_update(upstream, project)

    assert "allow_send: true" in (project / "config.yaml").read_text(encoding="utf-8"), (
        "the update reset your sending setting"
    )
    assert "me@example.com" in (project / "config.yaml").read_text(encoding="utf-8")
    assert "my-real-key" in (project / ".env").read_text(encoding="utf-8")
    assert "User Name: Me" in (project / "known_facts.txt").read_text(encoding="utf-8")
    assert '"username": "me"' in (project / "auth.json").read_text(encoding="utf-8")
    assert '"threads": 12' in (project / "state.json").read_text(encoding="utf-8")
    assert '"done": true' in (project / "idempotency.json").read_text(encoding="utf-8")
    assert '"calls": 40' in (project / "usage.jsonl").read_text(encoding="utf-8")
    assert '"event": "old"' in (project / "audit.jsonl").read_text(encoding="utf-8")


def test_every_protected_name_really_is_protected(project, upstream):
    """Guards the list itself: adding a name to PROTECTED must actually stop it
    being copied, or the list is decoration."""
    for name in PROTECTED:
        (upstream / name).write_text("FROM UPSTREAM", encoding="utf-8")
        (project / name).write_text("MINE", encoding="utf-8")

    apply_update(upstream, project)

    for name in PROTECTED:
        assert (project / name).read_text(encoding="utf-8") == "MINE", f"{name} was overwritten"


def test_git_internals_are_not_copied(project, upstream):
    apply_update(upstream, project)
    assert not (project / ".git" / "config").exists(), "it copied a git directory in"
    assert not (project / "__pycache__" / "x.pyc").exists()


def test_a_file_you_added_yourself_is_left_alone(project, upstream):
    (project / "my_notes.txt").write_text("mine", encoding="utf-8")
    apply_update(upstream, project)
    assert (project / "my_notes.txt").read_text(encoding="utf-8") == "mine"


def test_it_says_how_much_it_did(project, upstream):
    copied, problems = apply_update(upstream, project)
    assert copied >= 3
    assert problems == []


# --- knowing whether there is anything new ----------------------------------

def test_a_fresh_copy_counts_as_out_of_date(project, monkeypatch):
    monkeypatch.setattr(updates, "upstream_head", lambda url=None: "a" * 40)
    newer, sha, why = update_available(project)
    assert newer is True
    assert sha == "a" * 40
    assert "never been updated" in why


def test_being_up_to_date_is_recognised(project, monkeypatch):
    write_state(project, "b" * 40, "somewhere")
    monkeypatch.setattr(updates, "upstream_head", lambda url=None: "b" * 40)
    newer, _, why = update_available(project)
    assert newer is False
    assert "newest version" in why


def test_a_newer_commit_is_noticed(project, monkeypatch):
    write_state(project, "b" * 40, "somewhere")
    monkeypatch.setattr(updates, "upstream_head", lambda url=None: "c" * 40)
    newer, sha, why = update_available(project)
    assert newer is True and sha == "c" * 40
    assert "new changes" in why


def test_no_network_is_not_an_update(project, monkeypatch):
    """Offline must never read as "there is something new" - that would send
    someone chasing an update that does not exist."""
    monkeypatch.setattr(updates, "upstream_head", lambda url=None: None)
    newer, sha, why = update_available(project)
    assert newer is False and sha is None
    assert "Could not reach" in why


def test_the_recorded_version_survives_a_round_trip(project):
    write_state(project, "d" * 40, "https://example.com/repo.git")
    assert read_state(project)["commit"] == "d" * 40
    assert read_state(project)["source"] == "https://example.com/repo.git"


def test_a_corrupt_state_file_is_not_fatal(project):
    (project / ".update-state.json").write_text("{ not json", encoding="utf-8")
    assert read_state(project) == {}


# --- the hour you chose is a choice, not code -------------------------------

def workflow(root: Path, cron: str):
    path = root / ".github" / "workflows" / "email-workflow.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('on:\n  schedule:\n    - cron: "%s"\n' % cron, encoding="utf-8")
    return path


def test_an_update_does_not_move_your_daily_run(project, upstream):
    """The workflow file has to be updated - but not the time inside it."""
    from email_workflow.core.updates import keep_your_schedule, restore_your_schedule

    mine = workflow(project, "0 8 * * *")      # the hour this person chose
    workflow(upstream, "0 6 * * *")            # whatever upstream ships

    kept = keep_your_schedule(project)
    apply_update(upstream, project)
    assert '"0 6 * * *"' in mine.read_text(encoding="utf-8"), "test setup"

    assert restore_your_schedule(project, kept) is True
    assert '"0 8 * * *"' in mine.read_text(encoding="utf-8"), (
        "the update silently moved the daily run to a different hour"
    )


def test_nothing_to_restore_is_not_an_error(project):
    from email_workflow.core.updates import keep_your_schedule, restore_your_schedule

    assert keep_your_schedule(project) is None
    assert restore_your_schedule(project, None) is False
    assert restore_your_schedule(project, "0 8 * * *") is False


def test_the_same_hour_is_left_alone(project):
    from email_workflow.core.updates import restore_your_schedule

    workflow(project, "0 8 * * *")
    assert restore_your_schedule(project, "0 8 * * *") is False, "rewrote for nothing"
