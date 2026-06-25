"""Tests for the historical backfill (G1: did-it-clear). No network."""

import sqlite3

from financial_agent.backfill import backfill_recurring_instances, list_recently_cleared
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema


def _db(path, transactions=()):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT,
            first_seen_at TEXT, last_seen_at TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,
            amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT,
            first_seen_at TEXT, last_seen_at TEXT, fetched_at TEXT);
        """
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('ACT-chk','PREMIER PLUS CKG (4321)','Chase','checking','USD')")
    conn.executemany(
        "INSERT INTO transactions (id,account_id,posted,amount,payee,description,pending,source) VALUES (?,?,?,?,?,?,0,'simplefin')",
        transactions,
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    conn.commit()
    return conn


def _rent(conn):
    apply_obligation_instances(
        conn,
        obligation={"id": "rent_check", "name": "Rent check", "kind": "housing", "cadence": "monthly", "status": "active", "source": "seed"},
        instances=[{"id": "rent_check:2026-07-03", "due_date": "2026-07-03", "amount": 3000.0, "direction": "outflow", "source": "seed"}],
    )


def test_backfill_creates_past_instances_and_reconciles_exact_rent(tmp_path):
    conn = _db(tmp_path / "b.sqlite", transactions=[
        ("c1", "ACT-chk", "2026-06-03T08:00:00", -3000.0, "Check #1229", ""),
        ("c2", "ACT-chk", "2026-05-04T08:00:00", -3000.0, "Check #1227", ""),
    ])
    _rent(conn)
    res = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90)
    assert res["instances_created"] >= 2  # ~2026-06-03 and 2026-05-03 fall in the trailing 90d
    # the future instance is untouched, and past instances exist
    past = conn.execute("SELECT due_date FROM obligation_instances WHERE obligation_id='rent_check' AND due_date < '2026-06-21' ORDER BY due_date").fetchall()
    assert len(past) >= 2
    # exact $3000 checks reconciled -> show as cleared
    cleared = list_recently_cleared(conn, as_of_date="2026-06-21")
    assert any(c["obligation_name"] == "Rent check" and c["cleared"] for c in cleared)


def test_backfill_is_idempotent(tmp_path):
    conn = _db(tmp_path / "b.sqlite", transactions=[("c1", "ACT-chk", "2026-06-03T08:00:00", -3000.0, "Check #1229", "")])
    _rent(conn)
    first = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90)["instances_created"]
    second = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90)["instances_created"]
    assert first >= 1 and second == 0  # nothing new on the second pass


def test_backfill_skips_unknown_cadence(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "odd", "name": "Irregular thing", "kind": "misc", "cadence": "irregular", "status": "active", "source": "seed"},
        instances=[{"id": "odd:2026-07-01", "due_date": "2026-07-01", "amount": 50.0, "direction": "outflow", "source": "seed"}],
    )
    res = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90, reconcile=False)
    assert res["instances_created"] == 0


def test_backfill_does_not_create_future_instances(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _rent(conn)
    backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90, reconcile=False)
    # only the original future instance is on/after as_of
    future = conn.execute("SELECT COUNT(*) FROM obligation_instances WHERE obligation_id='rent_check' AND due_date >= '2026-06-21'").fetchone()[0]
    assert future == 1


def test_backfill_skips_inflows(tmp_path):
    # income/reimbursements must never be backfilled (they'd read as bogus "missing"/"owe")
    conn = _db(tmp_path / "b.sqlite")
    apply_obligation_instances(conn,
        obligation={"id": "anthem", "name": "Anthem reimbursement", "kind": "income", "cadence": "monthly", "status": "active", "source": "seed"},
        instances=[{"id": "anthem:2026-07-15", "due_date": "2026-07-15", "amount": 440.0, "direction": "inflow", "source": "seed"}])
    res = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90, reconcile=False)
    assert res["instances_created"] == 0


def test_backfill_cancels_unmatched_history_to_avoid_false_missing(tmp_path):
    # A monthly bill whose only posted payment is off-cadence/different-amount: the
    # backfilled past instances that can't be matched must be canceled, not left
    # 'expected' (which would become a false CRITICAL "missing payment" in drift).
    conn = _db(tmp_path / "b.sqlite", transactions=[
        ("c1", "ACT-chk", "2026-06-03T08:00:00", -3000.0, "Check #1229", ""),  # matches June rent exactly
    ])
    _rent(conn)
    res = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90)
    assert res["unmatched_canceled"] >= 1  # the older months with no matching check
    # no backfilled past instance is left 'expected' without a match
    leftover = conn.execute(
        "SELECT COUNT(*) FROM obligation_instances WHERE source='backfill' AND status='expected' "
        "AND id NOT IN (SELECT obligation_instance_id FROM transaction_obligation_matches)"
    ).fetchone()[0]
    assert leftover == 0
    # the matched June rent is kept and shows cleared
    assert any(c["obligation_name"] == "Rent check" for c in list_recently_cleared(conn, as_of_date="2026-06-21"))
