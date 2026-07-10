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

import argparse
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
    surface: bool = False,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the daily background sync under a non-blocking file lock.

    If another run already holds the lock, returns immediately with
    ``skipped_lock_held`` (so overlapping cron fires do not double-run). When
    ``sync`` is true, the run first pulls live SimpleFIN + Todoist data (the real
    cron entry point enables this; tests leave it off to stay offline).
    """

    options = {**(options or {}), "sync": sync}
    if surface and "surface" not in options:
        options["surface"] = {"write_enabled": None}

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
            return {
                "status": "skipped_lock_held",
                "semantic_status": "warn",
                "fresh_for_exports": False,
                "as_of_date": as_of,
                "lock_path": lock_path,
                "phases": _skipped_phases("lock held"),
            }

        if dry_run:
            return {
                "status": "dry_run",
                "semantic_status": "ok",
                "fresh_for_exports": False,
                "as_of_date": as_of,
                "lock_path": lock_path,
                "phases": _skipped_phases("dry run"),
            }

        conn = sqlite3.connect(resolved_db)
        conn.row_factory = sqlite3.Row
        try:
            run = run_background_sync(
                conn, as_of_date=as_of, run_type="daily_sync", trigger_type="scheduled", options=options
            )
            conn.commit()
        finally:
            conn.close()
        phases = _phase_summary(run, sync=sync, surface=surface)
        phase_statuses = {phase["status"] for phase in phases.values()}
        semantic_status = "warn" if phase_statuses & {"warn", "failed"} else "ok"
        return {
            "status": "completed",
            "semantic_status": semantic_status,
            "fresh_for_exports": phases["sync"]["status"] == "ok",
            "as_of_date": as_of,
            "run": run,
            "phases": phases,
        }
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            lock_fd.close()


def _skipped_phases(reason: str) -> dict[str, dict[str, Any]]:
    return {
        name: {"status": "skipped", "reason": reason}
        for name in ("sync", "pipeline", "surface", "reconcile_completions")
    }


def _phase_summary(run: dict[str, Any], *, sync: bool, surface: bool) -> dict[str, dict[str, Any]]:
    summary = run.get("result_summary") or {}
    sync_result = summary.get("sync_simplefin") or {}
    if not sync:
        sync_phase = {"status": "skipped", "reason": "sync disabled"}
    elif sync_result.get("error"):
        sync_phase = {"status": "failed", "error": str(sync_result["error"])[:200]}
    elif sync_result.get("warnings") or sync_result.get("skipped"):
        sync_phase = {"status": "warn"}
    else:
        sync_phase = {
            "status": "ok",
            **{key: sync_result[key] for key in ("accounts", "inserted", "updated") if key in sync_result},
        }

    run_status = run.get("status")
    pipeline_phase = {
        "status": "ok" if run_status == "succeeded" else "warn" if run_status in {"succeeded_with_warnings", "partial_success"} else "failed",
        "run_status": run_status,
    }

    def external_phase(name: str, enabled: bool) -> dict[str, Any]:
        if not enabled:
            return {"status": "skipped", "reason": "phase disabled"}
        result = summary.get(name) or {}
        if result.get("error"):
            return {"status": "failed"}
        failed = int(result.get("failed") or 0)
        source_status = result.get("status")
        status = "warn" if failed or source_status == "awaiting-integration" else "ok"
        return {
            "status": status,
            **{key: result[key] for key in ("created", "updated", "resolved", "failed") if key in result},
        }

    return {
        "sync": sync_phase,
        "pipeline": pipeline_phase,
        "surface": external_phase("surface_due_items", surface),
        "reconcile_completions": external_phase("reconcile_todoist_completions", True),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic daily finance pipeline.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db")
    parser.add_argument("--as-of-date")
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument("--no-surface", action="store_true")
    args = parser.parse_args()
    result = run_scheduled_daily_sync(
        db_path=args.db,
        as_of_date=args.as_of_date,
        dry_run=args.dry_run,
        sync=not args.no_sync,
        surface=not args.no_surface,
    )
    run = result.get("run") or {}
    print(json.dumps({
        "status": result["status"],
        "semantic_status": result.get("semantic_status"),
        "fresh_for_exports": result.get("fresh_for_exports"),
        "as_of_date": result.get("as_of_date"),
        "run_id": run.get("run_id"),
        "run_status": run.get("status"),
        "duration_ms": run.get("duration_ms"),
        "phases": result.get("phases"),
    }, separators=(",", ":")))
    if result.get("semantic_status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
