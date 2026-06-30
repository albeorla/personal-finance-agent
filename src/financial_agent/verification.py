"""Deterministic verification phase - the engine checking its own consistency.

The grounding gate (``grounding.py``) proves each headline number traces to a
source row. These checks prove the rows tie together: that a projection's ending
balance equals its own signed events, that no obligation carries two projectable
instances on one due date, that a statement cycle's denormalized rollup matches
the inputs behind it, and that projectable amounts carry a sane sign. They catch
a class of composition error the grounding gate cannot - a number that traces to
a real row but is still wrong because the rows do not add up.

Every check is pure code over the local database, deterministic for a given
``(database, as_of_date)``. No model is consulted, so a check can never
hallucinate a pass or a fail. A run records each failure as a
``verification_findings`` row and returns a structured summary; a clean run finds
nothing.

This raises the floor and routes attention - it does not make the system
correct. A wrong-but-internally-consistent number (right arithmetic, wrong
source row chosen) still passes every identity here; catching that needs a human
in the loop. The checks make the human's verification cheaper and the system's
inconsistencies visible, which is the honest scope of the guarantee.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from .schema import ensure_app_schema, has_app_schema

# Tolerance for floating-point money comparisons (one cent).
_MONEY_TOLERANCE = 0.01

# Instance statuses that feed the projection - the set the cashflow query uses.
# Mirrored here as literals so a sign/duplicate error in a projectable instance
# is flagged exactly when it could move the forecast.
_PROJECTABLE_STATUSES = ("expected", "needs_review", "partially_paid")

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"

# A recurring obligation should have modeled instances out to this horizon; when
# its last projectable instance falls short, the long-window projection is silently
# rosy (the later cycles are simply absent). Tunable knob.
COVERAGE_HORIZON_DAYS = 90
_RECURRING_CADENCES = ("monthly", "weekly", "biweekly", "quarterly", "semimonthly")


def run_verification(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    run_id: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run every deterministic consistency check and return a structured summary.

    Findings come back in a stable order (by check, then each check's own
    deterministic ordering). When ``persist`` is true, each finding is written to
    ``verification_findings`` (tagged with ``run_id`` when supplied) so it can be
    listed later and surfaced to the human. With ``persist=False`` the call is
    read-only, which is how the daily digest embeds a live check without writing.
    """

    ensure_app_schema(conn)
    if not has_app_schema(conn):
        return _empty_summary(as_of_date)

    checks: list[tuple[str, Callable[[sqlite3.Connection, str], list[dict[str, Any]]]]] = [
        ("projection_identity", _check_projection_identity),
        ("duplicate_instances", _check_duplicate_instances),
        ("statement_identity", _check_statement_identity),
        ("instance_sign_sanity", _check_instance_sign_sanity),
        ("coverage_horizon", _check_coverage_horizon),
    ]
    findings: list[dict[str, Any]] = []
    checks_run: list[str] = []
    for name, check in checks:
        checks_run.append(name)
        findings.extend(check(conn, as_of_date))

    if persist:
        # Reconcile on every persisting run - including a clean run with no
        # findings, which is how a fixed identity gets its stale 'open' row
        # flipped to 'resolved'.
        _reconcile_and_persist(conn, findings, run_id=run_id, as_of_date=as_of_date)

    by_severity: dict[str, int] = {}
    for finding in findings:
        by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1

    return {
        "as_of_date": as_of_date,
        "ok": len(findings) == 0,
        "checks_run": checks_run,
        "checks_total": len(checks_run),
        "findings_total": len(findings),
        "by_severity": by_severity,
        "findings": findings,
    }


