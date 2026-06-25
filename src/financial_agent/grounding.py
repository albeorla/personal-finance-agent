"""Grounding / verification harness (M5, slice V).

Checks that every headline dollar figure in a finance payload (a
``get_finance_status`` or ``get_daily_digest`` result) traces to a concrete
source: working cash to the latest balance snapshot of the operating account,
each upcoming obligation to its ``obligation_instances`` row, and each
projection endpoint recomputable as working cash plus the signed obligation
events inside the window. Anything it cannot trace is flagged ``ungrounded``.

Read-only. This is the "is the agent allowed to say this number" gate: a claim
is grounded only when a primary row (or arithmetic over primary rows) reproduces
it within tolerance.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from .status import default_db_path

DEFAULT_TOLERANCE = 0.02


def verify_grounding(
    payload: dict[str, Any],
    db_path: str | None = None,
    *,
    as_of_date: str | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Trace each headline figure in ``payload`` to a source row. Read-only."""

    norm = _normalize(payload)
    as_of = as_of_date or norm["as_of_date"]
    conn = sqlite3.connect(db_path or str(default_db_path()))
    conn.row_factory = sqlite3.Row
    checks: list[dict[str, Any]] = []
    try:
        # 1. Working cash -> latest balance snapshot of the operating account.
        snap = conn.execute(
            "SELECT bs.available FROM balance_snapshots bs JOIN accounts a ON a.id = bs.account_id "
            "WHERE a.name LIKE '%XXXX%' ORDER BY bs.recorded_at DESC LIMIT 1"
        ).fetchone()
        if norm["working_cash"] is not None:
            checks.append(_num_check(
                "working_cash", norm["working_cash"], snap["available"] if snap else None,
                "balance_snapshots(operating account XXXX).available", tolerance))

        # Net worth across all accounts must tie to the sum of each account's
        # latest balance (so a net line that secretly excludes card/loan debt is
        # caught, not silently "grounded").
        if norm["net_across_accounts"] is not None:
            net_row = conn.execute(
                "SELECT COALESCE(SUM(bs.balance), 0) FROM balance_snapshots bs "
                "JOIN (SELECT account_id, MAX(recorded_at) AS mr FROM balance_snapshots GROUP BY account_id) m "
                "ON m.account_id = bs.account_id AND m.mr = bs.recorded_at"
            ).fetchone()
            checks.append(_num_check(
                "net_across_accounts", norm["net_across_accounts"],
                round(float(net_row[0]), 2) if net_row else None,
                "SUM(latest balance per account)", tolerance))

        # Liquid available must sum `available` over DEPOSIT accounts only
        # (balance >= 0), so a card's negative available cannot drag it down.
        if norm.get("liquid_available") is not None:
            liq_row = conn.execute(
                "SELECT COALESCE(SUM(bs.available), 0) FROM balance_snapshots bs "
                "JOIN (SELECT account_id, MAX(recorded_at) AS mr FROM balance_snapshots GROUP BY account_id) m "
                "ON m.account_id = bs.account_id AND m.mr = bs.recorded_at WHERE bs.balance >= 0"
            ).fetchone()
            checks.append(_num_check(
                "liquid_available", norm["liquid_available"],
                round(float(liq_row[0]), 2) if liq_row else None,
                "SUM(available) over deposit accounts (balance >= 0)", tolerance))

        # 2. Each upcoming obligation -> its obligation_instances row (name + due date).
        for o in norm["upcoming"]:
            inst = conn.execute(
                "SELECT oi.amount FROM obligation_instances oi JOIN obligations ob ON ob.id = oi.obligation_id "
                "WHERE ob.name = ? AND oi.due_date = ?",
                (o["name"], o["due_date"]),
            ).fetchone()
            checks.append(_num_check(
                f"obligation:{o['name']}@{o['due_date']}", o["amount"],
                abs(inst["amount"]) if inst is not None else None,
                "obligation_instances row (obligation name + due_date)", tolerance))

        # 3. Each projection endpoint -> working cash + signed events inside the window.
        if norm["working_cash"] is not None and as_of:
            start = dt.date.fromisoformat(as_of[:10])
            for w in norm["windows"]:
                # Match the projection's window semantics exactly: start inclusive,
                # end EXCLUSIVE (cashflow.py uses due_date < start + window_days).
                horizon = start + dt.timedelta(days=int(w["window_days"]))
                recomputed = norm["working_cash"] + sum(
                    e["signed_amount"] for e in norm["upcoming"]
                    if e["signed_amount"] is not None and dt.date.fromisoformat(e["due_date"]) < horizon
                )
                checks.append(_num_check(
                    f"ending_balance_{w['window_days']}d", w["ending"], round(recomputed, 2),
                    "working_cash + sum(signed obligation events in [start, start+window))", max(tolerance, 0.5)))
    finally:
        conn.close()

    ungrounded = [c for c in checks if not c["grounded"]]
    return {
        "as_of_date": as_of,
        "payload_kind": norm["kind"],
        "grounded": len(ungrounded) == 0 and len(checks) > 0,
        "checks_total": len(checks),
        "grounded_count": len(checks) - len(ungrounded),
        "ungrounded": ungrounded,
        "checks": checks,
    }


# --- internals -------------------------------------------------------------


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract comparable fields from either a digest or a status payload."""

    balances = payload.get("balances", {})
    # Digest shape: balances.working_cash + upcoming_obligations + cash_flow.
    if "working_cash" in balances or "upcoming_obligations" in payload:
        upcoming = [
            {"name": o["obligation_name"], "due_date": o["due_date"], "amount": o["amount"], "signed_amount": o.get("signed_amount")}
            for o in payload.get("upcoming_obligations", [])
        ]
        windows = [{"window_days": c["window_days"], "ending": c["ending_balance"]} for c in payload.get("cash_flow", [])]
        return {
            "kind": "digest",
            "as_of_date": payload.get("as_of_date"),
            "working_cash": balances.get("working_cash"),
            "net_across_accounts": balances.get("net_across_accounts"),
            "liquid_available": balances.get("liquid_available"),
            "upcoming": upcoming,
            "windows": windows,
        }

    # Status shape: cash_flow_projections[].working_account / .events / .ending_balance.
    projections = payload.get("cash_flow_projections", [])
    working_account = projections[0].get("working_account") if projections else None
    longest = projections[-1] if projections else None
    upcoming = [
        {"name": e["obligation_name"], "due_date": e["due_date"], "amount": e["amount"], "signed_amount": e.get("signed_amount")}
        for e in (longest["events"] if longest else [])
    ]
    windows = [{"window_days": p["window_days"], "ending": p["ending_balance"]} for p in projections]
    return {
        "kind": "status",
        "as_of_date": (projections[0].get("start_date") if projections else None),
        "working_cash": working_account["available"] if working_account else None,
        "net_across_accounts": payload.get("balances", {}).get("total_balance"),
        "upcoming": upcoming,
        "windows": windows,
    }


def _num_check(claim: str, claimed: Any, source_value: Any, source: str, tolerance: float) -> dict[str, Any]:
    if source_value is None or claimed is None:
        grounded = False
        delta = None
    else:
        delta = round(float(claimed) - float(source_value), 2)
        grounded = abs(delta) <= tolerance
    return {
        "claim": claim,
        "claimed_value": claimed,
        "source_value": source_value,
        "source": source,
        "delta": delta,
        "grounded": grounded,
    }
