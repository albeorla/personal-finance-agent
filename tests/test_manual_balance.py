"""Tests for the manual balance correction tool.

Covers fuzzy account matching, snapshot recording through the shared insert,
ambiguity/not-found handling, input validation, the manual_note column
migration, recorded-at ordering against syncs, and that get_finance_status /
get_daily_digest reflect a manual correction.
"""

import sqlite3
from datetime import UTC, datetime

import pytest

from financial_agent.config import ensure_source_tables
from financial_agent.digest import build_daily_digest
from financial_agent.manual_balance import set_manual_balance
from financial_agent.status import get_finance_status


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _add_account(conn, account_id, name, org):
    conn.execute(
        "INSERT INTO accounts (id, name, org, kind, currency, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, name, org, "", "USD", "2026-06-01T00:00:00+00:00", "2026-06-20T10:00:00+00:00"),
    )


def _add_snapshot(conn, account_id, balance, recorded_at, source="simplefin", available=None):
    conn.execute(
        "INSERT INTO balance_snapshots (account_id, balance, available, recorded_at, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_id, balance, balance if available is None else available, recorded_at, source),
    )


def _build_db(path, accounts, snapshots=()):
    conn = _connect(path)
    ensure_source_tables(conn)
    for account_id, name, org in accounts:
        _add_account(conn, account_id, name, org)
    for account_id, balance, recorded_at in snapshots:
        _add_snapshot(conn, account_id, balance, recorded_at)
    conn.commit()
    conn.close()


def _latest_balance(db_path, account_id, *, now=None):
    result = get_finance_status(
        db_path=db_path,
        windows=[7],
        now=now or datetime(2026, 6, 21, 12, 0, tzinfo=UTC),
    )
    for account in result["balances"]["accounts"]:
        if account["account_id"] == account_id:
            return account["balance"]
    raise AssertionError(f"account {account_id} not in status balances")


def test_set_manual_balance_exact_match_updates_status(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_db(
        db_path,
        accounts=[("apple-1", "Apple Card", "Goldman Sachs")],
        snapshots=[("apple-1", -5949.0, "2026-06-01T10:00:00+00:00")],
    )

    conn = _connect(db_path)
    result = set_manual_balance(conn, "apple", 6122.0, "2026-06-20", note="Updated via portal")
    conn.commit()
    conn.close()

    assert result["status"] == "ok"
    assert result["account_id"] == "apple-1"
    assert result["balance"] == 6122.0
    assert result["available"] == 6122.0
    assert result["source"] == "manual"
    assert result["note"] == "Updated via portal"

    # Snapshot row recorded through the shared insert.
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT balance, available, source, manual_note FROM balance_snapshots "
        "WHERE account_id = ? ORDER BY recorded_at DESC, id DESC LIMIT 1",
        ("apple-1",),
    ).fetchone()
    conn.close()
    assert row["balance"] == 6122.0
    assert row["available"] == 6122.0
    assert row["source"] == "manual"
    assert row["manual_note"] == "Updated via portal"

    assert _latest_balance(db_path, "apple-1") == 6122.0

    digest = build_daily_digest(db_path=str(db_path), as_of_date="2026-06-20")
    apple = next(a for a in digest["balances"]["accounts"] if a["name"] == "Apple Card")
    assert apple["balance"] == 6122.0


def test_set_manual_balance_ambiguous_returns_candidates(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_db(
        db_path,
        accounts=[
            ("apple-card", "Apple Card", "Goldman Sachs"),
            ("apple-sav", "Apple Savings", "Goldman Sachs"),
        ],
    )

    conn = _connect(db_path)
    result = set_manual_balance(conn, "apple", 5000.0, "2026-06-20")
    conn.commit()
    conn.close()

    assert result["status"] == "ambiguous"
    ids = {c["account_id"] for c in result["candidates"]}
    assert ids == {"apple-card", "apple-sav"}
    for candidate in result["candidates"]:
        assert "account_name" in candidate
        assert "org" in candidate
        assert "match_score" in candidate

    # Nothing written.
    conn = _connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0]
    conn.close()
    assert count == 0


def test_set_manual_balance_no_match_returns_not_found(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("chase-1", "Chase Checking", "Chase")])

    conn = _connect(db_path)
    result = set_manual_balance(conn, "Bank of America", 1000.0, "2026-06-20")
    conn.commit()
    conn.close()

    assert result["status"] == "not_found"
    assert "message" in result

    conn = _connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0]
    conn.close()
    assert count == 0


