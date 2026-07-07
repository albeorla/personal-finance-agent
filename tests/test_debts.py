"""Tests for the structured debts layer (table, set/list, interest, avalanche)."""

import sqlite3

from financial_agent.config import SOURCE_SCHEMA
from financial_agent.debts import list_debts, set_debt_terms
from financial_agent.guardrails import evaluate_guardrails
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent.status import get_finance_status


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SOURCE_SCHEMA)
    ensure_app_schema(conn)
    return conn


def _seed_account(conn, *, account_id, name, org="Test Bank"):
    conn.execute(
        """
        INSERT INTO accounts (id, name, org, kind, currency, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, 'credit', 'USD', '2026-01-01', '2026-06-20')
        ON CONFLICT(id) DO NOTHING
        """,
        (account_id, name, org),
    )


def _seed_balance(conn, *, account_id, balance, recorded_at="2026-06-20T00:00:00+00:00"):
    conn.execute(
        """
        INSERT INTO balance_snapshots (account_id, balance, available, recorded_at, source)
        VALUES (?, ?, ?, ?, 'test')
        """,
        (account_id, balance, balance, recorded_at),
    )


_CHK = [{"account_id": "chk", "account_name": "Checking 4321", "kind": "checking",
         "available": 9000.0, "recorded_at": "2026-06-20T00:00:00+00:00"}]


# --- schema + set/list round-trip ------------------------------------------


def test_debts_table_exists(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='debts'"
    ).fetchone()
    assert row is not None


