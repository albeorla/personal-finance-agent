"""Drift detection: evidence-backed findings about what needs attention.

Drift is the gap between the plan and reality. This module turns that gap into
explicit, deterministic findings instead of leaving it to ad-hoc reasoning:

- ``missing_expected``  - a past-due obligation with no matching transaction.
- ``stale_estimate``    - an estimated amount whose review date has arrived.
- ``amount_changed``    - a matched transaction that differs materially from the
                          expected amount.
- ``unexpected_recurring`` - a recurring charge pattern discovered by onboarding
                          that has not been turned into an obligation yet.

Findings are conservative: a missing obligation is flagged for review, never
auto-declared overdue. Detection is read-only by default for status; the
standalone tool persists findings (idempotent upsert, with disappeared findings
marked resolved).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from .onboarding import CANDIDATE_TYPE_PRIORITY_WEIGHT
from .reconciliation import find_transaction_matches
from .schema import ensure_app_schema


def _recurring_monthly_impact(row: Any) -> float:
    """Recover the real estimated monthly dollar impact of a recurring candidate.

    priority_score = amount * monthly_rate * type_weight (a ranking score), so the
    monthly dollars are priority_score / type_weight. Without this, the digest
    renders the weighted ranking score as if it were "$/mo".
    """

    weight = CANDIDATE_TYPE_PRIORITY_WEIGHT.get(row["candidate_type"], 0.5) or 0.5
    return round(float(row["priority_score"] or 0.0) / weight, 2)


SEVERITY_RANK: dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1}

DEFAULT_OPTIONS: dict[str, Any] = {
    "grace_period_days": 7,
    "critical_age_days": 30,
    "amount_change_threshold": 0.20,
    # deposit_arrived: how far back to look for surprise inflows, and the floor
    # under which a credit is treated as noise (interest, cashback, tiny refunds).
    "deposit_lookback_days": 14,
    "deposit_min_amount": 50.0,
}

# Same-charge gate for amount_changed. To call a past-due obligation "paid but at
# a different amount" (vs unpaid), the candidate transaction must corroborate on
# BOTH merchant and date - not either alone. The old `merchant>0 OR date<=1` gate
# matched unrelated charges: an electric bill to a YouTube TV charge a day later,
# a car-lease bill to an Amex payment - then recommended overwriting the real
# amount. A miss here only downgrades to missing_expected ("confirm whether
# paid"), so leaning to precision protects the modeled numbers.
#
# Merchant corroboration = at least one shared meaningful token (merchant_score >
# 0). The tokenizer already drops stopwords and tokens < 3 chars, so any positive
# score is a real shared word - which cleanly separates a same-merchant amount
# change (Eversource->Eversource shares "eversource") from a coincidental nearby
# charge (Eversource vs "Youtube TV" shares nothing). SAME_CHARGE_DATE_DAYS is the
# date-proximity window, a tunable knob.
SAME_CHARGE_DATE_DAYS = 3

_RECONCILABLE_STATUSES = ("expected", "needs_review", "partially_paid")
_RECURRING_CANDIDATE_TYPES = ("direct_checking_outflow", "card_statement_input", "inflow")
_ACTIVE_CANDIDATE_STATUSES = ("discovered", "proposed", "in_review")


def detect_drift(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
    options: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Detect all drift types and return findings ordered by severity.

    When ``persist`` is true, findings are upserted into ``drift_findings`` and
    any previously-active finding that no longer appears is marked resolved.
    Status calls this with ``persist=False`` to stay read-only.
    """

    ensure_app_schema(conn)
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    as_of = _coerce_date(as_of_date)

    findings: list[dict[str, Any]] = []
    findings += find_payment_drift(conn, as_of, opts)
    findings += find_stale_estimates(conn, as_of, opts)
    findings += find_unexpected_recurring(conn, opts)
    findings += find_arrived_deposits(conn, as_of, opts)
    findings.sort(key=lambda f: (-SEVERITY_RANK[f["severity"]], f["finding_type"], f["id"]))

    if persist:
        _persist(conn, findings, as_of)

    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for finding in findings:
        by_type[finding["finding_type"]] = by_type.get(finding["finding_type"], 0) + 1
        by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1

    return {
        "as_of_date": as_of.isoformat(),
        "count": len(findings),
        "by_type": by_type,
        "by_severity": by_severity,
        "findings": findings,
    }


