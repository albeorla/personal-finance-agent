"""SimpleFIN live sync: pull accounts, balances, and transactions (cutover slice K).

Read-only against SimpleFIN; writes to the local copied database only. This is a
faithful port of `~/dev/areas/finances/finance/simplefin.py` + the SimpleFIN
upserts in `finance/db.py`, with the legacy `curl` subprocess swapped for stdlib
`urllib` (no new dependency). Transaction timestamps are stored as ISO strings,
matching the existing copied-DB format, so the onboarding scanner and
reconciliation keep working unchanged. Idempotent: re-sync upserts by id.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import sqlite3
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .config import ensure_source_tables, get_finance_config


def sync_simplefin(
    conn: sqlite3.Connection,
    *,
    access_url: str | None = None,
    env_path: str | None = None,
    start_date: Any = None,
    end_date: Any = None,
    lookback_days: int = 90,
    incremental: bool = False,
    overlap_days: int = 3,
    fetched_at: str | None = None,
    source: str = "simplefin",
    record_run: bool = True,
    timeout: int = 60,
) -> dict[str, Any]:
    """Fetch from SimpleFIN and upsert accounts/balances/transactions into the DB.

    SimpleFIN only returns transactions when a start-date is supplied. When
    ``start_date`` is omitted: ``incremental`` resumes from the oldest
    last-posted date across accounts (minus ``overlap_days``, floored at the
    90-day cap), so a daily job does not re-pull the full window; otherwise it
    defaults to ``lookback_days`` before today. A database with no prior
    transactions falls back to the full lookback.
    """

    ensure_source_tables(conn)
    if access_url is None:
        access_url = get_finance_config(env_path=env_path)["simplefin_access_url"]
        if not access_url:
            raise ValueError("no SIMPLEFIN_ACCESS_URL configured (.env or environment)")
    if start_date is None:
        if incremental:
            start_date = incremental_start_date(conn, overlap_days=overlap_days, max_lookback_days=lookback_days)
        if start_date is None:
            start_date = (dt.date.today() - dt.timedelta(days=int(lookback_days))).isoformat()

    started_at = _now()
    fetched_at = fetched_at or started_at
    error: str | None = None
    accounts: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        accounts, errors = fetch_simplefin_accounts(access_url, start_date=start_date, end_date=end_date, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - record the failure as a sync run
        error = str(exc)

    normalized = normalize_accounts(accounts, fetched_at)
    stored = store_accounts(conn, normalized, source=source)
    finished_at = _now()
    if errors and error is None:
        error = "; ".join(str(e) for e in errors)

    if record_run:
        _record_sync_run(
            conn, started_at=started_at, finished_at=finished_at, mode="incremental",
            accounts_seen=stored["accounts"], inserted=stored["inserted"], updated=stored["updated"], error=error,
        )

    return {
        "accounts": stored["accounts"],
        "inserted": stored["inserted"],
        "updated": stored["updated"],
        "errors": errors,
        "error": error,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def fetch_simplefin_accounts(
    access_url: str, *, start_date: Any = None, end_date: Any = None, timeout: int = 60
) -> tuple[list[dict[str, Any]], list[str]]:
    query: dict[str, Any] = {}
    start_ts = _to_epoch(start_date)
    end_ts = _to_epoch(end_date)
    if start_ts is not None:
        query["start-date"] = start_ts
    if end_ts is not None:
        query["end-date"] = end_ts
    data = _simplefin_request(access_url, "/accounts", query, timeout=timeout)
    return data.get("accounts", []), data.get("errors", [])


def normalize_accounts(accounts: list[dict[str, Any]], fetched_at: str) -> list[dict[str, Any]]:
    """Mirror finance/simplefin.py:normalize_accounts exactly."""

    normalized: list[dict[str, Any]] = []
    for account in accounts:
        normalized.append(
            {
                "id": account["id"],
                "name": account["name"],
                "org": (account.get("org") or {}).get("name", ""),
                "kind": account.get("type", ""),
                "currency": account.get("currency", "USD"),
                "balance": float(account["balance"]),
                "available_balance": float(account.get("available-balance", account["balance"])),
                "balance_date": int(account.get("balance-date", 0) or 0),
                "fetched_at": fetched_at,
                "transactions": [
                    {
                        "id": txn["id"],
                        "posted": int(txn.get("posted", 0) or 0),
                        "transacted_at": int(txn.get("transacted-at", 0) or 0),
                        "amount": float(txn["amount"]),
                        "payee": txn.get("payee") or "",
                        "description": txn.get("description") or "",
                        "pending": 1 if txn.get("pending") else 0,
                        "fetched_at": fetched_at,
                    }
                    for txn in account.get("transactions", [])
                ],
            }
        )
    return normalized


def store_accounts(conn: sqlite3.Connection, normalized: list[dict[str, Any]], *, source: str = "simplefin") -> dict[str, int]:
    ensure_source_tables(conn)
    inserted = updated = 0
    for account in normalized:
        _upsert_account(conn, account)
        _insert_balance_snapshot(conn, account, source)
        for txn in account["transactions"]:
            if _upsert_transaction(conn, account["id"], txn, source) == "inserted":
                inserted += 1
            else:
                updated += 1
    return {"accounts": len(normalized), "inserted": inserted, "updated": updated}


def incremental_start_date(conn: sqlite3.Connection, *, overlap_days: int = 3, max_lookback_days: int = 90) -> str | None:
    """Resume date for an incremental sync, or None when there is nothing to resume from.

    Uses the OLDEST of each account's most-recent posted date (so every account
    is covered from its last sync), minus an overlap, floored at the SimpleFIN
    90-day cap. Returns None when no posted transactions exist yet.
    """

    rows = conn.execute(
        "SELECT MAX(posted) FROM transactions WHERE posted IS NOT NULL GROUP BY account_id"
    ).fetchall()
    latests = [r[0] for r in rows if r[0]]
    if not latests:
        return None
    start = dt.date.fromisoformat(min(latests)[:10]) - dt.timedelta(days=int(overlap_days))
    floor = dt.date.today() - dt.timedelta(days=int(max_lookback_days))
    return max(start, floor).isoformat()


# --- ported helpers (mirror finance/db.py) ---------------------------------


def _simplefin_request(access_url: str, path: str, query: dict[str, Any] | None = None, *, timeout: int = 60) -> dict[str, Any]:
    parsed = urlsplit(access_url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("SIMPLEFIN_ACCESS_URL is invalid")
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    netloc = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    url = urlunsplit((parsed.scheme, netloc, f"{parsed.path.rstrip('/')}{path}", urlencode(query or {}), ""))
    req = Request(url)
    # Some SimpleFIN bridges reject the default python-urllib User-Agent (403);
    # send an explicit one, like the legacy curl-based sync did.
    req.add_header("User-Agent", "financial-agent-mcp/0.1")
    req.add_header("Accept", "application/json")
    if username or password:
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - access URL comes from the user's own .env
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("errors") and "accounts" not in data:
        raise RuntimeError(f"SimpleFIN returned errors: {data['errors']}")
    return data


def _to_epoch(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, str):
        parsed = dt.date.fromisoformat(value)
        return int(dt.datetime.combine(parsed, dt.time.min).timestamp())
    raise ValueError(f"unsupported date value for SimpleFIN query: {value!r}")


def _epoch_to_iso(value: int) -> str | None:
    if not value:
        return None
    return dt.datetime.fromtimestamp(value).isoformat()


def _upsert_account(conn: sqlite3.Connection, account: dict[str, Any]) -> None:
    existing = conn.execute("SELECT id FROM accounts WHERE id = ?", (account["id"],)).fetchone()
    if existing:
        conn.execute(
            "UPDATE accounts SET name=?, org=?, kind=?, currency=?, last_seen_at=? WHERE id=?",
            (account["name"], account["org"], account["kind"], account["currency"], account["fetched_at"], account["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO accounts (id, name, org, kind, currency, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account["id"], account["name"], account["org"], account["kind"], account["currency"], account["fetched_at"], account["fetched_at"]),
        )


def _insert_balance_snapshot(conn: sqlite3.Connection, account: dict[str, Any], source: str) -> None:
    conn.execute(
        "INSERT INTO balance_snapshots (account_id, balance, available, recorded_at, source) VALUES (?, ?, ?, ?, ?)",
        (account["id"], account["balance"], account["available_balance"], account["fetched_at"], source),
    )


def _upsert_transaction(conn: sqlite3.Connection, account_id: str, txn: dict[str, Any], source: str) -> str:
    existing = conn.execute("SELECT id FROM transactions WHERE id = ?", (txn["id"],)).fetchone()
    posted = _epoch_to_iso(txn["posted"])
    transacted_at = _epoch_to_iso(txn["transacted_at"])
    if existing:
        conn.execute(
            """
            UPDATE transactions
            SET account_id=?, posted=?, transacted_at=?, amount=?, payee=?, description=?,
                pending=?, source=?, last_seen_at=?, fetched_at=?
            WHERE id=?
            """,
            (account_id, posted, transacted_at, txn["amount"], txn["payee"], txn["description"],
             txn["pending"], source, txn["fetched_at"], txn["fetched_at"], txn["id"]),
        )
        return "updated"
    conn.execute(
        """
        INSERT INTO transactions (
            id, account_id, posted, transacted_at, amount, payee, description,
            pending, source, first_seen_at, last_seen_at, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (txn["id"], account_id, posted, transacted_at, txn["amount"], txn["payee"], txn["description"],
         txn["pending"], source, txn["fetched_at"], txn["fetched_at"], txn["fetched_at"]),
    )
    return "inserted"


def _record_sync_run(conn, *, started_at, finished_at, mode, accounts_seen, inserted, updated, error) -> None:
    conn.execute(
        """
        INSERT INTO sync_runs (started_at, finished_at, mode, accounts_seen, transactions_inserted, transactions_updated, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (started_at, finished_at, mode, accounts_seen, inserted, updated, error),
    )


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")
