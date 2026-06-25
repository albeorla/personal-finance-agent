"""Tests for reconciliation (slice C): match transactions to obligation instances."""

import sqlite3

from financial_agent.obligations import apply_obligation_instances
from financial_agent.reconciliation import (
    list_matched_obligation_instances,
    list_unmatched_obligation_instances,
    reconcile_obligation_instances,
)
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
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('ACT-chk','PREMIER PLUS CKG (4321)','Chase Bank','','USD')")
    conn.executemany(
        "INSERT INTO transactions (id,account_id,posted,amount,payee,description,pending,source) VALUES (?,?,?,?,?,?,0,'simplefin')",
        transactions,
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    conn.commit()
    return conn


def _obligation(conn, oid, name, kind, instances):
    apply_obligation_instances(
        conn,
        obligation={"id": oid, "name": name, "kind": kind, "status": "active", "source": "seed"},
        instances=instances,
    )


def test_exact_amount_near_due_date_auto_matches(tmp_path):
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-ever", "ACT-chk", "2026-06-26T08:00:00", -115.87, "Eversource Energy", "EVERSOURCE WEB_PAY")],
    )
    _obligation(conn, "eversource", "Eversource electric estimates", "utility",
                [{"id": "eversource:2026-06-25", "due_date": "2026-06-25", "amount": -115.87, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-06-30")
    assert summary["matched_auto"] == 1
    assert summary["unmatched"] == 0

    matched = list_matched_obligation_instances(conn)
    assert len(matched) == 1
    assert matched[0]["transaction_id"] == "t-ever"
    assert matched[0]["match_type"] == "auto"
    assert matched[0]["amount_delta"] == 0.0


def test_no_merchant_overlap_near_amount_is_not_matched(tmp_path):
    # A near-but-not-exact amount on a nearby date with ZERO merchant overlap is
    # a coincidence, not a payment (the false "Renewal Membership Fee" <-> Apple
    # paydown case from go-live QA).
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-renew", "ACT-chk", "2026-06-20T08:00:00", -297.54, "Renewal Membership Fee", "")],
    )
    _obligation(conn, "apple_paydown", "Apple Card paydown sweeps", "card_paydown",
                [{"id": "apple_paydown:2026-06-20", "due_date": "2026-06-20", "amount": -300.0, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-06-25")
    assert summary["matched_needs_review"] == 0 and summary["matched_auto"] == 0


def test_within_tolerance_but_not_exact_is_needs_review(tmp_path):
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-amex", "ACT-chk", "2026-07-16T08:00:00", -3450.00, "American Express", "AMEX EPAYMENT")],
    )
    _obligation(conn, "amex_statement_payment", "Amex statement payment", "credit_card_statement",
                [{"id": "amex_statement_payment:2026-07-16", "due_date": "2026-07-16", "amount": -3456.78, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-07-20")
    # Within 2.5% tolerance but not exact -> review, not an auto match.
    assert summary["matched_needs_review"] == 1
    assert summary["matched_auto"] == 0
    matched = list_matched_obligation_instances(conn)
    assert matched[0]["match_type"] == "needs_review"
    assert matched[0]["amount_delta"] == 6.78


def test_unmatched_past_grace_is_flagged_needs_review_not_overdue(tmp_path):
    conn = _db(tmp_path / "r.sqlite", transactions=[])
    _obligation(conn, "rent", "Rent check", "housing",
                [{"id": "rent:2026-06-01", "due_date": "2026-06-01", "amount": -3000.0, "source": "seed"}])

    summary = reconcile_obligation_instances(
        conn, as_of_date="2026-06-20", options={"flag_unmatched_needs_review": True}
    )
    assert summary["unmatched"] == 1
    assert summary["flagged_needs_review"] == 1

    status = conn.execute("SELECT status FROM obligation_instances WHERE id = 'rent:2026-06-01'").fetchone()[0]
    assert status == "needs_review"  # conservative: never auto-"overdue"
    unmatched = list_unmatched_obligation_instances(conn, past_grace_only=True)
    assert len(unmatched) == 1
    assert unmatched[0]["age_days"] == 19
    assert unmatched[0]["past_grace"] is True


def test_reconcile_is_idempotent(tmp_path):
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-ever", "ACT-chk", "2026-06-26T08:00:00", -115.87, "Eversource Energy", "EVERSOURCE")],
    )
    _obligation(conn, "eversource", "Eversource electric estimates", "utility",
                [{"id": "eversource:2026-06-25", "due_date": "2026-06-25", "amount": -115.87, "source": "seed"}])

    reconcile_obligation_instances(conn, as_of_date="2026-06-30")
    reconcile_obligation_instances(conn, as_of_date="2026-06-30")
    assert conn.execute("SELECT COUNT(*) FROM transaction_obligation_matches").fetchone()[0] == 1


def test_auto_mark_paid_sets_paid_with_evidence(tmp_path):
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-ever", "ACT-chk", "2026-06-25T08:00:00", -115.87, "Eversource Energy", "EVERSOURCE")],
    )
    _obligation(conn, "eversource", "Eversource electric estimates", "utility",
                [{"id": "eversource:2026-06-25", "due_date": "2026-06-25", "amount": -115.87, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-06-30", options={"auto_mark_paid": True})
    assert summary["marked_paid"] == 1
    row = conn.execute(
        "SELECT status, matched_transaction_id, match_confidence FROM obligation_instances WHERE id = 'eversource:2026-06-25'"
    ).fetchone()
    assert row["status"] == "paid"
    assert row["matched_transaction_id"] == "t-ever"
    assert row["match_confidence"] is not None


def test_card_statement_input_instances_are_excluded(tmp_path):
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-gault", "ACT-chk", "2026-06-25T08:00:00", -532.10, "Gault Energy", "GAULT")],
    )
    _obligation(conn, "gault_card_spend", "Gault Energy", "card_spend_input",
                [{"id": "gault_card_spend:2026-06-25", "due_date": "2026-06-25", "amount": -532.10,
                  "source": "seed", "cash_flow_treatment": "card_statement_input",
                  "statement_target_obligation_id": "amex_statement_payment"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-06-30")
    # Card-statement inputs settle via the statement, not a direct checking match.
    assert summary["considered"] == 0
    assert conn.execute("SELECT COUNT(*) FROM transaction_obligation_matches").fetchone()[0] == 0


def test_inflow_matches_positive_transaction(tmp_path):
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-pay", "ACT-chk", "2026-07-01T08:00:00", 800.00, "Town of Greenwich Payroll", "PAYROLL")],
    )
    _obligation(conn, "partner_pay", "Town of Greenwich Payroll", "income",
                [{"id": "partner_pay:2026-07-01", "due_date": "2026-07-01", "amount": 800.0,
                  "direction": "inflow", "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-07-05")
    assert summary["matched_auto"] == 1
    assert list_matched_obligation_instances(conn)[0]["transaction_id"] == "t-pay"


def test_one_transaction_matches_at_most_one_instance(tmp_path):
    # Two obligations with identical amount/merchant/date, but only one transaction.
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-one", "ACT-chk", "2026-06-25T08:00:00", -50.00, "Acme Co", "ACME")],
    )
    _obligation(conn, "acme_a", "Acme Co", "subscription",
                [{"id": "acme_a:2026-06-25", "due_date": "2026-06-25", "amount": -50.0, "source": "seed"}])
    _obligation(conn, "acme_b", "Acme Co", "subscription",
                [{"id": "acme_b:2026-06-25", "due_date": "2026-06-25", "amount": -50.0, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-06-30")
    matched = list_matched_obligation_instances(conn)
    # The single transaction is claimed by exactly one instance; the other is unmatched.
    assert sum(1 for m in matched if m["transaction_id"] == "t-one") == 1
    assert summary["matched_auto"] + summary["matched_needs_review"] == 1
    assert summary["unmatched"] == 1


def test_unmatched_is_cleared_when_a_match_later_appears(tmp_path):
    conn = _db(tmp_path / "r.sqlite", transactions=[])
    _obligation(conn, "eversource", "Eversource electric estimates", "utility",
                [{"id": "eversource:2026-06-25", "due_date": "2026-06-25", "amount": -115.87, "source": "seed"}])

    reconcile_obligation_instances(conn, as_of_date="2026-06-30")
    assert conn.execute("SELECT COUNT(*) FROM unmatched_obligations").fetchone()[0] == 1

    # A matching transaction shows up on a later sync.
    conn.execute(
        "INSERT INTO transactions (id,account_id,posted,amount,payee,source) "
        "VALUES ('t-late','ACT-chk','2026-06-26T08:00:00',-115.87,'Eversource Energy','simplefin')"
    )
    reconcile_obligation_instances(conn, as_of_date="2026-06-30")
    assert conn.execute("SELECT COUNT(*) FROM unmatched_obligations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transaction_obligation_matches").fetchone()[0] == 1
