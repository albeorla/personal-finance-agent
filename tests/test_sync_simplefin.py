"""Tests for SimpleFIN sync (slice K). No network: fetch is fixture/monkeypatched."""

import sqlite3

from financial_agent import sync_simplefin as sfin
from financial_agent.config import ensure_source_tables, get_finance_config, load_env_file
import datetime as dt

from financial_agent.sync_simplefin import (
    _epoch_to_iso,
    incremental_start_date,
    normalize_accounts,
    store_accounts,
    sync_simplefin,
)


def _insert_txn(conn, tid, account_id, posted):
    conn.execute(
        "INSERT INTO transactions (id, account_id, posted, amount, source, first_seen_at, last_seen_at, fetched_at) "
        "VALUES (?, ?, ?, 0, 'x', 'x', 'x', 'x')",
        (tid, account_id, posted),
    )


SAMPLE = {
    "accounts": [
        {
            "id": "ACT-chk", "name": "PREMIER PLUS CKG (4321)", "org": {"name": "Chase Bank"},
            "type": "checking", "currency": "USD", "balance": "5000.00", "available-balance": "4950.00",
            "balance-date": 1718000000,
            "transactions": [
                {"id": "T1", "posted": 1718000000, "transacted-at": 1717900000, "amount": "-30.30",
                 "payee": "New York Times", "description": "NYTIMES", "pending": False},
                {"id": "T2", "posted": 1718100000, "transacted-at": 0, "amount": "2011.67",
                 "payee": "Town of Greenwich Payroll", "description": "PAYROLL", "pending": False},
            ],
        }
    ],
    "errors": [],
}


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_source_tables(conn)
    return conn


def test_normalize_accounts_mirrors_legacy_shape():
    norm = normalize_accounts(SAMPLE["accounts"], "2026-06-21T08:00:00")
    a = norm[0]
    assert a["org"] == "Chase Bank"  # org.name
    assert a["kind"] == "checking"  # type
    assert a["balance"] == 5000.0 and a["available_balance"] == 4950.0
    assert len(a["transactions"]) == 2
    assert a["transactions"][0]["amount"] == -30.30 and a["transactions"][0]["pending"] == 0


def test_store_writes_accounts_balances_and_iso_transactions(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    store_accounts(conn, normalize_accounts(SAMPLE["accounts"], "2026-06-21T08:00:00"))
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0] == 1
    txns = conn.execute("SELECT id, posted, amount FROM transactions ORDER BY id").fetchall()
    assert len(txns) == 2
    # posted epoch is stored as an ISO string (matches the copied DB format).
    assert txns[0]["posted"] == _epoch_to_iso(1718000000)
    assert "T" in txns[0]["posted"]


