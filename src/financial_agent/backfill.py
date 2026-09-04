"""Materialize recurring obligation instances behind and ahead of today.

Recurring obligations are modeled only with FUTURE instances, so reconciliation
has nothing to match against the real posted transactions. This materializes the
PAST instances each recurring obligation's cadence implies over a trailing window
(idempotent), then reconciles them so matched ones become evidence-backed
"cleared" items in the digest. Past instances never enter the cash-flow
projection (which is forward-only), so the runway is unaffected; this is purely
for the did-it-clear question.

No payment is ever fabricated: an instance is only linked to a transaction by the
normal reconciliation matcher (exact amount near the due date, or amount + date +
merchant), and confirmation/paid still flows through the existing tools.

Manual recurring obligations also need enough future instances to cover the
cash-flow horizon. Their extension is deliberately narrow: only active
``obligations_yaml_manual`` rows are eligible, and generated/source-owned
obligations remain under their owning generators.
"""

from __future__ import annotations

import calendar
import datetime as dt
import sqlite3
from typing import Any

from .obligations import apply_obligation_instances
from .reconciliation import reconcile_obligation_instances
from .schema import ensure_app_schema

# Cadences we can step. Unknown cadences are skipped (no guessing).
_SUPPORTED_CADENCES = {"monthly", "biweekly", "biweekly_estimate", "weekly"}
_FORWARD_CADENCES = {"monthly", "weekly", "biweekly", "quarterly", "semimonthly"}


