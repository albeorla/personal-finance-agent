"""Operating guardrails as explicit, evidence-backed status warnings (slice I).

These carry forward the legacy ritual's rules-of-thumb as deterministic checks:

- cash_floor    - projected lowest balance must not drop below $2,500.
- drift_threshold - total drift cash-flow impact must not exceed $200.
- window_age    - the data must be fresher than 24h.
- debt_avalanche - advisory payoff order, highest APR first.

Thresholds are hard-coded from the legacy playbooks for V1 (a per-user config
table is future work). Evaluation is read-only by default; status calls it that
way. The thresholds came from ritual.md ($200 drift) and playbooks.md ($2,500
floor) in ~/dev/areas/finances.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, date, datetime
from typing import Any

from .cashflow import build_cash_flow_projections
from .schema import ensure_app_schema


CASH_FLOOR = 2500.0
DRIFT_THRESHOLD = 200.0
WINDOW_AGE_MAX_HOURS = 24.0

# APR-ordered payoff priority from the legacy playbook (highest first).
DEBT_AVALANCHE_APR_ORDER = [
    {"key": "amex_platinum", "apr": 21.74},
    {"key": "apple_card", "apr": 19.49},
    {"key": "chase_visa", "apr": 17.74},
    {"key": "amex_personal_loan", "apr": 7.49},
]

_DEBT_KINDS = ("credit_card_statement", "loan", "card_spend_input")
_DRIFT_IMPACT_TYPES = ("missing_expected", "amount_changed")


def apply_guardrail_rules(conn: sqlite3.Connection) -> dict[str, Any]:
    """Idempotently seed the default guardrail rules."""

    ensure_app_schema(conn)
    now = _now()
    rules = [
        ("cash_floor", CASH_FLOOR, None, "high", "projected lowest balance must not drop below the cash floor"),
        ("drift_threshold", DRIFT_THRESHOLD, None, "medium", "total drift cash-flow impact must not exceed the threshold"),
        ("window_age", WINDOW_AGE_MAX_HOURS, None, "low", "source data must be fresher than the max age in hours"),
        ("debt_avalanche", None, json.dumps({"apr_order": DEBT_AVALANCHE_APR_ORDER}, sort_keys=True), "low", "pay highest-APR debt first"),
    ]
    seeded = 0
    for rule_type, value, payload, severity, desc in rules:
        cur = conn.execute(
            """
            INSERT INTO guardrail_rules (id, rule_type, threshold_value, threshold_json, severity_default, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_type) DO NOTHING
            """,
            (f"guardrail_rule_{rule_type}", rule_type, value, payload, severity, desc, now),
        )
        seeded += cur.rowcount
    return {"seeded": seeded, "rules": [r[0] for r in rules]}


def evaluate_guardrails(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
    accounts: list[dict[str, Any]] | None = None,
    drift_findings: list[dict[str, Any]] | None = None,
    windows: tuple[int, ...] = (7, 14, 30),
    now: datetime | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    """Evaluate all guardrails and return findings ordered by severity."""

    ensure_app_schema(conn)
    apply_guardrail_rules(conn)
    as_of = _coerce_date(as_of_date)
    observed = now or datetime.now(UTC)

    findings: list[dict[str, Any]] = []
    findings += _check_cash_floor(conn, as_of, accounts, windows)
    findings += _check_drift_threshold(conn, as_of, drift_findings)
    findings += _check_window_age(conn, observed)
    findings += _check_debt_avalanche(conn)
    findings.sort(key=lambda f: (-_severity_rank(f["severity"]), f["rule_type"], f["id"]))

    if persist:
        _persist(conn, findings, as_of)

    return {
        "as_of_date": as_of.isoformat(),
        "count": len(findings),
        "findings": findings,
    }


def list_guardrail_findings(
    conn: sqlite3.Connection,
    *,
    evaluation_date: str | None = None,
    rule_type: str | None = None,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where: list[str] = []
    params: list[Any] = []
    if evaluation_date is not None:
        where.append("evaluation_date = ?")
        params.append(evaluation_date)
    if rule_type is not None:
        where.append("rule_type = ?")
        params.append(rule_type)
    query = "SELECT rule_type, evaluation_date, passed, finding_json FROM guardrail_evaluations"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY evaluation_date DESC, rule_type"
    return [
        {
            "rule_type": r["rule_type"],
            "evaluation_date": r["evaluation_date"],
            "passed": bool(r["passed"]),
            "finding": json.loads(r["finding_json"]) if r["finding_json"] else None,
        }
        for r in conn.execute(query, params).fetchall()
    ]


# --- checks ----------------------------------------------------------------


def _check_cash_floor(conn, as_of, accounts, windows) -> list[dict[str, Any]]:
    if accounts is None:
        accounts = _latest_accounts(conn)
    if not accounts:
        return []
    projections, _ = build_cash_flow_projections(conn, accounts=accounts, windows=list(windows), start_date=as_of)
    findings: list[dict[str, Any]] = []
    for p in projections:
        lowest = p.get("lowest_balance")
        if lowest is None or lowest >= CASH_FLOOR:
            continue
        window = p["window_days"]
        findings.append(_finding(
            "cash_floor", f"guardrail:cash_floor:{window}d",
            "high" if window <= 7 else "medium",
            f"Projected low of ${lowest:,.2f} in the {window}-day window is below the ${CASH_FLOOR:,.0f} cash floor.",
            {"window_days": window, "lowest_balance": lowest, "floor": CASH_FLOOR, "lowest_balance_date": p.get("lowest_balance_date")},
            cash_flow_impact=round(lowest - CASH_FLOOR, 2),
        ))
    return findings


def _check_drift_threshold(conn, as_of, drift_findings) -> list[dict[str, Any]]:
    if drift_findings is None:
        from .drift import detect_drift

        drift_findings = detect_drift(conn, as_of_date=as_of, persist=False)["findings"]
    total = round(sum(abs(float(f.get("cash_flow_impact") or 0.0))
                      for f in drift_findings if f.get("finding_type") in _DRIFT_IMPACT_TYPES), 2)
    if total <= DRIFT_THRESHOLD:
        return []
    return [_finding(
        "drift_threshold", "guardrail:drift_sum_exceeded", "medium",
        f"Total drift impact ${total:,.2f} exceeds the ${DRIFT_THRESHOLD:,.0f} review threshold.",
        {"total_drift_impact": total, "threshold": DRIFT_THRESHOLD,
         "contributing": [f["id"] for f in drift_findings if f.get("finding_type") in _DRIFT_IMPACT_TYPES]},
        cash_flow_impact=total,
    )]


def _check_window_age(conn, observed) -> list[dict[str, Any]]:
    if not _has_table(conn, "sync_runs"):
        return []
    row = conn.execute("SELECT finished_at FROM sync_runs ORDER BY finished_at DESC, id DESC LIMIT 1").fetchone()
    if row is None or not row["finished_at"]:
        return [_finding("window_age", "guardrail:window_age_unknown", "low",
                         "No sync run recorded; data freshness is unknown.", {"latest_sync": None})]
    finished = _parse_dt(row["finished_at"])
    age_hours = round((observed - finished).total_seconds() / 3600, 2)
    if age_hours <= WINDOW_AGE_MAX_HOURS:
        return []
    return [_finding("window_age", "guardrail:window_age_stale", "low",
                     f"Latest sync is {age_hours:.1f}h old (> {WINDOW_AGE_MAX_HOURS:.0f}h); refresh before trusting balances.",
                     {"age_hours": age_hours, "max_hours": WINDOW_AGE_MAX_HOURS, "latest_sync": finished.isoformat()})]


def _check_debt_avalanche(conn) -> list[dict[str, Any]]:
    # Advisory: only surface when interest-bearing debt obligations exist.
    placeholders = ",".join("?" for _ in _DEBT_KINDS)
    has_debt = conn.execute(
        f"SELECT 1 FROM obligations WHERE status='active' AND kind IN ({placeholders}) LIMIT 1", _DEBT_KINDS
    ).fetchone()
    if has_debt is None:
        return []
    return [_finding("debt_avalanche", "guardrail:debt_avalanche_order", "low",
                     "Configured debt-payoff order (highest APR first; from policy, not live balances): "
                     + " > ".join(f"{d['key']} ({d['apr']}%)" for d in DEBT_AVALANCHE_APR_ORDER),
                     {"apr_order": DEBT_AVALANCHE_APR_ORDER}, advisory=True)]


# --- helpers ---------------------------------------------------------------


def _finding(rule_type, finding_id, severity, message, evidence, cash_flow_impact=None, advisory=False) -> dict[str, Any]:
    return {
        "id": finding_id,
        "rule_type": rule_type,
        "finding_type": f"guardrail_{rule_type}",
        "severity": severity,
        "advisory": advisory,
        "message": message,
        "cash_flow_impact": cash_flow_impact,
        "evidence": evidence,
    }


def _persist(conn, findings, as_of) -> None:
    now = _now()
    firing = {f["rule_type"] for f in findings}
    for finding in findings:
        conn.execute(
            "INSERT INTO guardrail_evaluations (id, rule_type, evaluation_date, passed, finding_json, created_at) VALUES (?, ?, ?, 0, ?, ?)",
            (f"geval_{uuid.uuid4().hex[:12]}", finding["rule_type"], as_of.isoformat(), json.dumps(finding, sort_keys=True), now),
        )
    # Record a 'passed' row for rules that did not fire.
    for rule_type in ("cash_floor", "drift_threshold", "window_age"):
        if rule_type not in firing:
            conn.execute(
                "INSERT INTO guardrail_evaluations (id, rule_type, evaluation_date, passed, finding_json, created_at) VALUES (?, ?, ?, 1, NULL, ?)",
                (f"geval_{uuid.uuid4().hex[:12]}", rule_type, as_of.isoformat(), now),
            )


def _latest_accounts(conn) -> list[dict[str, Any]]:
    if not _has_table(conn, "balance_snapshots"):
        return []
    rows = conn.execute(
        """
        SELECT a.id AS account_id, a.name AS account_name, a.kind, bs.available, bs.recorded_at
        FROM balance_snapshots bs JOIN accounts a ON a.id = bs.account_id
        WHERE bs.id = (SELECT inner_bs.id FROM balance_snapshots inner_bs
                       WHERE inner_bs.account_id = bs.account_id
                       ORDER BY inner_bs.recorded_at DESC, inner_bs.id DESC LIMIT 1)
        """
    ).fetchall()
    return [{"account_id": r["account_id"], "account_name": r["account_name"], "kind": r["kind"],
             "available": round(float(r["available"]), 2), "recorded_at": r["recorded_at"]} for r in rows]


def _has_table(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)).fetchone() is not None


def _severity_rank(severity: str) -> int:
    return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(severity, 0)


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _now() -> str:
    return datetime.now().astimezone().isoformat()
