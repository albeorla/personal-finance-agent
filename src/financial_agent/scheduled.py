"""Scheduled daily runner (cutover slice J).

The proactive layer: a thin, stdlib-only wrapper that runs the background sync on
a schedule (e.g. cron / launchd), guarded by a file lock so two runs never
overlap. It replaces the legacy `jobs/finance-daily` ritual.

This adds no dependency and no new finance logic - it just orchestrates
`run_background_sync` (which is itself deterministic and idempotent) and prints a
one-line result. Run it with::

    uv run financial-agent-daily
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from datetime import date
from typing import Any

from .background import run_background_sync
from .status import default_db_path


LOCK_FILENAME = "finance_daily.lock"


def run_scheduled_daily_sync(
    db_path: str | None = None,
    *,
    lock_dir: str | None = None,
    as_of_date: str | None = None,
    dry_run: bool = False,
    sync: bool = False,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the daily background sync under a non-blocking file lock.

    If another run already holds the lock, returns immediately with
    ``skipped_lock_held`` (so overlapping cron fires do not double-run). When
    ``sync`` is true, the run first pulls live SimpleFIN + Todoist data (the real
    cron entry point enables this; tests leave it off to stay offline).
    """

    options = {**(options or {}), "sync": sync}

    resolved_db = db_path or str(default_db_path())
    resolved_lock_dir = lock_dir or os.path.dirname(os.path.abspath(resolved_db)) or "."
    os.makedirs(resolved_lock_dir, exist_ok=True)
    lock_path = os.path.join(resolved_lock_dir, LOCK_FILENAME)
    as_of = as_of_date or date.today().isoformat()

    lock_fd = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return {"status": "skipped_lock_held", "as_of_date": as_of, "lock_path": lock_path}

        if dry_run:
            return {"status": "dry_run", "as_of_date": as_of, "lock_path": lock_path}

        conn = sqlite3.connect(resolved_db)
        conn.row_factory = sqlite3.Row
        try:
            run = run_background_sync(
                conn, as_of_date=as_of, run_type="daily_sync", trigger_type="scheduled", options=options
            )
            conn.commit()
        finally:
            conn.close()
        return {"status": "completed", "as_of_date": as_of, "run": run}
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            lock_fd.close()


def main() -> None:
    # The real cron entry pulls live data first (config-gated inside the run).
    result = run_scheduled_daily_sync(sync=True)
    run = result.get("run") or {}
    print(json.dumps({
        "status": result["status"],
        "as_of_date": result.get("as_of_date"),
        "run_id": run.get("run_id"),
        "run_status": run.get("status"),
        "duration_ms": run.get("duration_ms"),
    }))


if __name__ == "__main__":
    main()
