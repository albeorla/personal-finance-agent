"""Tests for the grounding/verification harness (slice V). Read-only; no network."""

import sqlite3

from financial_agent.digest import build_daily_digest, summarize_daily_digest
from financial_agent.grounding import verify_grounding
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent.status import get_finance_status


def _status_db(path, *, available=9000.0, obligations=()):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL, recorded_at TEXT, source TEXT, balance_date TEXT);
        CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, accounts_seen INT, transactions_inserted INT, transactions_updated INT, error TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT, amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);
        """
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('chk','PREMIER PLUS CKG (4321)','Chase','checking','USD')")
    conn.execute("INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source,balance_date) VALUES ('chk',?,?,'2026-06-20T00:00:00+00:00','simplefin','2026-06-20')", (available, available))
    conn.execute("INSERT INTO sync_runs (started_at,finished_at,mode,accounts_seen,transactions_inserted,transactions_updated,error) VALUES ('2026-06-20T09:58:00+00:00','2026-06-20T10:00:00+00:00','i',1,0,0,NULL)")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    for oid, name, kind, instances in obligations:
        apply_obligation_instances(conn, obligation={"id": oid, "name": name, "kind": kind, "status": "active", "source": "seed"}, instances=instances)
    conn.commit()
    conn.close()
    return str(path)


_OBS = [
    ("rent", "Rent check", "housing", [{"id": "rent:2026-07-03", "due_date": "2026-07-03", "amount": -3000.0, "source": "seed"}]),
    ("nyt", "New York Times", "subscription", [{"id": "nyt:2026-07-23", "due_date": "2026-07-23", "amount": -30.30, "source": "seed"}]),
]


def test_grounding_passes_on_consistent_digest(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=_OBS)
    digest = build_daily_digest(db, as_of_date="2026-06-20")
    report = verify_grounding(digest, db, as_of_date="2026-06-20")
    assert report["grounded"] is True
    assert report["payload_kind"] == "digest"
    assert report["ungrounded"] == []
    # working cash + both obligations + 4 window endings all traced
    assert report["checks_total"] >= 1 + 2 + 4


def test_grounding_flags_tampered_working_cash(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=_OBS)
    digest = build_daily_digest(db, as_of_date="2026-06-20")
    digest["balances"]["working_cash"] = 12345.0  # lie
    report = verify_grounding(digest, db, as_of_date="2026-06-20")
    assert report["grounded"] is False
    wc = next(c for c in report["checks"] if c["claim"] == "working_cash")
    assert wc["grounded"] is False and wc["source_value"] == 9000.0


def test_grounding_flags_tampered_obligation_amount(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=_OBS)
    digest = build_daily_digest(db, as_of_date="2026-06-20")
    for o in digest["upcoming_obligations"]:
        if o["obligation_name"] == "Rent check":
            o["amount"] = 99.0  # lie
    report = verify_grounding(digest, db, as_of_date="2026-06-20")
    assert report["grounded"] is False
    assert any("Rent check" in c["claim"] and not c["grounded"] for c in report["checks"])


def test_grounding_checks_net_across_accounts_and_flags_a_wrong_one(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=_OBS)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('card','Amex','Amex','credit card','USD')")
    conn.execute("INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) VALUES ('card',-4000,0,'2026-06-20T00:00:00+00:00','simplefin')")
    conn.commit()
    conn.close()

    digest = build_daily_digest(db, as_of_date="2026-06-20")
    rep = verify_grounding(digest, db, as_of_date="2026-06-20")
    net = next(c for c in rep["checks"] if c["claim"] == "net_across_accounts")
    assert net["grounded"] is True and net["source_value"] == 5000.0  # 9000 cash - 4000 card

    digest["balances"]["net_across_accounts"] = 9999.0  # lie (the old bug)
    assert verify_grounding(digest, db, as_of_date="2026-06-20")["grounded"] is False


def test_grounding_window_boundary_is_exclusive(tmp_path):
    # An obligation due exactly on the 7-day boundary (2026-06-20 + 7 = 2026-06-27)
    # is EXCLUDED from the 7d projection (end is exclusive); grounding must match,
    # so the digest stays grounded rather than falsely flagged.
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=[
        ("boundary", "Boundary bill", "utility", [{"id": "b:2026-06-27", "due_date": "2026-06-27", "amount": -200.0, "source": "seed"}]),
    ])
    digest = build_daily_digest(db, as_of_date="2026-06-20")
    report = verify_grounding(digest, db, as_of_date="2026-06-20")
    assert report["grounded"] is True
    e7 = next(c for c in report["checks"] if c["claim"] == "ending_balance_7d")
    assert e7["grounded"] is True  # 7d ending excludes the boundary-date event


def test_grounding_on_status_payload(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=_OBS)
    status = get_finance_status(db_path=db, windows=[7, 30, 60], start_date="2026-06-20")
    report = verify_grounding(status, db, as_of_date="2026-06-20")
    assert report["payload_kind"] == "status"
    assert report["grounded"] is True


def test_grounding_rejects_an_omitted_canonical_obligation_even_when_endpoints_were_adjusted_self_consistently(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=_OBS)
    digest = build_daily_digest(db, as_of_date="2026-06-20")
    omitted = next(
        obligation
        for obligation in digest["upcoming_obligations"]
        if obligation["obligation_name"] == "Rent check"
    )
    digest["upcoming_obligations"].remove(omitted)
    for window in digest["cash_flow"]:
        if window["window_days"] >= 14:
            window["ending_balance"] -= omitted["signed_amount"]

    report = verify_grounding(digest, db, as_of_date="2026-06-20")

    assert report["grounded"] is False


def test_grounding_rejects_a_headline_contradicting_structured_status(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0)
    summary = summarize_daily_digest(build_daily_digest(db, as_of_date="2026-06-20"))
    assert summary["status_color"] == "GREEN"
    summary["headline"] = "RED: modeled bills push the balance below zero"

    report = verify_grounding(summary, db, as_of_date="2026-06-20")

    assert report["grounded"] is False


def test_grounding_flags_tampered_status_liquid_available(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=_OBS)
    status = get_finance_status(db_path=db, windows=[7, 30, 60], start_date="2026-06-20")
    status["balances"]["liquid_available"] = 12345.0

    report = verify_grounding(status, db, as_of_date="2026-06-20")

    assert report["grounded"] is False


def test_grounding_accepts_compact_digest_obligations_through_inclusive_14d_cutoff(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", obligations=[
        ("day13", "Day 13 bill", "utility", [{"id": "day13:2026-07-03", "due_date": "2026-07-03", "amount": -130.0, "source": "seed"}]),
        ("day14", "Day 14 bill", "utility", [{"id": "day14:2026-07-04", "due_date": "2026-07-04", "amount": -140.0, "source": "seed"}]),
    ])
    summary = summarize_daily_digest(build_daily_digest(db, as_of_date="2026-06-20"))

    assert [o["obligation_name"] for o in summary["upcoming_14d"]] == [
        "Day 13 bill",
        "Day 14 bill",
    ]
    assert verify_grounding(summary, db, as_of_date="2026-06-20")["grounded"] is True


def test_grounding_accepts_unsorted_status_windows_and_traces_longest_window_obligations(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", obligations=[
        ("day20", "Day 20 bill", "utility", [{"id": "day20:2026-07-10", "due_date": "2026-07-10", "amount": -200.0, "source": "seed"}]),
    ])
    status = get_finance_status(db_path=db, windows=[30, 7], start_date="2026-06-20")

    report = verify_grounding(status, db, as_of_date="2026-06-20")

    assert report["grounded"] is True
    assert any(c["claim"] == "obligation:Day 20 bill@2026-07-10" for c in report["checks"])