def find_payment_drift(
    conn: sqlite3.Connection, as_of: date, opts: dict[str, Any]
) -> list[dict[str, Any]]:
    """Classify each past-due obligation as cleanly matched, amount-changed, or missing.

    A single pass guarantees the three are mutually exclusive: a charge that
    happened but at a materially different amount is ``amount_changed``, not
    ``missing_expected``. A clean (auto) match produces no finding.
    """

    grace = int(opts["grace_period_days"])
    critical_age = int(opts["critical_age_days"])
    threshold = float(opts["amount_change_threshold"])
    loose = {"amount_pct_tolerance": 0.5, "amount_abs_tolerance": 5.0}

    rows = conn.execute(
        f"""
        SELECT oi.id, oi.obligation_id, o.name AS obligation_name, oi.due_date,
               oi.amount, oi.direction, oi.status
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE oi.status IN ({",".join("?" for _ in _RECONCILABLE_STATUSES)})
          AND oi.due_date <= ?
          AND o.status = 'active'
          AND COALESCE(oi.cash_flow_treatment, 'direct_checking') != 'card_statement_input'
        ORDER BY oi.due_date, oi.id
        """,
        (*_RECONCILABLE_STATUSES, as_of.isoformat()),
    ).fetchall()

    findings: list[dict[str, Any]] = []
    for inst in rows:
        age = (as_of - _coerce_date(inst["due_date"])).days
        expected = round(abs(float(inst["amount"])), 2)
        # Cash-flow convention (matches cashflow._signed_amount): outflow lowers
        # the balance (negative), inflow raises it (positive).
        signed = -expected if inst["direction"] == "outflow" else expected

        strict = find_transaction_matches(conn, obligation_instance=dict(inst))
        if any(m["match_type"] == "auto" for m in strict):
            continue  # cleanly reconciled

        approx = find_transaction_matches(conn, obligation_instance=dict(inst), options=loose)
        present = approx[0] if approx else None
        # Require merchant corroboration AND date proximity (not either alone):
        # otherwise an unrelated charge on a nearby date is mistaken for this bill
        # paid at a different amount, corrupting the modeled number.
        same_charge = (
            present is not None
            and present["merchant_score"] > 0.0
            and abs(present["date_delta_days"]) <= SAME_CHARGE_DATE_DAYS
            and _account_consistent(conn, inst["obligation_id"], present.get("txn_account_id"))
        )

        if same_charge and expected > 0:
            observed = round(abs(float(present["txn_amount"])), 2)
            pct = abs(observed - expected) / expected
            if pct > threshold:
                findings.append(
                    {
                        "id": f"drift:amount_changed:{inst['id']}",
                        "finding_type": "amount_changed",
                        "severity": "medium",
                        "obligation_id": inst["obligation_id"],
                        "obligation_instance_id": inst["id"],
                        "related_transaction_ids": [present["transaction_id"]],
                        "cash_flow_impact": round((observed - expected) * (-1 if inst["direction"] == "outflow" else 1), 2),
                        "confidence": "medium",
                        "evidence": {
                            "obligation_name": inst["obligation_name"],
                            "expected_amount": expected,
                            "observed_amount": observed,
                            "pct_change": round(pct, 3),
                            "transaction_id": present["transaction_id"],
                            "due_date": inst["due_date"],
                        },
                        "recommended_action": "Review the changed amount and update the obligation's expected amount/profile.",
                    }
                )
            # within threshold: the charge happened at roughly the expected amount; not drift.
            continue

        if age > grace:
            findings.append(
                {
                    "id": f"drift:missing_expected:{inst['id']}",
                    "finding_type": "missing_expected",
                    "severity": "critical" if age > critical_age else "high",
                    "obligation_id": inst["obligation_id"],
                    "obligation_instance_id": inst["id"],
                    "related_transaction_ids": [],
                    "cash_flow_impact": signed,
                    "confidence": "medium",
                    "evidence": {
                        "obligation_name": inst["obligation_name"],
                        "due_date": inst["due_date"],
                        "age_days": age,
                        "expected_amount": expected,
                        "direction": inst["direction"],
                    },
                    "recommended_action": "Confirm whether this obligation was paid; match a transaction or update the plan.",
                }
            )
    return findings


