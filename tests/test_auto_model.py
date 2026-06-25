"""Tests for G2: auto-model high-confidence direct-checking recurring. No network."""

import sqlite3

from financial_agent.onboarding import auto_model_high_confidence_recurring, scan_charge_onboarding_candidates
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


def _monthly(payee, amount, months, day=8):
    # 7 consistent monthly charges Dec..Jun -> a high-confidence recurring pattern
    return [(f"{payee[:4]}{m}", "ACT-chk", f"2026-{m:02d}-{day:02d}T08:00:00", amount, payee, "") for m in months]


def test_auto_model_applies_high_confidence_checking_bill_into_projection(tmp_path):
    conn = _db(tmp_path / "m.sqlite", transactions=_monthly("Volvo Car Fin Auto Finan Web", -580.84, [1, 2, 3, 4, 5, 6]))
    scan_charge_onboarding_candidates(conn)
    res = auto_model_high_confidence_recurring(conn, as_of_date="2026-06-21")
    assert res["applied_count"] >= 1
    assert any("Volvo" in a["merchant"] for a in res["applied"])
    # forward instances now exist (these are what enter the projection)
    future = conn.execute(
        "SELECT COUNT(*) FROM obligation_instances oi JOIN obligations o ON o.id=oi.obligation_id "
        "WHERE o.name LIKE '%Volvo%' AND oi.due_date >= '2026-06-21' AND oi.status='expected'"
    ).fetchone()[0]
    assert future >= 1


def test_auto_model_excludes_internal_transfers(tmp_path):
    conn = _db(tmp_path / "m.sqlite", transactions=_monthly("Online Transfer to Checking 4321", -2000.0, [1, 2, 3, 4, 5, 6]))
    scan_charge_onboarding_candidates(conn)
    res = auto_model_high_confidence_recurring(conn, as_of_date="2026-06-21")
    # a transfer is never auto-modeled as a bill
    assert not any("transfer" in a["merchant"].lower() for a in res["applied"])
    assert any("transfer" in s["reason"].lower() for s in res["skipped"]) or res["applied_count"] == 0
