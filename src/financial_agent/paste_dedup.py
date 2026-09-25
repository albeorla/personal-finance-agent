"""Keep pasted checking rows from coexisting with their bank-feed twins.

The operating checking account gets rows from two lanes: the SimpleFIN feed
(bank ids) and pasted CSVs (synthetic ``checking:`` ids). The paste importer
used to dedupe only against earlier pastes, so a paste covering dates the feed
also delivered wrote every row twice, and a feed row arriving after a paste did
the same from the other side. Spending totals and reconciliation then saw each
transaction twice.

Rule: the feed row wins. A paste row is a twin of a feed row when it sits on the
same account for the same amount and either (a) on the same posted date, or (b)
within ``TWIN_WINDOW_DAYS`` of it with a merchant word in common. (b) exists
because the paste lane and the feed can date one charge differently: a card
purchase pasted with its transaction date (a newspaper on 7/10) lands in the feed
with its posting date (7/13). Twins are skipped at paste time, absorbed when the
feed row lands, and cleaned up once for history by
``dedupe_pasted_transactions_for_db``. References to the paste id (reconciliation
matches, drift evidence, onboarding evidence, rejected check suggestions) are
re-pointed at the feed id before the paste row is deleted.

ponytail: an exact-date twin needs no merchant check, so two genuinely distinct
same-day same-amount checking transactions would collapse to one paste copy;
accepted because the feed is authoritative for this account and the paste lane
only backfills feed lag. A cross-date twin needs a shared merchant word, and
generic words (debit, withdrawal, transfer, ...) do not count, so ATM pulls and
transfers only ever twin on the exact date.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from typing import Any

PASTE_SOURCE = "checking_paste"
FEED_SOURCE = "simplefin"

# How far apart the paste lane and the feed may date the same charge. Observed:
# 2-4 days (transaction date vs posting date across a weekend).
TWIN_WINDOW_DAYS = 5

# Bank boilerplate that says how money moved, not who it went to. A shared word
# from this set is not evidence of the same merchant.
_GENERIC_WORDS = frozenset(
    {
        "pos", "debit", "credit", "card", "purchase", "payment", "online", "transfer",
        "withdrawal", "deposit", "atm", "check", "checking", "chk", "savings", "ach",
        "web", "ppd", "recurring", "transaction", "from", "the", "inc", "llc",
        "venmo", "zelle", "paypal", "cash",
    }
)

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


def _merchant_words(text: str | None) -> set[str]:
    words = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 4 and not w.isdigit() and w not in _GENERIC_WORDS}


def same_merchant(a: str | None, b: str | None) -> bool:
    """True when the two texts share a merchant word (a truncated word counts: DAILYPLAN ~ DAILYPLANET)."""

    wa, wb = _merchant_words(a), _merchant_words(b)
    return any(x == y or x.startswith(y) or y.startswith(x) for x in wa for y in wb)


def _day_gap(a: str, b: str) -> int:
    return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)


def find_feed_twin(
    conn: sqlite3.Connection, account_id: str, date_iso: str, amount: float, text: str | None = None
) -> str | None:
    """Id of the feed row on ``account_id`` that already carries this paste row, else None.

    Same posted date and amount is a twin outright. Without one, a feed row for
    the same amount within ``TWIN_WINDOW_DAYS`` that shares a merchant word with
    ``text`` is a twin; the nearest date wins.
    """

    row = conn.execute(
        "SELECT id FROM transactions WHERE account_id = ? AND source = ? "
        "AND substr(posted, 1, 10) = ? AND round(amount, 2) = round(?, 2) LIMIT 1",
        (account_id, FEED_SOURCE, date_iso, amount),
    ).fetchone()
    if row:
        return row[0]
    if not text:
        return None
    nearby = conn.execute(
        "SELECT id, substr(posted, 1, 10) AS posted, payee, description FROM transactions "
        "WHERE account_id = ? AND source = ? AND round(amount, 2) = round(?, 2) "
        "AND abs(julianday(substr(posted, 1, 10)) - julianday(?)) <= ?",
        (account_id, FEED_SOURCE, amount, date_iso, TWIN_WINDOW_DAYS),
    ).fetchall()
    matches = [
        r for r in nearby if same_merchant(text, f"{r[2] or ''} {r[3] or ''}")
    ]
    if not matches:
        return None
    return min(matches, key=lambda r: (_day_gap(r[1], date_iso), r[0]))[0]


def absorb_paste_twins(conn: sqlite3.Connection, feed_id: str, account_id: str, posted: str | None, amount: float) -> int:
    """A feed row just landed: delete its paste twin(s), re-pointing references. Returns rows removed.

    Same-date twins go first and outright. Without one, the paste rows on the
    nearest date within the window that share a merchant word with the feed row
    go (several paste copies of one charge on that date all go).
    """

    if not posted:
        return 0
    rows = conn.execute(
        "SELECT id FROM transactions WHERE account_id = ? AND source = ? "
        "AND substr(posted, 1, 10) = ? AND round(amount, 2) = round(?, 2)",
        (account_id, PASTE_SOURCE, posted[:10], amount),
    ).fetchall()
    paste_ids = [r[0] for r in rows]
    if not paste_ids:
        feed = conn.execute("SELECT payee, description FROM transactions WHERE id = ?", (feed_id,)).fetchone()
        feed_text = f"{feed[0] or ''} {feed[1] or ''}" if feed else ""
        nearby = [
            r
            for r in conn.execute(
                "SELECT id, substr(posted, 1, 10), payee, description FROM transactions "
                "WHERE account_id = ? AND source = ? AND round(amount, 2) = round(?, 2) "
                "AND abs(julianday(substr(posted, 1, 10)) - julianday(?)) <= ?",
                (account_id, PASTE_SOURCE, amount, posted[:10], TWIN_WINDOW_DAYS),
            ).fetchall()
            if same_merchant(feed_text, f"{r[2] or ''} {r[3] or ''}")
        ]
        if nearby:
            nearest = min(_day_gap(r[1], posted) for r in nearby)
            paste_ids = [r[0] for r in nearby if _day_gap(r[1], posted) == nearest]
    for paste_id in paste_ids:
        _retire_paste_row(conn, paste_id, feed_id)
    return len(paste_ids)


def dedupe_pasted_transactions_for_db(conn: sqlite3.Connection, *, dry_run: bool = True) -> dict[str, Any]:
    """Pair every paste row with its feed twin and remove the paste copies.

    One feed row claims at most one paste row, exact-date pairs first, then the
    nearest cross-date pair with a shared merchant word. A paste row left over
    after that, sitting on the same date for the same amount and merchant as a
    paste row already paired, is a second paste copy of the same charge (two
    pastes that formatted the payee differently) and is retired into the same
    feed row, mirroring ``absorb_paste_twins``.
    """

    rows = conn.execute(
        """
        SELECT p.id AS paste_id, f.id AS feed_id, p.account_id, substr(p.posted, 1, 10) AS posted, p.amount,
               p.payee AS paste_payee, p.description AS paste_description,
               f.payee AS feed_payee, f.description AS feed_description,
               abs(julianday(substr(f.posted, 1, 10)) - julianday(substr(p.posted, 1, 10))) AS gap
        FROM transactions p
        JOIN transactions f
          ON f.account_id = p.account_id
         AND f.source = ?
         AND round(f.amount, 2) = round(p.amount, 2)
         AND abs(julianday(substr(f.posted, 1, 10)) - julianday(substr(p.posted, 1, 10))) <= ?
        WHERE p.source = ?
        """,
        (FEED_SOURCE, TWIN_WINDOW_DAYS, PASTE_SOURCE),
    ).fetchall()
    candidates = [
        r
        for r in rows
        if r["gap"] == 0
        or same_merchant(
            f"{r['paste_payee'] or ''} {r['paste_description'] or ''}",
            f"{r['feed_payee'] or ''} {r['feed_description'] or ''}",
        )
    ]
    candidates.sort(key=lambda r: (r["gap"], r["posted"], r["paste_id"], r["feed_id"]))

    pairs: list[dict[str, Any]] = []
    claimed_feed: set[str] = set()
    claimed_paste: set[str] = set()
    for row in candidates:
        if row["paste_id"] in claimed_paste or row["feed_id"] in claimed_feed:
            continue
        claimed_paste.add(row["paste_id"])
        claimed_feed.add(row["feed_id"])
        pairs.append(dict(row))

    # Second paste copies of an already-paired charge.
    paired = list(pairs)
    for row in candidates:
        if row["paste_id"] in claimed_paste:
            continue
        text = f"{row['paste_payee'] or ''} {row['paste_description'] or ''}"
        for pair in paired:
            if (
                pair["account_id"] == row["account_id"]
                and pair["posted"] == row["posted"]
                and round(float(pair["amount"]), 2) == round(float(row["amount"]), 2)
                and same_merchant(text, f"{pair['paste_payee'] or ''} {pair['paste_description'] or ''}")
            ):
                claimed_paste.add(row["paste_id"])
                pairs.append({**dict(row), "feed_id": pair["feed_id"]})
                break

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
