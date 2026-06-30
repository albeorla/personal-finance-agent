"""Tests for the spending analytics lane (slice W). Read-only; no network."""

import sqlite3

import pytest

from financial_agent.analytics import (
    categorize,
    list_transactions,
    normalize_merchant,
    render_spending_markdown,
    summarize_spending,
)


def _db_with_accounts(path, txns):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO accounts (id, name) VALUES ('chk', 'Checking 4321')")
    conn.execute(
        "CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT, "
        "amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT)"
    )
    for i, (posted, amount, payee, desc) in enumerate(txns):
        conn.execute(
            "INSERT INTO transactions (id, account_id, posted, amount, payee, description, pending, source) "
            "VALUES (?,?,?,?,?,?,0,'simplefin')",
            (f"t{i}", "chk", posted, amount, payee, desc),
        )
    conn.commit()
    return conn


def test_list_transactions_filters_and_orders(tmp_path):
    conn = _db_with_accounts(
        tmp_path / "f.db",
        [
            ("2026-06-01", -12.50, "Starbucks", "coffee"),
            ("2026-06-15", -250.00, "DMV", "Auto payment"),
            ("2026-06-20", 5000.00, "Payroll", "direct deposit"),
        ],
    )
    # newest first, with the account name joined in
    res = list_transactions(conn)
    assert res["count"] == 3
    assert res["transactions"][0]["posted"] == "2026-06-20"
    assert res["transactions"][0]["account_name"] == "Checking 4321"

    # text search spans payee + description
    hit = list_transactions(conn, query="auto")
    assert hit["count"] == 1 and hit["transactions"][0]["amount"] == -250.00

    # absolute-amount floor
    big = list_transactions(conn, min_amount=100)
    assert {t["payee"] for t in big["transactions"]} == {"DMV", "Payroll"}

    # inclusive date range
    rng = list_transactions(conn, start_date="2026-06-10", end_date="2026-06-18")
    assert rng["count"] == 1 and rng["transactions"][0]["payee"] == "DMV"

    # limit + truncated flag
    lim = list_transactions(conn, limit=1)
    assert lim["count"] == 1 and lim["truncated"] is True


def test_list_transactions_missing_table_is_empty(tmp_path):
    conn = sqlite3.connect(tmp_path / "empty.db")
    conn.row_factory = sqlite3.Row
    assert list_transactions(conn) == {
        "count": 0,
        "truncated": False,
        "limit": 50,
        "transactions": [],
    }


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
