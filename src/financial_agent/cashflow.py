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

# Minimum working-cash balance the projection protects. An optional discretionary
# sweep (a debt paydown the user chooses to make) is capped so it never drives the
# projected balance below this floor. Single source of truth: guardrails imports it
# from here (guardrails already imports cashflow, so cashflow cannot import it back).
CASH_FLOOR = 2500.0


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
    # working_balance_stale_days). Missing and future dates are also unverified.
    age_days = working_account.get("balance_age_days")
    source_age_days = (
        working_account.get("source_balance_age_days")
        if "source_balance_age_days" in working_account
        else age_days
    )
    working_account = {
        **working_account,
        "source_balance_date": (
            working_account.get("source_balance_date")
            if "source_balance_date" in working_account
            else working_account.get("balance_date")
        ),
        "source_balance_age_days": source_age_days,
        "balance_date_stale": bool(
            source_age_days is None
            or source_age_days < 0
            or source_age_days > working_balance_stale_days
        ),
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
    conn: sqlite3.Connection,
    *,
    snapshot_date: date,
    start_date: date,
    snapshot_balance: float,
) -> float:
    """Net signed amount of instances dated between the balance snapshot and the
    projection start, used to carry the snapshot balance forward to ``start_date``.

    The balance snapshot is only true as of ``recorded_at``. When a projection
    starts later than that (e.g. a what-if query for a future date), every
    instance dated in between - paychecks included - falls outside the window's
    ``due_date >= start_date`` filter and silently vanishes, so the run starts
    from today's cash with no upcoming income and trips a false cash-floor breach.
    Netting that gap back into the starting balance is the fix.

    A discretionary sweep dated in this gap is capped by the SAME cash-floor rule
    the in-window loop applies (``_discretionary_capped_signed_amount``), walking a
    running balance from ``snapshot_balance`` in due-date order. Without this a
    pre-window sweep subtracted its full amount here and the cap only ran later, so
    the projection could start below the floor. Non-discretionary items are unchanged.
    """
    running = round(float(snapshot_balance), 2)
    total = 0.0
    for r in conn.execute(
        """
        SELECT oi.amount, oi.direction, oi.estimation_inputs_json,
               o.amount_discretionary AS amount_discretionary
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE oi.due_date > ?
          AND oi.due_date < ?
          AND oi.status IN ('expected', 'needs_review', 'partially_paid')
          AND o.status = 'active'
          AND (o.active_until IS NULL OR oi.due_date <= o.active_until)
          AND COALESCE(oi.cash_flow_treatment, 'direct_checking') = 'direct_checking'
        ORDER BY oi.due_date, oi.id
        """,
        (snapshot_date.isoformat(), start_date.isoformat()),
    ).fetchall():
        signed = _discretionary_capped_signed_amount(r, running)
        running = round(running + signed, 2)
        total += signed
    return round(total, 2)


