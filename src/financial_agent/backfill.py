"""Historical backfill so "did my rent / Amex / Apple payment clear?" is answerable.

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
_SUPPORTED_CADENCES = {"monthly", "biweekly", "biweekly_estimate", "weekly", "semimonthly"}


def _months_back(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 - n
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
    if cadence == "semimonthly":
        return anchor - dt.timedelta(days=15 * i)
    return None


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

    conn.commit()
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
        conn.commit()
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


def dedupe_todoist_recurring_duplicates(conn: sqlite3.Connection, *, as_of_date: str) -> dict[str, Any]:
    """Cancel future instances of a Todoist-imported obligation that DUPLICATES a
    proper recurring obligation (e.g. the one-off "New York Times" $28.62 vs the
    recurring "New York Times subscription" $30.30). Conservative: only when the
    Todoist obligation's significant name tokens are a subset of a non-Todoist
    active obligation's, so unrelated items are never canceled. Reversible
    (status -> canceled, not deleted)."""

    from .migration import _label_tokens

    ensure_app_schema(conn)
    as_of = dt.date.fromisoformat(as_of_date).isoformat()
    now = dt.datetime.now().astimezone().isoformat()
    obs = conn.execute("SELECT id, name, source FROM obligations WHERE status = 'active'").fetchall()
    proper = [(o, _label_tokens(o["name"])) for o in obs if not (o["source"] or "").startswith("todoist")]

    deduped: list[dict[str, Any]] = []
    for o in obs:
        if not (o["source"] or "").startswith("todoist"):
            continue
        tokens = _label_tokens(o["name"])
        if not tokens:
            continue
        twin = next((p for p, pt in proper if p["id"] != o["id"] and pt and tokens <= pt), None)
        if twin is None:
            continue
        cur = conn.execute(
            "UPDATE obligation_instances SET status = 'canceled', updated_at = ? "
            "WHERE obligation_id = ? AND status = 'expected' AND due_date >= ?",
            (now, o["id"], as_of),
        )
        if cur.rowcount:
            deduped.append({"obligation": o["name"], "duplicate_of": twin["name"], "canceled_instances": cur.rowcount})
    conn.commit()
    return {"deduped": deduped, "count": len(deduped)}
