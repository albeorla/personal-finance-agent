"""Keep pasted checking rows from coexisting with their bank-feed twins.

The operating checking account gets rows from two lanes: the SimpleFIN feed
(bank ids) and pasted CSVs (synthetic ``checking:`` ids). The paste importer
used to dedupe only against earlier pastes, so a paste covering dates the feed
also delivered wrote every row twice, and a feed row arriving after a paste did
the same from the other side. Spending totals and reconciliation then saw each
transaction twice.

Rule: the feed row wins. A paste row is a twin of a feed row when it sits on the
same account, the same posted date, and the same amount. Twins are skipped at
paste time, absorbed when the feed row lands, and cleaned up once for history by
``dedupe_pasted_transactions_for_db``. References to the paste id (reconciliation
matches, drift evidence, onboarding evidence, rejected check suggestions) are
re-pointed at the feed id before the paste row is deleted.

ponytail: the twin key is date + amount on one account. Two genuinely distinct
same-day same-amount checking transactions would collapse to one paste copy;
accepted because the feed is authoritative for this account and the paste lane
only backfills feed lag. Tighten with a payee-token check if that ever bites.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

PASTE_SOURCE = "checking_paste"
FEED_SOURCE = "simplefin"

# (table, column) pairs holding a single transaction id.
_ID_COLUMNS = (
    ("transaction_obligation_matches", "transaction_id"),
    ("obligation_instances", "matched_transaction_id"),
    ("check_suggestion_rejections", "transaction_id"),
)
# (table, key column, json column) holding a JSON list of transaction ids.
_JSON_ID_COLUMNS = (
    ("drift_findings", "id", "related_transaction_ids_json"),
    ("charge_onboarding_candidates", "id", "evidence_transaction_ids_json"),
)


def find_feed_twin(conn: sqlite3.Connection, account_id: str, date_iso: str, amount: float) -> str | None:
    """Id of a feed row on ``account_id`` posted on ``date_iso`` for ``amount``, else None."""

    row = conn.execute(
        "SELECT id FROM transactions WHERE account_id = ? AND source = ? "
        "AND substr(posted, 1, 10) = ? AND round(amount, 2) = round(?, 2) LIMIT 1",
        (account_id, FEED_SOURCE, date_iso, amount),
    ).fetchone()
    return row[0] if row else None


def absorb_paste_twins(conn: sqlite3.Connection, feed_id: str, account_id: str, posted: str | None, amount: float) -> int:
    """A feed row just landed: delete its paste twin(s), re-pointing references. Returns rows removed."""

    if not posted:
        return 0
    rows = conn.execute(
        "SELECT id FROM transactions WHERE account_id = ? AND source = ? "
        "AND substr(posted, 1, 10) = ? AND round(amount, 2) = round(?, 2)",
        (account_id, PASTE_SOURCE, posted[:10], amount),
    ).fetchall()
    for (paste_id,) in rows:
        _retire_paste_row(conn, paste_id, feed_id)
    return len(rows)


def dedupe_pasted_transactions_for_db(conn: sqlite3.Connection, *, dry_run: bool = True) -> dict[str, Any]:
    """Pair every paste row with a feed twin (one feed row claims at most one paste row) and remove the paste copies."""

    candidates = conn.execute(
        """
        SELECT p.id AS paste_id, f.id AS feed_id, p.account_id, substr(p.posted, 1, 10) AS posted, p.amount,
               p.payee AS paste_payee, f.payee AS feed_payee
        FROM transactions p
        JOIN transactions f
          ON f.account_id = p.account_id
         AND f.source = ?
         AND substr(f.posted, 1, 10) = substr(p.posted, 1, 10)
         AND round(f.amount, 2) = round(p.amount, 2)
        WHERE p.source = ?
        ORDER BY p.posted DESC, p.id, f.id
        """,
        (FEED_SOURCE, PASTE_SOURCE),
    ).fetchall()

    pairs: list[dict[str, Any]] = []
    claimed_feed: set[str] = set()
    claimed_paste: set[str] = set()
    for row in candidates:
        if row["paste_id"] in claimed_paste or row["feed_id"] in claimed_feed:
            continue
        claimed_paste.add(row["paste_id"])
        claimed_feed.add(row["feed_id"])
        pairs.append(dict(row))

    if not dry_run:
        for pair in pairs:
            _retire_paste_row(conn, pair["paste_id"], pair["feed_id"])

    total = sum(abs(float(p["amount"])) for p in pairs)
    return {
        "status": "preview" if dry_run else "ok",
        "dry_run": dry_run,
        "pairs": len(pairs),
        "deleted": 0 if dry_run else len(pairs),
        "duplicate_amount_total": round(total, 2),
        "sample": [
            {k: pair[k] for k in ("posted", "amount", "paste_payee", "feed_payee")} for pair in pairs[:10]
        ],
        "preview": (
            f"dedupe_pasted_transactions ({'DRY RUN' if dry_run else 'APPLIED'}): "
            f"{len(pairs)} paste row(s) twin a feed row, ${total:,.2f} of double-counted activity. "
            + ("Re-run with dry_run=false to remove the paste copies." if dry_run else "Paste copies removed; feed rows kept.")
        ),
    }


def _retire_paste_row(conn: sqlite3.Connection, paste_id: str, feed_id: str) -> None:
    for table, column in _ID_COLUMNS:
        if _table_exists(conn, table):
            conn.execute(f"UPDATE OR IGNORE {table} SET {column} = ? WHERE {column} = ?", (feed_id, paste_id))
    for table, key, column in _JSON_ID_COLUMNS:
        if not _table_exists(conn, table):
            continue
        for row_key, raw in conn.execute(
            f"SELECT {key}, {column} FROM {table} WHERE {column} LIKE ?", (f"%{paste_id}%",)
        ).fetchall():
            try:
                ids = json.loads(raw or "[]")
            except json.JSONDecodeError:
                continue
            if paste_id in ids:
                ids = [feed_id if i == paste_id else i for i in ids]
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (json.dumps(ids), row_key))
    conn.execute("DELETE FROM transactions WHERE id = ?", (paste_id,))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None