def _select_past_due_unreconciled(
    conn: sqlite3.Connection, *, before_date: date
) -> list[sqlite3.Row]:
    """Active, direct-checking obligation instances dated before ``before_date``
    that are still projectable and have no confirmed transaction match. These
    past-due, unpaid items fall outside the window's ``due_date >= start_date``
    filter; carrying them into the runway as due-now events keeps a genuinely
    missed obligation counted instead of silently dropped (which overstates the
    runway) and flags it for reconcile-or-re-date.

    ``before_date`` is the snapshot day (plus one) rather than ``start_date`` when
    the roll-forward already netted the ``(snapshot, start)`` gap, so an instance
    counted there is not carried here as well (no double-count)."""
    return conn.execute(
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
            oi.statement_target_obligation_id,
            o.amount_discretionary AS amount_discretionary
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
        ORDER BY oi.due_date, oi.id
        """,
        (before_date.isoformat(),),
    ).fetchall()


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
            conn,
            snapshot_date=snapshot_date,
            start_date=start_date,
            snapshot_balance=snapshot_balance,
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
            oi.statement_target_obligation_id,
            o.amount_discretionary AS amount_discretionary
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

    # Past-due, unpaid, unreconciled instances predate the window and so are absent
    # from ``rows``. Carry them as due-now events at start_date so the owed dollars
    # stay on the runway. When the roll-forward netted the (snapshot, start) gap,
    # cap the carry at the snapshot day so those items are not counted twice.
    carry_before = (
        snapshot_date + timedelta(days=1)
        if snapshot_date is not None and snapshot_date < start_date
        else start_date
    )
    past_due_rows = _select_past_due_unreconciled(conn, before_date=carry_before)

    running_balance = round(float(starting_balance), 2)
    lowest_balance = running_balance
    lowest_balance_date = start_date.isoformat()
    events = []
    carried_past_due: list[dict[str, Any]] = []

    for is_past_due, row in [(True, r) for r in past_due_rows] + [(False, r) for r in rows]:
        signed_amount = _discretionary_capped_signed_amount(row, running_balance)
        running_balance = round(running_balance + signed_amount, 2)
        if running_balance < lowest_balance:
            lowest_balance = running_balance
            # A carried item is due now, so its trough dates to start_date.
            lowest_balance_date = start_date.isoformat() if is_past_due else row["due_date"]
        event = {
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
        if is_past_due:
            event["past_due_carried"] = True
            carried_past_due.append(
                {
                    "instance_id": row["instance_id"],
                    "amount": event["amount"],
                    "signed_amount": event["signed_amount"],
                    "due_date": row["due_date"],
                }
            )
        events.append(event)

    return {
        "window_days": window_days,
        "start_date": start_date.isoformat(),
        "end_date_exclusive": end_date_exclusive.isoformat(),
        "working_account": {
            "account_id": working_account["account_id"],
            "account_name": working_account["account_name"],
            "available": working_account["available"],
            "recorded_at": working_account["recorded_at"],
            "source": working_account.get("source"),
            "balance_date": working_account.get("balance_date"),
            "balance_age_days": working_account.get("balance_age_days"),
            "source_balance_date": working_account.get("source_balance_date"),
            "source_balance_age_days": working_account.get("source_balance_age_days"),
            "balance_date_stale": working_account.get("balance_date_stale", False),
        },
        "starting_balance": round(float(starting_balance), 2),
        "snapshot_balance": snapshot_balance,
        "rolled_forward_to_start": rolled_forward,
        "ending_balance": running_balance,
        "lowest_balance": round(lowest_balance, 2),
        "lowest_balance_date": lowest_balance_date,
        "carried_past_due_unreconciled_count": len(carried_past_due),
        "carried_past_due": carried_past_due,
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


def _discretionary_capped_signed_amount(row: sqlite3.Row, running_balance: float) -> float:
    """Signed amount for one event, capping an optional discretionary sweep.

    A discretionary outflow (an optional debt-paydown sweep the user chooses to
    make) is split into a required minimum, always applied, and an optional sweep,
    applied only up to the headroom above ``CASH_FLOOR``. So the sweep never drives
    the projected balance below the cash floor -- the user tops it up by hand
    instead of the runway silently dipping under the floor. The required minimum
    defaults to 0 (the whole modeled amount is optional) and can be pinned per
    instance via ``estimation_inputs.required_minimum``. Fixed/required
    obligations and inflows are unaffected.
    """

    amount = float(row["amount"])
    direction = row["direction"]
    if direction != "outflow" or not _row_discretionary(row):
        return _signed_amount(amount, direction)
    required = min(amount, _required_minimum(row))
    headroom = running_balance - required - CASH_FLOOR
    optional_applied = min(amount - required, max(0.0, headroom))
    return -round(required + optional_applied, 2)


def _row_discretionary(row: sqlite3.Row) -> bool:
    try:
        return bool(row["amount_discretionary"])
    except (IndexError, KeyError):
        return False


def _required_minimum(row: sqlite3.Row) -> float:
    """Required-minimum floor for a discretionary instance, from estimation_inputs."""
    inputs = _decode_json(row["estimation_inputs_json"])
    if not isinstance(inputs, dict):
        return 0.0
    try:
        return max(0.0, float(inputs.get("required_minimum") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _decode_json(value: str | None) -> Any:
    if value is None:
        return None
    import json

    return json.loads(value)
