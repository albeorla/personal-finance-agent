"""Stale working cash cannot produce a verified cash-floor verdict."""

import sqlite3
from datetime import UTC, datetime

import pytest

from financial_agent import server
from financial_agent.config import ensure_source_tables
from financial_agent.digest import build_daily_digest, render_digest_markdown
from financial_agent.guardrails import (
    CASH_FLOOR,
    evaluate_guardrails,
    list_guardrail_findings,
)
from financial_agent.schema import ensure_app_schema
from financial_agent.status import get_finance_status
from financial_agent.surface_queue import get_surface_queue


AS_OF = "2026-07-11"
NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
STALE_BALANCE_DATE = "2026-07-08"


def _finance_db(path, *, available, balance_date=STALE_BALANCE_DATE):
    conn = sqlite3.connect(path)
    ensure_source_tables(conn)
    ensure_app_schema(conn)
    conn.execute(
        "INSERT INTO accounts "
        "(id, name, org, kind, currency, first_seen_at, last_seen_at) "
        "VALUES ('chk', 'Checking 4321', 'Chase', 'checking', 'USD', ?, ?)",
        (f"{AS_OF}T00:00:00+00:00", f"{AS_OF}T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO balance_snapshots "
        "(account_id, balance, available, recorded_at, source, balance_date) "
        "VALUES ('chk', ?, ?, ?, 'simplefin', ?)",
        (available, available, f"{AS_OF}T10:00:00+00:00", balance_date),
    )
    conn.execute(
        "INSERT INTO sync_runs "
        "(started_at, finished_at, mode, accounts_seen, transactions_inserted, "
        "transactions_updated, error) VALUES (?, ?, 'incremental', 1, 0, 0, NULL)",
        (f"{AS_OF}T09:58:00+00:00", f"{AS_OF}T10:00:00+00:00"),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def _cash_floor_findings(result):
    return [f for f in result["findings"] if f["rule_type"] == "cash_floor"]


@pytest.mark.parametrize(
    ("available", "would_be_breach_windows"),
    [(9000.0, []), (1000.0, [7, 14, 30])],
)
def test_stale_working_balance_makes_cash_floor_unverified(
    tmp_path, available, would_be_breach_windows
):
    conn = _finance_db(tmp_path / "finance.sqlite", available=available)

    findings = _cash_floor_findings(
        evaluate_guardrails(
            conn,
            as_of_date=AS_OF,
            drift_findings=[],
            now=NOW,
        )
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding["id"] == "guardrail:cash_floor:unverified"
    assert finding["severity"] == "medium"
    assert finding["evidence"]["verdict"] == "unverified"
    assert finding["evidence"]["balance_date"] == STALE_BALANCE_DATE
    assert finding["evidence"]["balance_age_days"] == 3
    assert finding["evidence"]["would_be_breach_windows"] == would_be_breach_windows
    assert "current portal balance" in finding["message"].lower()
    assert "fresh export" in finding["message"].lower()
    assert not any(
        f["id"] in {
            "guardrail:cash_floor:7d",
            "guardrail:cash_floor:14d",
            "guardrail:cash_floor:30d",
        }
        for f in findings
    )


def test_persisted_stale_verdict_is_not_recorded_as_cash_floor_pass(tmp_path):
    conn = _finance_db(tmp_path / "finance.sqlite", available=9000.0)

    evaluate_guardrails(
        conn,
        as_of_date=AS_OF,
        drift_findings=[],
        now=NOW,
        persist=True,
    )
    rows = list_guardrail_findings(
        conn, evaluation_date=AS_OF, rule_type="cash_floor"
    )

    assert len(rows) == 1
    assert rows[0]["passed"] is False
    assert rows[0]["finding"]["evidence"]["verdict"] == "unverified"


def test_finance_status_includes_stale_floor_warning(tmp_path):
    conn = _finance_db(tmp_path / "finance.sqlite", available=9000.0)
    conn.close()

    status = get_finance_status(
        db_path=tmp_path / "finance.sqlite",
        start_date=AS_OF,
        now=NOW,
    )
    floor = [f for f in status["guardrail_findings"] if f["rule_type"] == "cash_floor"]

    assert len(floor) == 1
    assert floor[0]["evidence"]["verdict"] == "unverified"
    assert floor[0]["message"] in status["warnings"]


def test_daily_digest_keeps_stale_floor_unverified_and_yellow(tmp_path):
    conn = _finance_db(tmp_path / "finance.sqlite", available=9000.0)
    conn.close()

    digest = build_daily_digest(
        str(tmp_path / "finance.sqlite"),
        as_of_date=AS_OF,
        now=NOW,
    )
    floor = [f for f in digest["guardrails"] if f["rule_type"] == "cash_floor"]

    assert digest["status_color"] == "YELLOW"
    assert len(floor) == 1
    assert floor[0]["evidence"]["verdict"] == "unverified"
    assert "all guardrails pass" not in render_digest_markdown(digest, verbose=True).lower()


def test_surface_queue_derives_stale_floor_verdict_without_injected_metadata(tmp_path):
    conn = _finance_db(tmp_path / "finance.sqlite", available=1000.0)

    queue = get_surface_queue(conn, as_of_date=AS_OF)
    floor_items = [
        item
        for item in queue["items"]
        if item["type"] == "guardrail_warning"
        and item["evidence"].get("rule_type") == "cash_floor"
    ]

    assert len(floor_items) == 1
    assert floor_items[0]["evidence"]["detail"]["verdict"] == "unverified"
    assert not any(
        related_id in {
            "guardrail:cash_floor:7d",
            "guardrail:cash_floor:14d",
            "guardrail:cash_floor:30d",
        }
        for item in floor_items
        for related_id in item["related_ids"]
    )


def test_surface_queue_suppresses_stale_trough_breach_risk(tmp_path):
    conn = _finance_db(tmp_path / "finance.sqlite", available=1000.0)

    queue = get_surface_queue(
        conn,
        as_of_date=AS_OF,
        working_account_balance_stale={
            "stale": True,
            "account_name": "Checking 4321",
            "balance_date": STALE_BALANCE_DATE,
            "balance_age_days": 3,
        },
        trough_sensitivity={
            "low_estimate": CASH_FLOOR - 1,
            "breach_risk": True,
        },
    )
    item_types = {item["type"] for item in queue["items"]}

    assert "confirm_live_balance" in item_types
    assert "trough_breach_risk" not in item_types


def test_server_surface_queue_suppresses_stale_cash_floor_warning(tmp_path):
    db_path = tmp_path / "finance.sqlite"
    conn = _finance_db(db_path, available=1000.0)
    conn.close()

    queue = server.get_surface_queue(as_of_date=AS_OF, db_path=str(db_path))
    item_types = {item["type"] for item in queue["items"]}
    floor_warnings = [
        item
        for item in queue["items"]
        if item["type"] == "guardrail_warning"
        and item["evidence"].get("rule_type") == "cash_floor"
    ]

    assert "confirm_live_balance" in item_types
    assert floor_warnings == []


@pytest.mark.parametrize(
    ("available", "expected_breach_windows"),
    [(9000.0, []), (1000.0, [7, 14, 30])],
)
def test_same_day_working_balance_keeps_verified_floor_behavior(
    tmp_path, available, expected_breach_windows
):
    conn = _finance_db(
        tmp_path / "finance.sqlite",
        available=available,
        balance_date=AS_OF,
    )

    findings = _cash_floor_findings(
        evaluate_guardrails(
            conn,
            as_of_date=AS_OF,
            drift_findings=[],
            now=NOW,
        )
    )

    assert not any(f["evidence"].get("verdict") == "unverified" for f in findings)
    assert [f["evidence"]["window_days"] for f in findings] == expected_breach_windows


@pytest.mark.parametrize(
    "balance_date",
    ["2026-07-10", None, "not-a-date", "2026-07-12"],
    ids=["yesterday", "missing", "malformed", "future"],
)
def test_cash_floor_requires_same_day_source_backed_balance(tmp_path, balance_date):
    conn = _finance_db(
        tmp_path / "finance.sqlite",
        available=9000.0,
        balance_date=balance_date,
    )

    findings = _cash_floor_findings(
        evaluate_guardrails(
            conn,
            as_of_date=AS_OF,
            drift_findings=[],
            now=NOW,
        )
    )

    assert len(findings) == 1
    evidence = findings[0]["evidence"]
    assert evidence["verdict"] == "unverified"
    assert evidence["balance_source"] == "simplefin"
    assert evidence["balance_recorded_at"] == f"{AS_OF}T10:00:00+00:00"
