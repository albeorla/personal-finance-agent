"""Manually correct a stale account balance.

Some feeds are balance-only and refresh slowly (e.g. the Apple Card portal
shows "Updated Monthly"), so the latest SimpleFIN snapshot can lag reality.
``set_manual_balance`` records a fresh ``balance_snapshots`` row for the matched
account through the same insert the sync layer uses, so ``get_finance_status``
and ``get_daily_digest`` pick it up as the latest balance immediately.

The manual snapshot is written as just another ``balance_snapshots`` row with
``source='manual'``. Balance resolution (in ``status._latest_balances``) makes a
manual row STICKY: it prefers ``source='manual'`` over ``source='simplefin'``
regardless of calendar day, ``recorded_at``, or insert order, so a manual
correction wins over every feed snapshot -- including a SimpleFIN sync on a later
day -- until the user records a NEWER manual correction (which replaces the older
one) or clears it. This is intended for balance-only "Updated Monthly" accounts
(e.g. the Apple Card) whose feed lags reality for weeks: the user's correction
must hold until they explicitly change it.

The manual row is still stamped strictly later than any snapshot already present
for the account. That out-stamping is now belt-and-suspenders rather than the
load-bearing mechanism: the source-priority ordering is what guarantees the
correction wins, so the result no longer depends on a fragile timestamp race.

The caller's sign is preserved verbatim: a liability correction such as the
Apple Card stays negative (e.g. ``-6122.03``), matching how SimpleFIN stores
card and loan balances.

A fresh manual correction also *replaces* any earlier manual correction for the
same account on the same as-of date: those superseded manual rows are deleted
before the new one is inserted. Out-stamping alone left the old row in history as
a latent landmine -- a wrong-sign manual row (e.g. ``+6122.03``) could re-win
balance resolution if a future same-day correction ever landed at or before its
timestamp. Deleting same-account, same-day manual rows removes that hazard while
leaving the feed (``simplefin``) history untouched.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from difflib import SequenceMatcher
from typing import Any


# Canonical precedence for "which balance_snapshots row is the truth for an
# account": a manual correction wins over any feed snapshot regardless of
# timestamp, then newest recorded_at, then newest row id. Every code path that
# reads the latest balance per account must order by this, or a manual correction
# silently reverts to the stale feed the next time it syncs. ``{alias}`` is the
# balance_snapshots alias (or the bare table name when unaliased).
BALANCE_PRECEDENCE_ORDER_BY = """
    ORDER BY
        CASE WHEN {alias}.source = 'manual' THEN 0 ELSE 1 END,
        {alias}.recorded_at DESC,
        {alias}.id DESC