def list_verification_findings(
    conn: sqlite3.Connection,
    *,
    status: str | None = "open",
    check_id: str | None = None,
    source: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List persisted verification findings, newest first.

    Defaults to ``status='open'`` (pass ``None`` for every status). Optionally
    filter to one ``check_id`` or one ``source`` ('deterministic' for the
    pure-code checks, 'adversarial' for the spawned reviewer). This is the read
    the agent calls after a correction to confirm the checks now pass.
    """

    ensure_app_schema(conn)
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if check_id is not None:
        where.append("check_id = ?")
        params.append(check_id)
    if source is not None:
        where.append("source = ?")
        params.append(source)
    query = (
        "SELECT id, run_id, check_id, severity, title, detail, evidence_json, "
        "as_of_date, status, source, created_at FROM verification_findings"
    )
    if where:
        query += " WHERE " + " AND ".join(where)
    # rowid is the stable insertion order; the random id makes a poor tiebreaker
    # because a single run writes one shared created_at across its findings.
    query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": r["id"],
            "run_id": r["run_id"],
            "check_id": r["check_id"],
            "severity": r["severity"],
            "title": r["title"],
            "detail": r["detail"],
            "evidence": json.loads(r["evidence_json"]) if r["evidence_json"] else None,
            "as_of_date": r["as_of_date"],
            "status": r["status"],
            "source": r["source"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# --- checks ----------------------------------------------------------------


def _check_projection_identity(conn: sqlite3.Connection, as_of_date: str) -> list[dict[str, Any]]:
    """Projection ending balance must equal starting balance plus its signed events.

    Recomputes the identity independently of the projection's own running
    balance: a divergence means the projection arithmetic drifted from the
    instances it claims to sum (a regression guard on the cash-flow engine).
    """

    from .cashflow import build_cash_flow_projections
    from .status import _latest_balances

    findings: list[dict[str, Any]] = []
    try:
        start = date.fromisoformat(as_of_date[:10])
    except ValueError:
        return findings
    try:
        accounts = _latest_balances(conn)
        if not accounts:
            return findings
        projections, _warnings = build_cash_flow_projections(
            conn, accounts=accounts, windows=[30, 60, 90], start_date=start
        )
    except sqlite3.OperationalError:
        # Source tables (accounts / balance_snapshots) are not present, so there
        # is no projection to check. Mirrors the OperationalError guards in
        # digest.py (_matches_to_confirm / _recently_cleared / _coverage).
        return findings
    for proj in projections:
        starting = float(proj["starting_balance"])
        ending = float(proj["ending_balance"])
        # Recompute with the SAME stepwise rounding the projection uses, so a
        # normal two-decimal model ties exactly and only a real divergence flags
        # (re-summing the pre-rounded events could differ by a cent otherwise).
        recomputed = round(starting, 2)
        for event in proj["events"]:
            recomputed = round(recomputed + float(event["signed_amount"]), 2)
        delta = round(ending - recomputed, 2)
        if abs(delta) > _MONEY_TOLERANCE:
            findings.append(
                _finding(
                    check_id="projection_identity",
                    severity=SEVERITY_ERROR,
                    title=(
                        f"{proj['window_days']}d projection ending balance does not "
                        "match its events"
                    ),
                    detail=(
                        f"Reported ending {ending:.2f}, but starting {starting:.2f} plus "
                        f"{len(proj['events'])} signed events recomputes to "
                        f"{recomputed:.2f} (off by {delta:.2f})."
                    ),
                    evidence={
                        "window_days": proj["window_days"],
                        "starting_balance": starting,
                        "reported_ending": ending,
                        "recomputed_ending": recomputed,
                        "delta": delta,
                        "event_count": len(proj["events"]),
                    },
                )
            )
    return findings


def _check_duplicate_instances(conn: sqlite3.Connection, as_of_date: str) -> list[dict[str, Any]]:
    """No obligation may carry two projectable instances on the same due date.

    Two instances with the same obligation and due date both feed the
    projection, double-counting that obligation - an idempotency violation in
    instance generation or backfill.
    """

    rows = conn.execute(
        f"""
        SELECT oi.obligation_id, ob.name AS obligation_name, oi.due_date,
               COUNT(*) AS n
        FROM obligation_instances oi
        JOIN obligations ob ON ob.id = oi.obligation_id
        WHERE oi.status IN ({_status_placeholders()})
        GROUP BY oi.obligation_id, oi.due_date
        HAVING COUNT(*) > 1
        ORDER BY oi.obligation_id, oi.due_date
        """,
        _PROJECTABLE_STATUSES,
    ).fetchall()
    findings: list[dict[str, Any]] = []
    for r in rows:
        findings.append(
            _finding(
                check_id="duplicate_instances",
                severity=SEVERITY_ERROR,
                title=(
                    f"Duplicate projectable instances for {r['obligation_name']} "
                    f"on {r['due_date']}"
                ),
                detail=(
                    f"{r['n']} projectable instances share obligation "
                    f"{r['obligation_id']} and due date {r['due_date']}; each one "
                    "double-counts in the projection."
                ),
                evidence={
                    "obligation_id": r["obligation_id"],
                    "obligation_name": r["obligation_name"],
                    "due_date": r["due_date"],
                    "count": r["n"],
                },
            )
        )
    return findings


def _check_statement_identity(conn: sqlite3.Connection, as_of_date: str) -> list[dict[str, Any]]:
    """A statement cycle's denormalized rollup must tie to its input rows.

    ``statement_cycles`` stores a running ``input_sum`` and ``input_count``; the
    ``statement_cycle_inputs`` rows are the source of truth. A mismatch means the
    rollup that feeds the card's statement estimate has drifted from the charges
    behind it.
    """

    rows = conn.execute(
        """
        SELECT sc.id, sc.input_sum, sc.input_count,
               COALESCE(SUM(sci.input_amount), 0) AS actual_sum,
               COUNT(sci.obligation_instance_id) AS actual_count
        FROM statement_cycles sc
        LEFT JOIN statement_cycle_inputs sci
            ON sci.statement_cycle_id = sc.id
        GROUP BY sc.id
        ORDER BY sc.id
        """
    ).fetchall()
    findings: list[dict[str, Any]] = []
    for r in rows:
        sum_delta = round(float(r["input_sum"]) - float(r["actual_sum"]), 2)
        count_ok = int(r["input_count"]) == int(r["actual_count"])
        if abs(sum_delta) > _MONEY_TOLERANCE or not count_ok:
            findings.append(
                _finding(
                    check_id="statement_identity",
                    severity=SEVERITY_ERROR,
                    title=f"Statement cycle {r['id']} rollup does not tie to its inputs",
                    detail=(
                        f"Stored input_sum {float(r['input_sum']):.2f} / count "
                        f"{r['input_count']} vs actual {float(r['actual_sum']):.2f} / "
                        f"{r['actual_count']} from input rows (sum off by "
                        f"{sum_delta:.2f})."
                    ),
                    evidence={
                        "statement_cycle_id": r["id"],
                        "stored_sum": round(float(r["input_sum"]), 2),
                        "actual_sum": round(float(r["actual_sum"]), 2),
                        "stored_count": r["input_count"],
                        "actual_count": r["actual_count"],
                        "sum_delta": sum_delta,
                    },
                )
            )
    return findings


def _check_instance_sign_sanity(conn: sqlite3.Connection, as_of_date: str) -> list[dict[str, Any]]:
    """Projectable amounts are stored unsigned; ``direction`` carries the sign.

    A stored negative amount double-negates against its outflow direction, so a
    bill would read as money coming in and silently inflate the projected
    balance. This catches that sign error at the source.
    """

    rows = conn.execute(
        f"""
        SELECT oi.id, ob.name AS obligation_name, oi.due_date, oi.amount,
               oi.direction
        FROM obligation_instances oi
        JOIN obligations ob ON ob.id = oi.obligation_id
        WHERE oi.status IN ({_status_placeholders()})
          AND oi.amount < 0
        ORDER BY oi.id
        """,
        _PROJECTABLE_STATUSES,
    ).fetchall()
    findings: list[dict[str, Any]] = []
    for r in rows:
        findings.append(
            _finding(
                check_id="instance_sign_sanity",
                severity=SEVERITY_ERROR,
                title=f"Negative stored amount on {r['obligation_name']} ({r['due_date']})",
                detail=(
                    f"Instance {r['id']} stores amount {float(r['amount']):.2f} with "
                    f"direction '{r['direction']}'; amounts must be non-negative "
                    "because direction carries the sign."
                ),
                evidence={
                    "instance_id": r["id"],
                    "obligation_name": r["obligation_name"],
                    "due_date": r["due_date"],
                    "amount": round(float(r["amount"]), 2),
                    "direction": r["direction"],
                },
            )
        )
    return findings


# --- internals -------------------------------------------------------------


def _check_coverage_horizon(conn: sqlite3.Connection, as_of_date: str) -> list[dict[str, Any]]:
    """A recurring obligation should be modeled out to the projection horizon.

    When an active recurring obligation has future instances that stop short of
    ``as_of + COVERAGE_HORIZON_DAYS``, the long-window runway omits its later
    cycles and reads rosier than reality. WARN (not an error): the model is
    incomplete, not wrong. Obligations already fully in the past are left to the
    drift/missing checks; this only flags ones that run out mid-horizon.
    """

    as_of = date.fromisoformat(as_of_date[:10])
    horizon = (as_of + timedelta(days=COVERAGE_HORIZON_DAYS)).isoformat()
    rows = conn.execute(
        f"""
        SELECT ob.id, ob.name, ob.cadence, MAX(oi.due_date) AS last_due
        FROM obligations ob
        JOIN obligation_instances oi ON oi.obligation_id = ob.id
        WHERE ob.status = 'active'
          AND ob.cadence IN ({",".join("?" for _ in _RECURRING_CADENCES)})
          AND oi.status IN ({_status_placeholders()})
        GROUP BY ob.id
        HAVING MAX(oi.due_date) >= ? AND MAX(oi.due_date) < ?
        ORDER BY ob.id
        """,
        (*_RECURRING_CADENCES, *_PROJECTABLE_STATUSES, as_of.isoformat(), horizon),
    ).fetchall()
    findings: list[dict[str, Any]] = []
    for r in rows:
        findings.append(
            _finding(
                check_id="coverage_horizon",
                severity=SEVERITY_WARN,
                title=f"{r['name']} has no modeled instances past {r['last_due']}",
                detail=(
                    f"{r['name']} is a recurring ({r['cadence']}) obligation, but its last "
                    f"projectable instance is {r['last_due']} - short of the "
                    f"{COVERAGE_HORIZON_DAYS}-day horizon ({horizon}). The long-window "
                    f"projection omits its later cycles, so the runway past {r['last_due']} "
                    "is rosier than reality. Backfill/extend its instances."
                ),
                evidence={
                    "obligation_id": r["id"],
                    "cadence": r["cadence"],
                    "last_due": r["last_due"],
                    "horizon": horizon,
                },
            )
        )
    return findings


def _status_placeholders() -> str:
    return ",".join("?" for _ in _PROJECTABLE_STATUSES)


def _finding(
    *,
    check_id: str,
    severity: str,
    title: str,
    detail: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "severity": severity,
        "title": title,
        "detail": detail,
        "evidence": evidence,
    }


def _reconcile_and_persist(
    conn: sqlite3.Connection,
    findings: list[dict[str, Any]],
    *,
    run_id: str | None,
    as_of_date: str,
) -> None:
    """Persist current findings and resolve ones that no longer fail.

    Keyed by (check_id, evidence) so a still-failing identity is not duplicated
    across daily runs, and a fixed one is flipped to 'resolved' rather than
    lingering as 'open'. This is the same reconcile shape drift.py uses, and it
    is what lets ``list_verification_findings(status='open')`` reflect the live
    state after a correction.
    """

    now = _now()
    current: dict[str, dict[str, Any]] = {}
    for finding in findings:
        current[_finding_key(finding["check_id"], finding["evidence"])] = finding

    existing: dict[str, list[str]] = {}
    for row in conn.execute(
        # Scope to this producer only: the deterministic reconciler must never
        # resolve an adversarial-review finding (and vice versa).
        "SELECT id, check_id, evidence_json FROM verification_findings "
        "WHERE status = 'open' AND source = 'deterministic'"
    ).fetchall():
        existing.setdefault(_row_key(row["check_id"], row["evidence_json"]), []).append(row["id"])

    # Resolve open findings whose identity no longer fails.
    for key, ids in existing.items():
        if key not in current:
            for finding_id in ids:
                conn.execute(
                    "UPDATE verification_findings SET status = 'resolved' WHERE id = ?",
                    (finding_id,),
                )

    # Insert findings that are not already open (leave a still-open row in place).
    for key, finding in current.items():
        if key in existing:
            continue
        conn.execute(
            """
            INSERT INTO verification_findings (
                id, run_id, check_id, severity, title, detail, evidence_json,
                as_of_date, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                f"vf_{uuid.uuid4().hex[:16]}",
                run_id,
                finding["check_id"],
                finding["severity"],
                finding["title"],
                finding["detail"],
                json.dumps(finding["evidence"], sort_keys=True),
                as_of_date,
                now,
            ),
        )


def _finding_key(check_id: str, evidence: dict[str, Any]) -> str:
    return check_id + "|" + json.dumps(evidence, sort_keys=True)


def _row_key(check_id: str, evidence_json: str | None) -> str:
    return check_id + "|" + (evidence_json or "null")


def _empty_summary(as_of_date: str) -> dict[str, Any]:
    return {
        "as_of_date": as_of_date,
        "ok": True,
        "checks_run": [],
        "checks_total": 0,
        "findings_total": 0,
        "by_severity": {},
        "findings": [],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
