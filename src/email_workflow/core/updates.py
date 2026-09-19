"""Bring this copy up to date with the project it came from.

The whole difficulty is that a copy of this app is not just code. It is code
plus the things that make it yours: your keys, your knowledge base, your
settings, the hour you chose for the daily run, and the record of what has
already been handled. `git pull` treats all of that as files to be overwritten
- `config.yaml` is in the repository, so a plain pull would silently reset your
mailbox settings, put `allow_send` back to false and change your schedule.

So this does not merge. It fetches the new code, copies it in file by file, and
never touches anything on the protected list. What is yours stays yours, and
the update is boring.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Where the code comes from. A different fork can point somewhere else with
# `update.source` in config.yaml.
DEFAULT_UPSTREAM = "https://github.com/wiktorskrabel89-byte/autonomous-email-workflow.git"

# Never overwritten by an update. Two kinds of thing: what you configured, and
# what the app recorded about your mail. Losing either would be worse than
# missing the update entirely.
PROTECTED = (
    ".env",
    "config.yaml",
    "known_facts.txt",
    "auth.json",
    "state.json",
    "audit.jsonl",
    "idempotency.json",
    "usage.jsonl",
    "demo_state.json",
    "demo_audit.jsonl",
    "demo_idempotency.json",
)

# Not code either: a stray virtualenv or build output in the source tree is not
# something to copy over someone's installation.
SKIP_DIRS = (".git", "__pycache__", ".pytest_cache", ".venv", "venv",
             "build", "dist", "node_modules")

STATE_FILE = ".update-state.json"


def _git(args: List[str], cwd: Optional[Path] = None, timeout: int = 120) -> Tuple[bool, str]:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
        return done.returncode == 0, (done.stdout or "") + (done.stderr or "")
    except FileNotFoundError:
        return False, "git is not installed."
    except Exception as e:  # a timeout, a broken network
        return False, str(e)


def upstream_head(url: str = DEFAULT_UPSTREAM) -> Optional[str]:
    """The newest commit upstream, without downloading anything.

    ls-remote is a single cheap request, so checking for updates costs nothing
    and can be done often.
    """
    ok, out = _git(["ls-remote", url, "HEAD"], timeout=45)
    if not ok or not out.strip():
        return None
    first = out.strip().splitlines()[0].split()
    return first[0] if first and len(first[0]) == 40 else None


def read_state(project_root: Path) -> Dict:
    path = project_root / STATE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(project_root: Path, sha: str, source: str) -> None:
    try:
        (project_root / STATE_FILE).write_text(
            json.dumps({"commit": sha, "source": source}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def update_available(project_root: Path, url: str = DEFAULT_UPSTREAM) -> Tuple[bool, Optional[str], str]:
    """(there is something new, the upstream commit, a line explaining).

    A copy that has never been updated has nothing recorded, and then anything
    upstream counts as new - which is the honest answer for a fresh clone.
    """
    head = upstream_head(url)
    if head is None:
        return False, None, "Could not reach the project to check for updates."

    known = read_state(project_root).get("commit")
    if known == head:
        return False, head, "You are on the newest version."
    if not known:
        return True, head, "This copy has never been updated from the project."
    return True, head, "There are new changes."


def fetch_upstream(url: str, into: Path, timeout: int = 300) -> Tuple[bool, str]:
    """A shallow clone of the newest code, somewhere temporary."""
    ok, out = _git(["clone", "--depth", "1", url, str(into)], timeout=timeout)
    return ok, out


def changed_files(new_tree: Path, project_root: Path) -> List[str]:
    """Which files an update would actually change, protected ones excluded.

    Shown before anything is written: an update that lists what it will touch
    is one you can say no to.
    """
    changed = []
    for source in sorted(new_tree.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(new_tree)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if relative.as_posix() in PROTECTED or relative.name in PROTECTED:
            continue
        target = project_root / relative
        if not target.exists():
            changed.append(relative.as_posix() + "  (new)")
            continue
        try:
            if source.read_bytes() != target.read_bytes():
                changed.append(relative.as_posix())
        except OSError:
            changed.append(relative.as_posix())
    return changed


def files_to_copy(new_tree: Path) -> List[Path]:
    """Every file an update would deliver, protected names already removed.

    One place decides what is copyable, so counting for a progress bar and
    doing the copying can never disagree about what is about to happen.
    """
    wanted = []
    for source in sorted(new_tree.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(new_tree)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if relative.as_posix() in PROTECTED or relative.name in PROTECTED:
            continue
        wanted.append(relative)
    return wanted


def apply_update(new_tree: Path, project_root: Path, on_file=None) -> Tuple[int, List[str]]:
    """Copy the new code in. Returns (how many files, what went wrong).

    Deliberately copy-in rather than replace-the-folder: a file you added
    yourself is left alone, and nothing on the protected list is opened at all.

    on_file(relative_path) is called after each one, so a caller can draw a
    progress bar without this module knowing anything about how it is drawn.
    """
    copied, problems = 0, []
    for relative in files_to_copy(new_tree):
        target = project_root / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(new_tree / relative, target)
            copied += 1
        except OSError as e:
            problems.append(f"{relative.as_posix()}: {e}")
        if on_file:
            on_file(relative)
    return copied, problems


def locally_modified(project_root: Path) -> List[str]:
    """Tracked files changed here and not committed.

    An update replaces files wholesale. If you have been editing the code -
    fixing something yourself, or mid-change - those edits are simply gone, and
    an "update" that quietly deletes your own work is worse than no update.
    Anything not under git cannot be checked, and then the honest answer is an
    empty list rather than a false reassurance.
    """
    ok, out = _git(["status", "--porcelain"], cwd=project_root)
    if not ok:
        return []
    changed = []
    for raw in out.splitlines():
        if len(raw) < 4:
            continue
        marks, name = raw[:2], raw[3:].strip().strip('"')
        if "?" in marks:          # untracked: an update never deletes those
            continue
        changed.append(name)
    return changed


def keep_your_schedule(project_root: Path) -> Optional[str]:
    """The cron line this copy runs on, read before an update replaces it.

    The workflow file is code and has to be updated, but the time inside it is
    a choice somebody made. An update that silently moved the daily run to
    whatever hour upstream happens to ship would be the same bug as one that
    reset your settings, just harder to notice - the report would simply start
    arriving at the wrong time.
    """
    from email_workflow.core.scheduling import cron_in_workflow

    path = project_root / ".github" / "workflows" / "email-workflow.yml"
    if not path.exists():
        return None
    try:
        return cron_in_workflow(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def restore_your_schedule(project_root: Path, cron: Optional[str]) -> bool:
    """Put the time back after the workflow file has been replaced."""
    from email_workflow.core.scheduling import cron_in_workflow, workflow_with_cron

    if not cron:
        return False
    path = project_root / ".github" / "workflows" / "email-workflow.yml"
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        if cron_in_workflow(text) == cron:
            return False
        path.write_text(workflow_with_cron(text, cron), encoding="utf-8")
        return True
    except OSError:
        return False


def recent_subjects(new_tree: Path, limit: int = 15) -> List[str]:
    """What changed upstream, in the words of whoever changed it."""
    ok, out = _git(["log", f"-{limit}", "--pretty=format:%s"], cwd=new_tree)
    if not ok:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]
