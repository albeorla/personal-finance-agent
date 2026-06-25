"""Tests for the charge-onboarding candidate scanner and review queue.

These exercise deterministic discovery of charge-pattern candidates from copied
transaction evidence. The acceptance cases use the real merchant amounts and
account placements observed in ``data/transactions.source-copy.sqlite``:

- Gault Energy fills land on the Amex Platinum card -> card statement input.
- Eversource electric is paid from checking XXXX -> direct checking, seasonal.
- New York Times is a monthly checking subscription that settled at $30.30.

Candidates are never cash-flow truth: they live in their own table and must not
change projections until they are applied as canonical obligations.
"""

import sqlite3
from datetime import date

import pytest

from financial_agent.cashflow import build_cash_flow_projections
from financial_agent.obligations import apply_obligation_instances, list_statement_input_estimates
from financial_agent.onboarding import (
    _confidence,
    account_class,
    apply_charge_onboarding_candidate,
    get_next_charge_onboarding_candidate,
    list_charge_onboarding_queue,
    normalize_merchant_key,
    preview_charge_onboarding_apply,
    record_charge_onboarding_decision,
    scan_charge_onboarding_candidates,
)
from financial_agent.schema import ensure_app_schema


# Account fixtures mirror the real copied database, where account.kind is empty
# and class must be inferred from name/org.
AMEX = ("ACT-amex", "Platinum Card® (5000)", "American Express", "", "USD")
CHECKING = ("ACT-chk", "PREMIER PLUS CKG (XXXX)", "Chase Bank", "", "USD")
SAVINGS = ("ACT-sav", "PREMIER SAVINGS (6175)", "Chase Bank", "", "USD")

GAULT_AMEX_ROWS = [
    ("gault-1", "ACT-amex", "2026-01-15T08:00:00", -532.10, "Gault Energy", "GAULT ENERGY & HOME 203-2275181 CT"),
    ("gault-2", "ACT-amex", "2026-02-23T08:00:00", -602.48, "Gault Energy", "GAULT ENERGY & HOME 203-2275181 CT"),
    ("gault-3", "ACT-amex", "2026-04-12T08:00:00", -499.12, "Gault Energy", "GAULT ENERGY & HOME 203-2275181 CT"),
]