def test_set_manual_balance_requires_non_empty_account_query(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("chase-1", "Chase Checking", "Chase")])
    conn = _connect(db_path)
    try:
        with pytest.raises(ValueError, match="account_query"):
            set_manual_balance(conn, "", 1000.0, "2026-06-20")
    finally:
        conn.close()


def test_set_manual_balance_requires_numeric_balance(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("chase-1", "Chase Checking", "Chase")])
    conn = _connect(db_path)
    try:
        with pytest.raises(TypeError):
            set_manual_balance(conn, "chase", True, "2026-06-20")
    finally:
        conn.close()


def test_set_manual_balance_requires_valid_iso_date(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("chase-1", "Chase Checking", "Chase")])
    conn = _connect(db_path)
    try:
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            set_manual_balance(conn, "chase", 1000.0, "2026/06/20")
    finally:
        conn.close()


def test_set_manual_balance_note_max_length(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("chase-1", "Chase Checking", "Chase")])
    conn = _connect(db_path)
    try:
        with pytest.raises(ValueError, match="500"):
            set_manual_balance(conn, "chase", 1000.0, "2026-06-20", note="x" * 501)
    finally:
        conn.close()


def test_set_manual_balance_manual_note_column_added_idempotently(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    # Source schema deliberately has no manual_note column.
    _build_db(db_path, accounts=[("chase-1", "Chase Checking", "Chase")])

    conn = _connect(db_path)
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(balance_snapshots)").fetchall()}
    assert "manual_note" not in cols_before

    set_manual_balance(conn, "chase", 1000.0, "2026-06-20", note="first")
    conn.commit()
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(balance_snapshots)").fetchall()}
    assert "manual_note" in cols_after

    # Second call is a no-op on the column and does not raise.
    result = set_manual_balance(conn, "chase", 1010.0, "2026-06-20", note="second")
    conn.commit()
    conn.close()
    assert result["status"] == "ok"


def test_set_manual_balance_newer_manual_snapshot_wins_over_older_simplefin(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_db(
        db_path,
        accounts=[("chase-1", "Chase Checking", "Chase")],
        snapshots=[("chase-1", 5949.0, "2026-06-20T10:00:00+00:00")],
    )

    conn = _connect(db_path)
    result = set_manual_balance(conn, "chase", 6122.0, "2026-06-20", note="Apple portal")
    conn.commit()
    conn.close()

    assert result["recorded_at"] == "2026-06-20T12:00:00+00:00"
    assert _latest_balance(db_path, "chase-1") == 6122.0


def test_same_day_simplefin_after_manual_does_not_override(tmp_path):
    """Regression: manual is authoritative for its day regardless of insert order.

    Item B requirement: for balance-only / "Updated Monthly" accounts, a manual
    correction must win over a same-day SimpleFIN snapshot regardless of
    recorded_at or insert order. Previously the manual row only won because it was
    out-stamped later; a same-day SimpleFIN sync recorded after it would shadow
    the correction. Now manual is preferred for the period: inserting a same-day
    SimpleFIN row -- even one recorded later in the day -- does NOT override the
    manual value, and get_finance_status still returns the manual balance.
    """

    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("chase-1", "Chase Checking", "Chase")])

    conn = _connect(db_path)
    set_manual_balance(conn, "chase", 6122.0, "2026-06-20")
    conn.commit()
    conn.close()
    assert _latest_balance(db_path, "chase-1") == 6122.0

    # A same-day SimpleFIN snapshot recorded LATER in the day must not shadow the
    # manual correction.
    conn = _connect(db_path)
    _add_snapshot(conn, "chase-1", 6100.0, "2026-06-20T14:00:00+00:00")
    conn.commit()
    conn.close()
    assert _latest_balance(db_path, "chase-1") == 6122.0


def test_same_day_simplefin_before_manual_does_not_override(tmp_path):
    """Insert-order independence: SimpleFIN first, then manual, manual still wins.

    Mirrors the regression above but with the opposite insert order so the rule
    holds regardless of which row landed first.
    """

    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("chase-1", "Chase Checking", "Chase")])

    conn = _connect(db_path)
    _add_snapshot(conn, "chase-1", 6100.0, "2026-06-20T14:00:00+00:00")
    conn.commit()
    conn.close()
    assert _latest_balance(db_path, "chase-1") == 6100.0

    conn = _connect(db_path)
    set_manual_balance(conn, "chase", 6122.0, "2026-06-20")
    conn.commit()
    conn.close()
    assert _latest_balance(db_path, "chase-1") == 6122.0


