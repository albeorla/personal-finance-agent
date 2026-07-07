from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from .config import get_finance_config
from .schema import has_app_schema


# Instance statuses that count toward the runway. A ``dormant_suppressed``
# obligation (auto-deactivated because its source account went dormant) never
# reaches projection because the query also requires ``o.status = 'active'`` and
# suppression flips the obligation status; this set documents the instance-level
# contract and is the single source for the projectable-status list.
PROJECTABLE_STATUSES = {"expected", "needs_review", "partially_paid"}

# Obligation-level statuses excluded from projection. ``dormant_suppressed`` is
# the auto-suppression status; only ``active`` obligations project.
NON_PROJECTABLE_OBLIGATION_STATUSES = {"dormant_suppressed"}


def build_cash_flow_projections(
    conn: sqlite3.Connection,
    *,
    accounts: list[dict[str, Any]],
    windows: list[int],
    start_date: date,
    working_account_id: str | None = None,
    working_account_hint: str | None = None,
    working_balance_stale_days: int = 3,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not has_app_schema(conn):
        return [], ["local obligation schema is not initialized"]

    # Resolve the operating-account name hint from config when not supplied and
    # no explicit account id pins the selection. Kept out of source (config).
    if working_account_hint is None and working_account_id is None:
        working_account_hint = get_finance_config().get("working_account_hint")
    working_account = _select_working_account(accounts, working_account_id, working_account_hint)
    if working_account is None:
        return [], ["no working cash account found for cash-flow projection"]

    # Override the generic per-account staleness flag (status.py's
    # BALANCE_DATE_STALE_DAYS) with the working account's own, tighter bar
    # (status.py's WORKING_BALANCE_STALE_DAYS, passed in as
    # working_balance_stale_days) so a 1-day-old checking balance reads as
    # stale here even though it would not for e.g. a monthly card feed.
    age_days = working_account.get("balance_age_days")
    working_account = {
        **working_account,
        "balance_date_stale": bool(age_days is not None and age_days > working_balance_stale_days),
    }

    projections = []
    for window in windows:
        projections.append(
            _build_window_projection(
                conn,
                window_days=window,
                start_date=start_date,
                starting_balance=working_account["available"],
                working_account=working_account,
            )
        )

    return projections, [
        "cash-flow projection includes only seeded local obligation instances; coverage is not complete until obligations are fully modeled"
    ]


def _date_part(value: str | None) -> date | None:
    """Date portion of a snapshot ``recorded_at`` (which may carry a time)."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _roll_forward_to_start(
    conn: sqlite3.Connection, *, snapshot_date: date, start_date: date
) -> float:
    """Net signed amount of instances dated between the balance snapshot and the
    projection start, used to carry the snapshot balance forward to ``start_date``.

    The balance snapshot is only true as of ``recorded_at``. When a projection
    starts later than that (e.g. a what-if query for a future date), every
    instance dated in between - paychecks included - falls outside the window's
    ``due_date >= start_date`` filter and silently vanishes, so the run starts
    from today's cash with no upcoming income and trips a false cash-floor breach.
    Netting that gap back into the starting balance is the fix.
    """
    total = 0.0
    for r in conn.execute(
        """
        SELECT oi.amount, oi.direction
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE oi.due_date > ?
          AND oi.due_date < ?
          AND oi.status IN ('expected', 'needs_review', 'partially_paid')
          AND o.status = 'active'
          AND (o.active_until IS NULL OR oi.due_date <= o.active_until)
          AND COALESCE(oi.cash_flow_treatment, 'direct_checking') = 'direct_checking'
        """,
        (snapshot_date.isoformat(), start_date.isoformat()),
    ).fetchall():
        total += _signed_amount(float(r["amount"]), r["direction"])
    return round(total, 2)


def _count_past_due_unreconciled(conn: sqlite3.Connection, *, start_date: date) -> int:
    """Count active, direct-checking obligation instances dated before the
    projection start that are still projectable and have no confirmed transaction
    match. These fall outside the window's ``due_date >= start_date`` filter, so
    without surfacing this count a genuinely missed obligation disappears from the
    projection silently instead of being flagged for reconcile-or-re-date."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE oi.due_date < ?
          AND oi.status IN ('expected', 'needs_review', 'partially_paid')
          AND o.status = 'active'
          AND (o.active_until IS NULL OR oi.due_date <= o.active_until)
          AND COALESCE(oi.cash_flow_treatment, 'direct_checking') = 'direct_checking'
          AND NOT EXISTS (
              SELECT 1 FROM transaction_obligation_matches m
              WHERE m.obligation_instance_id = oi.id
          )
        """,
        (start_date.isoformat(),),
    ).fetchone()
    return int(row["n"]) if row else 0


def _build_window_projection(
    conn: sqlite3.Connection,
    *,
    window_days: int,
    start_date: date,
    starting_balance: float,
    working_account: dict[str, Any],
) -> dict[str, Any]:
    end_date_exclusive = start_date + timedelta(days=window_days)

    # Carry the balance snapshot forward to start_date when the snapshot predates
    # it, so a future-dated projection includes the income/bills between then and
    # now instead of starting from a stale pre-paycheck balance (false floor breach).
    snapshot_balance = round(float(starting_balance), 2)
    snapshot_date = _date_part(working_account.get("recorded_at"))
    rolled_forward = 0.0
    if snapshot_date is not None and snapshot_date < start_date:
        rolled_forward = _roll_forward_to_start(
            conn, snapshot_date=snapshot_date, start_date=start_date
        )
    starting_balance = round(snapshot_balance + rolled_forward, 2)

    rows = conn.execute(
        """
        SELECT
            oi.id AS instance_id,
            oi.obligation_id,
            o.name AS obligation_name,
            o.kind AS obligation_kind,
            oi.due_date,
            oi.amount,
            oi.direction,
            oi.status,
            oi.source,
            oi.confidence,
            oi.notes,
            oi.amount_status,
            oi.amount_source,
            oi.amount_observed_at,
            oi.statement_close_date,
            oi.review_after,
            oi.estimation_method,
            oi.estimation_inputs_json,
            oi.cash_flow_treatment,
            oi.statement_target_obligation_id
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE oi.due_date >= ?
          AND oi.due_date < ?
          AND oi.status IN ('expected', 'needs_review', 'partially_paid')
          AND o.status = 'active'
          AND (o.active_until IS NULL OR oi.due_date <= o.active_until)
          AND COALESCE(oi.cash_flow_treatment, 'direct_checking') = 'direct_checking'
        ORDER BY oi.due_date, oi.id
        """,
        (start_date.isoformat(), end_date_exclusive.isoformat()),
    ).fetchall()

    omitted_past_due = _count_past_due_unreconciled(conn, start_date=start_date)

    running_balance = round(float(starting_balance), 2)
    lowest_balance = running_balance
    lowest_balance_date = start_date.isoformat()
    events = []

    for row in rows:
        signed_amount = _signed_amount(float(row["amount"]), row["direction"])
        running_balance = round(running_balance + signed_amount, 2)
        if running_balance < lowest_balance:
            lowest_balance = running_balance
            lowest_balance_date = row["due_date"]
        events.append(
            {
                "instance_id": row["instance_id"],
                "obligation_id": row["obligation_id"],
                "obligation_name": row["obligation_name"],
                "obligation_kind": row["obligation_kind"],
                "due_date": row["due_date"],
                "amount": round(float(row["amount"]), 2),
                "signed_amount": round(signed_amount, 2),
                "direction": row["direction"],
                "status": row["status"],
                "source": row["source"],
                "confidence": row["confidence"],
                "notes": row["notes"],
                "amount_status": row["amount_status"],
                "amount_source": row["amount_source"],
                "amount_observed_at": row["amount_observed_at"],
                "statement_close_date": row["statement_close_date"],
                "review_after": row["review_after"],
                "estimation_method": row["estimation_method"],
                "estimation_inputs": _decode_json(row["estimation_inputs_json"]),
                "cash_flow_treatment": row["cash_flow_treatment"],
                "statement_target_obligation_id": row["statement_target_obligation_id"],
                "running_balance": running_balance,
            }
        )

    return {
        "window_days": window_days,
        "start_date": start_date.isoformat(),
        "end_date_exclusive": end_date_exclusive.isoformat(),
        "working_account": {
            "account_id": working_account["account_id"],
            "account_name": working_account["account_name"],
            "available": working_account["available"],
            "recorded_at": working_account["recorded_at"],
            "balance_date": working_account.get("balance_date"),
            "balance_age_days": working_account.get("balance_age_days"),
            "balance_date_stale": working_account.get("balance_date_stale", False),
        },
        "starting_balance": round(float(starting_balance), 2),
        "snapshot_balance": snapshot_balance,
        "rolled_forward_to_start": rolled_forward,
        "ending_balance": running_balance,
        "lowest_balance": round(lowest_balance, 2),
        "lowest_balance_date": lowest_balance_date,
        "omitted_past_due_unreconciled_count": omitted_past_due,
        "events": events,
        "provenance": {
            "tables": ["obligations", "obligation_instances", "balance_snapshots"],
            "date_rule": "start_date inclusive, end_date exclusive",
            "projectable_instance_statuses": sorted(PROJECTABLE_STATUSES),
            "source_model": "local obligation instances drive projection; Todoist is not a projection source",
            "cash_flow_treatment": "direct_checking only; card_statement_input rows feed statement estimates instead of checking directly",
        },
    }


def _select_working_account(
    accounts: list[dict[str, Any]],
    working_account_id: str | None,
    working_account_hint: str | None = None,
) -> dict[str, Any] | None:
    if working_account_id is not None:
        return next((account for account in accounts if account["account_id"] == working_account_id), None)

    # Name-match the operating account by the configured hint (e.g. its last-4).
    # When no hint is configured, fall through to the first checking account.
    if working_account_hint:
        for account in accounts:
            if working_account_hint in (account["account_name"] or ""):
                return account

    for account in accounts:
        if account["kind"] == "checking":
            return account

    return accounts[0] if accounts else None


def _signed_amount(amount: float, direction: str) -> float:
    if direction == "inflow":
        return amount
    return -amount


def _decode_json(value: str | None) -> Any:
    if value is None:
        return None
    import json

    return json.loads(value)
