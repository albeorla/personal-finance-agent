"""Live-data validation harness (cutover slice O).

Proves the pipeline is correct on real data and supports the parallel-run: copy
the canonical snapshot to a throwaway working DB, pull live SimpleFIN into the
COPY (never the committed snapshot or the legacy source), then run the whole read
pipeline (onboarding scan, reconciliation, drift, guardrails) and return a
structured report with integrity checks.

``build_validation_report`` is the pure, network-free core (testable on any DB).
``run_live_validation`` adds the copy + live sync around it.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from typing import Any

from .config import get_finance_config
from .drift import detect_drift
from .guardrails import evaluate_guardrails
from .onboarding import scan_charge_onboarding_candidates
from .reconciliation import (
    list_matched_obligation_instances,
    list_unmatched_obligation_instances,
    reconcile_obligation_instances,
)
from .status import default_db_path
from .sync_simplefin import sync_simplefin


def build_validation_report(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the read pipeline on a database and report results + integrity checks."""

    opts = options or {}
    scan = scan_charge_onboarding_candidates(conn, options=opts.get("scan"))
    rec = reconcile_obligation_instances(conn, as_of_date=as_of_date, options=opts.get("reconcile"))
    drift = detect_drift(conn, as_of_date=as_of_date, persist=False)
    guard = evaluate_guardrails(conn, as_of_date=as_of_date, persist=False)

    matched = list_matched_obligation_instances(conn)
    unmatched = list_unmatched_obligation_instances(conn, past_grace_only=True)
    working_hint = get_finance_config().get("working_account_hint")
    checks = _integrity_checks(conn, working_hint)

    return {
        "as_of_date": as_of_date,
        "candidates_total": scan["candidates_total"],
        "reconcile": {k: rec[k] for k in ("considered", "matched_auto", "matched_needs_review", "unmatched")},
        "matched_sample": [
            {"obligation_id": m["obligation_id"], "due_date": m["due_date"], "match_type": m["match_type"], "match_score": m["match_score"]}
            for m in matched[:10]
        ],
        "unmatched_past_grace": [
            {"obligation_id": u["obligation_id"], "due_date": u["due_date"], "age_days": u["age_days"]} for u in unmatched[:10]
        ],
        "drift_by_type": drift["by_type"],
        "drift_by_severity": drift["by_severity"],
        "guardrails": [{"rule_type": f["rule_type"], "severity": f["severity"], "message": f["message"]} for f in guard["findings"]],
        "checks": checks,
        "all_checks_passed": all(c["passed"] for c in checks),
    }


def run_live_validation(
    *,
    source_db_path: str | None = None,
    as_of_date: str,
    work_dir: str | None = None,
    sync: bool = True,
    env_path: str | None = None,
    keep_work_db: bool = False,
) -> dict[str, Any]:
    """Copy the snapshot to a working DB, sync live data into it, and validate.

    Never mutates the source database. Returns the report plus sync counts (no
    secrets). Set ``keep_work_db`` to inspect the working copy afterward.
    """

    source = source_db_path or str(default_db_path())
    created_dir = work_dir is None
    work_dir = work_dir or tempfile.mkdtemp(prefix="finance-validate-")
    os.makedirs(work_dir, exist_ok=True)
    work_db = os.path.join(work_dir, "validation.sqlite")
    shutil.copy(source, work_db)

    conn = sqlite3.connect(work_db)
    conn.row_factory = sqlite3.Row
    synced: dict[str, Any] = {}
    try:
        if sync:
            cfg = get_finance_config(env_path=env_path)
            if cfg["has_simplefin"]:
                synced["simplefin"] = _safe_counts(sync_simplefin(conn, incremental=True, env_path=env_path))
            else:
                synced["simplefin"] = {"skipped": "no SIMPLEFIN_ACCESS_URL"}
            conn.commit()
        report = build_validation_report(conn, as_of_date=as_of_date)
    finally:
        conn.close()

    if not keep_work_db:
        # Remove the whole temp dir we created (incl. the work DB and any sqlite
        # sidecar files); only delete the file when the caller supplied work_dir.
        try:
            if created_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
            else:
                os.remove(work_db)
        except OSError:
            pass

    return {"source_db": source, "work_db_path": work_db if keep_work_db else None, "synced": synced, "report": report}


# --- integrity checks ------------------------------------------------------


def _integrity_checks(conn: sqlite3.Connection, working_hint: str | None = None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    checks.append(_check("accounts_present", accounts > 0, {"count": accounts}))

    # Operating account present: match by the configured name hint (bound
    # parameter, never interpolated). With no hint, fall back to counting
    # checking-kind accounts so the check still runs.
    if working_hint:
        working = conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE name LIKE '%' || ? || '%'", (working_hint,)
        ).fetchone()[0]
    else:
        working = conn.execute("SELECT COUNT(*) FROM accounts WHERE kind = 'checking'").fetchone()[0]
    checks.append(_check("working_account_present", working >= 1, {"count": working}))

    # Card-statement-input instances must target an obligation that still exists,
    # so a renamed/re-issued card never orphans the statement-payment link.
    orphans = conn.execute(
        """
        SELECT DISTINCT statement_target_obligation_id
        FROM obligation_instances
        WHERE cash_flow_treatment = 'card_statement_input'
          AND statement_target_obligation_id IS NOT NULL
          AND statement_target_obligation_id NOT IN (SELECT id FROM obligations)
        """
    ).fetchall()
    checks.append(_check("no_orphan_statement_targets", len(orphans) == 0,
                         {"orphans": [r[0] for r in orphans]}))

    # Every projectable instance has a non-negative amount and a valid direction.
    bad = conn.execute(
        "SELECT COUNT(*) FROM obligation_instances WHERE amount < 0 OR direction NOT IN ('inflow','outflow')"
    ).fetchone()[0]
    checks.append(_check("instance_amounts_normalized", bad == 0, {"bad_rows": bad}))

    return checks


def _check(name: str, passed: bool, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "evidence": evidence}


def _safe_counts(sync_result: dict[str, Any]) -> dict[str, Any]:
    safe_keys = ("accounts", "inserted", "updated", "errors", "error",
                 "tasks_seen", "cashflow_tasks_seen", "missing_marked_deleted")
    return {k: sync_result[k] for k in safe_keys if k in sync_result}
