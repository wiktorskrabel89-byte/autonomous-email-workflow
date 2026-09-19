import os
from pathlib import Path


def _find_project_root() -> Path:
    """
    Locate the project root by searching for config.yaml walking upward from
    the installed package location, falling back to an env variable or CWD.

    Priority:
    1. EMAIL_WORKFLOW_ROOT environment variable (explicit override)
    2. Walk up from __file__ looking for config.yaml (works for editable installs)
    3. Walk up from CWD looking for config.yaml (works when running from project dir)
    4. Fall back to __file__.parents[3] (original heuristic)
    """
    # 1. Explicit env override
    env_root = os.getenv("EMAIL_WORKFLOW_ROOT")
    if env_root:
        return Path(env_root).resolve()

    # 2. Walk up from the package file (editable install: src tree)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.yaml").exists() and (parent / "pyproject.toml").exists():
            return parent

    # 3. Walk up from CWD
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "config.yaml").exists() and (parent / "pyproject.toml").exists():
            return parent

    # 4. Last-resort fallback
    return here.parents[3]


PROJECT_ROOT = _find_project_root()


def resolve_project_file(file_path: str) -> Path:
    """
    Resolve a file path the same way no matter which folder you run from.

    Order:
    1. An absolute path is used as given.
    2. The project root wins, if the file is there. This is what stops the app
       from silently reading a different config.yaml or state.json just because
       it was started from another directory.
    3. Otherwise a file that exists relative to the current directory, so
       "--mock-inbox ./my_inbox.json" still works.
    4. Otherwise the project root, which is where new files get created.
    """
    p = Path(file_path)
    if p.is_absolute():
        return p

    in_project = PROJECT_ROOT / p
    if in_project.exists():
        return in_project.resolve()

    if p.exists():
        return p.resolve()

    return in_project.resolve()


def find_env_file() -> Path:
    """Locate the .env file, checking the usual places in order.

    The project folder first, then the folder the command was run from, then
    the user's home directory. Returning the project path when none exists
    means a newly saved key lands next to the project, not wherever the user
    happened to be standing.
    """
    candidates = [
        PROJECT_ROOT / ".env",
        Path.cwd() / ".env",
        Path.home() / ".env",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return PROJECT_ROOT / ".env"