EVERSOURCE_CHECKING_ROWS = [
    ("ever-1", "ACT-chk", "2025-11-28T08:00:00", -79.50, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: 3020181050"),
    ("ever-2", "ACT-chk", "2025-12-30T08:00:00", -98.86, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: 3020181050"),
    ("ever-3", "ACT-chk", "2026-02-02T08:00:00", -148.36, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: 3020181050"),
    ("ever-4", "ACT-chk", "2026-03-02T08:00:00", -121.80, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: XXXXXX1050"),
    ("ever-5", "ACT-chk", "2026-03-30T08:00:00", -111.93, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: XXXXXX1050"),
    ("ever-6", "ACT-chk", "2026-04-28T08:00:00", -106.39, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: XXXXXX1050"),
    ("ever-7", "ACT-chk", "2026-05-28T08:00:00", -144.22, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: XXXXXX1050"),
]

# Checking NYT rows: $25.25 early, then a price increase that settled at $30.30.
NYT_CHECKING_ROWS = [
    ("nyt-1", "ACT-chk", "2025-12-01T08:00:00", -25.25, "New York Times", "NYTIMES* 800-698-4637 NY 11/28"),
    ("nyt-2", "ACT-chk", "2025-12-29T08:00:00", -25.25, "New York Times", "NYTIMES* 800-698-4637 NY 12/26"),
    ("nyt-3", "ACT-chk", "2026-01-26T08:00:00", -25.25, "New York Times", "NYTIMES* 800-698-4637 NY 01/23"),
    ("nyt-4", "ACT-chk", "2026-02-23T08:00:00", -30.30, "New York Times", "NYTIMES* 800-698-4637 NY 02/20"),
    ("nyt-5", "ACT-chk", "2026-03-23T08:00:00", -30.30, "New York Times", "NYTIMES* 800-698-4637 NY 03/20"),
    ("nyt-6", "ACT-chk", "2026-04-20T08:00:00", -30.30, "New York Times", "NYTIMES* 800-698-4637 NY 04/17"),
    ("nyt-7", "ACT-chk", "2026-05-18T08:00:00", -30.30, "New York Times", "NYTIMES* 800-698-4637 NY 05/15"),
    ("nyt-8", "ACT-chk", "2026-06-15T08:00:00", -30.30, "New York Times", "NYTIMES* 800-698-4637 NY 06/12"),
]


def _seed_source_db(path, *, accounts, transactions, with_balances=False):
    """Build a copied-style source DB with accounts + transactions tables."""

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            org TEXT,
            kind TEXT,
            currency TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT
        );

        CREATE TABLE transactions (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            posted TEXT,
            transacted_at TEXT,
            amount REAL NOT NULL,
            payee TEXT,
            description TEXT,
            pending INTEGER,
            source TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            fetched_at TEXT
        );
        """
    )
    if with_balances:
        conn.executescript(
            """
            CREATE TABLE balance_snapshots (
                id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL,
                balance REAL NOT NULL,
                available REAL NOT NULL,
                recorded_at TEXT NOT NULL,
                source TEXT NOT NULL
            );
            """
        )
    conn.executemany(
        "INSERT INTO accounts (id, name, org, kind, currency, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, '2025-01-01', '2026-06-20')",
        accounts,
    )
    conn.executemany(
        "INSERT INTO transactions (id, account_id, posted, amount, payee, description, pending, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 'simplefin')",
        transactions,
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    conn.commit()
    return conn


def _find(queue, merchant_key, *, treatment=None):
    for candidate in queue:
        if candidate["merchant_key"] == merchant_key:
            if treatment is None or candidate["cash_flow_treatment"] == treatment:
                return candidate
    return None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_merchant_key_is_deterministic_slug():
    assert normalize_merchant_key("Gault Energy") == "gault_energy"
    assert normalize_merchant_key("New York Times") == "new_york_times"
    assert normalize_merchant_key("Eversource Energy") == "eversource_energy"
    assert normalize_merchant_key("  AT&T  Wireless ") == "at_t_wireless"


def test_account_class_infers_from_name_and_org_when_kind_empty():
    assert account_class({"name": "PREMIER PLUS CKG (XXXX)", "org": "Chase Bank", "kind": ""}) == "checking"
    assert account_class({"name": "PREMIER SAVINGS (6175)", "org": "Chase Bank", "kind": ""}) == "savings"
    assert account_class({"name": "Platinum Card® (5000)", "org": "American Express", "kind": ""}) == "card"
    assert account_class({"name": "Owner", "org": "Apple Card (Updated Monthly)", "kind": ""}) == "card"
    assert account_class({"name": "Personal Loan (1004)", "org": "American Express", "kind": ""}) == "loan"
    assert account_class({"name": "PCRA Trust ...746 (746)", "org": "Charles Schwab US", "kind": ""}) == "investment"
    assert account_class({"name": "Mystery Wallet", "org": "Unknown Fintech", "kind": ""}) == "other"


def test_confidence_thresholds_at_boundaries():
    # Single observation is never trustworthy.
    assert _confidence(n=1, months_covered=1, cv=0.0, cadence="monthly") == "very_low"
    # High needs >=4 occ over >=4 months, regular cadence, and tight amounts.
    assert _confidence(n=4, months_covered=4, cv=0.10, cadence="monthly") == "high"
    # Just over the amount-variability line drops to medium.
    assert _confidence(n=4, months_covered=4, cv=0.11, cadence="monthly") == "medium"
    # Not enough months for high, but still a medium recurring signal.
    assert _confidence(n=4, months_covered=3, cv=0.05, cadence="monthly") == "medium"
    # Lumpy multi-week cadence (e.g. Gault) can still reach medium with 3 over 3 months.
    assert _confidence(n=3, months_covered=3, cv=0.30, cadence="irregular_multiweek") == "medium"
    # Two observations is low no matter how clean.
    assert _confidence(n=2, months_covered=2, cv=0.0, cadence="monthly") == "low"
    # Truly irregular timing stays low even with 3 observations.
    assert _confidence(n=3, months_covered=3, cv=0.05, cadence="irregular") == "low"


# ---------------------------------------------------------------------------
# Acceptance: candidate detection
# ---------------------------------------------------------------------------


def test_scan_creates_gault_card_statement_input_candidate(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[AMEX], transactions=GAULT_AMEX_ROWS)

    result = scan_charge_onboarding_candidates(conn)
    assert result["created"] == 1

    queue = list_charge_onboarding_queue(conn)
    gault = _find(queue, "gault_energy")
    assert gault is not None
    assert gault["display_name"] == "Gault Energy"
    assert gault["cash_flow_treatment"] == "card_statement_input"
    assert gault["candidate_type"] == "card_statement_input"
    assert gault["status"] in {"proposed", "discovered"}
    assert gault["account_class"] == "card"
    assert gault["direction"] == "outflow"
    assert gault["evidence_count"] == 3
    assert set(gault["evidence_transaction_ids"]) == {"gault-1", "gault-2", "gault-3"}

    amount_policy = gault["proposed_amount_policy"]
    assert amount_policy["method"] == "seasonal_card_spend"
    assert amount_policy["base_average"] == 544.57

    cash_impact = gault["proposed_cash_impact_policy"]
    assert cash_impact["cash_flow_treatment"] == "card_statement_input"
    assert cash_impact["statement_target_obligation_id"] == "amex_statement_payment"


def test_scan_creates_eversource_direct_checking_candidate(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=EVERSOURCE_CHECKING_ROWS)

    scan_charge_onboarding_candidates(conn)

    queue = list_charge_onboarding_queue(conn)
    ever = _find(queue, "eversource_energy")
    assert ever is not None
    assert ever["cash_flow_treatment"] == "direct_checking"
    assert ever["candidate_type"] == "direct_checking_outflow"
    assert ever["evidence_count"] == 7
    assert set(ever["evidence_transaction_ids"]) == {f"ever-{n}" for n in range(1, 8)}

    amount_policy = ever["proposed_amount_policy"]
    assert amount_policy["method"] in {"average", "seasonal_multiplier"}
    # Mean of the seven observed Eversource payments is $115.87 (matches the seed).
    assert amount_policy["base_average"] == 115.87

    schedule = ever["proposed_schedule_policy"]
    assert schedule["cadence"] == "monthly"


def test_scan_creates_nyt_fixed_subscription_candidate(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=NYT_CHECKING_ROWS)

    scan_charge_onboarding_candidates(conn)

    queue = list_charge_onboarding_queue(conn)
    nyt = _find(queue, "new_york_times")
    assert nyt is not None
    assert nyt["cash_flow_treatment"] == "direct_checking"
    assert nyt["candidate_type"] == "direct_checking_outflow"

    amount_policy = nyt["proposed_amount_policy"]
    # The subscription settled at $30.30 after a price increase, so the proposal
    # should anchor on the current stable price, not the historical average.
    assert amount_policy["method"] == "fixed"
    assert amount_policy["amount"] == 30.30
    assert nyt["proposed_schedule_policy"]["cadence"] == "monthly"


# ---------------------------------------------------------------------------
# Acceptance: queue behaviour
# ---------------------------------------------------------------------------


def test_candidates_appear_in_queue_and_get_next_is_highest_priority(tmp_path):
    conn = _seed_source_db(
        tmp_path / "src.sqlite",
        accounts=[AMEX, CHECKING],
        transactions=GAULT_AMEX_ROWS + EVERSOURCE_CHECKING_ROWS + NYT_CHECKING_ROWS,
    )

    scan_charge_onboarding_candidates(conn)

    queue = list_charge_onboarding_queue(conn)
    keys = {candidate["merchant_key"] for candidate in queue}
    assert {"gault_energy", "eversource_energy", "new_york_times"} <= keys

    # Queue is priority-ordered by estimated monthly cash impact, descending.
    scores = [candidate["priority_score"] for candidate in queue]
    assert scores == sorted(scores, reverse=True)

    nxt = get_next_charge_onboarding_candidate(conn)
    assert nxt is not None
    # Gault fills (~$544 each) dwarf Eversource (~$116) and NYT (~$30).
    assert nxt["merchant_key"] == "gault_energy"


def test_get_next_skips_resolved_candidates(tmp_path):
    conn = _seed_source_db(
        tmp_path / "src.sqlite",
        accounts=[AMEX, CHECKING],
        transactions=GAULT_AMEX_ROWS + EVERSOURCE_CHECKING_ROWS,
    )
    scan_charge_onboarding_candidates(conn)

    first = get_next_charge_onboarding_candidate(conn)
    assert first["merchant_key"] == "gault_energy"

    record_charge_onboarding_decision(conn, first["id"], {"action": "defer"})

    second = get_next_charge_onboarding_candidate(conn)
    assert second is not None
    assert second["merchant_key"] == "eversource_energy"


# ---------------------------------------------------------------------------
# Acceptance: candidates are not cash-flow truth
# ---------------------------------------------------------------------------


def test_cash_flow_projection_excludes_unapplied_candidates(tmp_path):
    conn = _seed_source_db(
        tmp_path / "src.sqlite",
        accounts=[AMEX, CHECKING],
        transactions=GAULT_AMEX_ROWS + EVERSOURCE_CHECKING_ROWS + NYT_CHECKING_ROWS,
        with_balances=True,
    )
    # One real canonical obligation so the projection is non-empty.
    apply_obligation_instances(
        conn,
        obligation={
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "test",
        },
        instances=[{"due_date": "2026-07-03", "amount": -3000.0, "source": "test"}],
    )

    accounts = [
        {
            "account_id": "ACT-chk",
            "account_name": "PREMIER PLUS CKG (XXXX)",
            "kind": "checking",
            "available": 9000.0,
            "recorded_at": "2026-06-20T00:00:00+00:00",
        }
    ]

    before, _ = build_cash_flow_projections(conn, accounts=accounts, windows=[40], start_date=date(2026, 6, 21))
    instances_before = conn.execute("SELECT COUNT(*) FROM obligation_instances").fetchone()[0]

    scan_result = scan_charge_onboarding_candidates(conn)
    assert scan_result["created"] >= 3

    after, _ = build_cash_flow_projections(conn, accounts=accounts, windows=[40], start_date=date(2026, 6, 21))
    instances_after = conn.execute("SELECT COUNT(*) FROM obligation_instances").fetchone()[0]

    # Discovering candidates must not write obligation instances or move cash flow.
    assert instances_after == instances_before
    assert after[0]["ending_balance"] == before[0]["ending_balance"]
    assert [e["instance_id"] for e in after[0]["events"]] == [e["instance_id"] for e in before[0]["events"]]
    assert after[0]["ending_balance"] == 6000.0


# ---------------------------------------------------------------------------
# Acceptance: idempotency
# ---------------------------------------------------------------------------


def test_scan_is_idempotent(tmp_path):
    conn = _seed_source_db(
        tmp_path / "src.sqlite",
        accounts=[AMEX, CHECKING],
        transactions=GAULT_AMEX_ROWS + EVERSOURCE_CHECKING_ROWS + NYT_CHECKING_ROWS,
    )

    first = scan_charge_onboarding_candidates(conn)
    queue_after_first = list_charge_onboarding_queue(conn)

    second = scan_charge_onboarding_candidates(conn)
    queue_after_second = list_charge_onboarding_queue(conn)

    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == first["created"]
    # Same candidates, same ids, no duplicates.
    assert {c["id"] for c in queue_after_first} == {c["id"] for c in queue_after_second}
    assert len(queue_after_second) == len(queue_after_first)
    total = conn.execute("SELECT COUNT(*) FROM charge_onboarding_candidates").fetchone()[0]
    assert total == len(queue_after_first)


def test_rescan_preserves_human_decisions(tmp_path):
    conn = _seed_source_db(
        tmp_path / "src.sqlite",
        accounts=[AMEX, CHECKING],
        transactions=GAULT_AMEX_ROWS + EVERSOURCE_CHECKING_ROWS,
    )
    scan_charge_onboarding_candidates(conn)

    gault = _find(list_charge_onboarding_queue(conn), "gault_energy")
    record_charge_onboarding_decision(conn, gault["id"], {"action": "reject", "notes": "already modeled"})

    # A later background scan must not silently revive a rejected candidate.
    scan_charge_onboarding_candidates(conn)

    row = conn.execute(
        "SELECT status FROM charge_onboarding_candidates WHERE id = ?", (gault["id"],)
    ).fetchone()
    assert row[0] == "rejected"
    assert _find(list_charge_onboarding_queue(conn), "gault_energy") is None


# ---------------------------------------------------------------------------
# Acceptance: review-state decisions
# ---------------------------------------------------------------------------


def test_record_decision_supports_defer_reject_needs_more_evidence(tmp_path):
    conn = _seed_source_db(
        tmp_path / "src.sqlite",
        accounts=[AMEX, CHECKING],
        transactions=GAULT_AMEX_ROWS + EVERSOURCE_CHECKING_ROWS + NYT_CHECKING_ROWS,
    )
    scan_charge_onboarding_candidates(conn)
    queue = list_charge_onboarding_queue(conn)
    by_key = {c["merchant_key"]: c for c in queue}

    deferred = record_charge_onboarding_decision(conn, by_key["gault_energy"]["id"], {"action": "defer"})
    assert deferred["status"] == "deferred"
    assert deferred["reviewed_at"] is not None

    rejected = record_charge_onboarding_decision(conn, by_key["eversource_energy"]["id"], {"action": "reject"})
    assert rejected["status"] == "rejected"

    flagged = record_charge_onboarding_decision(
        conn, by_key["new_york_times"]["id"], {"action": "needs_more_evidence", "notes": "confirm price"}
    )
    assert flagged["status"] == "needs_more_evidence"

    # All three leave the active walk.
    assert list_charge_onboarding_queue(conn) == []
    # but remain visible when explicitly requested.
    assert len(list_charge_onboarding_queue(conn, include_resolved=True)) == 3
    assert len(list_charge_onboarding_queue(conn, status="deferred")) == 1


def test_record_decision_rejects_apply_in_first_slice(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[AMEX], transactions=GAULT_AMEX_ROWS)
    scan_charge_onboarding_candidates(conn)
    candidate = get_next_charge_onboarding_candidate(conn)

    # Applying a candidate into canonical obligations is a separate guarded slice.
    with pytest.raises(ValueError):
        record_charge_onboarding_decision(conn, candidate["id"], {"action": "apply"})


def test_record_decision_unknown_candidate_raises(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[AMEX], transactions=GAULT_AMEX_ROWS)
    scan_charge_onboarding_candidates(conn)
    with pytest.raises(ValueError):
        record_charge_onboarding_decision(conn, "cand:does_not_exist", {"action": "defer"})


def test_reset_returns_candidate_to_active_queue_and_clears_reviewed_at(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[AMEX], transactions=GAULT_AMEX_ROWS)
    scan_charge_onboarding_candidates(conn)
    candidate = get_next_charge_onboarding_candidate(conn)

    deferred = record_charge_onboarding_decision(conn, candidate["id"], {"action": "defer"})
    assert deferred["status"] == "deferred"
    assert deferred["reviewed_at"] is not None
    assert get_next_charge_onboarding_candidate(conn) is None

    # Reset un-decides the candidate: back to proposed, reviewed_at cleared, walkable again.
    reset = record_charge_onboarding_decision(conn, candidate["id"], {"action": "reset"})
    assert reset["status"] == "proposed"
    assert reset["reviewed_at"] is None
    assert get_next_charge_onboarding_candidate(conn)["id"] == candidate["id"]


def test_record_decision_supports_multi_step_transitions(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[AMEX], transactions=GAULT_AMEX_ROWS)
    scan_charge_onboarding_candidates(conn)
    cid = get_next_charge_onboarding_candidate(conn)["id"]

    # A candidate can move through several states on one id, and the latest
    # decision is the one recorded.
    assert record_charge_onboarding_decision(conn, cid, {"action": "in_review"})["status"] == "in_review"
    # in_review stays in the active queue.
    assert get_next_charge_onboarding_candidate(conn)["id"] == cid
    assert record_charge_onboarding_decision(conn, cid, {"action": "defer"})["status"] == "deferred"
    revived = record_charge_onboarding_decision(conn, cid, {"action": "in_review", "notes": "re-opened"})
    assert revived["status"] == "in_review"
    assert revived["decision"]["action"] == "in_review"
    assert revived["decision"]["notes"] == "re-opened"
    final = record_charge_onboarding_decision(conn, cid, {"action": "reject"})
    assert final["status"] == "rejected"
    assert get_next_charge_onboarding_candidate(conn) is None


def test_investment_and_other_accounts_become_review_only(tmp_path):
    schwab = ("ACT-pcra", "PCRA Trust ...746 (746)", "Charles Schwab US", "", "USD")
    rows = [
        ("sch-1", "ACT-pcra", "2026-03-15T08:00:00", -250.00, "Advisory Fee", "ADVISORY FEE"),
        ("sch-2", "ACT-pcra", "2026-04-15T08:00:00", -250.00, "Advisory Fee", "ADVISORY FEE"),
    ]
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[schwab], transactions=rows)
    scan_charge_onboarding_candidates(conn)

    candidate = _find(list_charge_onboarding_queue(conn), "advisory_fee")
    assert candidate is not None
    assert candidate["account_class"] == "investment"
    assert candidate["cash_flow_treatment"] == "review_only"
    assert candidate["candidate_type"] == "review_only"


# ---------------------------------------------------------------------------
# Acceptance: direction + thresholds
# ---------------------------------------------------------------------------


def test_inflows_are_excluded_by_default_and_included_on_request(tmp_path):
    rows = [
        ("rein-1", "ACT-chk", "2026-04-17T08:00:00", 196.02, "Anthem", "REMOTE ONLINE DEPOSIT"),
        ("rein-2", "ACT-chk", "2026-05-01T08:00:00", 196.02, "Anthem", "REMOTE ONLINE DEPOSIT"),
        ("rein-3", "ACT-chk", "2026-05-15T08:00:00", 196.02, "Anthem", "REMOTE ONLINE DEPOSIT"),
    ]
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=rows)

    scan_charge_onboarding_candidates(conn)
    assert _find(list_charge_onboarding_queue(conn), "anthem") is None

    scan_charge_onboarding_candidates(conn, options={"include_inflows": True})
    inflow = _find(list_charge_onboarding_queue(conn), "anthem")
    assert inflow is not None
    assert inflow["direction"] == "inflow"
    assert inflow["candidate_type"] == "inflow"


def test_single_occurrence_merchants_are_not_candidates_by_default(tmp_path):
    rows = [("one-1", "ACT-chk", "2026-04-17T08:00:00", -42.00, "One Off Store", "ONE OFF")]
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=rows)
    scan_charge_onboarding_candidates(conn)
    assert list_charge_onboarding_queue(conn) == []


def test_high_variance_discretionary_spend_is_variable_spend_and_deprioritized(tmp_path):
    grocery = [
        ("wf-1", "ACT-amex", "2026-01-05T08:00:00", -12.00, "Whole Foods", "WHOLE FOODS"),
        ("wf-2", "ACT-amex", "2026-02-05T08:00:00", -45.00, "Whole Foods", "WHOLE FOODS"),
        ("wf-3", "ACT-amex", "2026-03-05T08:00:00", -130.00, "Whole Foods", "WHOLE FOODS"),
        ("wf-4", "ACT-amex", "2026-04-05T08:00:00", -277.00, "Whole Foods", "WHOLE FOODS"),
    ]
    conn = _seed_source_db(
        tmp_path / "src.sqlite",
        accounts=[AMEX, CHECKING],
        transactions=grocery + NYT_CHECKING_ROWS,
    )
    scan_charge_onboarding_candidates(conn)

    queue = list_charge_onboarding_queue(conn)
    wf = _find(queue, "whole_foods")
    nyt = _find(queue, "new_york_times")
    assert wf is not None
    assert wf["candidate_type"] == "variable_spend"
    assert wf["proposed_amount_policy"]["method"] == "needs_review"
    # A ~$30 fixed subscription should still outrank ~$116-average chaotic spend,
    # because variable discretionary spend is not a schedulable obligation.
    assert nyt["priority_score"] > wf["priority_score"]


def test_internal_transfers_are_flagged_and_deprioritized(tmp_path):
    rows = [
        ("tv-1", "ACT-chk", "2026-03-10T08:00:00", -260.00, "Transfer to Venmo", "TRANSFER TO VENMO"),
        ("tv-2", "ACT-chk", "2026-03-17T08:00:00", -200.00, "Transfer to Venmo", "TRANSFER TO VENMO"),
        ("tv-3", "ACT-chk", "2026-03-24T08:00:00", -337.00, "Transfer to Venmo", "TRANSFER TO VENMO"),
    ]
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=rows)
    scan_charge_onboarding_candidates(conn)

    candidate = _find(list_charge_onboarding_queue(conn), "transfer_to_venmo")
    assert candidate is not None
    assert candidate["candidate_type"] == "internal_transfer"


def test_existing_obligation_match_is_precise(tmp_path):
    volvo = [
        ("volvo-1", "ACT-chk", "2026-05-08T08:00:00", -580.84, "Volvo Car Fin Auto Finan Web", "VOLVO CAR FINANCIAL"),
        ("volvo-2", "ACT-chk", "2026-06-08T08:00:00", -580.84, "Volvo Car Fin Auto Finan Web", "VOLVO CAR FINANCIAL"),
    ]
    conn = _seed_source_db(
        tmp_path / "src.sqlite",
        accounts=[AMEX, CHECKING],
        transactions=volvo + GAULT_AMEX_ROWS,
    )
    # Existing canonical obligations the scanner might (wrongly) link against.
    for obligation_id, name, kind in [
        ("amex_personal_loan_autopay", "Amex Personal Loan autopay", "loan"),
        ("gault_card_spend_estimates", "Gault card spend estimates", "card_spend_input"),
    ]:
        apply_obligation_instances(
            conn,
            obligation={"id": obligation_id, "name": name, "kind": kind, "status": "active", "source": "seed"},
            instances=[],
        )

    scan_charge_onboarding_candidates(conn)
    queue = list_charge_onboarding_queue(conn)

    # "...Auto Finan..." must NOT be linked to an autopay obligation.
    volvo_candidate = _find(queue, "volvo_car_fin_auto_finan_web")
    assert volvo_candidate["existing_obligation_id"] is None
    # Gault is a genuine distinctive-token match.
    gault_candidate = _find(queue, "gault_energy")
    assert gault_candidate["existing_obligation_id"] == "gault_card_spend_estimates"


def test_same_merchant_on_card_and_checking_yields_separate_candidates(tmp_path):
    rows = GAULT_AMEX_ROWS + [
        ("gault-chk-1", "ACT-chk", "2025-12-03T08:00:00", -283.25, "Gault Energy", "GAULT ENERGY & H PURCHASE WEB"),
        ("gault-chk-2", "ACT-chk", "2025-10-30T08:00:00", -240.00, "Gault Energy", "GAULT ENERGY & H PURCHASE WEB"),
    ]
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[AMEX, CHECKING], transactions=rows)
    scan_charge_onboarding_candidates(conn)

    queue = list_charge_onboarding_queue(conn, include_resolved=True)
    card = _find(queue, "gault_energy", treatment="card_statement_input")
    checking = _find(queue, "gault_energy", treatment="direct_checking")
    assert card is not None
    assert checking is not None
    assert card["id"] != checking["id"]


# ---------------------------------------------------------------------------
# Apply slice: candidate -> canonical obligation
# ---------------------------------------------------------------------------


def _accept(conn, merchant_key, **kw):
    candidate = _find(list_charge_onboarding_queue(conn, include_resolved=True), merchant_key, **kw)
    record_charge_onboarding_decision(conn, candidate["id"], {"action": "accept"})
    return candidate["id"]


def test_accept_decision_moves_to_accepted_and_leaves_active_queue(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[AMEX], transactions=GAULT_AMEX_ROWS)
    scan_charge_onboarding_candidates(conn)
    candidate = get_next_charge_onboarding_candidate(conn)

    accepted = record_charge_onboarding_decision(conn, candidate["id"], {"action": "accept"})
    assert accepted["status"] == "accepted"
    assert accepted["reviewed_at"] is not None
    # Accepted is decided (pending apply), so it is off the active walk.
    assert get_next_charge_onboarding_candidate(conn) is None


def test_apply_requires_accepted_candidate(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[AMEX], transactions=GAULT_AMEX_ROWS)
    scan_charge_onboarding_candidates(conn)
    candidate = get_next_charge_onboarding_candidate(conn)

    # Still 'proposed' -> apply is refused until it is accepted.
    with pytest.raises(ValueError):
        apply_charge_onboarding_candidate(conn, candidate["id"], start_date="2026-07-01")


def test_preview_apply_does_not_write(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=EVERSOURCE_CHECKING_ROWS)
    scan_charge_onboarding_candidates(conn)
    cid = _accept(conn, "eversource_energy")

    before = conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]
    plan = preview_charge_onboarding_apply(conn, cid, start_date="2026-07-01", through_date="2026-09-30")
    after = conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]

    assert after == before  # preview wrote nothing
    assert plan["obligation"]["id"] == "onboarded_eversource_energy_checking"
    assert [i["due_date"] for i in plan["instances"]] == ["2026-07-28", "2026-08-28", "2026-09-28"]
    assert all(i["amount"] == 115.87 for i in plan["instances"])


def test_apply_creates_obligation_and_dated_instances(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=EVERSOURCE_CHECKING_ROWS)
    scan_charge_onboarding_candidates(conn)
    cid = _accept(conn, "eversource_energy")

    result = apply_charge_onboarding_candidate(conn, cid, start_date="2026-07-01", through_date="2026-09-30")
    assert result["status"] == "applied"
    assert result["obligation_id"] == "onboarded_eversource_energy_checking"
    assert result["instances_created"] == 3
    assert result["instances_updated"] == 0

    obligation = conn.execute(
        "SELECT kind, status FROM obligations WHERE id = 'onboarded_eversource_energy_checking'"
    ).fetchone()
    assert obligation["kind"] == "bill"
    instances = conn.execute(
        "SELECT due_date, amount, direction, cash_flow_treatment FROM obligation_instances "
        "WHERE obligation_id = 'onboarded_eversource_energy_checking' ORDER BY due_date"
    ).fetchall()
    assert [(r["due_date"], r["amount"], r["direction"], r["cash_flow_treatment"]) for r in instances] == [
        ("2026-07-28", 115.87, "outflow", "direct_checking"),
        ("2026-08-28", 115.87, "outflow", "direct_checking"),
        ("2026-09-28", 115.87, "outflow", "direct_checking"),
    ]
    # Candidate is now applied and off the active queue.
    assert get_next_charge_onboarding_candidate(conn) is None
    assert conn.execute(
        "SELECT status FROM charge_onboarding_candidates WHERE id = ?", (cid,)
    ).fetchone()[0] == "applied"


def test_applied_direct_checking_obligation_projects_into_cash_flow(tmp_path):
    conn = _seed_source_db(
        tmp_path / "src.sqlite", accounts=[CHECKING], transactions=EVERSOURCE_CHECKING_ROWS, with_balances=True
    )
    scan_charge_onboarding_candidates(conn)
    cid = _accept(conn, "eversource_energy")
    apply_charge_onboarding_candidate(conn, cid, start_date="2026-07-01", through_date="2026-09-30")

    accounts = [
        {
            "account_id": "ACT-chk",
            "account_name": "PREMIER PLUS CKG (XXXX)",
            "kind": "checking",
            "available": 1000.0,
            "recorded_at": "2026-06-20T00:00:00+00:00",
        }
    ]
    projections, _ = build_cash_flow_projections(conn, accounts=accounts, windows=[120], start_date=date(2026, 7, 1))
    event_obligations = [e["obligation_id"] for e in projections[0]["events"]]
    assert event_obligations == ["onboarded_eversource_energy_checking"] * 3
    # 1000 - 3 * 115.87
    assert projections[0]["ending_balance"] == 652.39


def test_applied_card_statement_input_is_excluded_from_checking_but_listed(tmp_path):
    conn = _seed_source_db(
        tmp_path / "src.sqlite", accounts=[AMEX], transactions=GAULT_AMEX_ROWS, with_balances=True
    )
    scan_charge_onboarding_candidates(conn)
    cid = _accept(conn, "gault_energy")
    result = apply_charge_onboarding_candidate(conn, cid, start_date="2026-06-01", through_date="2026-10-31")

    obligation_id = result["obligation_id"]
    assert obligation_id == "onboarded_gault_energy_card"

    # Card-statement-input instances never reduce checking directly.
    accounts = [
        {
            "account_id": "ACT-chk",
            "account_name": "PREMIER PLUS CKG (XXXX)",
            "kind": "checking",
            "available": 5000.0,
            "recorded_at": "2026-06-20T00:00:00+00:00",
        }
    ]
    projections, _ = build_cash_flow_projections(conn, accounts=accounts, windows=[200], start_date=date(2026, 6, 1))
    assert projections[0]["events"] == []
    assert projections[0]["ending_balance"] == 5000.0

    # But they ARE visible as statement inputs feeding the Amex statement payment.
    inputs = list_statement_input_estimates(conn, target_obligation_id="amex_statement_payment")
    assert len(inputs) >= 1
    assert all(i["obligation_id"] == obligation_id for i in inputs)
    assert all(i["cash_flow_treatment"] == "card_statement_input" for i in inputs)


def test_apply_is_idempotent(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=EVERSOURCE_CHECKING_ROWS)
    scan_charge_onboarding_candidates(conn)
    cid = _accept(conn, "eversource_energy")

    first = apply_charge_onboarding_candidate(conn, cid, start_date="2026-07-01", through_date="2026-09-30")
    # Re-applying the same window updates in place rather than duplicating.
    second = apply_charge_onboarding_candidate(
        conn, cid, start_date="2026-07-01", through_date="2026-09-30", require_accepted=False
    )
    assert first["instances_created"] == 3
    assert second["instances_created"] == 0
    assert second["instances_updated"] == 3
    count = conn.execute(
        "SELECT COUNT(*) FROM obligation_instances WHERE obligation_id = 'onboarded_eversource_energy_checking'"
    ).fetchone()[0]
    assert count == 3


def test_preview_matches_apply(tmp_path):
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=NYT_CHECKING_ROWS)
    scan_charge_onboarding_candidates(conn)
    cid = _accept(conn, "new_york_times")

    plan = preview_charge_onboarding_apply(conn, cid, start_date="2026-07-01", through_date="2026-10-31")
    apply_charge_onboarding_candidate(conn, cid, start_date="2026-07-01", through_date="2026-10-31")

    applied = conn.execute(
        "SELECT id FROM obligation_instances WHERE obligation_id = ? ORDER BY due_date",
        (plan["obligation"]["id"],),
    ).fetchall()
    assert [i["id"] for i in plan["instances"]] == [r["id"] for r in applied]
    # New York Times settled at a fixed $30.30 monthly subscription.
    assert all(i["amount"] == 30.30 for i in plan["instances"])


def test_apply_through_date_is_exclusive_so_preview_matches_projection(tmp_path):
    # Eversource is monthly on day 28. A through_date landing exactly on a due
    # date must exclude that date, matching the cash-flow window (due < end), so
    # nothing is previewed/applied that would not also project.
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=EVERSOURCE_CHECKING_ROWS)
    scan_charge_onboarding_candidates(conn)
    cid = _accept(conn, "eversource_energy")

    on_boundary = preview_charge_onboarding_apply(conn, cid, start_date="2026-07-01", through_date="2026-08-28")
    assert [i["due_date"] for i in on_boundary["instances"]] == ["2026-07-28"]  # 08-28 excluded

    just_past = preview_charge_onboarding_apply(conn, cid, start_date="2026-07-01", through_date="2026-08-29")
    assert [i["due_date"] for i in just_past["instances"]] == ["2026-07-28", "2026-08-28"]  # 08-28 included


def test_apply_inflow_projects_as_income(tmp_path):
    rows = [
        ("rein-1", "ACT-chk", "2026-04-17T08:00:00", 196.02, "Anthem", "REMOTE ONLINE DEPOSIT"),
        ("rein-2", "ACT-chk", "2026-05-17T08:00:00", 196.02, "Anthem", "REMOTE ONLINE DEPOSIT"),
        ("rein-3", "ACT-chk", "2026-06-17T08:00:00", 196.02, "Anthem", "REMOTE ONLINE DEPOSIT"),
    ]
    conn = _seed_source_db(tmp_path / "src.sqlite", accounts=[CHECKING], transactions=rows)
    scan_charge_onboarding_candidates(conn, options={"include_inflows": True})
    cid = _accept(conn, "anthem")
    apply_charge_onboarding_candidate(conn, cid, start_date="2026-07-01", through_date="2026-09-30")

    accounts = [
        {
            "account_id": "ACT-chk",
            "account_name": "PREMIER PLUS CKG (XXXX)",
            "kind": "checking",
            "available": 100.0,
            "recorded_at": "2026-06-20T00:00:00+00:00",
        }
    ]
    projections, _ = build_cash_flow_projections(conn, accounts=accounts, windows=[120], start_date=date(2026, 7, 1))
    # Inflows raise the projected balance (income behavior): 100 + 3 * 196.02.
    assert all(e["direction"] == "inflow" for e in projections[0]["events"])
    assert projections[0]["ending_balance"] == 688.06
