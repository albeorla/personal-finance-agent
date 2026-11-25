"""The cash floor is a household setting: CASH_FLOOR in the finances .env."""

import sqlite3

import pytest

from financial_agent import guardrails
from financial_agent.cashflow import DEFAULT_CASH_FLOOR, resolve_cash_floor
from financial_agent.config import get_finance_config
from financial_agent.schema import ensure_app_schema


def test_unset_floor_is_the_default(monkeypatch):
    monkeypatch.delenv("CASH_FLOOR", raising=False)
    assert get_finance_config()["cash_floor"] is None
    assert resolve_cash_floor() == DEFAULT_CASH_FLOOR
    assert resolve_cash_floor("   ") == DEFAULT_CASH_FLOOR


def test_floor_is_read_from_the_env_file(tmp_path):
    env = tmp_path / "finances.env"
    env.write_text("CASH_FLOOR=3000\n")
    raw = get_finance_config(env_path=env)["cash_floor"]
    assert resolve_cash_floor(raw) == 3000.0


@pytest.mark.parametrize("text, expected", [("3000", 3000.0), ("$3,000", 3000.0), ("4250.50", 4250.5)])
def test_floor_accepts_dollar_text(text, expected):
    assert resolve_cash_floor(text) == expected


@pytest.mark.parametrize("text", ["abc", "0", "-100", "nan", "inf"])
def test_bad_floor_is_an_error_not_a_silent_default(text):
    with pytest.raises(ValueError, match="CASH_FLOOR"):
        resolve_cash_floor(text)


def test_stored_rule_threshold_follows_a_changed_floor(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "g.sqlite")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)

    def stored():
        row = conn.execute("SELECT threshold_value FROM guardrail_rules WHERE rule_type = 'cash_floor'").fetchone()
        return row["threshold_value"]

    assert guardrails.apply_guardrail_rules(conn)["seeded"] == 4
    assert stored() == guardrails.CASH_FLOOR

    monkeypatch.setattr(guardrails, "CASH_FLOOR", 3000.0)
    assert guardrails.apply_guardrail_rules(conn)["seeded"] == 0
    assert stored() == 3000.0


def test_guardrail_fires_against_the_floor_in_force(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "g.sqlite")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    accounts = [{"account_id": "chk", "account_name": "Checking 4321", "kind": "checking",
                 "available": 2800.0, "recorded_at": "2026-06-21T00:00:00+00:00",
                 "balance_date": "2026-06-21", "balance_age_days": 0, "source": "simplefin"}]

    def cash_findings():
        res = guardrails.evaluate_guardrails(conn, as_of_date="2026-06-21", accounts=accounts, drift_findings=[])
        return [f for f in res["findings"] if f["rule_type"] == "cash_floor"]

    assert cash_findings() == []  # 2,800 clears the default 2,500 floor

    monkeypatch.setattr(guardrails, "CASH_FLOOR", 3000.0)
    fired = cash_findings()
    assert fired and all(f["evidence"]["floor"] == 3000.0 for f in fired)