"""


# Default stamp: noon UTC on the as-of date. Readable, and after most morning
# syncs. When a later same-day snapshot already exists we bump past it (see
# ``_effective_recorded_at``) so the manual correction is never shadowed.
_MANUAL_RECORDED_TIME = "T12:00:00+00:00"
_MANUAL_SOURCE = "manual"
_NOTE_MAX_LEN = 500
# Below this fuzzy score a candidate is not considered a match at all.
_MATCH_FLOOR = 0.4
# Two candidates within this score of the top match make the result ambiguous.
_TIE_BAND = 0.1


def _ensure_manual_note_column(conn: sqlite3.Connection) -> None:
    """Add balance_snapshots.manual_note if missing (idempotent, non-breaking).

    The source schema mirrors the SimpleFIN feed and has no note column, so we
    add it here rather than in SOURCE_SCHEMA. ALTER TABLE ADD COLUMN is a no-op
    on existing rows and safe to run repeatedly.
    """

    columns = {row[1] for row in conn.execute("PRAGMA table_info(balance_snapshots)").fetchall()}
    if "manual_note" not in columns:
        conn.execute("ALTER TABLE balance_snapshots ADD COLUMN manual_note TEXT")


def _parse_recorded_at(value: str) -> dt.datetime | None:
    """Parse a snapshot recorded_at into an aware UTC datetime, or None.

    Snapshots from SimpleFIN may carry no timezone offset (e.g.
    ``2026-06-24T14:56:45``) while manual rows are written with an explicit
    ``+00:00``. Treat a naive timestamp as UTC so both compare on the same axis.
    """

    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _effective_recorded_at(conn: sqlite3.Connection, account_id: str, as_of_date: str) -> str:
    """Recorded-at for a manual snapshot that is guaranteed to be the newest.

    Starts from noon UTC on the as-of date, then bumps to one second after the
    account's latest existing snapshot if that snapshot is at or after the
    default stamp. This makes the manual correction win balance resolution
    (which orders by recorded_at DESC, id DESC) even when a same-day SimpleFIN
    snapshot was recorded later in the day.
    """

    default_at = f"{as_of_date}{_MANUAL_RECORDED_TIME}"
    default_dt = _parse_recorded_at(default_at)

    rows = conn.execute(
        "SELECT recorded_at FROM balance_snapshots WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    latest: dt.datetime | None = None
    for row in rows:
        value = row["recorded_at"] if isinstance(row, sqlite3.Row) else row[0]
        parsed = _parse_recorded_at(value)
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed

    if latest is not None and default_dt is not None and latest >= default_dt:
        bumped = latest + dt.timedelta(seconds=1)
        return bumped.isoformat()
    return default_at


def _delete_superseded_manual_snapshots(
    conn: sqlite3.Connection, account_id: str, as_of_date: str
) -> int:
    """Delete prior manual snapshots for this account on the same as-of date.

    A new manual correction is the authoritative manual value for that account on
    that day, so any earlier manual row for the same as-of date is stale and
    should not linger in history (a wrong-sign one could re-win resolution if a
    future same-day correction landed at or before its timestamp). Only
    ``source='manual'`` rows are removed; feed snapshots are never touched.

    The as-of date is matched against the date portion of ``recorded_at``,
    normalized to UTC so a naive timestamp and an offset-bearing one on the same
    calendar day both match.
    """

    rows = conn.execute(
        "SELECT id, recorded_at FROM balance_snapshots WHERE account_id = ? AND source = ?",
        (account_id, _MANUAL_SOURCE),
    ).fetchall()

    stale_ids = []
    for row in rows:
        recorded_at = row["recorded_at"] if isinstance(row, sqlite3.Row) else row[1]
        parsed = _parse_recorded_at(recorded_at)
        if parsed is not None and parsed.date().isoformat() == as_of_date:
            stale_ids.append(row["id"] if isinstance(row, sqlite3.Row) else row[0])

    for snapshot_id in stale_ids:
        conn.execute("DELETE FROM balance_snapshots WHERE id = ?", (snapshot_id,))
    return len(stale_ids)


def _score(query: str, name: str, org: str | None) -> float:
    """Fuzzy match score of a query against an account's name and org.

    Substring hits (case-insensitive) score high; otherwise fall back to a
    sequence-ratio similarity. The org contributes a smaller boost so it breaks
    ties when names are equally good but does not dominate the name signal.
    """

    q = query.strip().lower()
    name_l = (name or "").lower()
    org_l = (org or "").lower()

    if not q:
        return 0.0

    name_score = SequenceMatcher(None, q, name_l).ratio()
    if q in name_l:
        # Strong signal: query appears verbatim in the name.
        name_score = max(name_score, 0.9)

    org_score = 0.0
    if org_l:
        org_score = SequenceMatcher(None, q, org_l).ratio()
        if q in org_l or any(tok and tok in org_l for tok in q.split()):
            org_score = max(org_score, 0.9)

    # Name is the primary signal; org is a smaller tiebreaker boost.
    return round(name_score + 0.15 * org_score, 4)


def _validate_inputs(account_query: str, balance: Any, as_of_date: str, note: str | None) -> float:
    if not isinstance(account_query, str) or not account_query.strip():
        raise ValueError("account_query must be a non-empty string")

    if isinstance(balance, bool) or not isinstance(balance, (int, float)):
        raise TypeError(f"balance must be a number, got {type(balance).__name__}")

    if not isinstance(as_of_date, str):
        raise ValueError("as_of_date must be a YYYY-MM-DD string")
    try:
        dt.date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise ValueError(f"as_of_date must be YYYY-MM-DD, got {as_of_date!r}") from exc

    if note is not None:
        if not isinstance(note, str):
            raise ValueError("note must be a string or None")
        if len(note) > _NOTE_MAX_LEN:
            raise ValueError(f"note must be at most {_NOTE_MAX_LEN} characters")

    return float(balance)


def set_manual_balance(
    conn: sqlite3.Connection,
    account_query: str,
    balance: float,
    as_of_date: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Record a manual balance correction for the account matching account_query.

    Resolves account_query against account name/org via fuzzy matching. Requires
    an unambiguous match: if two or more accounts tie closely, returns a
    candidate list and writes nothing. On a clean match, inserts a balance
    snapshot (balance and available both set to the provided balance,
    source='manual') so status and digest reflect the corrected balance.

    The caller owns the transaction (commit/rollback).
    """

    corrected = _validate_inputs(account_query, balance, as_of_date, note)

    rows = conn.execute("SELECT id, name, org FROM accounts").fetchall()
    if not rows:
        return {"status": "not_found", "message": f"No accounts matched {account_query!r} (no accounts in database)"}

    scored = sorted(
        (
            {
                "account_id": row["id"] if isinstance(row, sqlite3.Row) else row[0],
                "account_name": row["name"] if isinstance(row, sqlite3.Row) else row[1],
                "org": row["org"] if isinstance(row, sqlite3.Row) else row[2],
                "match_score": _score(
                    account_query,
                    row["name"] if isinstance(row, sqlite3.Row) else row[1],
                    row["org"] if isinstance(row, sqlite3.Row) else row[2],
                ),
            }
            for row in rows
        ),
        key=lambda c: c["match_score"],
        reverse=True,
    )

    top = scored[0]
    if top["match_score"] < _MATCH_FLOOR:
        return {"status": "not_found", "message": f"No accounts matched {account_query!r}"}

    contenders = [c for c in scored if top["match_score"] - c["match_score"] <= _TIE_BAND]
    if len(contenders) > 1:
        return {
            "status": "ambiguous",
            "candidates": contenders,
            "message": "Multiple accounts matched; please refine account_query",
        }

    _ensure_manual_note_column(conn)
    recorded_at = _effective_recorded_at(conn, top["account_id"], as_of_date)
    # Drop any earlier manual correction for this account on the same as-of date
    # so a stale (possibly wrong-sign) manual row cannot linger and re-win
    # resolution. Computed after _effective_recorded_at so the out-stamp still
    # accounts for the feed snapshot we are superseding.
    superseded = _delete_superseded_manual_snapshots(conn, top["account_id"], as_of_date)

    # Sign sanity. A card/loan balance is stored negative (owed), a deposit
    # positive. If the FEED's established sign for this account is the opposite of
    # the entered value and the magnitude is plausibly the same balance (a
    # fat-fingered sign, e.g. +6122 for a card that reads -5949), flip it - else a
    # wrong-sign manual would beat the feed by precedence and silently flip net
    # worth, hidden by abs() in the debt math. Non-silent: sign_corrected is
    # returned, and entered_balance preserves what was typed.
    sign_corrected = False
    entered_balance = corrected
    feed = conn.execute(
        "SELECT balance FROM balance_snapshots WHERE account_id = ? AND source != 'manual' "
        "AND balance != 0 ORDER BY recorded_at DESC, id DESC LIMIT 1",
        (top["account_id"],),
    ).fetchone()
    if feed is not None and corrected != 0:
        feed_balance = feed["balance"] if isinstance(feed, sqlite3.Row) else feed[0]
        opposite_sign = feed_balance and (feed_balance < 0) != (corrected < 0)
        plausible_magnitude = feed_balance and 0.25 <= abs(corrected) / abs(feed_balance) <= 4.0
        if opposite_sign and plausible_magnitude:
            corrected = -corrected
            sign_corrected = True

    conn.execute(
        "INSERT INTO balance_snapshots (account_id, balance, available, recorded_at, source, manual_note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (top["account_id"], corrected, corrected, recorded_at, _MANUAL_SOURCE, note),
    )

    return {
        "status": "ok",
        "account_id": top["account_id"],
        "account_name": top["account_name"],
        "org": top["org"],
        "balance": round(corrected, 2),
        "available": round(corrected, 2),
        "recorded_at": recorded_at,
        "source": _MANUAL_SOURCE,
        "note": note,
        "superseded_manual_snapshots": superseded,
        "sign_corrected": sign_corrected,
        "entered_balance": round(entered_balance, 2),
    }
