"""Every gh command has to name the repository it means.

What he saw, on all six keys at once:

    could not set GEMINI_API_KEY: multiple remotes detected. please specify
    which repo to use by providing the -R, --repo argument

gh works the repository out from the remotes, and refuses outright as soon as
there is more than one. A second remote is an ordinary thing to have - this
folder grew one the day its code started being published to a second
repository - and from that moment every secret upload failed together.

The earlier bug here was the opposite: passing a BARE repo name where gh wants
OWNER/REPO, which silently uploaded nothing. So the rule is narrow: pass
--repo, and only ever with an owner in it.
"""

import pytest

from email_workflow.cli import cli as cli_module
from email_workflow.cli.cli import repo_from_remote_url


# --- reading OWNER/REPO off a remote ----------------------------------------

@pytest.mark.parametrize("url", [
    "https://github.com/wiktorskrabel89-byte/email-workflow-lab.git",
    "https://github.com/wiktorskrabel89-byte/email-workflow-lab",
    "git@github.com:wiktorskrabel89-byte/email-workflow-lab.git",
    "ssh://git@github.com/wiktorskrabel89-byte/email-workflow-lab.git",
    "  https://github.com/wiktorskrabel89-byte/email-workflow-lab.git\n",
])
def test_every_way_a_github_remote_is_written(url):
    assert repo_from_remote_url(url) == "wiktorskrabel89-byte/email-workflow-lab"


@pytest.mark.parametrize("url", [
    "",
    "https://gitlab.com/someone/thing.git",
    "/a/local/path",
    "https://github.com/onlyanowner",
])
def test_anything_that_is_not_a_github_repo_gives_nothing(url):
    assert repo_from_remote_url(url) == ""


# --- what gets put on the command line --------------------------------------

def run_gh(monkeypatch, full_name):
    seen = {}

    def fake_shell(argv, **kwargs):
        seen["argv"] = list(argv)
        return True, ""

    monkeypatch.setattr(cli_module, "_shell", fake_shell)
    cli_module._gh(["gh", "secret", "set", "GEMINI_API_KEY"], full_name)
    return seen["argv"]


def test_the_repository_is_named(monkeypatch):
    argv = run_gh(monkeypatch, "wiktorskrabel89-byte/email-workflow-lab")
    assert argv[-2:] == ["--repo", "wiktorskrabel89-byte/email-workflow-lab"]


def test_a_bare_name_is_never_passed(monkeypatch):
    """gh wants OWNER/REPO. A bare folder name is what lost every secret the
    first time this broke, so it is left off entirely instead."""
    argv = run_gh(monkeypatch, "email-workflow-lab")
    assert "--repo" not in argv


def test_no_name_at_all_is_left_to_gh(monkeypatch):
    assert "--repo" not in run_gh(monkeypatch, "")


# --- a failed upload is not the same as nothing being there -----------------

def secrets_result(monkeypatch, existing, failing=("GEMINI_API_KEY",)):
    printed = []

    def capture(*args, **kwargs):
        if not args:
            return
        first = args[0]
        # A Panel's own str() is its repr, so read what is inside it.
        printed.append(str(getattr(first, "renderable", first)))

    monkeypatch.setattr(cli_module.console, "print", capture)

    def fake_gh(argv, full_name, **kwargs):
        if "list" in argv:
            return True, "\n".join(f"{n}\t2026-09-19T20:29:16Z" for n in existing)
        name = argv[-1] if argv[-1] != "--repo" else argv[-3]
        return (name not in failing), "multiple remotes detected"

    monkeypatch.setattr(cli_module, "_gh", fake_gh)
    ok = cli_module._upload_secrets(
        None, {"GEMINI_API_KEY": "x", "GMAIL_ADDRESS": "y"}, "owner/repo"
    )
    return ok, " ".join(printed)


def test_a_key_that_is_already_set_is_not_reported_as_missing(monkeypatch):
    """His case exactly: the re-upload failed, and every key was already there
    from an earlier day - so the daily run was working the whole time."""
    ok, printed = secrets_result(
        monkeypatch, existing={"GEMINI_API_KEY", "GMAIL_ADDRESS"},
    )
    assert ok is True, "nothing is actually missing, so this is not a failure"
    assert "already set on GitHub" in printed
    assert "NOT working yet" not in printed


def test_a_key_that_really_is_missing_still_stops_it(monkeypatch):
    ok, printed = secrets_result(monkeypatch, existing=set())
    assert ok is False
    assert "NOT working yet" in printed


def test_a_missing_one_alongside_an_existing_one_is_still_a_failure(monkeypatch):
    ok, printed = secrets_result(
        monkeypatch, existing={"GMAIL_ADDRESS"},
        failing=("GEMINI_API_KEY", "GMAIL_ADDRESS"),
    )
    assert ok is False
    assert "already there from before" in printed, (
        "say which ones are fine, so the list is not read as all-bad"
    )