def test_set_and_list_round_trip_with_account_balance(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    _seed_account(conn, account_id="acct_apple", name="Alex", org="Apple Card (Updated Monthly)")
    _seed_balance(conn, account_id="acct_apple", balance=-5949.32)

    result = set_debt_terms(
        conn,
        id="apple_card",
        name="Apple Card",
        apr=19.49,
        account_query="Apple Card",
        is_revolving=True,
        autopay=False,
        note="avalanche target #1",
    )
    assert result["created"] is True
    assert result["account_id"] == "acct_apple"

    listed = list_debts(conn, "2026-06-24")
    assert listed["count"] == 1
    debt = listed["debts"][0]
    assert debt["id"] == "apple_card"
    assert debt["account_id"] == "acct_apple"
    assert debt["current_balance"] == -5949.32
    assert debt["is_revolving"] is True
    assert debt["autopay"] is False
    assert debt["note"] == "avalanche target #1"


def test_set_debt_terms_idempotent_upsert(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    first = set_debt_terms(
        conn, id="stu_loan", name="Student Loans", apr=4.22,
        balance_source="manual", balance_override=-16375.79, is_revolving=True,
    )
    second = set_debt_terms(
        conn, id="stu_loan", name="Federal Student Loans", apr=4.22,
        balance_source="manual", balance_override=-16000.0, is_revolving=True,
    )
    assert first["created"] is True
    assert second["created"] is False
    assert second["updated"] is True

    rows = conn.execute("SELECT COUNT(*) AS n FROM debts").fetchone()
    assert rows["n"] == 1
    debt = list_debts(conn, "2026-06-24")["debts"][0]
    assert debt["name"] == "Federal Student Loans"
    assert debt["current_balance"] == -16000.0


def test_manual_balance_override_no_account(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    set_debt_terms(
        conn, id="stu_loan", name="Student Loans", apr=4.22,
        balance_source="manual", balance_override=-16375.79,
        min_payment=557.10, is_revolving=True,
    )
    debt = list_debts(conn, "2026-06-24")["debts"][0]
    assert debt["account_id"] is None
    assert debt["current_balance"] == -16375.79
    assert debt["min_payment"] == 557.10


def test_account_source_requires_resolvable_account(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    try:
        set_debt_terms(conn, id="x", name="X", apr=10.0, balance_source="account")
    except ValueError as exc:
        assert "balance_source='account'" in str(exc)
    else:  # pragma: no cover - guard
        raise AssertionError("expected ValueError for account source without account")


# --- interest math ----------------------------------------------------------


def test_monthly_interest_math(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    _seed_account(conn, account_id="acct_apple", name="Apple")
    _seed_balance(conn, account_id="acct_apple", balance=-5949.32)
    set_debt_terms(conn, id="apple_card", name="Apple Card", apr=19.49,
                   account_id="acct_apple", is_revolving=True)

    debt = list_debts(conn, "2026-06-24")["debts"][0]
    # abs(5949.32) * 19.49 / 100 / 12 = 96.63 (rounded to 2dp)
    expected = round(5949.32 * 19.49 / 100 / 12, 2)
    assert debt["monthly_interest"] == expected == 96.63


def test_total_monthly_interest_sums_revolving_only(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    _seed_account(conn, account_id="acct_apple", name="Apple")
    _seed_balance(conn, account_id="acct_apple", balance=-1200.0)
    _seed_account(conn, account_id="acct_amex", name="Amex Gold")
    _seed_balance(conn, account_id="acct_amex", balance=-5485.61)

    # Revolving Apple Card contributes; paid-in-full Amex (is_revolving False)
    # does not, even though it carries a balance and a high APR.
    set_debt_terms(conn, id="apple_card", name="Apple Card", apr=19.49,
                   account_id="acct_apple", is_revolving=True)
    set_debt_terms(conn, id="amex_gold", name="Amex Gold", apr=21.74,
                   account_id="acct_amex", is_revolving=False)

    result = list_debts(conn, "2026-06-24")
    apple_interest = round(1200.0 * 19.49 / 100 / 12, 2)
    assert result["total_monthly_interest"] == apple_interest


# --- balance precedence: list_debts agrees with get_finance_status ----------


def _seed_sync_run(conn, *, finished_at="2026-06-24T15:00:00+00:00"):
    conn.execute(
        """
        INSERT INTO sync_runs (
            started_at, finished_at, mode, accounts_seen,
            transactions_inserted, transactions_updated, error
        ) VALUES (?, ?, 'incremental', 1, 0, 0, NULL)
        """,
        (finished_at, finished_at),
    )


def test_list_debts_balance_matches_get_finance_status_apple_card(tmp_path):
    """An account-sourced debt resolves its balance through the SAME canonical
    helper as get_finance_status, so for a balance-only "Updated Monthly"
    account a same-day manual correction wins over a later same-day SimpleFIN
    snapshot -- and the two surfaces report the identical number.
    """

    db_path = tmp_path / "d.sqlite"
    conn = _db(db_path)
    acct = "ACT-apple"
    _seed_account(conn, account_id=acct, name="Alex", org="Apple Card (Updated Monthly)")
    # Same calendar day: a manual correction recorded EARLIER than a later
    # SimpleFIN sync. Recency alone would pick the stale -5949.32; the canonical
    # precedence must pick the manual -6122.03.
    _seed_balance(conn, account_id=acct, balance=-6122.03,
                  recorded_at="2026-06-24T14:56:47+00:00")
    conn.execute(
        "UPDATE balance_snapshots SET source='manual' WHERE account_id=? AND balance=-6122.03",
        (acct,),
    )
    _seed_balance(conn, account_id=acct, balance=-5949.32,
                  recorded_at="2026-06-24T20:39:06+00:00")
    conn.execute(
        "UPDATE balance_snapshots SET source='simplefin' WHERE account_id=? AND balance=-5949.32",
        (acct,),
    )
    _seed_sync_run(conn)
    set_debt_terms(conn, id="apple_card", name="Apple Card", apr=19.49,
                   account_id=acct, is_revolving=True)
    conn.commit()

    debt = [d for d in list_debts(conn, "2026-06-24")["debts"] if d["id"] == "apple_card"][0]
    assert debt["current_balance"] == -6122.03
    assert debt["monthly_interest"] == round(6122.03 * 19.49 / 100 / 12, 2) == 99.43

    # The status response, resolving the same account, agrees exactly.
    status = get_finance_status(db_path=str(db_path), start_date="2026-06-24")
    status_apple = [
        a for a in status["balances"]["accounts"] if a["account_id"] == acct
    ][0]
    assert status_apple["balance"] == debt["current_balance"] == -6122.03


def test_list_debts_sticky_manual_wins_over_next_day_feed(tmp_path):
    """Sticky manual propagates to list_debts across calendar days.

    The live Apple Card bug: a manual correction on day D (-6122.03) was shadowed
    by a next-day SimpleFIN feed row on D+1 (-5949.32) because the old precedence
    ordered by calendar day first. With sticky manual precedence the manual wins
    over the later-day feed, and list_debts (and thus the avalanche it feeds)
    reports the corrected balance.
    """

    db_path = tmp_path / "d.sqlite"
    conn = _db(db_path)
    acct = "ACT-apple"
    _seed_account(conn, account_id=acct, name="Alex", org="Apple Card (Updated Monthly)")
    # Manual correction on day D.
    _seed_balance(conn, account_id=acct, balance=-6122.03,
                  recorded_at="2026-06-24T14:56:47+00:00")
    conn.execute(
        "UPDATE balance_snapshots SET source='manual' WHERE account_id=? AND balance=-6122.03",
        (acct,),
    )
    # Stale SimpleFIN feed row on the NEXT day (D+1).
    _seed_balance(conn, account_id=acct, balance=-5949.32,
                  recorded_at="2026-06-25T08:10:18+00:00")
    conn.execute(
        "UPDATE balance_snapshots SET source='simplefin' WHERE account_id=? AND balance=-5949.32",
        (acct,),
    )
    _seed_sync_run(conn, finished_at="2026-06-25T08:10:18+00:00")
    set_debt_terms(conn, id="apple_card", name="Apple Card", apr=19.49,
                   account_id=acct, is_revolving=True)
    conn.commit()

    debt = [d for d in list_debts(conn, "2026-06-25")["debts"] if d["id"] == "apple_card"][0]
    assert debt["current_balance"] == -6122.03

    # Status agrees on the same later day.
    status = get_finance_status(db_path=str(db_path), start_date="2026-06-25")
    status_apple = [
        a for a in status["balances"]["accounts"] if a["account_id"] == acct
    ][0]
    assert status_apple["balance"] == -6122.03


# --- avalanche ordering -----------------------------------------------------


def _seed_real_debts(conn):
    """Seed the live-shaped debt set: Apple (revolving), Amex Gold (paid in
    full), Amex Personal Loan (revolving), and a manual student loan."""
    _seed_account(conn, account_id="acct_apple", name="Apple")
    _seed_balance(conn, account_id="acct_apple", balance=-5949.32)
    _seed_account(conn, account_id="acct_amex_gold", name="Amex Gold")
    _seed_balance(conn, account_id="acct_amex_gold", balance=-5485.61)
    _seed_account(conn, account_id="acct_amex_loan", name="Amex Personal Loan")
    _seed_balance(conn, account_id="acct_amex_loan", balance=-22898.70)

    set_debt_terms(conn, id="apple_card", name="Apple Card", apr=19.49,
                   account_id="acct_apple", is_revolving=True, autopay=False)
    set_debt_terms(conn, id="amex_gold", name="Amex Gold", apr=21.74,
                   account_id="acct_amex_gold", is_revolving=False, autopay=True)
    set_debt_terms(conn, id="amex_personal_loan", name="Amex Personal Loan", apr=7.49,
                   account_id="acct_amex_loan", is_revolving=True, autopay=True,
                   min_payment=500.84)
    set_debt_terms(conn, id="student_loans", name="Federal Student Loans", apr=4.22,
                   balance_source="manual", balance_override=-16375.79,
                   is_revolving=True, min_payment=557.10, autopay=False)


def test_avalanche_ranks_revolving_excludes_paid_in_full(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    _seed_real_debts(conn)

    res = evaluate_guardrails(conn, as_of_date="2026-06-24", accounts=_CHK, drift_findings=[])
    av = [f for f in res["findings"] if f["rule_type"] == "debt_avalanche"]
    assert av and av[0]["advisory"] is True

    order = [d["id"] for d in av[0]["evidence"]["apr_order"]]
    # Highest-APR revolving first; Amex Gold (21.74% but paid in full) excluded.
    assert order == ["apple_card", "amex_personal_loan", "student_loans"]
    assert "amex_gold" not in order
    # The top target reports its live balance + monthly interest, not a constant.
    top = av[0]["evidence"]["apr_order"][0]
    assert top["current_balance"] == -5949.32
    assert top["monthly_interest"] == round(5949.32 * 19.49 / 100 / 12, 2)


def test_avalanche_excludes_zero_balance_revolving(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    _seed_account(conn, account_id="acct_chase", name="Amazon Visa")
    _seed_balance(conn, account_id="acct_chase", balance=0.0)
    _seed_account(conn, account_id="acct_apple", name="Apple")
    _seed_balance(conn, account_id="acct_apple", balance=-100.0)

    set_debt_terms(conn, id="chase_visa", name="Chase Amazon Visa", apr=17.74,
                   account_id="acct_chase", is_revolving=True)
    set_debt_terms(conn, id="apple_card", name="Apple Card", apr=19.49,
                   account_id="acct_apple", is_revolving=True)

    res = evaluate_guardrails(conn, as_of_date="2026-06-24", accounts=_CHK, drift_findings=[])
    av = [f for f in res["findings"] if f["rule_type"] == "debt_avalanche"]
    order = [d["id"] for d in av[0]["evidence"]["apr_order"]]
    assert order == ["apple_card"]  # zero-balance Chase excluded


def test_avalanche_empty_debts_falls_back_to_constant(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    # No debts rows. An interest-bearing obligation triggers the legacy fallback.
    apply_obligation_instances(
        conn,
        obligation={"id": "amex_loan", "name": "Amex Personal Loan", "kind": "loan",
                    "cadence": "monthly", "status": "active", "source": "seed"},
        instances=[{"id": "amex_loan:2026-07-27", "due_date": "2026-07-27",
                    "amount": -500.84, "source": "seed"}],
    )
    res = evaluate_guardrails(conn, as_of_date="2026-06-24", accounts=_CHK, drift_findings=[])
    av = [f for f in res["findings"] if f["rule_type"] == "debt_avalanche"]
    assert av and av[0]["advisory"] is True
    # Legacy constant uses the abstract 'key' field, highest APR first.
    assert av[0]["evidence"]["apr_order"][0]["key"] == "amex_platinum"


def test_avalanche_silent_when_no_revolving_targets(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    # Only a paid-in-full card exists: debts table is non-empty, so no fallback,
    # and there is no revolving target -- the avalanche stays silent.
    _seed_account(conn, account_id="acct_amex", name="Amex Gold")
    _seed_balance(conn, account_id="acct_amex", balance=-5485.61)
    set_debt_terms(conn, id="amex_gold", name="Amex Gold", apr=21.74,
                   account_id="acct_amex", is_revolving=False)

    res = evaluate_guardrails(conn, as_of_date="2026-06-24", accounts=_CHK, drift_findings=[])
    assert [f for f in res["findings"] if f["rule_type"] == "debt_avalanche"] == []
