"""Tests for operating guardrails (slice I)."""

import sqlite3
from datetime import UTC, datetime

from financial_agent.guardrails import (
    apply_guardrail_rules,
    evaluate_guardrails,
    list_guardrail_findings,
)
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


_CHK = [{"account_id": "chk", "account_name": "Checking 4321", "kind": "checking",
         "available": 0.0, "recorded_at": "2026-06-21T00:00:00+00:00",
         "balance_date": "2026-06-21", "balance_age_days": 0,
         "source": "simplefin"}]


def _accounts(available):
    return [{**_CHK[0], "available": available}]


def test_cash_floor_breach_flags_by_window(tmp_path):
    conn = _db(tmp_path / "g.sqlite")
    res = evaluate_guardrails(conn, as_of_date="2026-06-21", accounts=_accounts(1000.0), drift_findings=[])
    cash = [f for f in res["findings"] if f["rule_type"] == "cash_floor"]
    assert cash  # below the $2500 floor
    assert any(f["id"] == "guardrail:cash_floor:7d" and f["severity"] == "high" for f in cash)


def test_cash_floor_pass_when_above(tmp_path):
    conn = _db(tmp_path / "g.sqlite")
    res = evaluate_guardrails(conn, as_of_date="2026-06-21", accounts=_accounts(9000.0), drift_findings=[])
    assert [f for f in res["findings"] if f["rule_type"] == "cash_floor"] == []


def test_manual_balance_wins_over_newer_feed_in_guardrails(tmp_path):
    # Sticky manual: a manual correction must win over a LATER feed snapshot
    # everywhere a balance is read, not just in status.py. Here the same-day
    # manual $9,000 (above the floor) was recorded BEFORE a $100 feed sync. The cash-floor check
    # reads accounts from balance_snapshots and must see $9,000, so no breach.
    from financial_agent.config import SOURCE_SCHEMA

    conn = _db(tmp_path / "g.sqlite")
    conn.executescript(SOURCE_SCHEMA)
    conn.execute(
        "INSERT INTO accounts (id, name, kind, first_seen_at, last_seen_at) "
        "VALUES ('chk','Checking 4321','checking','2026-06-01','2026-06-01')"
    )
    conn.execute(
        "INSERT INTO balance_snapshots (account_id, balance, available, recorded_at, source, balance_date) "
        "VALUES ('chk', 9000, 9000, '2026-06-21T07:00:00+00:00', 'manual', '2026-06-21')"
    )
    conn.execute(
        "INSERT INTO balance_snapshots (account_id, balance, available, recorded_at, source, balance_date) "
        "VALUES ('chk', 100, 100, '2026-06-21T08:00:00+00:00', 'simplefin', '2026-06-21')"
    )
    conn.commit()

    res = evaluate_guardrails(conn, as_of_date="2026-06-21", drift_findings=[])
    assert [f for f in res["findings"] if f["rule_type"] == "cash_floor"] == []


def test_stale_balance_keeps_future_roll_forward_verdict_unverified(tmp_path):
    # Snapshot is 2026-06-20 with $1,000 (below the $2,500 floor). A $5,000
    # paycheck lands 2026-07-01. Evaluating the floor as-of a FUTURE date must
    # roll that paycheck into the starting balance, but the stale snapshot must
    # still prevent a verified clean floor verdict.
    conn = _db(tmp_path / "g.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "paycheck", "name": "Paycheck", "kind": "income",
                    "cadence": "monthly", "status": "active", "source": "seed"},
        instances=[{"id": "paycheck:2026-07-01", "due_date": "2026-07-01",
                    "amount": 5000.0, "direction": "inflow", "status": "expected",
                    "source": "seed"}],
    )
    accounts = [{**_accounts(1000.0)[0], "balance_age_days": 24}]
    res = evaluate_guardrails(conn, as_of_date="2026-07-15", accounts=accounts, drift_findings=[])
    cash = [f for f in res["findings"] if f["rule_type"] == "cash_floor"]
    assert len(cash) == 1
    assert cash[0]["evidence"]["verdict"] == "unverified"
    assert cash[0]["evidence"]["would_be_breach_windows"] == []


