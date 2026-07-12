"""Advisory matches between generic posted checks and expected bills."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from .cashflow import build_cash_flow_projections
from .reconciliation import DEFAULT_OPTIONS, confirm_reconciliation_match
from .schema import ensure_app_schema


_GENERIC_CHECK = re.compile(r"\bCHECK\s*(?:#\s*)?(\d+)\b", re.IGNORECASE)


def list_check_suggestions(
    conn: sqlite3.Connection, *, as_of_date: date | str | None = None
) -> list[dict[str, Any]]:
    """Derive possible bill/check pairs without changing the database."""

    ensure_app_schema(conn)
    as_of = _coerce_date(as_of_date or date.today())
    grace_days = int(DEFAULT_OPTIONS["grace_period_days"])
    bills = conn.execute(
        """
        SELECT oi.id, oi.obligation_id, oi.due_date, oi.amount, oi.direction,
               o.name, o.cadence
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        LEFT JOIN transaction_obligation_matches m
          ON m.obligation_instance_id = oi.id
        WHERE oi.status = 'expected'
          AND o.status = 'active'
          AND oi.due_date <= ?
          AND COALESCE(oi.cash_flow_treatment, 'direct_checking') = 'direct_checking'
          AND oi.matched_transaction_id IS NULL
          AND m.obligation_instance_id IS NULL
        ORDER BY oi.due_date, oi.id
        """,
        (as_of.isoformat(),),
    ).fetchall()
    checks = conn.execute(
        """
        SELECT t.id, t.posted, t.amount, t.account_id, t.payee, t.description,
               a.name AS account_name
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN transaction_obligation_matches m ON m.transaction_id = t.id
        WHERE t.posted IS NOT NULL
          AND substr(t.posted, 1, 10) <= ?
          AND COALESCE(t.pending, 0) = 0
          AND a.kind = 'checking'
          AND t.amount < 0
          AND m.transaction_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM obligation_instances used
              WHERE used.matched_transaction_id = t.id
          )
        ORDER BY t.posted, t.id
        """,
        (as_of.isoformat(),),
    ).fetchall()
    rejected = {
        row[0]
        for row in conn.execute("SELECT suggestion_id FROM check_suggestion_rejections")
    }

    suggestions: list[dict[str, Any]] = []
    for bill in bills:
        due = date.fromisoformat(bill["due_date"])
        amount = abs(float(bill["amount"]))
        tolerance = max(
            float(DEFAULT_OPTIONS["amount_abs_tolerance"]),
            amount * float(DEFAULT_OPTIONS["amount_pct_tolerance"]),
        )
        confirmed_payments = conn.execute(
            """
            SELECT t.account_id
            FROM obligation_instances prior
            JOIN transaction_obligation_matches m
              ON m.obligation_instance_id = prior.id
            JOIN transactions t ON t.id = m.transaction_id
            WHERE prior.obligation_id = ?
              AND prior.id != ?
              AND prior.status = 'paid'
              AND prior.due_date < ?
            ORDER BY prior.due_date, prior.id
            """,
            (bill["obligation_id"], bill["id"], bill["due_date"]),
        ).fetchall()
        prior_account_ids = sorted({row["account_id"] for row in confirmed_payments})
        confirmed_payment_count = len(confirmed_payments)
        for check in checks:
            identifier = _check_identifier(check)
            if identifier is None:
                continue
            posted = date.fromisoformat(check["posted"][:10])
            days_after_due = (posted - due).days
            amount_delta = round(abs(abs(float(check["amount"])) - amount), 2)
            if abs(days_after_due) > grace_days or amount_delta > tolerance:
                continue
            suggestion_id = _suggestion_id(bill["id"], check["id"])
            if suggestion_id in rejected:
                continue
            matches_confirmed_history = check["account_id"] in prior_account_ids
            amount_score = 1.0 - amount_delta / tolerance
            date_score = 1.0 - abs(days_after_due) / grace_days
            score = round(
                amount_score * 0.5
                + date_score * 0.3
                + float(matches_confirmed_history) * 0.1
                + float(confirmed_payment_count > 0) * 0.1,
                3,
            )
            suggestions.append(
                {
                    "suggestion_id": suggestion_id,
                    "score": score,
                    "bill": {
                        "obligation_id": bill["obligation_id"],
                        "instance_id": bill["id"],
                        "name": bill["name"],
                        "due_date": bill["due_date"],
                        "amount": -amount if bill["direction"] == "outflow" else amount,
                        "recurrence": bill["cadence"],
                    },
                    "transaction": {
                        "id": check["id"],
                        "date": posted.isoformat(),
                        "amount": round(float(check["amount"]), 2),
                        "account_id": check["account_id"],
                        "account_name": check["account_name"],
                        "check_identifier": identifier,
                    },
                    "reasons": {
                        "amount": (
                            f"amount delta ${amount_delta:,.2f} is within the "
                            f"${tolerance:,.2f} reconciliation tolerance"
                        ),
                        "date": (
                            f"posted {abs(days_after_due)} day(s) "
                            f"{'after' if days_after_due >= 0 else 'before'} due date, "
                            f"within {grace_days}-day window"
                        ),
                        "account": (
                            f"posted from checking account {check['account_name']}; "
                            f"confirmed payment history {'matches' if matches_confirmed_history else 'does not match'} this account"
                        ),
                        "recurrence": (
                            f"bill recurrence is {bill['cadence'] or 'unspecified'}; "
                            f"{confirmed_payment_count} prior payment(s) are confirmed"
                        ),
                        "competition": "generic check text has no merchant identity; other eligible bills may compete",
                    },
                    "evidence": {
                        "amount": {"delta": amount_delta, "tolerance": tolerance},
                        "date": {
                            "delta_days": days_after_due,
                            "window_days": grace_days,
                        },
                        "account": {
                            "current_account_id": check["account_id"],
                            "confirmed_prior_payment_account_ids": prior_account_ids,
                            "matches_confirmed_history": matches_confirmed_history,
                        },
                        "recurrence": {
                            "confirmed_payment_count": confirmed_payment_count,
                        },
                    },
                }
            )

    eligible_by_check: dict[str, list[str]] = {}
    for item in suggestions:
        transaction_id = item["transaction"]["id"]
        eligible_by_check.setdefault(transaction_id, []).append(item["bill"]["instance_id"])
    for item in suggestions:
        eligible_bill_ids = eligible_by_check[item["transaction"]["id"]]
        count = len(eligible_bill_ids)
        competition_score = 1.0 / count
        item["score"] = round(item["score"] * 0.9 + competition_score * 0.1, 3)
        item["reasons"]["competition"] = (
            f"{count} eligible bill(s) fit this generic check; check text has no merchant identity"
        )
        item["evidence"]["competition"] = {
            "eligible_bill_count": count,
            "eligible_bill_instance_ids": eligible_bill_ids,
            "score": competition_score,
        }
        item["ambiguous"] = count > 1
    return suggestions


def reject_check_suggestion(conn: sqlite3.Connection, suggestion_id: str) -> dict[str, Any]:
    """Durably hide one currently eligible bill/check pair."""

    suggestion = _eligible_suggestion(conn, suggestion_id)
    conn.execute(
        """
        INSERT INTO check_suggestion_rejections (
            suggestion_id, obligation_instance_id, transaction_id, rejected_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(suggestion_id) DO NOTHING
        """,
        (
            suggestion_id,
            suggestion["bill"]["instance_id"],
            suggestion["transaction"]["id"],
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return {"suggestion_id": suggestion_id, "status": "rejected"}


def confirm_check_suggestion(
    conn: sqlite3.Connection,
    suggestion_id: str,
    *,
    as_of_date: date | str | None = None,
    accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Revalidate and explicitly confirm one suggested pair as paid."""

    pair = _pair_for_suggestion_id(conn, suggestion_id)
    if pair is None:
        raise ValueError(f"unknown check suggestion: {suggestion_id}")
    instance_id, transaction_id = pair
    reused = conn.execute(
        """
        SELECT 1 FROM transaction_obligation_matches
        WHERE transaction_id = ? AND obligation_instance_id != ?
        UNION ALL
        SELECT 1 FROM obligation_instances
        WHERE matched_transaction_id = ? AND id != ?
        LIMIT 1
        """,
        (transaction_id, instance_id, transaction_id, instance_id),
    ).fetchone()
    if reused is not None:
        raise ValueError(f"transaction {transaction_id} already confirmed for another bill")

    as_of = _coerce_date(as_of_date or date.today())
    _eligible_suggestion(conn, suggestion_id, as_of_date=as_of)
    result = confirm_reconciliation_match(conn, instance_id, transaction_id)
    projections, _warnings = build_cash_flow_projections(
        conn,
        accounts=accounts or [],
        windows=[30],
        start_date=as_of,
    )
    return {**result, "projection": projections[0] if projections else {"events": []}}


def _eligible_suggestion(
    conn: sqlite3.Connection,
    suggestion_id: str,
    *,
    as_of_date: date | str | None = None,
) -> dict[str, Any]:
    for suggestion in list_check_suggestions(conn, as_of_date=as_of_date):
        if suggestion["suggestion_id"] == suggestion_id:
            return suggestion
    raise ValueError(f"check suggestion is no longer eligible: {suggestion_id}")


def _pair_for_suggestion_id(
    conn: sqlite3.Connection, suggestion_id: str
) -> tuple[str, str] | None:
    bill_ids = conn.execute("SELECT id FROM obligation_instances").fetchall()
    transaction_ids = conn.execute("SELECT id FROM transactions").fetchall()
    for bill in bill_ids:
        for transaction in transaction_ids:
            if _suggestion_id(bill[0], transaction[0]) == suggestion_id:
                return bill[0], transaction[0]
    return None


def _check_identifier(check: sqlite3.Row) -> str | None:
    text = f"{check['payee'] or ''} {check['description'] or ''}"
    match = _GENERIC_CHECK.search(text)
    return match.group(1) if match else None


def _suggestion_id(instance_id: str, transaction_id: str) -> str:
    pair = f"{instance_id}\0{transaction_id}".encode()
    return f"check-{hashlib.sha256(pair).hexdigest()[:24]}"


def _coerce_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)
