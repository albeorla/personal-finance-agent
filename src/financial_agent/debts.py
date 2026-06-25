"""Structured, queryable debts layer.

Debt facts (APR, which account holds the balance, whether the card revolves)
used to live as a hard-coded Python constant keyed by abstract strings. This
module makes them data: each debt is a row that links to a synced account for a
live balance (or carries a manual balance override for debts with no feed, like
a federal student loan), and the avalanche + interest math read from here.

- ``set_debt_terms`` upserts one debt (idempotent), optionally resolving an
  ``account_query`` against the accounts table to a concrete account_id.
- ``list_debts`` returns each debt with its resolved live balance, the modeled
  monthly interest, and a total monthly interest across the revolving debts.

Both are read-only against live source tables except for the debts table itself.
No raw SQL lives outside this module's helpers.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from .schema import ensure_app_schema


def set_debt_terms(
    conn: sqlite3.Connection,
    id: str,
    name: str,
    apr: float,
    account_query: str | None = None,
    account_id: str | None = None,
    balance_source: str = "account",
    balance_override: float | None = None,
    min_payment: float | None = None,
    is_revolving: bool = True,
    autopay: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    """Create or update a debt's terms (idempotent upsert keyed by ``id``).

    When ``account_query`` is given and ``account_id`` is not, the query is
    resolved against the accounts table (by name or org, case-insensitive
    substring) to a concrete account_id. An explicit ``account_id`` wins over a
    query. A debt with ``balance_source='manual'`` carries its balance in
    ``balance_override`` and need not link to any account.
    """

    ensure_app_schema(conn)

    if not id or not id.strip():
        raise ValueError("Debt id must be non-empty.")
    if not name or not name.strip():
        raise ValueError("Debt name must be non-empty.")
    if balance_source not in ("account", "manual"):
        raise ValueError("balance_source must be 'account' or 'manual'.")

    resolved_account_id = account_id
    if resolved_account_id is None and account_query is not None:
        resolved_account_id = _resolve_account_id(conn, account_query)
        if resolved_account_id is None:
            raise ValueError(f"No account matched account_query {account_query!r}.")

    if balance_source == "account" and resolved_account_id is None:
        raise ValueError(
            "balance_source='account' requires an account_id or a resolvable "
            "account_query."
        )

    now = _now()
    existing = conn.execute(
        "SELECT created_at FROM debts WHERE id = ?",
        (id,),
    ).fetchone()
    created = existing is None
    created_at = now if created else existing["created_at"]

    conn.execute(
        """
        INSERT INTO debts (
            id, account_id, name, apr, balance_source, balance_override,
            min_payment, is_revolving, autopay, note, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            account_id = excluded.account_id,
            name = excluded.name,
            apr = excluded.apr,
            balance_source = excluded.balance_source,
            balance_override = excluded.balance_override,
            min_payment = excluded.min_payment,
            is_revolving = excluded.is_revolving,
            autopay = excluded.autopay,
            note = excluded.note,
            updated_at = excluded.updated_at
        """,
        (
            id.strip(),
            resolved_account_id,
            name.strip(),
            float(apr),
            balance_source,
            None if balance_override is None else round(float(balance_override), 2),
            None if min_payment is None else round(float(min_payment), 2),
            1 if is_revolving else 0,
            1 if autopay else 0,
            note,
            created_at,
            now,
        ),
    )

    return {
        "id": id.strip(),
        "account_id": resolved_account_id,
        "name": name.strip(),
        "apr": float(apr),
        "balance_source": balance_source,
        "balance_override": None if balance_override is None else round(float(balance_override), 2),
        "min_payment": None if min_payment is None else round(float(min_payment), 2),
        "is_revolving": bool(is_revolving),
        "autopay": bool(autopay),
        "note": note,
        "created": created,
        "updated": not created,
    }


def list_debts(
    conn: sqlite3.Connection,
    as_of_date: date | str,
) -> dict[str, Any]:
    """List debts with resolved live balances and modeled monthly interest.

    For each debt: ``current_balance`` is the linked account's latest balance
    snapshot on or before ``as_of_date`` when ``balance_source='account'``, else
    the ``balance_override``. ``monthly_interest`` is
    ``round(abs(current_balance) * apr/100 / 12, 2)``. The result also carries a
    ``total_monthly_interest`` summed across the REVOLVING debts only.
    """

    ensure_app_schema(conn)
    as_of = _coerce_date(as_of_date)

    rows = conn.execute(
        """
        SELECT id, account_id, name, apr, balance_source, balance_override,
               min_payment, is_revolving, autopay, note
        FROM debts
        ORDER BY apr DESC, name, id
        """
    ).fetchall()

    debts: list[dict[str, Any]] = []
    total_monthly_interest = 0.0
    for row in rows:
        current_balance = _current_balance(
            conn,
            account_id=row["account_id"],
            balance_source=row["balance_source"],
            balance_override=row["balance_override"],
            as_of=as_of,
        )
        apr = float(row["apr"])
        is_revolving = bool(row["is_revolving"])
        if current_balance is None:
            monthly_interest: float | None = None
        else:
            monthly_interest = round(abs(current_balance) * apr / 100 / 12, 2)
            if is_revolving:
                total_monthly_interest += monthly_interest
        debts.append(
            {
                "id": row["id"],
                "account_id": row["account_id"],
                "name": row["name"],
                "apr": apr,
                "balance_source": row["balance_source"],
                "current_balance": current_balance,
                "monthly_interest": monthly_interest,
                "is_revolving": is_revolving,
                "autopay": bool(row["autopay"]),
                "min_payment": None if row["min_payment"] is None else round(float(row["min_payment"]), 2),
                "note": row["note"],
            }
        )

    return {
        "as_of_date": as_of.isoformat(),
        "count": len(debts),
        "debts": debts,
        "total_monthly_interest": round(total_monthly_interest, 2),
    }


def _current_balance(
    conn: sqlite3.Connection,
    *,
    account_id: str | None,
    balance_source: str,
    balance_override: float | None,
    as_of: date,
) -> float | None:
    """Resolve a debt's current balance from its account or manual override.

    ``balance_source='account'`` reads the linked account's latest balance
    snapshot on or before ``as_of``; ``'manual'`` returns ``balance_override``.
    Returns None when the source yields nothing (e.g. an account with no
    snapshot, or a manual debt with no override set).
    """

    if balance_source == "manual":
        return None if balance_override is None else round(float(balance_override), 2)
    if account_id is None:
        return None
    return _live_balance(conn, account_id=account_id, as_of=as_of)


def _live_balance(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    as_of: date,
) -> float | None:
    """Return an account's signed balance on or before ``as_of``.

    Delegates to the canonical balance resolver that ``get_finance_status`` uses
    (``status.resolve_account_balance``) so an account-sourced debt's balance
    matches the status response exactly. That resolver applies the manual-over-
    feed precedence within a day, so for a balance-only "Updated Monthly" account
    (e.g. the Apple Card) a manual correction wins over a same-day SimpleFIN
    snapshot regardless of recorded_at. Returns None when no snapshot qualifies.
    """

    if not _has_table(conn, "balance_snapshots"):
        return None
    # Imported lazily to avoid a circular import: status -> guardrails -> debts.
    from .status import resolve_account_balance

    return resolve_account_balance(conn, account_id, as_of=as_of)


def _resolve_account_id(conn: sqlite3.Connection, account_query: str) -> str | None:
    """Resolve an account_query to a single accounts.id by name or org.

    Matches case-insensitively against either ``name`` or ``org``. A SQL ``LIKE``
    pattern (containing ``%`` or ``_``) is used as-is; a plain string is matched
    as a substring. Returns the single match, or None when there is no match or
    the query is ambiguous (more than one account matches).
    """

    if not _has_table(conn, "accounts"):
        return None
    pattern = account_query if _looks_like_pattern(account_query) else f"%{account_query}%"
    rows = conn.execute(
        """
        SELECT id
        FROM accounts
        WHERE name LIKE ? COLLATE NOCASE
           OR org LIKE ? COLLATE NOCASE
        ORDER BY id
        """,
        (pattern, pattern),
    ).fetchall()
    if len(rows) != 1:
        return None
    return rows[0]["id"]


def _looks_like_pattern(value: str) -> bool:
    return "%" in value or "_" in value


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _now() -> str:
    return datetime.now().astimezone().isoformat()