def test_next_day_simplefin_does_not_supersede_manual(tmp_path):
    """Sticky manual: a NEXT-day SimpleFIN sync does NOT supersede the manual.

    The user has decided manual balances are sticky: a manual correction holds
    over every feed snapshot -- including one recorded on a later calendar day --
    until the user records a newer manual or clears it. For balance-only
    "Updated Monthly" accounts (e.g. the Apple Card) the feed can lag reality for
    weeks, so a stale next-day feed value must not silently re-shadow the
    correction.
    """

    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("chase-1", "Chase Checking", "Chase")])

    conn = _connect(db_path)
    set_manual_balance(conn, "chase", 6122.0, "2026-06-20")
    conn.commit()
    conn.close()
    assert _latest_balance(db_path, "chase-1") == 6122.0

    # A later-day SimpleFIN snapshot must NOT shadow the sticky manual.
    conn = _connect(db_path)
    _add_snapshot(conn, "chase-1", 6100.0, "2026-06-21T09:00:00+00:00")
    conn.commit()
    conn.close()
    now = datetime(2026, 6, 21, 18, 0, tzinfo=UTC)
    assert _latest_balance(db_path, "chase-1", now=now) == 6122.0


def test_newer_manual_replaces_older_manual_across_days(tmp_path):
    """A newer manual correction supersedes an older manual ('until you change it').

    Sticky does not mean frozen forever: the LATEST manual wins among manuals, so
    a fresh manual correction on a later day replaces the prior manual value.
    """

    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("chase-1", "Chase Checking", "Chase")])

    conn = _connect(db_path)
    set_manual_balance(conn, "chase", 6122.0, "2026-06-20")
    conn.commit()
    conn.close()
    assert _latest_balance(db_path, "chase-1") == 6122.0

    conn = _connect(db_path)
    set_manual_balance(conn, "chase", 5800.0, "2026-06-22")
    conn.commit()
    conn.close()
    now = datetime(2026, 6, 22, 18, 0, tzinfo=UTC)
    assert _latest_balance(db_path, "chase-1", now=now) == 5800.0


def test_no_manual_latest_feed_wins(tmp_path):
    """With no manual snapshot, the latest feed snapshot wins (recency)."""

    db_path = tmp_path / "transactions.sqlite"
    _build_db(
        db_path,
        accounts=[("chase-1", "Chase Checking", "Chase")],
        snapshots=[("chase-1", 5000.0, "2026-06-19T10:00:00+00:00")],
    )

    conn = _connect(db_path)
    _add_snapshot(conn, "chase-1", 5500.0, "2026-06-21T10:00:00+00:00")
    conn.commit()
    conn.close()
    now = datetime(2026, 6, 21, 18, 0, tzinfo=UTC)
    assert _latest_balance(db_path, "chase-1", now=now) == 5500.0


def test_set_manual_balance_negative_correction_wins_over_same_day_later_simplefin(tmp_path):
    """Apple Card regression: a negative manual correction must win over a stale
    same-day SimpleFIN snapshot recorded later in the day.

    Reproduces the live bug where the Apple Card (a liability stored negative)
    was corrected to -6122.03 but get_finance_status kept showing the stale
    -5949.32 feed value, because the manual row was stamped noon UTC while the
    SimpleFIN snapshot was recorded at 14:56 (and carried no timezone offset, so
    string ordering put it ahead). The fix stamps the manual row strictly after
    the latest existing snapshot and preserves the caller's sign.
    """

    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("apple-1", "Owner", "Apple Card (Updated Monthly)")])

    conn = _connect(db_path)
    # Same-day SimpleFIN snapshot, recorded after noon, stored with no tz offset
    # (exactly how SimpleFIN writes it) and negative (liability).
    _add_snapshot(
        conn, "apple-1", -5949.32, "2026-06-24T14:56:45", available=-7917.48
    )
    conn.commit()
    conn.close()

    conn = _connect(db_path)
    result = set_manual_balance(conn, "Apple Card", -6122.03, "2026-06-24", note="Portal")
    conn.commit()
    conn.close()

    assert result["status"] == "ok"
    assert result["account_id"] == "apple-1"
    # Sign preserved: the card correction stays negative.
    assert result["balance"] == -6122.03
    assert result["available"] == -6122.03

    # Manual snapshot stamped strictly after the 14:56 feed snapshot.
    assert result["recorded_at"] > "2026-06-24T14:56:45"

    # get_finance_status reflects the manual correction, not the stale feed value.
    now = datetime(2026, 6, 24, 18, 0, tzinfo=UTC)
    assert _latest_balance(db_path, "apple-1", now=now) == -6122.03


def test_set_manual_balance_deletes_superseded_same_day_manual_row(tmp_path):
    """A wrong-sign manual row must not linger as a landmine.

    Reproduces the live Apple Card history: a first manual correction stored the
    wrong sign (+6122.03 at noon UTC). A second, correct manual correction
    (-6122.03) on the same as-of date must both win resolution AND delete the
    stale wrong-sign manual row, so it can never re-win if a future same-day
    correction ever lands at or before its timestamp. Feed snapshots are
    untouched.
    """

    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("apple-1", "Owner", "Apple Card (Updated Monthly)")])

    conn = _connect(db_path)
    # Same-day SimpleFIN feed snapshot (negative liability, no tz offset).
    _add_snapshot(conn, "apple-1", -5949.32, "2026-06-24T14:56:45", available=-7917.48)
    conn.commit()
    conn.close()

    # First manual correction stores the WRONG sign (the original live bug).
    conn = _connect(db_path)
    first = set_manual_balance(conn, "Apple Card", 6122.03, "2026-06-24", note="wrong sign")
    conn.commit()
    conn.close()
    assert first["status"] == "ok"

    # Correct manual correction, same as-of date, right (negative) sign.
    conn = _connect(db_path)
    second = set_manual_balance(conn, "Apple Card", -6122.03, "2026-06-24", note="fixed")
    conn.commit()
    conn.close()

    assert second["status"] == "ok"
    assert second["balance"] == -6122.03
    assert second["superseded_manual_snapshots"] == 1

    conn = _connect(db_path)
    manual_rows = conn.execute(
        "SELECT balance FROM balance_snapshots WHERE account_id = ? AND source = 'manual'",
        ("apple-1",),
    ).fetchall()
    feed_rows = conn.execute(
        "SELECT balance FROM balance_snapshots WHERE account_id = ? AND source = 'simplefin'",
        ("apple-1",),
    ).fetchall()
    conn.close()

    # Exactly one manual row remains (the correct one); the wrong-sign row is gone.
    assert len(manual_rows) == 1
    assert manual_rows[0]["balance"] == -6122.03
    # Feed history is preserved.
    assert len(feed_rows) == 1
    assert feed_rows[0]["balance"] == -5949.32

    now = datetime(2026, 6, 24, 18, 0, tzinfo=UTC)
    assert _latest_balance(db_path, "apple-1", now=now) == -6122.03


def test_set_manual_balance_keeps_manual_rows_from_other_dates(tmp_path):
    """The landmine cleanup is scoped to the same as-of date only."""

    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("apple-1", "Apple Card", "Goldman Sachs")])

    conn = _connect(db_path)
    set_manual_balance(conn, "apple", -5000.0, "2026-06-23")
    conn.commit()
    conn.close()

    conn = _connect(db_path)
    result = set_manual_balance(conn, "apple", -6122.03, "2026-06-24")
    conn.commit()
    conn.close()

    # The prior-day manual row is on a different as-of date, so it stays.
    assert result["superseded_manual_snapshots"] == 0
    conn = _connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM balance_snapshots WHERE account_id = ? AND source = 'manual'",
        ("apple-1",),
    ).fetchone()[0]
    conn.close()
    assert count == 2


def test_set_manual_balance_substring_match_scores_high(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_db(db_path, accounts=[("sav-1", "Savings 6175", "Chase")])

    conn = _connect(db_path)
    result = set_manual_balance(conn, "savings", 10000.0, "2026-06-20")
    conn.commit()
    conn.close()

    assert result["status"] == "ok"
    assert result["account_id"] == "sav-1"


def test_set_manual_balance_org_tie_when_names_equal_is_ambiguous(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_db(
        db_path,
        accounts=[
            ("chase-card", "Card (5000)", "Chase"),
            ("amex-card", "Card (5000)", "Amex"),
        ],
    )

    conn = _connect(db_path)
    result = set_manual_balance(conn, "chase", 1000.0, "2026-06-20")
    conn.commit()
    conn.close()

    # The names are identical and neither contains the query, so the only signal
    # is the smaller org boost. That keeps the two accounts within the tie band,
    # so the tool refuses to guess and returns both candidates for refinement
    # rather than silently picking the Chase card.
    assert result["status"] == "ambiguous"
    ids = {c["account_id"] for c in result["candidates"]}
    assert ids == {"chase-card", "amex-card"}