def test_stale_balance_suppresses_future_breach_verdict(tmp_path):
    # Same future as-of, but no income before it. The projection can retain the
    # would-be breach evidence, but stale cash prevents a verified breach.
    conn = _db(tmp_path / "g.sqlite")
    accounts = [{**_accounts(1000.0)[0], "balance_age_days": 24}]
    res = evaluate_guardrails(conn, as_of_date="2026-07-15", accounts=accounts, drift_findings=[])
    cash = [f for f in res["findings"] if f["rule_type"] == "cash_floor"]
    assert len(cash) == 1
    assert cash[0]["evidence"]["verdict"] == "unverified"
    assert cash[0]["evidence"]["would_be_breach_windows"] == [7, 14, 30]


def test_drift_threshold_exceeded(tmp_path):
    conn = _db(tmp_path / "g.sqlite")
    drift = [{"finding_type": "missing_expected", "id": "d1", "cash_flow_impact": -3000.0}]
    res = evaluate_guardrails(conn, as_of_date="2026-06-21", accounts=_accounts(9000.0), drift_findings=drift)
    fired = [f for f in res["findings"] if f["rule_type"] == "drift_threshold"]
    assert fired and fired[0]["evidence"]["total_drift_impact"] == 3000.0


def test_drift_threshold_pass_under_200(tmp_path):
    conn = _db(tmp_path / "g.sqlite")
    drift = [{"finding_type": "amount_changed", "id": "d1", "cash_flow_impact": -50.0}]
    res = evaluate_guardrails(conn, as_of_date="2026-06-21", accounts=_accounts(9000.0), drift_findings=drift)
    assert [f for f in res["findings"] if f["rule_type"] == "drift_threshold"] == []


def test_window_age_stale(tmp_path):
    conn = _db(tmp_path / "g.sqlite")
    conn.execute("CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, finished_at TEXT, error TEXT)")
    conn.execute("INSERT INTO sync_runs (finished_at) VALUES ('2026-06-19T00:00:00+00:00')")  # >24h before
    res = evaluate_guardrails(conn, as_of_date="2026-06-21", accounts=_accounts(9000.0), drift_findings=[],
                              now=datetime(2026, 6, 21, 12, 0, tzinfo=UTC))
    stale = [f for f in res["findings"] if f["rule_type"] == "window_age"]
    assert stale and stale[0]["id"] == "guardrail:window_age_stale"


def test_debt_avalanche_advisory_when_debt_present(tmp_path):
    conn = _db(tmp_path / "g.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "amex_loan", "name": "Amex Personal Loan", "kind": "loan",
                    "cadence": "monthly", "status": "active", "source": "seed"},
        instances=[{"id": "amex_loan:2026-07-27", "due_date": "2026-07-27", "amount": -500.84, "source": "seed"}],
    )
    res = evaluate_guardrails(conn, as_of_date="2026-06-21", accounts=_accounts(9000.0), drift_findings=[])
    av = [f for f in res["findings"] if f["rule_type"] == "debt_avalanche"]
    assert av and av[0]["advisory"] is True
    assert av[0]["evidence"]["apr_order"][0]["key"] == "amex_platinum"


def test_no_debt_no_avalanche(tmp_path):
    conn = _db(tmp_path / "g.sqlite")
    res = evaluate_guardrails(conn, as_of_date="2026-06-21", accounts=_accounts(9000.0), drift_findings=[])
    assert [f for f in res["findings"] if f["rule_type"] == "debt_avalanche"] == []


def test_seed_rules_idempotent(tmp_path):
    conn = _db(tmp_path / "g.sqlite")
    apply_guardrail_rules(conn)
    apply_guardrail_rules(conn)
    assert conn.execute("SELECT COUNT(*) FROM guardrail_rules").fetchone()[0] == 4


def test_persist_records_evaluations(tmp_path):
    conn = _db(tmp_path / "g.sqlite")
    evaluate_guardrails(conn, as_of_date="2026-06-21", accounts=_accounts(1000.0), drift_findings=[], persist=True)
    rows = list_guardrail_findings(conn, evaluation_date="2026-06-21")
    assert any(r["rule_type"] == "cash_floor" and r["passed"] is False for r in rows)
    assert any(r["passed"] is True for r in rows)  # rules that did not fire