def test_store_is_idempotent_for_transactions(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    norm = normalize_accounts(SAMPLE["accounts"], "2026-06-21T08:00:00")
    first = store_accounts(conn, norm)
    second = store_accounts(conn, norm)
    assert first["inserted"] == 2 and first["updated"] == 0
    assert second["inserted"] == 0 and second["updated"] == 2
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_sync_simplefin_records_a_sync_run(tmp_path, monkeypatch):
    conn = _db(tmp_path / "s.sqlite")
    monkeypatch.setattr(sfin, "fetch_simplefin_accounts", lambda *a, **k: (SAMPLE["accounts"], []))
    result = sync_simplefin(conn, access_url="https://user:pass@bridge.example/simplefin")
    assert result["accounts"] == 1 and result["inserted"] == 2 and result["error"] is None
    run = conn.execute("SELECT accounts_seen, transactions_inserted FROM sync_runs").fetchone()
    assert (run["accounts_seen"], run["transactions_inserted"]) == (1, 2)


def test_sync_records_run_even_when_fetch_fails(tmp_path, monkeypatch):
    conn = _db(tmp_path / "s.sqlite")
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(sfin, "fetch_simplefin_accounts", _boom)
    result = sync_simplefin(conn, access_url="https://user:pass@bridge.example/simplefin")
    assert result["error"] == "network down" and result["accounts"] == 0
    assert conn.execute("SELECT error FROM sync_runs").fetchone()[0] == "network down"


def test_incremental_start_from_oldest_last_posted(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _insert_txn(conn, "a1", "ACT-a", "2026-06-19T08:00:00")
    _insert_txn(conn, "b1", "ACT-b", "2026-06-10T08:00:00")  # oldest per-account latest
    # Large lookback so the floor is irrelevant; expect oldest-latest minus overlap.
    start = incremental_start_date(conn, overlap_days=3, max_lookback_days=100000)
    assert start == "2026-06-07"


def test_incremental_none_when_no_transactions(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    assert incremental_start_date(conn) is None


def test_incremental_floored_at_lookback_cap(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _insert_txn(conn, "old", "ACT-a", "2020-01-01T08:00:00")  # ancient
    start = incremental_start_date(conn, overlap_days=3, max_lookback_days=90)
    assert start == (dt.date.today() - dt.timedelta(days=90)).isoformat()


def test_sync_incremental_uses_resumed_start(tmp_path, monkeypatch):
    conn = _db(tmp_path / "s.sqlite")
    _insert_txn(conn, "a1", "ACT-a", "2026-06-19T08:00:00")
    captured = {}
    def _fetch(access_url, *, start_date=None, end_date=None, timeout=60):
        captured["start_date"] = start_date
        return ([], [])
    monkeypatch.setattr(sfin, "fetch_simplefin_accounts", _fetch)
    sync_simplefin(conn, access_url="https://u:p@host/simplefin", incremental=True, overlap_days=3, lookback_days=100000)
    assert captured["start_date"] == "2026-06-16"  # 2026-06-19 minus 3


def test_sync_incremental_falls_back_to_lookback_when_empty(tmp_path, monkeypatch):
    conn = _db(tmp_path / "s.sqlite")
    captured = {}
    monkeypatch.setattr(sfin, "fetch_simplefin_accounts",
                        lambda url, *, start_date=None, end_date=None, timeout=60: captured.update(start_date=start_date) or ([], []))
    # lookback_days above the SimpleFIN-recommended cap is clamped to 45.
    sync_simplefin(conn, access_url="https://u:p@host/simplefin", incremental=True, lookback_days=90)
    assert captured["start_date"] == (dt.date.today() - dt.timedelta(days=45)).isoformat()


def test_sync_explicit_start_date_is_not_clamped(tmp_path, monkeypatch):
    conn = _db(tmp_path / "s.sqlite")
    captured = {}
    monkeypatch.setattr(sfin, "fetch_simplefin_accounts",
                        lambda url, *, start_date=None, end_date=None, timeout=60: captured.update(start_date=start_date) or ([], []))
    sync_simplefin(conn, access_url="https://u:p@host/simplefin", start_date="2025-01-01")
    assert captured["start_date"] == "2025-01-01"  # deliberate backfill, caller's choice


def test_sync_splits_balance_only_notes_from_warnings(tmp_path, monkeypatch):
    conn = _db(tmp_path / "s.sqlite")
    errors = [
        "Connection to Apple Card (Updated Monthly) may need attention: Auth required",
        "Connection to Chase Bank failed: timeout",
    ]
    monkeypatch.setattr(sfin, "fetch_simplefin_accounts", lambda *a, **k: ([], errors))
    result = sync_simplefin(conn, access_url="https://u:p@host/simplefin")
    # The permanent Apple Card balance-only state is an expected note, not a warning.
    assert len(result["notes"]) == 1 and "Apple Card" in result["notes"][0]
    assert "expected: balance-only" in result["notes"][0]
    # The real feed problem is an actionable warning and lands on the sync run.
    assert result["warnings"] == ["Connection to Chase Bank failed: timeout"]
    assert result["error"] == "Connection to Chase Bank failed: timeout"
    assert conn.execute("SELECT error FROM sync_runs").fetchone()[0] == result["error"]


def test_sync_only_expected_notes_means_no_error(tmp_path, monkeypatch):
    conn = _db(tmp_path / "s.sqlite")
    errors = ["Connection to Apple Card (Updated Monthly) may need attention: Auth required"]
    monkeypatch.setattr(sfin, "fetch_simplefin_accounts", lambda *a, **k: ([], errors))
    result = sync_simplefin(conn, access_url="https://u:p@host/simplefin")
    assert result["warnings"] == [] and result["error"] is None
    assert len(result["notes"]) == 1


def test_config_loads_env_without_mutating_environ(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SIMPLEFIN_ACCESS_URL=https://u:p@host/simplefin\nTODOIST_API_TOKEN=tok123\n# comment\n")
    loaded = load_env_file(env)
    assert loaded["SIMPLEFIN_ACCESS_URL"].startswith("https://")
    cfg = get_finance_config(env_path=env, obligations_path=tmp_path / "missing.yaml")
    assert cfg["has_simplefin"] is True
