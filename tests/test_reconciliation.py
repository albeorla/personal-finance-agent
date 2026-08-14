"""Tests for reconciliation (slice C): match transactions to obligation instances."""

import sqlite3

from financial_agent.obligations import (
    apply_obligation_instances,
    deactivate_obligation,
    set_obligation_end,
)
from financial_agent.reconciliation import (
    confirm_reconciliation_match,
    list_matched_obligation_instances,
    list_reconciliation_review_items,
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


def test_one_transaction_covering_a_group_of_instances_shared_matches(tmp_path):
    # A single lump payment settles two separate bills from the SAME vendor at
    # once: the transaction amount equals the SUM of two same-direction,
    # nearby-date instances that neither match 1:1, AND the transaction shares
    # merchant evidence with each member (a real group, not a coincidence). Both
    # instances should record the SAME transaction as a shared needs_review match
    # (not merged, not auto-paid), so neither keeps projecting as a phantom outflow.
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-lump", "ACT-chk", "2026-06-16T08:00:00", -75.00, "Acme Streaming", "ACME STREAMING DRAFT")],
    )
    _obligation(conn, "svc_a", "Acme Streaming line one", "subscription",
                [{"id": "svc_a:2026-06-15", "due_date": "2026-06-15", "amount": -30.00, "source": "seed"}])
    _obligation(conn, "svc_b", "Acme Streaming line two", "subscription",
                [{"id": "svc_b:2026-06-15", "due_date": "2026-06-15", "amount": -45.00, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-06-20")
    assert summary["matched_shared"] == 2
    assert summary["unmatched"] == 0
    assert summary["matched_auto"] == 0

    matched = list_matched_obligation_instances(conn)
    assert {m["transaction_id"] for m in matched} == {"t-lump"}
    assert all(m["match_type"] == "needs_review" for m in matched)
    assert len(matched) == 2
    ev = next(m["evidence"] for m in matched if m["obligation_instance_id"] == "svc_a:2026-06-15")
    assert ev["group"]["shared_transaction"] is True
    assert ev["group"]["group_instance_ids"] == ["svc_a:2026-06-15", "svc_b:2026-06-15"]


def test_shared_group_requires_merchant_evidence_not_amount_coincidence(tmp_path):
    # BUG A regression: a transaction whose amount merely EQUALS the sum of two
    # unrelated bills, with ZERO merchant overlap with either, must NOT group. A
    # $75 car wash that happens to equal internet ($30) + phone ($45) is a
    # coincidence; grouping it would record match rows that then hide both bills
    # from the cash-flow past-due carry and overstate runway.
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-wash", "ACT-chk", "2026-06-16T08:00:00", -75.00, "Sparkle Car Wash", "CAR WASH")],
    )
    _obligation(conn, "internet", "Internet subscription", "subscription",
                [{"id": "internet:2026-06-15", "due_date": "2026-06-15", "amount": -30.00, "source": "seed"}])
    _obligation(conn, "phone", "Phone subscription", "subscription",
                [{"id": "phone:2026-06-15", "due_date": "2026-06-15", "amount": -45.00, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-06-20")
    assert summary["matched_shared"] == 0
    assert summary["matched_needs_review"] == 0
    assert summary["unmatched"] == 2
    # No coincidental match rows -> both bills stay visible to the past-due carry.
    assert conn.execute("SELECT COUNT(*) FROM transaction_obligation_matches").fetchone()[0] == 0


def test_shared_group_does_not_reuse_an_already_matched_transaction(tmp_path):
    # BUG B regression: a transaction already settling (confirmed/paid against)
    # one obligation must not be reused to invent a group for two still-open
    # obligations, which would suppress real owed dollars. Here the two open
    # bills DO share merchant evidence with the transaction (so BUG A's evidence
    # gate would let them through) - only the already-matched exclusion stops the
    # false group.
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-big", "ACT-chk", "2026-06-16T08:00:00", -2500.00, "Acme Payment", "ACME")],
    )
    _obligation(conn, "acme_paid", "Acme big bill", "housing",
                [{"id": "acme_paid:2026-06-15", "due_date": "2026-06-15", "amount": -2500.00, "source": "seed"}])

    # First pass: t-big auto-matches the $2,500 bill 1:1; confirm it paid.
    reconcile_obligation_instances(conn, as_of_date="2026-06-20")
    confirm_reconciliation_match(conn, "acme_paid:2026-06-15")
    assert conn.execute(
        "SELECT status FROM obligation_instances WHERE id = 'acme_paid:2026-06-15'"
    ).fetchone()[0] == "paid"

    # Two still-open Acme bills summing to the same $2,500 appear later.
    _obligation(conn, "acme_a", "Acme service a", "subscription",
                [{"id": "acme_a:2026-06-15", "due_date": "2026-06-15", "amount": -1000.00, "source": "seed"}])
    _obligation(conn, "acme_b", "Acme service b", "subscription",
                [{"id": "acme_b:2026-06-15", "due_date": "2026-06-15", "amount": -1500.00, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-06-20")
    # t-big is spent; it must not back a group for the two open bills.
    assert summary["matched_shared"] == 0
    open_matches = conn.execute(
        "SELECT COUNT(*) FROM transaction_obligation_matches WHERE obligation_instance_id IN "
        "('acme_a:2026-06-15','acme_b:2026-06-15')"
    ).fetchone()[0]
    assert open_matches == 0
    open_unmatched = {
        u["obligation_instance_id"] for u in list_unmatched_obligation_instances(conn)
    }
    assert {"acme_a:2026-06-15", "acme_b:2026-06-15"} <= open_unmatched


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


def test_price_change_against_last_cycle_is_raised_with_both_amounts(tmp_path):
    # The subscription billed $20 in July and $200 in August. Both charges match
    # their own instance exactly, so nothing here is "wrong" - and that is how a
    # price change used to clear silently. It must be raised instead.
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[
            ("t-jul", "ACT-chk", "2026-07-05T08:00:00", -20.00, "OpenAI ChatGPT", "CHATGPT SUBSCR"),
            ("t-aug", "ACT-chk", "2026-08-05T08:00:00", -200.00, "OpenAI ChatGPT", "CHATGPT SUBSCR"),
        ],
    )
    _obligation(conn, "chatgpt", "OpenAI ChatGPT subscription", "subscription",
                [{"id": "chatgpt:2026-07-05", "due_date": "2026-07-05", "amount": -20.0, "source": "seed"},
                 {"id": "chatgpt:2026-08-05", "due_date": "2026-08-05", "amount": -200.0, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-08-10")
    assert summary["amount_changed"] == 1
    assert summary["matched_auto"] == 1  # July cleared; August was pulled back for review

    items = list_reconciliation_review_items(conn, as_of_date="2026-08-10")
    raised = [i for i in items if i["obligation_instance_id"] == "chatgpt:2026-08-05"]
    assert len(raised) == 1
    change = raised[0]["amount_change"]
    assert change["previous_amount"] == 20.0 and change["previous_date"] == "2026-07-05"
    assert change["current_amount"] == 200.0 and change["current_date"] == "2026-08-05"
    assert change["delta"] == 180.0


def test_small_amount_move_still_clears_silently(tmp_path):
    # A dollar of variation is not a price change; it must not become a task.
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[
            ("t-jun", "ACT-chk", "2026-06-05T08:00:00", -20.00, "OpenAI ChatGPT", "CHATGPT"),
            ("t-jul", "ACT-chk", "2026-07-05T08:00:00", -21.00, "OpenAI ChatGPT", "CHATGPT"),
        ],
    )
    _obligation(conn, "chatgpt", "OpenAI ChatGPT subscription", "subscription",
                [{"id": "chatgpt:2026-06-05", "due_date": "2026-06-05", "amount": -20.0, "source": "seed"},
                 {"id": "chatgpt:2026-07-05", "due_date": "2026-07-05", "amount": -21.0, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-07-10")
    assert summary["amount_changed"] == 0
    assert summary["matched_auto"] == 2


def test_cycle_settled_inside_a_lump_payment_is_not_a_price_change(tmp_path):
    # July's $150 bill was settled inside a $500 lump that also covered a $350
    # sibling bill. August's own $150 charge is the SAME price - but the lump is
    # what the July instance points at, so a naive prior-cycle lookup reads
    # "$500 last cycle" and raises a $350 drop that never happened.
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[
            ("t-lump", "ACT-chk", "2026-07-16T08:00:00", -500.00, "Acme Streaming", "ACME STREAMING DRAFT"),
            ("t-aug", "ACT-chk", "2026-08-15T08:00:00", -150.00, "Acme Streaming", "ACME STREAMING DRAFT"),
        ],
    )
    _obligation(conn, "svc_a", "Acme Streaming line one", "subscription",
                [{"id": "svc_a:2026-07-15", "due_date": "2026-07-15", "amount": -150.00, "source": "seed"},
                 {"id": "svc_a:2026-08-15", "due_date": "2026-08-15", "amount": -150.00, "source": "seed"}])
    _obligation(conn, "svc_b", "Acme Streaming line two", "subscription",
                [{"id": "svc_b:2026-07-15", "due_date": "2026-07-15", "amount": -350.00, "source": "seed"}])

    summary = reconcile_obligation_instances(conn, as_of_date="2026-08-20")
    assert summary["matched_shared"] == 2  # July really was settled by the lump
    assert summary["amount_changed"] == 0
    assert summary["matched_auto"] == 1

    matched = {m["obligation_instance_id"]: m for m in list_matched_obligation_instances(conn)}
    assert matched["svc_a:2026-07-15"]["evidence"]["group"]["member_amount"] == 150.0
    assert matched["svc_a:2026-08-15"]["match_type"] == "auto"
    assert matched["svc_a:2026-08-15"]["evidence"].get("amount_change") is None


def test_refund_after_cancellation_is_not_raised_as_a_continuing_charge(tmp_path):
    # After cancelling, the most likely next transaction from that merchant is the
    # refund. Same size, same merchant - but money coming BACK is not the bill
    # still billing, and must never be reported as one.
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[
            ("t-jun", "ACT-chk", "2026-06-01T08:00:00", -32.00, "Dearborn Media", "DEARBORN"),
            ("t-refund", "ACT-chk", "2026-07-10T08:00:00", 32.00, "Dearborn Media", "DEARBORN REFUND"),
            ("t-aug", "ACT-chk", "2026-08-01T08:00:00", -32.00, "Dearborn Media", "DEARBORN"),
        ],
    )
    _obligation(conn, "dearborn", "Dearborn Media subscription", "subscription",
                [{"id": "dearborn:2026-06-01", "due_date": "2026-06-01", "amount": -32.0, "source": "seed"}])
    reconcile_obligation_instances(conn, as_of_date="2026-06-10")
    set_obligation_end(conn, "dearborn", "2026-07-01")

    zombies = [
        i for i in list_reconciliation_review_items(conn, as_of_date="2026-08-10")
        if i["review_type"] == "charge_after_cancellation"
    ]
    # The real charge that kept arriving is still raised; the refund is not.
    assert [z["transaction_id"] for z in zombies] == ["t-aug"]


def test_charge_after_active_until_is_raised(tmp_path):
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[
            ("t-jun", "ACT-chk", "2026-06-01T08:00:00", -32.00, "Dearborn Media", "DEARBORN"),
            ("t-aug", "ACT-chk", "2026-08-01T08:00:00", -32.00, "Dearborn Media", "DEARBORN"),
            # Same amount, same week, unrelated merchant: a coincidence, not this bill.
            ("t-decoy", "ACT-chk", "2026-08-02T08:00:00", -32.00, "Shell Oil", "FUEL"),
        ],
    )
    _obligation(conn, "dearborn", "Dearborn Media subscription", "subscription",
                [{"id": "dearborn:2026-06-01", "due_date": "2026-06-01", "amount": -32.0, "source": "seed"}])
    reconcile_obligation_instances(conn, as_of_date="2026-06-10")
    set_obligation_end(conn, "dearborn", "2026-07-01")

    items = list_reconciliation_review_items(conn, as_of_date="2026-08-10")
    zombies = [i for i in items if i["review_type"] == "charge_after_cancellation"]
    assert [z["transaction_id"] for z in zombies] == ["t-aug"]
    assert zombies[0]["obligation_id"] == "dearborn"
    assert zombies[0]["due_date"] == "2026-08-01" and zombies[0]["amount"] == -32.0
    assert zombies[0]["cancelled_on"] == "2026-07-01"
    assert zombies[0]["previous_amount"] == 32.0 and zombies[0]["previous_date"] == "2026-06-01"
    # Stable id, so the surfaced task dedupes instead of re-raising every day.
    assert zombies[0]["obligation_instance_id"] == "post-cancellation:dearborn:t-aug"
    assert list_reconciliation_review_items(conn, as_of_date="2026-08-10") == items


def test_instance_past_end_date_does_not_absorb_the_zombie_charge(tmp_path):
    # The bill still has an August instance on the books when it is cancelled as of
    # July. That instance must not quietly match the August charge (same price as
    # July, so no amount-change raise either) - the charge is the whole point.
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[
            ("t-jun", "ACT-chk", "2026-06-01T08:00:00", -32.00, "Dearborn Media", "DEARBORN"),
            ("t-aug", "ACT-chk", "2026-08-01T08:00:00", -32.00, "Dearborn Media", "DEARBORN"),
        ],
    )
    _obligation(conn, "dearborn", "Dearborn Media subscription", "subscription",
                [{"id": "dearborn:2026-06-01", "due_date": "2026-06-01", "amount": -32.0, "source": "seed"},
                 {"id": "dearborn:2026-08-01", "due_date": "2026-08-01", "amount": -32.0, "source": "seed"}])
    # Reconciled once while still active, so the August instance carries a match row
    # recorded before anyone cancelled the bill.
    reconcile_obligation_instances(conn, as_of_date="2026-08-10")
    set_obligation_end(conn, "dearborn", "2026-07-01")

    summary = reconcile_obligation_instances(conn, as_of_date="2026-08-10")
    assert summary["considered"] == 1 and summary["skipped_after_end_date"] == 1

    items = list_reconciliation_review_items(conn, as_of_date="2026-08-10")
    zombies = [i for i in items if i["review_type"] == "charge_after_cancellation"]
    assert [z["transaction_id"] for z in zombies] == ["t-aug"]
    assert zombies[0]["cancelled_on"] == "2026-07-01"
    assert zombies[0]["previous_amount"] == 32.0 and zombies[0]["previous_date"] == "2026-06-01"
    # The stale match on the past-end instance is gone, not just ignored.
    assert [m["obligation_instance_id"] for m in list_matched_obligation_instances(conn)] == [
        "dearborn:2026-06-01"
    ]


def test_charge_after_deactivation_is_raised(tmp_path):
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[
            ("t-jun", "ACT-chk", "2026-06-01T08:00:00", -32.00, "Dearborn Media", "DEARBORN"),
            ("t-aug", "ACT-chk", "2026-08-01T08:00:00", -32.00, "Dearborn Media", "DEARBORN"),
        ],
    )
    _obligation(conn, "dearborn", "Dearborn Media subscription", "subscription",
                [{"id": "dearborn:2026-06-01", "due_date": "2026-06-01", "amount": -32.0, "source": "seed"}])
    reconcile_obligation_instances(conn, as_of_date="2026-06-10")
    deactivate_obligation(conn, "dearborn")
    # deactivate_obligation stamps updated_at with the wall clock; pin it so the
    # cancellation date is deterministic rather than "whenever the test ran".
    conn.execute("UPDATE obligations SET updated_at = '2026-07-01T12:00:00' WHERE id = 'dearborn'")

    zombies = [
        i for i in list_reconciliation_review_items(conn, as_of_date="2026-08-10")
        if i["review_type"] == "charge_after_cancellation"
    ]
    assert len(zombies) == 1
    assert zombies[0]["transaction_id"] == "t-aug" and zombies[0]["cancelled_on"] == "2026-07-01"


def test_charge_before_cancellation_is_not_raised(tmp_path):
    conn = _db(
        tmp_path / "r.sqlite",
        transactions=[("t-jun", "ACT-chk", "2026-06-01T08:00:00", -32.00, "Dearborn Media", "DEARBORN")],
    )
    _obligation(conn, "dearborn", "Dearborn Media subscription", "subscription",
                [{"id": "dearborn:2026-06-01", "due_date": "2026-06-01", "amount": -32.0, "source": "seed"}])
    reconcile_obligation_instances(conn, as_of_date="2026-06-10")
    set_obligation_end(conn, "dearborn", "2026-07-01")

    items = list_reconciliation_review_items(conn, as_of_date="2026-08-10")
    assert [i for i in items if i["review_type"] == "charge_after_cancellation"] == []
