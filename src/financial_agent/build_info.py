"""Build metadata captured once at process startup.

Why this exists: the MCP server is a long-running process. Code merged to main
does not take effect until the process restarts, so a live session can keep
serving stale logic (old balances, an old avalanche order) while the repo on
disk already has the fix - and nothing makes that gap visible. The values below
are read ONCE at import time (process startup) so they describe the RUNNING
process, not whatever git happens to report on a later call. Comparing the
startup commit (``RUNNING_COMMIT``) against a live HEAD read
(``current_repo_head``) turns "the server is running older code than what is
checked out" into a signal a user can see.

Everything here degrades gracefully when there is no git repo: nothing raises,
and the commit fields fall back to ``"unknown"``.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

# The git repo that contains this code, located from this file rather than from
# the current working directory (which the MCP host controls and may set
# anywhere). git rev-parse walks upward from here to find the .git directory.
_REPO_DIR = Path(__file__).resolve().parent


def _git(args: list[str]) -> str | None:
    """Run a git command in the repo containing this file.

    Returns stripped stdout on success, or ``None`` when git is missing, the
    directory is not a git repo, or the command fails. Never raises.
    """

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(_REPO_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _read_running_commit() -> str:
    commit = _git(["rev-parse", "--short", "HEAD"])
    return commit or "unknown"


def _read_running_dirty() -> bool:
    status = _git(["status", "--porcelain"])
    if status is None:
        return False
    return bool(status.strip())


def current_repo_head() -> str:
    """Read the repo's git HEAD live, at call time (not the startup snapshot).

    Staleness detection compares this against ``RUNNING_COMMIT``: if they differ,
    the checked-out code is newer than the running process. Returns ``"unknown"``
    when there is no git repo.
    """

    commit = _git(["rev-parse", "--short", "HEAD"])
    return commit or "unknown"


# Captured ONCE at import (process startup). Do not recompute these per call -
# they must reflect the running process, not the live repo.
VERSION: str = __version__
RUNNING_COMMIT: str = _read_running_commit()
RUNNING_DIRTY: bool = _read_running_dirty()
STARTED_AT: str = datetime.now(timezone.utc).isoformat()