def _months_back(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 - n
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _months_forward(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _step_back(anchor: dt.date, cadence: str, i: int) -> dt.date | None:
    if cadence == "monthly":
        return _months_back(anchor, i)
    if cadence in ("biweekly", "biweekly_estimate"):
        return anchor - dt.timedelta(days=14 * i)
    if cadence == "weekly":
        return anchor - dt.timedelta(days=7 * i)
    return None


def _step_forward(anchor: dt.date, cadence: str, i: int) -> dt.date | None:
    if cadence == "monthly":
        return _months_forward(anchor, i)
    if cadence == "quarterly":
        return _months_forward(anchor, 3 * i)
    if cadence == "biweekly":
        return anchor + dt.timedelta(days=14 * i)
    if cadence == "weekly":
        return anchor + dt.timedelta(days=7 * i)
    return None


def _semimonthly_anchor_days(due_dates: list[dt.date]) -> tuple[int, int] | None:
    """Return two observed anchors without mistaking a short month for proof."""

    by_month: dict[tuple[int, int], set[int]] = {}
    for due in due_dates:
        by_month.setdefault((due.year, due.month), set()).add(due.day)
    for (year, month), days in sorted(by_month.items()):
        if len(days) != 2:
            continue
        first, second = sorted(days)
        month_end = calendar.monthrange(year, month)[1]
        if second < month_end or month_end == 31:
            return first, second
    return None


def _semimonthly_dates(
    anchor: dt.date,
    anchor_days: tuple[int, int],
):
    """Yield calendar-anchored dates, clamping each anchor to month end."""

    month_anchor = anchor.replace(day=1)
    for month_index in range(400):
        month = _months_forward(month_anchor, month_index)
        month_end = calendar.monthrange(month.year, month.month)[1]
        for day in anchor_days:
            yield month.replace(day=min(day, month_end))


def extend_manual_recurring_instances(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    horizon_days: int,
) -> dict[str, int]:
    """Extend manual recurring outflows through the next post-horizon cycle."""

    savepoint = "extend_manual_recurring_instances"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        ensure_app_schema(conn)
        horizon = dt.date.fromisoformat(as_of_date) + dt.timedelta(
            days=int(horizon_days)
        )
        obligations = conn.execute(
            """
            SELECT id, cadence, active_until
            FROM obligations
            WHERE status = 'active' AND source = 'obligations_yaml_manual'
            ORDER BY id
            """,
        ).fetchall()
        created = 0
        obligations_touched = 0
        now = dt.datetime.now().astimezone().isoformat()

        for obligation in obligations:
            cadence = (obligation["cadence"] or "").strip()
            if cadence not in _FORWARD_CADENCES:
                continue
            template = conn.execute(
                """
                SELECT due_date, amount, direction, source, confidence, notes,
                       amount_status, amount_source, amount_observed_at,
                       statement_close_date, review_after, estimation_method,
                       estimation_inputs_json, cash_flow_treatment,
                       statement_target_obligation_id
                FROM obligation_instances
                WHERE obligation_id = ?
                  AND source = 'obligations_yaml_manual'
                  AND status IN (
                      'expected', 'needs_review', 'partially_paid', 'paid'
                  )
                ORDER BY due_date DESC, id DESC
                LIMIT 1
                """,
                (obligation["id"],),
            ).fetchone()
            if template is None or template["direction"] != "outflow":
                continue

            latest_due = dt.date.fromisoformat(template["due_date"])
            if latest_due > horizon:
                continue
            anchor = latest_due
            manual_dates = [
                dt.date.fromisoformat(row["due_date"])
                for row in conn.execute(
                    """
                    SELECT due_date
                    FROM obligation_instances
                    WHERE obligation_id = ?
                      AND source = 'obligations_yaml_manual'
                      AND status IN (
                          'expected', 'needs_review', 'partially_paid', 'paid'
                      )
                    ORDER BY due_date, id
                    """,
                    (obligation["id"],),
                )
            ]
            if cadence == "monthly":
                anchor = manual_dates[0]
            semimonthly_days = (
                _semimonthly_anchor_days(manual_dates)
                if cadence == "semimonthly"
                else None
            )
            if cadence == "semimonthly" and semimonthly_days is None:
                continue

            active_until = (
                dt.date.fromisoformat(obligation["active_until"])
                if obligation["active_until"]
                else None
            )
            new_for_obligation = 0
            due_dates = (
                _semimonthly_dates(anchor, semimonthly_days)
                if semimonthly_days is not None
                else (
                    _step_forward(anchor, cadence, index)
                    for index in range(1, 400)
                )
            )
            for due in due_dates:
                if due is None or (active_until is not None and due > active_until):
                    break
                if due <= latest_due:
                    continue
                existing = conn.execute(
                    """
                    SELECT 1
                    FROM obligation_instances
                    WHERE obligation_id = ? AND due_date = ?
                    """,
                    (obligation["id"], due.isoformat()),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO obligation_instances (
                            id, obligation_id, due_date, amount, direction, status,
                            source, confidence, notes, amount_status, amount_source,
                            amount_observed_at, statement_close_date, review_after,
                            estimation_method, estimation_inputs_json,
                            cash_flow_treatment, statement_target_obligation_id,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'expected', ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"{obligation['id']}:{due.isoformat()}",
                            obligation["id"],
                            due.isoformat(),
                            template["amount"],
                            template["direction"],
                            template["source"],
                            template["confidence"],
                            template["notes"],
                            template["amount_status"],
                            template["amount_source"],
                            template["amount_observed_at"],
                            template["statement_close_date"],
                            template["review_after"],
                            template["estimation_method"],
                            template["estimation_inputs_json"],
                            template["cash_flow_treatment"],
                            template["statement_target_obligation_id"],
                            now,
                            now,
                        ),
                    )
                    created += 1
                    new_for_obligation += 1
                if due > horizon:
                    break
            if new_for_obligation:
                obligations_touched += 1

        result = {
            "instances_created": created,
            "obligations_touched": obligations_touched,
        }
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    conn.execute(f"RELEASE {savepoint}")
    return result


def backfill_recurring_instances(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    lookback_days: int = 90,
    reconcile: bool = True,
) -> dict[str, Any]:
    """Create past-due instances for active recurring obligations, then reconcile."""

    ensure_app_schema(conn)
    as_of = dt.date.fromisoformat(as_of_date)
    floor = as_of - dt.timedelta(days=int(lookback_days))

    obligations = conn.execute(
        "SELECT id, name, kind, cadence, status, source FROM obligations WHERE status = 'active'"
    ).fetchall()

    created = 0
    obligations_touched = 0
    for ob in obligations:
        cadence = (ob["cadence"] or "").strip()
        if cadence not in _SUPPORTED_CADENCES:
            continue
        template = conn.execute(
            "SELECT due_date, amount, direction, cash_flow_treatment FROM obligation_instances "
            "WHERE obligation_id = ? ORDER BY due_date LIMIT 1",
            (ob["id"],),
        ).fetchone()
        if template is None:
            continue
        # Only backfill OUTFLOWS. "Did it clear?" is about bills; backfilling income
        # (reimbursements, paychecks) would surface as bogus "missing"/"may still
        # owe" items and inflate drift.
        if (template["direction"] or "outflow") != "outflow":
            continue
        anchor = dt.date.fromisoformat(template["due_date"])

        new_instances: list[dict[str, Any]] = []
        for i in range(1, 400):  # generous cap; the floor/break ends it
            d = _step_back(anchor, cadence, i)
            if d is None or d < floor:
                break
            if not (floor <= d < as_of):
                continue
            iid = f"{ob['id']}:{d.isoformat()}"
            if conn.execute("SELECT 1 FROM obligation_instances WHERE id = ?", (iid,)).fetchone():
                continue
            inst = {
                "id": iid,
                "due_date": d.isoformat(),
                "amount": template["amount"],
                "direction": template["direction"],
                "source": "backfill",
                "status": "expected",
            }
            if template["cash_flow_treatment"] is not None:
                inst["cash_flow_treatment"] = template["cash_flow_treatment"]
            new_instances.append(inst)

        if new_instances:
            apply_obligation_instances(
                conn,
                obligation={"id": ob["id"], "name": ob["name"], "kind": ob["kind"],
                            "cadence": cadence, "status": "active", "source": ob["source"]},
                instances=new_instances,
            )
            created += len(new_instances)
            obligations_touched += 1

    result: dict[str, Any] = {"instances_created": created, "obligations_touched": obligations_touched}
    if reconcile:
        result["reconcile"] = reconcile_obligation_instances(conn, as_of_date=as_of_date)
        # Keep as "cleared" ONLY backfilled history we can prove (has a recorded
        # match); CANCEL the rest. An unmatched backfilled instance is ambiguous -
        # paid-but-unmatched (variable amount, autopay posting off its modeled day)
        # vs genuinely unpaid - and the matchers can't tell them apart. Left as
        # 'expected' it becomes a false CRITICAL drift "did your payment fail?" alarm
        # on bills that actually cleared. Canceling (not deleting) keeps the backfill
        # idempotent and excludes it from drift. Only backfill rows are touched.
        canceled = conn.execute(
            "UPDATE obligation_instances SET status = 'canceled' WHERE source = 'backfill' "
            "AND due_date < ? AND status = 'expected' "
            "AND id NOT IN (SELECT obligation_instance_id FROM transaction_obligation_matches)",
            (as_of.isoformat(),),
        ).rowcount
        result["unmatched_canceled"] = canceled
        result["cleared_kept"] = conn.execute(
            "SELECT COUNT(*) FROM obligation_instances WHERE source = 'backfill' AND status != 'canceled'"
        ).fetchone()[0]
    return result


def list_recently_cleared(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    lookback_days: int = 30,
) -> list[dict[str, Any]]:
    """Obligations whose due instance has a recorded transaction match in the
    trailing window - i.e. payments that have (likely) cleared. ``auto`` matches
    read as cleared; ``needs_review`` as 'likely, confirm'."""

    ensure_app_schema(conn)
    as_of = dt.date.fromisoformat(as_of_date)
    floor = (as_of - dt.timedelta(days=int(lookback_days))).isoformat()
    rows = conn.execute(
        """
        SELECT oi.id AS instance_id, o.name AS obligation_name, oi.due_date, oi.amount, oi.direction,
               m.transaction_id, m.match_type, m.match_score, t.posted, t.amount AS txn_amount
        FROM transaction_obligation_matches m
        JOIN obligation_instances oi ON oi.id = m.obligation_instance_id
        JOIN obligations o ON o.id = oi.obligation_id
        LEFT JOIN transactions t ON t.id = m.transaction_id
        WHERE oi.due_date >= ? AND oi.due_date <= ?
          -- Only confident (auto) matches read as "cleared". needs_review matches
          -- belong in "Matches to Confirm" (mutually exclusive, no double-listing).
          AND m.match_type = 'auto'
        ORDER BY oi.due_date DESC
        """,
        (floor, as_of.isoformat()),
    ).fetchall()
    return [
        {
            "obligation_name": r["obligation_name"],
            "due_date": r["due_date"],
            "amount": round(float(r["amount"]), 2),
            "transaction_id": r["transaction_id"],
            "posted": r["posted"],
            "txn_amount": round(float(r["txn_amount"]), 2) if r["txn_amount"] is not None else None,
            "cleared": r["match_type"] == "auto",
            "match_type": r["match_type"],
            "match_score": round(float(r["match_score"]), 3),
        }
        for r in rows
    ]