def _account_consistent(
    conn: sqlite3.Connection, obligation_id: str, txn_account_id: str | None
) -> bool:
    """Same-account gate for amount_changed: the candidate must post to an account
    this obligation has actually settled from before (confirmed matches). A bill
    that always clears checking is never "paid at a new amount" by a card charge.
    No payment history means no constraint (cold start), so the merchant/date
    gates alone decide.
    """

    if not txn_account_id:
        return True
    rows = conn.execute(
        """
        SELECT DISTINCT t.account_id
        FROM obligation_instances oi
        JOIN transactions t ON t.id = oi.matched_transaction_id
        WHERE oi.obligation_id = ? AND oi.matched_transaction_id IS NOT NULL
        """,
        (obligation_id,),
    ).fetchall()
    known = {r["account_id"] for r in rows if r["account_id"]}
    return not known or txn_account_id in known


def find_stale_estimates(
    conn: sqlite3.Connection, as_of: date, opts: dict[str, Any]
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT oi.id, oi.obligation_id, o.name AS obligation_name, oi.due_date,
               oi.amount, oi.direction, oi.amount_source, oi.review_after,
               oi.statement_close_date
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE oi.status IN ({",".join("?" for _ in _RECONCILABLE_STATUSES)})
          AND oi.amount_status = 'estimated'
          AND oi.review_after IS NOT NULL
          AND oi.review_after <= ?
          AND o.status = 'active'
        ORDER BY oi.review_after, oi.due_date, oi.id
        """,
        (*_RECONCILABLE_STATUSES, as_of.isoformat()),
    ).fetchall()
    findings: list[dict[str, Any]] = []
    for inst in rows:
        amount = round(abs(float(inst["amount"])), 2)
        findings.append(
            {
                "id": f"drift:stale_estimate:{inst['id']}",
                "finding_type": "stale_estimate",
                "severity": "high",
                "obligation_id": inst["obligation_id"],
                "obligation_instance_id": inst["id"],
                "related_transaction_ids": [],
                "cash_flow_impact": -amount if inst["direction"] == "outflow" else amount,
                "confidence": "high",
                "evidence": {
                    "obligation_name": inst["obligation_name"],
                    "due_date": inst["due_date"],
                    "amount_source": inst["amount_source"],
                    "review_after": inst["review_after"],
                    "statement_close_date": inst["statement_close_date"],
                    "current_estimate": amount,
                },
                "recommended_action": "Refresh the amount from the source (portal/bill) now that the statement has closed.",
            }
        )
    return findings


def find_unexpected_recurring(
    conn: sqlite3.Connection, opts: dict[str, Any]
) -> list[dict[str, Any]]:
    if not _has_table(conn, "charge_onboarding_candidates"):
        return []
    rows = conn.execute(
        f"""
        SELECT id, merchant_key, display_name, candidate_type, cash_flow_treatment,
               priority_score, evidence_count, confidence
        FROM charge_onboarding_candidates
        WHERE status IN ({",".join("?" for _ in _ACTIVE_CANDIDATE_STATUSES)})
          -- Unmodeled = not linked to an obligation, OR linked to a "dead" one that
          -- has no projectable (expected/needs_review/partially_paid) instance. The
          -- latter catches a recurring charge whose only instance was canceled
          -- (e.g. a car payment mis-imported as a Todoist one-off) but still posts -
          -- otherwise it is invisible in every section while still draining checking.
          AND (existing_obligation_id IS NULL
               OR existing_obligation_id NOT IN (
                   SELECT obligation_id FROM obligation_instances
                   WHERE status IN ('expected', 'needs_review', 'partially_paid')
               ))
          AND candidate_type IN ({",".join("?" for _ in _RECURRING_CANDIDATE_TYPES)})
        ORDER BY priority_score DESC, merchant_key
        """,
        (*_ACTIVE_CANDIDATE_STATUSES, *_RECURRING_CANDIDATE_TYPES),
    ).fetchall()
    findings: list[dict[str, Any]] = []
    for row in rows:
        findings.append(
            {
                "id": f"drift:unexpected_recurring:{row['id']}",
                "finding_type": "unexpected_recurring",
                "severity": "low",
                "obligation_id": None,
                "obligation_instance_id": None,
                "related_transaction_ids": [],
                "cash_flow_impact": round(float(row["priority_score"] or 0.0), 2),
                "confidence": row["confidence"] or "low",
                "evidence": {
                    "candidate_id": row["id"],
                    "merchant": row["display_name"],
                    "candidate_type": row["candidate_type"],
                    "cash_flow_treatment": row["cash_flow_treatment"],
                    "evidence_count": row["evidence_count"],
                    "estimated_monthly_impact": _recurring_monthly_impact(row),
                },
                "recommended_action": "Review this discovered recurring charge in the onboarding queue and apply it if it should be modeled.",
            }
        )
    return findings


# Wording that means an internal account-to-account move (savings->checking),
# not new money. A reimbursement via Zelle/Venmo/check is real inflow and must
# NOT be filtered, so the list stays deliberately narrow.
# ponytail: keyword heuristic; upgrade to a matched-transfer pair check if
# internal transfers ever slip through as false deposits.
_TRANSFER_HINTS = ("transfer", "xfer")


def _looks_like_transfer(payee: str | None, description: str | None) -> bool:
    text = f"{payee or ''} {description or ''}".lower()
    return any(hint in text for hint in _TRANSFER_HINTS)


def find_arrived_deposits(
    conn: sqlite3.Connection, as_of: date, opts: dict[str, Any]
) -> list[dict[str, Any]]:
    """Flag recent positive deposits to the working account that are NOT matched
    to scheduled payroll/income.

    This is an OBSERVED event ("a deposit arrived"), not a new projected income
    stream: a one-off reimbursement should be noticed, not extrapolated into a
    recurring paycheck. Conservative by construction - only the account income is
    tied to, only unmatched inflows (scheduled payroll reconciles and drops out),
    above a noise floor, and internal transfers are filtered.
    """

    if not _has_table(conn, "transactions"):
        return []
    accounts = [
        r["working_account_id"]
        for r in conn.execute(
            "SELECT DISTINCT working_account_id FROM income_sources "
            "WHERE working_account_id IS NOT NULL"
        ).fetchall()
    ]
    if not accounts:
        return []

    lookback = int(opts["deposit_lookback_days"])
    min_amount = float(opts["deposit_min_amount"])
    start = (as_of - timedelta(days=lookback)).isoformat()
    placeholders = ",".join("?" for _ in accounts)
    rows = conn.execute(
        f"""
        SELECT t.id, t.account_id, t.posted, t.transacted_at, t.amount, t.payee,
               t.description
        FROM transactions t
        WHERE t.account_id IN ({placeholders})
          AND t.amount >= ?
          AND COALESCE(t.pending, 0) = 0
          AND substr(COALESCE(t.posted, t.transacted_at), 1, 10) >= ?
          AND substr(COALESCE(t.posted, t.transacted_at), 1, 10) <= ?
          AND t.id NOT IN (
              SELECT matched_transaction_id FROM obligation_instances
              WHERE matched_transaction_id IS NOT NULL
          )
          AND t.id NOT IN (SELECT transaction_id FROM transaction_obligation_matches)
        ORDER BY substr(COALESCE(t.posted, t.transacted_at), 1, 10) DESC, t.id
        """,
        (*accounts, min_amount, start, as_of.isoformat()),
    ).fetchall()

    findings: list[dict[str, Any]] = []
    for txn in rows:
        if _looks_like_transfer(txn["payee"], txn["description"]):
            continue
        amount = round(float(txn["amount"]), 2)
        posted_date = str(txn["posted"] or txn["transacted_at"] or "")[:10]
        findings.append(
            {
                "id": f"drift:deposit_arrived:{txn['id']}",
                "finding_type": "deposit_arrived",
                "severity": "low",
                "obligation_id": None,
                "obligation_instance_id": None,
                "related_transaction_ids": [txn["id"]],
                "cash_flow_impact": amount,
                "confidence": "medium",
                "evidence": {
                    "transaction_id": txn["id"],
                    "account_id": txn["account_id"],
                    "payee": txn["payee"],
                    "amount": amount,
                    "posted_date": posted_date,
                },
                "recommended_action": "A deposit arrived that isn't scheduled income. Confirm what it is; do not model it as recurring income.",
            }
        )
    return findings


def list_drift_findings(
    conn: sqlite3.Connection,
    *,
    status: str | None = "active",
    finding_type: str | None = None,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if finding_type is not None:
        where.append("finding_type = ?")
        params.append(finding_type)
    query = "SELECT * FROM drift_findings"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY severity, finding_type, id"
    rows = conn.execute(query, params).fetchall()
    return [_row_to_finding(r) for r in rows]


# --- persistence -----------------------------------------------------------


# Evidence keys that tick daily without the finding materially changing (a
# missing_expected ages by one day every day). Excluded from the change
# signature so updated_at stays put and the surface queue can snooze an
# unchanged finding instead of re-alerting daily.
_VOLATILE_EVIDENCE_KEYS: frozenset[str] = frozenset({"age_days"})


def _content_signature(severity: str, cash_flow_impact: Any, evidence: dict[str, Any] | None) -> str:
    stable = {k: v for k, v in (evidence or {}).items() if k not in _VOLATILE_EVIDENCE_KEYS}
    return json.dumps([severity, cash_flow_impact, stable], sort_keys=True)


def _persist(conn: sqlite3.Connection, findings: list[dict[str, Any]], as_of: date) -> None:
    now = _now()
    live_ids = {f["id"] for f in findings}
    existing = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, severity, cash_flow_impact, evidence_json, updated_at "
            "FROM drift_findings WHERE status = 'active'"
        ).fetchall()
    }
    for finding in findings:
        # updated_at means "when this finding's content last changed", not "last
        # seen" (as_of_date tracks that). An unchanged re-detect keeps the old
        # timestamp so downstream surfacing can tell new/changed from repeat.
        prior = existing.get(finding["id"])
        updated_at = now
        if prior is not None:
            prior_evidence = json.loads(prior["evidence_json"]) if prior["evidence_json"] else None
            if _content_signature(prior["severity"], prior["cash_flow_impact"], prior_evidence) == _content_signature(
                finding["severity"], finding["cash_flow_impact"], finding["evidence"]
            ):
                updated_at = prior["updated_at"]
        conn.execute(
            """
            INSERT INTO drift_findings (
                id, finding_type, severity, obligation_id, obligation_instance_id,
                related_transaction_ids_json, cash_flow_impact, confidence,
                evidence_json, recommended_action, status, as_of_date,
                created_at, updated_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                finding_type = excluded.finding_type,
                severity = excluded.severity,
                obligation_id = excluded.obligation_id,
                obligation_instance_id = excluded.obligation_instance_id,
                related_transaction_ids_json = excluded.related_transaction_ids_json,
                cash_flow_impact = excluded.cash_flow_impact,
                confidence = excluded.confidence,
                evidence_json = excluded.evidence_json,
                recommended_action = excluded.recommended_action,
                status = 'active',
                as_of_date = excluded.as_of_date,
                updated_at = excluded.updated_at,
                resolved_at = NULL
            """,
            (
                finding["id"], finding["finding_type"], finding["severity"],
                finding["obligation_id"], finding["obligation_instance_id"],
                json.dumps(finding["related_transaction_ids"], sort_keys=True),
                finding["cash_flow_impact"], finding["confidence"],
                json.dumps(finding["evidence"], sort_keys=True), finding["recommended_action"],
                as_of.isoformat(), now, updated_at,
            ),
        )
    # Resolve findings that no longer appear.
    for row in conn.execute("SELECT id FROM drift_findings WHERE status = 'active'").fetchall():
        if row["id"] not in live_ids:
            conn.execute(
                "UPDATE drift_findings SET status = 'resolved', resolved_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )


def _row_to_finding(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "finding_type": row["finding_type"],
        "severity": row["severity"],
        "obligation_id": row["obligation_id"],
        "obligation_instance_id": row["obligation_instance_id"],
        "related_transaction_ids": json.loads(row["related_transaction_ids_json"]) if row["related_transaction_ids_json"] else [],
        "cash_flow_impact": row["cash_flow_impact"],
        "confidence": row["confidence"],
        "evidence": json.loads(row["evidence_json"]) if row["evidence_json"] else None,
        "recommended_action": row["recommended_action"],
        "status": row["status"],
        "as_of_date": row["as_of_date"],
        "resolved_at": row["resolved_at"],
    }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone() is not None


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _now() -> str:
    return datetime.now().astimezone().isoformat()
