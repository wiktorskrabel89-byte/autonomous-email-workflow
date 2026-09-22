"""Run the app on a schedule - on this computer, or on GitHub's.

Two honest options, and the difference matters more than it looks:

* **This computer.** Windows Task Scheduler, a launchd agent on macOS, or cron
  on Linux. Free and private, but it only fires while the machine is awake. A
  laptop that is shut at 7am simply misses the run.
* **GitHub Actions.** Free, needs no server, and runs whether your computer is
  on or not - which is the whole point for a morning mail triage.

Everything here is built rather than executed: the plan is a value the caller
can show the user, confirm, and only then run. That keeps the irreversible
parts - pushing code to GitHub, uploading API keys as secrets - behind an
explicit yes, and it makes all of this testable without touching the machine.
"""

import os
import platform
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# The variables worth copying into GitHub secrets. Anything absent from .env is
# skipped, so an unused provider costs nothing.
SECRET_NAMES = (
    "GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
    "GROQ_API_KEY", "GROQ_API_KEY_2",
    "OPENAI_API_KEY", "OPENAI_API_KEY_2",
    "OPENROUTER_API_KEY",
    "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
    "IMAP_SERVER", "SMTP_SERVER", "SMTP_PORT",
    "DISCORD_WEBHOOK_URL",
    # Without this the scheduled run knows nothing about you.
    "KNOWN_FACTS",
    # Your settings: the repo no longer carries config.yaml.
    "EMAIL_WORKFLOW_CONFIG",
    "NOTIFICATION_SENDER_EMAIL", "NOTIFICATION_SENDER_PASSWORD",
    "NOTIFICATION_RECIPIENT_EMAIL",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_FROM", "TWILIO_WHATSAPP_TO",
)

TASK_NAME = "EmailWorkflowDaily"


# --- the time ---------------------------------------------------------------

def parse_time(text: str) -> Tuple[int, int]:
    """"7", "7:30", "07:30" -> (7, 30). Raises ValueError with a usable message."""
    cleaned = (text or "").strip().replace(".", ":")
    if not re.fullmatch(r"\d{1,2}(:\d{1,2})?", cleaned):
        raise ValueError(f"'{text}' is not a time. Write it like 07:30.")
    parts = cleaned.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"'{text}' is not a real time of day.")
    return hour, minute


def utc_cron_for_local_time(
    hour: int, minute: int, now: Optional[datetime] = None
) -> str:
    """A daily GitHub cron line for a local wall-clock time.

    GitHub cron is always UTC, so the local time has to be converted. The
    offset used is the one in force *now*: a fixed cron line cannot follow
    daylight saving, so a schedule set in winter fires an hour later in summer.
    That is a property of GitHub's scheduler, not something this can fix - so
    it is said out loud rather than hidden.
    """
    reference = now or datetime.now().astimezone()
    if reference.tzinfo is None:
        reference = reference.astimezone()
    local_target = reference.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    as_utc = local_target.astimezone(timezone.utc)
    return f"{as_utc.minute} {as_utc.hour} * * *"


def describe_drift(hour: int, minute: int, now: Optional[datetime] = None) -> str:
    """One line warning about the daylight-saving shift, or "" if there is none."""
    reference = now or datetime.now().astimezone()
    if reference.tzinfo is None:
        reference = reference.astimezone()
    offset = reference.utcoffset()
    if offset is None or offset.total_seconds() == 0:
        return ""
    return (
        f"Your clock is {_offset_text(offset)} from UTC today. GitHub schedules "
        f"in UTC only, so when the clocks change this will fire an hour earlier "
        f"or later until you set it again."
    )


def _offset_text(offset) -> str:
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 60}" + (f":{total % 60:02d}" if total % 60 else "")


# --- what is installed ------------------------------------------------------

def tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def current_system() -> str:
    """"windows", "macos", "linux", or the raw name for anything else."""
    raw = platform.system().lower()
    return {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(raw, raw)


def install_hint(tool: str, system: Optional[str] = None) -> str:
    """How to install a missing tool on this machine, in one line."""
    system = system or current_system()
    hints = {
        "git": {
            "windows": "winget install Git.Git",
            "macos": "brew install git",
            "linux": "sudo apt install git   (or your distro's package manager)",
        },
        "gh": {
            "windows": "winget install GitHub.cli",
            "macos": "brew install gh",
            "linux": "sudo apt install gh   (see https://github.com/cli/cli#installation)",
        },
    }
    return hints.get(tool, {}).get(system, f"Install '{tool}' and try again.")


# --- scheduling on this computer --------------------------------------------

def local_schedule_plan(
    hour: int,
    minute: int,
    command: str,
    working_dir: Path,
    system: Optional[str] = None,
) -> Dict:
    """How this machine would register a daily task. Built, never run here.

    Returns {kind, argv, files, explain}. `files` are (path, contents) pairs to
    write first - launchd needs a plist on disk, the other two do not.
    """
    system = system or current_system()
    clock = f"{hour:02d}:{minute:02d}"

    if system == "windows":
        return {
            "kind": "schtasks",
            "argv": [
                "schtasks", "/Create", "/TN", TASK_NAME,
                "/TR", f'cmd /c cd /d "{working_dir}" && {command}',
                "/SC", "DAILY", "/ST", clock, "/F",
            ],
            "files": [],
            "explain": (
                f"Windows Task Scheduler, task '{TASK_NAME}', daily at {clock}. "
                f"It only runs while the computer is on."
            ),
        }

    if system == "macos":
        label = "com.emailworkflow.daily"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        contents = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-c</string>
    <string>cd '{working_dir}' &amp;&amp; {command}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
"""
        return {
            "kind": "launchd",
            "argv": ["launchctl", "load", "-w", str(plist)],
            "files": [(plist, contents)],
            "explain": (
                f"A launchd agent at {plist}, daily at {clock}. "
                f"It only runs while the Mac is awake."
            ),
        }

    # linux and anything unix-shaped
    line = f"{minute} {hour} * * * cd '{working_dir}' && {command}"
    return {
        "kind": "cron",
        "argv": ["crontab", "-"],
        "stdin_line": line,
        "files": [],
        "explain": (
            f"A crontab entry, daily at {clock}: it only runs while the machine "
            f"is on. For a machine that sleeps, use systemd timers with "
            f"Persistent=true instead."
        ),
    }


def existing_crontab() -> str:
    """Whatever the user already has, so a new line is added and not lost."""
    try:
        done = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=15
        )
        return done.stdout if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def merge_crontab(existing: str, line: str, marker: str = TASK_NAME) -> str:
    """Add or replace our line, leaving every other entry untouched.

    Rewriting a crontab is destructive: the user's other jobs live in the same
    file, so ours is tagged and only the tagged line is ever replaced.
    """
    tag = f"# {marker}"
    kept = []
    skip_next = False
    for raw in existing.splitlines():
        if skip_next:
            skip_next = False
            continue
        if raw.strip() == tag:
            skip_next = True      # drop the tag and the line under it
            continue
        kept.append(raw)
    while kept and not kept[-1].strip():
        kept.pop()
    kept += [tag, line]
    return "\n".join(kept) + "\n"


# --- secrets for the cloud path ---------------------------------------------

def secrets_from_env_file(env_path: Path) -> Dict[str, str]:
    """The values worth uploading, read from .env.

    Only names this app actually uses are returned, so an unrelated variable
    sitting in the same file is never sent to GitHub.
    """
    found: Dict[str, str] = {}
    if not env_path.exists():
        return found
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        name, value = raw.split("=", 1)
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name in SECRET_NAMES and value:
            found[name] = value
    return found


# Personal files the repository deliberately does not carry, and the secret
# each one travels as. Without them a cloud run is a different app: no idea
# who you are, and the shipped settings rather than yours.
FILE_SECRETS = {
    "KNOWN_FACTS": "known_facts.txt",
    "EMAIL_WORKFLOW_CONFIG": "config.yaml",
}


def secrets_from_files(project_root: Path) -> Dict[str, str]:
    """Your gitignored files, as secrets the scheduled run can read."""
    found: Dict[str, str] = {}
    for name, filename in FILE_SECRETS.items():
        path = project_root / filename
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            found[name] = text
    return found


def repo_name_suggestion(project_root: Path) -> str:
    """A repo name GitHub will accept, based on the folder."""
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", project_root.name).strip("-")
    return base or "email-workflow-lab"


def workflow_with_cron(template: str, cron: str) -> str:
    """Put the chosen time into the workflow file, leaving the rest alone."""
    return re.sub(
        r'(\n\s*-\s*cron:\s*)"[^"]*"',
        lambda m: f'{m.group(1)}"{cron}"',
        template,
        count=1,
    )


def git_is_clean_of_secrets(project_root: Path) -> List[str]:
    """Files that must never be committed but currently would be.

    A private repo is still a copy of these on someone else's computer, and a
    repo can be made public by one click later.
    """
    must_be_ignored = [".env", "auth.json", "known_facts.txt",
                       "usage.jsonl", "state.json", "audit.jsonl",
                       "idempotency.json"]
    exposed = []
    for name in must_be_ignored:
        if not (project_root / name).exists():
            continue
        try:
            done = subprocess.run(
                ["git", "check-ignore", "-q", name],
                cwd=project_root, capture_output=True, timeout=15,
            )
            if done.returncode != 0:
                exposed.append(name)
        except (OSError, subprocess.SubprocessError):
            exposed.append(name)
    return exposed


def cron_in_workflow(workflow_text: str) -> Optional[str]:
    """The cron line already in the workflow file, if there is one."""
    found = re.search(r'\n\s*-\s*cron:\s*"([^"]*)"', workflow_text)
    return found.group(1).strip() if found else None


def local_time_for_utc_cron(
    cron: str, now: Optional[datetime] = None
) -> Optional[Tuple[int, int]]:
    """Turn a UTC cron line back into the local time it fires at.

    The inverse of utc_cron_for_local_time, so an existing schedule can be
    shown to the user in the clock they actually read - telling someone their
    job runs at "0 5 * * *" is not telling them anything.
    """
    parts = (cron or "").split()
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    minute, hour = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    reference = now or datetime.now().astimezone()
    if reference.tzinfo is None:
        reference = reference.astimezone()
    as_utc = reference.astimezone(timezone.utc).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    local = as_utc.astimezone(reference.tzinfo)
    return local.hour, local.minute
