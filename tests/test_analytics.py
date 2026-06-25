"""Tests for the spending analytics lane (slice W). Read-only; no network."""

import sqlite3

import pytest

from financial_agent.analytics import (
    categorize,
    normalize_merchant,
    render_spending_markdown,
    summarize_spending,
)


def _db(path, txns):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT, "
        "amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT)"
    )
    for i, (posted, amount, payee) in enumerate(txns):
        conn.execute(
            "INSERT INTO transactions (id, account_id, posted, amount, payee, source) VALUES (?,?,?,?,?, 'simplefin')",
            (f"t{i}", "chk", posted, amount, payee),
        )
    conn.commit()
    return conn


def test_categorize_rules():
    assert categorize("Eversource Energy") == "Utilities"
    assert categorize("Spotify USA") == "Subscriptions"
    assert categorize("WHOLE FOODS MARKET #123") == "Groceries"
    assert categorize("ONLINE PAYMENT THANK YOU") == "Transfers"
    assert categorize("IntelliBridge Payroll") == "Income"
    assert categorize("Joe's Random LLC") == "Other"


def test_categorize_new_categories():
    assert categorize("State of Ct Bus Dirpay") == "Taxes"
    assert categorize("Interest Charge") == "Fees"
    assert categorize("Safeco Corporation") == "Insurance"
    assert categorize("OpenAI") == "Software"
    assert categorize("VCA Animal Hospitals") == "Pets"
    assert categorize("Lottermanpsychologicgreenwich") == "Health"


def test_categorize_excludes_checks_and_debt_payments():
    # Paper checks, card payments, and auto-loan payments are transfers/debt, not spend.
    assert categorize("Check #1228") == "Transfers"
    assert categorize("Apple Credit Card", "APPLECARD GSBANK PAYMENT") == "Transfers"
    assert categorize("Volvo Car Fin Auto Finan Web") == "Transfers"
    assert categorize("Optimum") == "Utilities"


def test_normalize_merchant_strips_store_numbers():
    assert normalize_merchant("STARBUCKS #1234") == "Starbucks"
    assert normalize_merchant("SHELL OIL 57542136") == "Shell Oil"
    assert normalize_merchant(None) == "Unknown"


def test_summarize_by_category_excludes_transfers_and_income(tmp_path):
    conn = _db(tmp_path / "a.sqlite", [
        ("2026-06-03T00:00:00", -100.00, "Eversource Energy"),
        ("2026-06-05T00:00:00", -10.00, "Spotify"),
        ("2026-06-10T00:00:00", -50.00, "Whole Foods Market"),
        ("2026-06-15T00:00:00", -500.00, "ONLINE PAYMENT THANK YOU"),  # transfer -> excluded
        ("2026-06-25T00:00:00", 3000.00, "IntelliBridge Payroll"),     # inflow -> ignored (amount>0)
    ])
    rep = summarize_spending(conn, start_date="2026-06-01", end_date="2026-06-30", group_by="category")
    assert rep["total_spending"] == 160.00
    assert rep["excluded_counts"]["Transfers"] == 1
    keys = {b["key"]: b["total"] for b in rep["buckets"]}
    assert keys == {"Utilities": 100.0, "Subscriptions": 10.0, "Groceries": 50.0}
    util = next(b for b in rep["buckets"] if b["key"] == "Utilities")
    assert util["txn_ids"] == ["t0"]  # provenance


def test_summarize_can_include_transfers(tmp_path):
    conn = _db(tmp_path / "a.sqlite", [
        ("2026-06-03T00:00:00", -100.00, "Eversource Energy"),
        ("2026-06-15T00:00:00", -500.00, "ONLINE PAYMENT THANK YOU"),
    ])
    rep = summarize_spending(conn, start_date="2026-06-01", end_date="2026-06-30", group_by="category", exclude_transfers=False)
    assert rep["total_spending"] == 600.00


def test_summarize_by_month_trend(tmp_path):
    conn = _db(tmp_path / "a.sqlite", [
        ("2026-05-10T00:00:00", -100.00, "Whole Foods Market"),
        ("2026-06-10T00:00:00", -160.00, "Whole Foods Market"),
    ])
    rep = summarize_spending(conn, start_date="2026-05-01", end_date="2026-06-30", group_by="month")
    assert [t["month"] for t in rep["by_month"]] == ["2026-05", "2026-06"]
    assert rep["month_over_month_change"] == 60.00


def test_invalid_group_by_raises(tmp_path):
    conn = _db(tmp_path / "a.sqlite", [])
    with pytest.raises(ValueError, match="group_by"):
        summarize_spending(conn, start_date="2026-06-01", end_date="2026-06-30", group_by="bogus")


def test_render_spending_markdown(tmp_path):
    conn = _db(tmp_path / "a.sqlite", [
        ("2026-06-03T00:00:00", -100.00, "Eversource Energy"),
        ("2026-06-10T00:00:00", -50.00, "Whole Foods Market"),
    ])
    md = render_spending_markdown(summarize_spending(conn, start_date="2026-06-01", end_date="2026-06-30", group_by="category"))
    assert "# Spending 2026-06-01 -> 2026-06-30" in md
    assert "Utilities: $100.00" in md
    assert "Total spending: $150.00" in md
