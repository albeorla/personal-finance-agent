"""Background runner + telemetry (BUILD_PLAN M3 + M6).

A background run orchestrates the deterministic finance pipeline in one place -
discover charges, reconcile transactions, detect drift, surface due items - and
records an auditable run with an ordered event log. This is the proactive
execution layer (it does not need an active chat session) and the per-operation
telemetry answer: every step emits an event with its operation, result counts,
timing, and any error.

Resilience: an error in one step is recorded and the run continues
(``partial_success``) rather than aborting the whole sync. The sequence of event
types and result counts is deterministic for a given database and as-of date,
even though wall-clock timing is not.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Callable

from .config import get_finance_config
from .drift import detect_drift
from .obligations import suppress_dormant_avg_estimates
from .onboarding import scan_charge_onboarding_candidates
from .reconciliation import reconcile_obligation_instances
from .schema import ensure_app_schema
from .surface_queue import build_surface_items
from .sync_simplefin import sync_simplefin
from .todoist_outbox import surface_to_todoist


DEFAULT_RUN_TYPE = "daily_sync"

# A daily job that has not completed within this many hours is treated as stale
# (the scheduler probably stopped). 26h = the 24h cadence plus a 2h grace for an
# overnight run that slips. A completed run (succeeded OR partial_success) counts
# as a heartbeat: partial_success still ingested and saw the data.
STALE_JOB_THRESHOLD_HOURS = 26
_HEALTHY_RUN_STATUSES = ("succeeded", "partial_success")


def run_background_sync(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    options: dict[str, Any] | None = None,
    run_type: str = DEFAULT_RUN_TYPE,
    trigger_type: str = "manual",
) -> dict[str, Any]:
    """Run the finance pipeline as one auditable background run.

    Steps: scan charge candidates, reconcile transactions, detect drift, suppress
    dormant estimates, and surface the day's due items to Todoist (gated off by
    default). Each step is recorded as an event; a failing step is logged and the
    run continues.
    """

    ensure_app_schema(conn)
    opts = options or {}
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    trace_id = f"trace_{uuid.uuid4().hex}"
    started = _now_dt()

    conn.execute(
        """
        INSERT INTO background_runs (
            id, trace_id, run_type, trigger_type, status, as_of_date, started_at, created_at
        ) VALUES (?, ?, ?, ?, 'in_progress', ?, ?, ?)
        """,
        (run_id, trace_id, run_type, trigger_type, as_of_date, started.isoformat(), started.isoformat()),
    )
    _emit(conn, run_id, "run_started", "ok", {"as_of_date": as_of_date, "run_type": run_type})

    # Live ingest is opt-in (options["sync"]) and config-gated, so the default
    # run stays offline and its event sequence stays deterministic for tests.
    sync_steps: list[tuple[str, Callable[[], dict[str, Any]]]] = []
    if opts.get("sync"):
        sync_steps = [
            ("sync_simplefin", lambda: _sync_simplefin_step(conn, opts)),
        ]

    steps: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        *sync_steps,
        ("scan_charge_candidates", lambda: _summarize_scan(
            scan_charge_onboarding_candidates(conn, options=opts.get("scan")))),
        ("reconcile", lambda: reconcile_obligation_instances(
            conn, as_of_date=as_of_date, options=opts.get("reconcile"))),
        ("detect_drift", lambda: _summarize_drift(
            detect_drift(conn, as_of_date=as_of_date, options=opts.get("drift"), persist=True))),
        ("suppress_dormant_estimates", lambda: _summarize_suppression(
            suppress_dormant_avg_estimates(
                conn, as_of_date=as_of_date, options=opts.get("suppress_dormant")))),
        ("surface_due_items", lambda: _summarize_surface(_surface_due_items_step(
            conn, as_of_date, opts))),
    ]

    summary: dict[str, Any] = {}
    errors = 0
    for name, fn in steps:
        try:
            result = fn()
            summary[name] = result
            _emit(conn, run_id, name, "ok", result)
        except Exception as exc:  # noqa: BLE001 - one bad step must not abort the run
            errors += 1
            summary[name] = {"error": str(exc)}
            _emit(conn, run_id, name, "error", {"error": str(exc)}, error=str(exc))

    finished = _now_dt()
    duration_ms = int((finished - started).total_seconds() * 1000)
    status = "succeeded" if errors == 0 else "partial_success"
    _emit(conn, run_id, "run_finished", status, {"errors": errors, "duration_ms": duration_ms})
    conn.execute(
        """
        UPDATE background_runs
        SET status = ?, finished_at = ?, duration_ms = ?, result_summary_json = ?,
            error = ?
        WHERE id = ?
        """,
        (
            status, finished.isoformat(), duration_ms, json.dumps(summary, sort_keys=True),
            f"{errors} step(s) failed" if errors else None, run_id,
        ),
    )

    return {
        "run_id": run_id,
        "trace_id": trace_id,
        "run_type": run_type,
        "status": status,
        "as_of_date": as_of_date,
        "duration_ms": duration_ms,
        "errors": errors,
        "result_summary": summary,
    }


def get_background_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """Return a run record plus its ordered event log for inspection."""

    ensure_app_schema(conn)
    run = conn.execute(
        """
        SELECT id, trace_id, run_type, trigger_type, status, as_of_date,
               started_at, finished_at, duration_ms, result_summary_json, error
        FROM background_runs WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        return None
    events = conn.execute(
        """
        SELECT event_seq, event_type, status, event_data_json, error, event_time
        FROM operation_events WHERE run_id = ? ORDER BY event_seq
        """,
        (run_id,),
    ).fetchall()
    return {
        "run_id": run["id"],
        "trace_id": run["trace_id"],
        "run_type": run["run_type"],
        "trigger_type": run["trigger_type"],
        "status": run["status"],
        "as_of_date": run["as_of_date"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "duration_ms": run["duration_ms"],
        "result_summary": json.loads(run["result_summary_json"]) if run["result_summary_json"] else None,
        "error": run["error"],
        "events": [
            {
                "event_seq": e["event_seq"],
                "event_type": e["event_type"],
                "status": e["status"],
                "event_data": json.loads(e["event_data_json"]) if e["event_data_json"] else None,
                "error": e["error"],
                "event_time": e["event_time"],
            }
            for e in events
        ],
    }


def list_background_runs(
    conn: sqlite3.Connection,
    *,
    run_type: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where: list[str] = []
    params: list[Any] = []
    if run_type is not None:
        where.append("run_type = ?")
        params.append(run_type)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    query = "SELECT id, trace_id, run_type, status, as_of_date, started_at, finished_at, duration_ms, error FROM background_runs"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "run_id": r["id"],
            "trace_id": r["trace_id"],
            "run_type": r["run_type"],
            "status": r["status"],
            "as_of_date": r["as_of_date"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "duration_ms": r["duration_ms"],
            "error": r["error"],
        }
        for r in rows
    ]


def get_job_health(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    run_type: str = DEFAULT_RUN_TYPE,
    stale_threshold_hours: int = STALE_JOB_THRESHOLD_HOURS,
) -> dict[str, Any]:
    """Report whether the daily job is alive based on its last completed run.

    A silently-stopped scheduler is invisible: nothing fails, the data just goes
    stale. This turns the absence of a recent run into a visible signal. It reads
    the most recent ``run_type`` run that COMPLETED (``succeeded`` or
    ``partial_success`` - both ingested and saw the data) and measures how long
    ago it finished.

    Staleness is always measured against the live wall clock: "is the daily job
    alive right now?" is a present-tense question, so ``as_of_date`` is echoed for
    context but does not move the reference time.

    Returns ``healthy`` / ``is_stale`` (a run finished within the threshold),
    ``last_run_id``, ``last_run_status``, ``last_run_finished_at``, and
    ``hours_since_last_run`` (None when no completed run exists). With no completed
    run at all, the job is unhealthy and stale.
    """

    ensure_app_schema(conn)
    placeholders = ",".join("?" for _ in _HEALTHY_RUN_STATUSES)
    row = conn.execute(
        f"""
        SELECT id, status, finished_at
        FROM background_runs
        WHERE run_type = ? AND status IN ({placeholders}) AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (run_type, *_HEALTHY_RUN_STATUSES),
    ).fetchone()

    reference = _now_dt()

    if row is None:
        return {
            "healthy": False,
            "is_stale": True,
            "last_run_id": None,
            "last_run_status": None,
            "last_run_finished_at": None,
            "hours_since_last_run": None,
            "stale_threshold_hours": stale_threshold_hours,
            "as_of_date": as_of_date,
        }

    finished = _parse_dt(row["finished_at"])
    hours_since = round((reference - finished).total_seconds() / 3600.0, 2)
    # A run exactly at the threshold is NOT stale; only strictly past it is. The
    # 2-decimal round keeps sub-second test/exec drift from flipping the boundary.
    is_stale = hours_since > stale_threshold_hours
    return {
        "healthy": not is_stale,
        "is_stale": is_stale,
        "last_run_id": row["id"],
        "last_run_status": row["status"],
        "last_run_finished_at": row["finished_at"],
        "hours_since_last_run": hours_since,
        "stale_threshold_hours": stale_threshold_hours,
        "as_of_date": as_of_date,
    }


# --- helpers ---------------------------------------------------------------


def _emit(
    conn: sqlite3.Connection,
    run_id: str,
    event_type: str,
    status: str,
    event_data: dict[str, Any],
    error: str | None = None,
) -> None:
    seq = conn.execute(
        "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM operation_events WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO operation_events (run_id, event_seq, event_type, status, event_data_json, error, event_time)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, seq, event_type, status, json.dumps(event_data, sort_keys=True), error, _now_dt().isoformat()),
    )


def _sync_simplefin_step(conn: sqlite3.Connection, opts: dict[str, Any]) -> dict[str, Any]:
    if not get_finance_config(env_path=opts.get("env_path"))["has_simplefin"]:
        return {"skipped": "no SIMPLEFIN_ACCESS_URL configured"}
    r = sync_simplefin(conn, env_path=opts.get("env_path"), incremental=True)
    return {k: r[k] for k in ("accounts", "inserted", "updated", "error") if k in r}


def _summarize_scan(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result[k] for k in ("created", "updated", "unchanged", "candidates_total", "scanned_transactions") if k in result}


def _summarize_drift(result: dict[str, Any]) -> dict[str, Any]:
    return {"count": result.get("count"), "by_type": result.get("by_type"), "by_severity": result.get("by_severity")}


def _summarize_suppression(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "evaluated": result.get("evaluated"),
        "suppressed_count": result.get("suppressed_count"),
        "suppressed": result.get("suppressed"),
    }


def _surface_due_items_step(
    conn: sqlite3.Connection, as_of_date: str, opts: dict[str, Any]
) -> dict[str, Any]:
    """Build and surface the day's due items to Todoist (de-duped via ledger).

    Live write is OPT-IN here, mirroring how live ingest is gated: the daily
    background run stays offline and deterministic unless the caller passes
    ``options["surface"]`` (a dict, optionally with ``write_enabled``). With no
    such option the call is gated off (``write_enabled=False``), so it makes no
    external Todoist call and the event sequence is stable for tests.
    """

    surface_opts = opts.get("surface")
    if not surface_opts:
        # Default: gated off, hermetic. No live send, ledger untouched.
        return surface_to_todoist(
            conn,
            build_surface_items(conn, as_of_date=as_of_date),
            as_of_date,
            write_enabled=False,
        )
    if not isinstance(surface_opts, dict):
        surface_opts = {}
    return surface_to_todoist(
        conn,
        build_surface_items(conn, as_of_date=as_of_date),
        as_of_date,
        write_enabled=surface_opts.get("write_enabled"),
        token=surface_opts.get("token"),
        project_id=surface_opts.get("project_id"),
        env_path=surface_opts.get("env_path", opts.get("env_path")),
    )


def _summarize_surface(result: dict[str, Any]) -> dict[str, Any]:
    # The full per-item list is intentionally dropped from the run summary to
    # keep the event log compact; the ledger holds the durable per-key state.
    return {
        k: result[k]
        for k in ("status", "integration_enabled", "created", "updated", "skipped", "resolved", "failed")
        if k in result
    }


def _now_dt() -> datetime:
    return datetime.now().astimezone()


def _parse_dt(value: str) -> datetime:
    """Parse a stored ISO timestamp, tolerating naive values by localizing them."""

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt
