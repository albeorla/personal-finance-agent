"""Tests for the daily surface queue (one read for the surfacing job).

Composition over grounded helpers; no network. Each test seeds a SQLite db and
asserts the aggregated, prioritized queue without touching live data.
"""

import sqlite3
from datetime import datetime, timedelta

from financial_agent.guardrails import CASH_FLOOR
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent.surface_queue import (
    SNAPSHOT_STALE_DAYS,
    _finance_status_surface_item,
    get_surface_queue,
)

AS_OF = "2026-06-24"


def test_finance_status_surface_item_is_singleton():
    headline = (
        "YELLOW: cushion is thin | trough sensitivity (60d): low point $500 "
        "could land between ~$300 and ~$700"
    )

    assert _finance_status_surface_item(None) == []
    assert _finance_status_surface_item(headline) == [
        {
            "surface_key": "finance-status",
            "content": "Finance status",
            "description": headline,
        }
    ]


def _recent_iso():
    """A timestamp a few minutes ago: a fresh daily-run heartbeat that is not stale
    against the live wall clock (job-health measures real elapsed hours)."""

    return (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()


def _db(path):
    """Fresh db with both app and source tables, ready to seed."""

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT
        );
        CREATE TABLE IF NOT EXISTS balance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, balance REAL,
            available REAL, recorded_at TEXT, source TEXT
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,
            amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
            mode TEXT, accounts_seen INT, transactions_inserted INT,
            transactions_updated INT, error TEXT
        );
        """
    )
    return conn


def _checking(conn, *, available=9000.0, recorded_at="2026-06-23T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO accounts (id,name,org,kind,currency) VALUES ('chk','Checking 4321','Chase','checking','USD')"
    )
    conn.execute(
        "INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) VALUES ('chk',?,?,?,'simplefin')",
        (available, available, recorded_at),
    )


def _fresh_sync(conn):
    # A recent sync keeps the window-age guardrail quiet so it does not add noise.
    # The window-age guardrail measures real elapsed hours against the wall clock,
    # so finished_at must be anchored to "now" (a few minutes ago), not a fixed
    # calendar date -- otherwise the seed silently ages past the 24h freshness bar
    # as the suite runs on a later day and trips a spurious guardrail item.
    fresh = _recent_iso()
    conn.execute(
        "INSERT INTO sync_runs (started_at,finished_at,mode,accounts_seen,transactions_inserted,transactions_updated,error) "
        "VALUES (?,?,'i',1,0,0,NULL)",
        (fresh, fresh),
    )
    # A recent successful daily background run is the job-health heartbeat: a freshly
    # synced system has, by definition, just run the daily job, so the stale-job
    # alert stays quiet. finished_at is the heartbeat timestamp.
    conn.execute(
        "INSERT INTO background_runs (id,trace_id,run_type,trigger_type,status,as_of_date,started_at,finished_at,created_at) "
        "VALUES ('run_fresh','trace_fresh','daily_sync','manual','succeeded',?,?,?,?)",
        (AS_OF, _recent_iso(), _recent_iso(), _recent_iso()),
    )


def _seed_obligation(conn, oid, name, kind, instance, *, autopay=True):
    apply_obligation_instances(
        conn,
        obligation={
            "id": oid, "name": name, "kind": kind, "status": "active",
            "source": "seed", "autopay": autopay,
        },
        instances=[instance],
    )


def _seed_manual_bill(conn, oid, name, *, due_date, amount, status="expected", autopay=False, amount_discretionary=False):
    """A bill that needs a human action (autopay=False by default)."""

    apply_obligation_instances(
        conn,
        obligation={
            "id": oid, "name": name, "kind": "housing", "status": "active",
            "source": "seed", "autopay": autopay,
            "amount_discretionary": amount_discretionary,
        },
        instances=[
            {"id": f"{oid}:{due_date}", "due_date": due_date, "amount": amount, "status": status, "source": "seed"},
        ],
    )


def _seed_needs_review_match(conn, oid, name, *, due_date, amount, txn_id):
    _seed_obligation(conn, oid, name, "card_paydown", {"id": f"{oid}:{due_date}", "due_date": due_date, "amount": amount, "source": "seed"})
    conn.execute(
        "INSERT INTO transaction_obligation_matches (obligation_instance_id,transaction_id,match_type,match_score,created_at,updated_at) "
        "VALUES (?,?, 'needs_review',0.65,'x','x')",
        (f"{oid}:{due_date}", txn_id),
    )


def _seed_estimate(conn, oid, name, *, due_date, amount, review_after):
    _seed_obligation(
        conn, oid, name, "utility",
        {
            "id": f"{oid}:{due_date}", "due_date": due_date, "amount": amount, "source": "seed",
            "amount_status": "estimated", "review_after": review_after, "estimation_method": "average",
        },
    )


def _seed_stale_snapshot(conn, *, account_id, name, days_old, source="manual"):
    from datetime import date, timedelta

    recorded = (date.fromisoformat(AS_OF) - timedelta(days=days_old)).isoformat()
    conn.execute(
        "INSERT INTO accounts (id,name,org,kind,currency) VALUES (?,?,?,'','USD')",
        (account_id, name, "Apple Card (Updated Monthly)"),
    )
    conn.execute(
        "INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) VALUES (?,?,?,?,?)",
        (account_id, -1200.0, -1200.0, f"{recorded}T12:00:00+00:00", source),
    )


# --- per-source coverage ---------------------------------------------------


def test_match_confirmation_items_from_reconciliation_review(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_needs_review_match(conn, "ap", "Apple paydown", due_date="2026-06-10", amount=-300.0, txn_id="TRN-x")
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    matches = [i for i in q["items"] if i["type"] == "match_confirmation"]
    assert len(matches) == 1
    item = matches[0]
    assert "Apple paydown" in item["message"] and "TRN-x" in item["message"]
    assert item["suggested_todoist_due"] == "today"
    assert item["severity"] == "high"
    assert item["evidence"]["transaction_id"] == "TRN-x"
    assert item["evidence"]["obligation_id"] == "ap"


def test_estimate_review_items_from_review_candidates(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_estimate(conn, "elec", "Electric bill", due_date="2026-06-28", amount=-140.0, review_after="2026-06-20")
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    ests = [i for i in q["items"] if i["type"] == "estimate_review"]
    assert len(ests) == 1
    item = ests[0]
    assert "Electric bill" in item["message"] and "2026-06-20" in item["message"]
    assert item["suggested_todoist_due"] == "today"
    assert item["evidence"]["amount_status"] == "estimated"
    assert item["evidence"]["review_after"] == "2026-06-20"


def test_estimate_not_surfaced_when_review_after_in_future(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_estimate(conn, "elec", "Electric bill", due_date="2026-07-28", amount=-140.0, review_after="2026-07-20")
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    assert not [i for i in q["items"] if i["type"] == "estimate_review"]


def test_large_estimate_is_high_severity(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_estimate(conn, "amex", "Amex statement", due_date="2026-06-28", amount=-2500.0, review_after="2026-06-20")
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    item = next(i for i in q["items"] if i["type"] == "estimate_review")
    assert item["severity"] == "high"


def test_snapshot_refresh_items_from_stale_balance_snapshots(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_stale_snapshot(conn, account_id="apple", name="Apple Card", days_old=35)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    snaps = [i for i in q["items"] if i["type"] == "snapshot_refresh"]
    assert len(snaps) == 1
    item = snaps[0]
    assert "Apple Card" in item["message"] and "35 days old" in item["message"]
    assert item["suggested_todoist_due"] == "today"
    assert item["evidence"]["account_id"] == "apple"
    assert item["evidence"]["days_old"] == 35


def test_snapshot_staleness_detection_respects_30day_threshold(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_stale_snapshot(conn, account_id="fresh", name="Fresh Card", days_old=SNAPSHOT_STALE_DAYS - 1)
    _seed_stale_snapshot(conn, account_id="stale", name="Stale Card", days_old=SNAPSHOT_STALE_DAYS + 1)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    snap_ids = {i["evidence"]["account_id"] for i in q["items"] if i["type"] == "snapshot_refresh"}
    assert snap_ids == {"stale"}


def test_synced_account_snapshot_not_surfaced_even_if_old(tmp_path):
    # A simplefin (non-manual) snapshot is an actively-synced feed, not a
    # balance-only account, so it is never surfaced for manual refresh.
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_stale_snapshot(conn, account_id="synced", name="Synced Card", days_old=90, source="simplefin")
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    assert not [i for i in q["items"] if i["type"] == "snapshot_refresh"]


def test_guardrail_warning_items_from_guardrails_evaluation(tmp_path):
    conn = _db(tmp_path / "t.db")
    # A near-empty checking account trips the cash-floor guardrail.
    _checking(conn, available=500.0)
    _fresh_sync(conn)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    guards = [i for i in q["items"] if i["type"] == "guardrail_warning"]
    assert any(g["evidence"]["rule_type"] == "cash_floor" for g in guards)
    floor = next(g for g in guards if g["evidence"]["rule_type"] == "cash_floor")
    assert floor["severity"] in {"high", "medium"}
    assert "cash floor" in floor["message"].lower()


def test_suppress_balance_guardrails_drops_cash_floor_keeps_other_items(tmp_path):
    """When the day's sync failed the caller passes suppress_balance_guardrails:
    the cash-floor / drift trip (false on stale balances) is dropped, but
    non-balance items still surface."""

    conn = _db(tmp_path / "t.db")
    # Near-empty checking trips the cash-floor guardrail...
    _checking(conn, available=500.0)
    _fresh_sync(conn)
    # ...and a manual bill due soon is a non-balance item that must still surface.
    _seed_obligation(
        conn, "rent", "Rent check", "housing",
        {"id": "rent:2026-06-26", "due_date": "2026-06-26", "amount": -3000.0, "source": "seed"},
        autopay=0,
    )
    conn.commit()

    # Default: the cash-floor guardrail surfaces.
    base = get_surface_queue(conn, as_of_date=AS_OF)
    assert any(i["evidence"].get("rule_type") == "cash_floor" for i in base["items"] if i["type"] == "guardrail_warning")

    # Suppressed: cash floor / drift are gone, the manual bill stays.
    suppressed = get_surface_queue(conn, as_of_date=AS_OF, suppress_balance_guardrails=True)
    rule_types = {i["evidence"].get("rule_type") for i in suppressed["items"] if i["type"] == "guardrail_warning"}
    assert "cash_floor" not in rule_types and "drift_threshold" not in rule_types
    assert any(i["type"] == "obligation_due" for i in suppressed["items"])


def test_stale_working_balance_suppresses_balance_guardrails_and_adds_confirm_item(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn, available=500.0)
    _fresh_sync(conn)
    conn.commit()

    not_stale = get_surface_queue(
        conn,
        as_of_date=AS_OF,
        working_account_balance_stale={
            "stale": False,
            "account_name": "Checking",
            "balance_age_days": 0,
            "balance_date": AS_OF,
        },
    )
    assert any(
        i["evidence"].get("rule_type") == "cash_floor"
        for i in not_stale["items"]
        if i["type"] == "guardrail_warning"
    )

    stale = get_surface_queue(
        conn,
        as_of_date=AS_OF,
        working_account_balance_stale={
            "stale": True,
            "account_name": "Checking",
            "balance_age_days": 2,
            "balance_date": "2026-06-22",
        },
    )
    rule_types = {
        i["evidence"].get("rule_type")
        for i in stale["items"]
        if i["type"] == "guardrail_warning"
    }
    assert "cash_floor" not in rule_types
    assert "drift_threshold" not in rule_types

    confirm_items = [i for i in stale["items"] if i["type"] == "confirm_live_balance"]
    assert len(confirm_items) == 1
    confirm = confirm_items[0]
    assert confirm["id"] == "confirm-live-balance"
    assert confirm["severity"] == "medium"
    assert "Checking" in confirm["message"]
    assert "2 days old" in confirm["message"]
    assert "2026-06-22" in confirm["message"]
    assert "confirm the live balance" in confirm["message"]


def test_advisory_guardrails_excluded(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    # An interest-bearing debt obligation triggers the advisory avalanche finding.
    _seed_obligation(conn, "loan", "Car loan", "loan", {"id": "loan:2026-07-01", "due_date": "2026-07-01", "amount": -400.0, "source": "seed"})
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    assert not [i for i in q["items"] if i.get("evidence", {}).get("rule_type") == "debt_avalanche"]


def test_unchanged_drift_warning_snoozes_after_first_emission(tmp_path):
    """The same drift-threshold warning must not re-alert daily: once persisted
    and unchanged since an earlier day it is snoozed, until its content changes."""

    from financial_agent.drift import detect_drift

    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    # A missing $3,000 rent past grace pushes total drift over the $200 threshold.
    _seed_obligation(
        conn, "rent", "Rent check", "housing",
        {"id": "rent:2026-06-01", "due_date": "2026-06-01", "amount": -3000.0, "source": "seed"},
    )
    conn.commit()

    def drift_items():
        q = get_surface_queue(conn, as_of_date=AS_OF)
        return [
            i for i in q["items"]
            if i["type"] == "guardrail_warning" and i["evidence"]["rule_type"] == "drift_threshold"
        ]

    # Not yet persisted (never emitted): surfaces.
    assert drift_items()

    # Persisted by the day's sync, content changed "today" (real wall clock,
    # after AS_OF): still surfaces the day it appears.
    detect_drift(conn, as_of_date=AS_OF, persist=True)
    assert drift_items()

    # Simulate the next day: the finding last changed BEFORE as_of -> snoozed.
    conn.execute("UPDATE drift_findings SET updated_at = '2026-06-23T08:00:00+00:00'")
    conn.commit()
    assert not drift_items()

    # The underlying amount changes -> persisted impacts no longer match the
    # live total -> the warning re-surfaces.
    conn.execute("UPDATE obligation_instances SET amount = -3500.0 WHERE id = 'rent:2026-06-01'")
    conn.commit()
    assert drift_items()


def test_goal_review_items_surface_behind_goals(tmp_path):
    from financial_agent.goals import set_goal

    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    # A goal with a deadline soon and no progress is behind / due_soon.
    set_goal(conn, name="Emergency fund", target_amount=5000.0, deadline="2026-06-30")
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    goals = [i for i in q["items"] if i["type"] == "goal_review"]
    assert len(goals) == 1
    assert "Emergency fund" in goals[0]["message"]
    assert goals[0]["evidence"]["status"] in {"behind", "due_soon"}


def test_trough_breach_risk_from_digest_trough_sensitivity(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    conn.commit()

    def trough_items(trough_sensitivity=None):
        kwargs = {"as_of_date": AS_OF}
        if trough_sensitivity is not None:
            kwargs["trough_sensitivity"] = trough_sensitivity
        q = get_surface_queue(conn, **kwargs)
        return [i for i in q["items"] if i["type"] == "trough_breach_risk"]

    base = {
        "lowest_balance": 1200.0,
        "lowest_balance_date": "2026-07-10",
        "high_estimate": 2400.0,
        "drivers": [
            {
                "obligation_name": "Sample Card statement (avg estimate)",
                "amount": 840.0,
                "confidence": "low",
                "downside": 1350.0,
                "balance_only": True,
            }
        ],
    }

    high = trough_items({**base, "low_estimate": -150.0, "breach_risk": True})
    assert len(high) == 1
    assert high[0]["id"] == "trough-breach"
    assert high[0]["severity"] == "high"
    assert "Sample Card statement (avg estimate)" in high[0]["message"]
    assert "-150.00" in high[0]["message"]

    medium = trough_items({**base, "low_estimate": 840.0, "breach_risk": True})
    assert len(medium) == 1
    assert medium[0]["severity"] == "medium"

    assert not trough_items({**base, "low_estimate": CASH_FLOOR + 100.0, "breach_risk": True})
    assert not trough_items({**base, "low_estimate": -150.0, "breach_risk": False})
    assert not trough_items()


# --- prioritization & compactness ------------------------------------------


def test_queue_sorted_by_severity_then_type(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn, available=500.0)  # cash-floor guardrail (high in 7d window)
    _fresh_sync(conn)
    _seed_needs_review_match(conn, "ap", "Apple paydown", due_date="2026-06-10", amount=-300.0, txn_id="TRN-x")  # high
    _seed_estimate(conn, "elec", "Electric", due_date="2026-06-28", amount=-140.0, review_after="2026-06-20")  # medium
    _seed_stale_snapshot(conn, account_id="apple", name="Apple Card", days_old=40)  # medium
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    ranks = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    sev_seq = [ranks[i["severity"]] for i in q["items"]]
    assert sev_seq == sorted(sev_seq, reverse=True)
    # Within the high tier, a match confirmation outranks a guardrail.
    highs = [i["type"] for i in q["items"] if i["severity"] == "high"]
    assert highs.index("match_confirmation") < highs.index("guardrail_warning")


def test_limit_parameter_respected(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    for n in range(8):
        _seed_estimate(conn, f"e{n}", f"Bill {n}", due_date="2026-06-28", amount=-50.0, review_after="2026-06-20")
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF, limit=3)
    assert len(q["items"]) == 3
    assert q["total_items"] >= 8
    assert q["returned_items"] == 3


# --- empty & edge cases ----------------------------------------------------


def test_empty_queue_when_no_items(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn, available=9000.0)
    _fresh_sync(conn)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    assert q["total_items"] == 0
    assert q["items"] == []


def test_stale_daily_job_surfaces_high_priority_alert(tmp_path):
    """A stopped daily job is surfaced as a HIGH-priority alert, ranked first."""
    from datetime import datetime, timedelta

    conn = _db(tmp_path / "t.db")
    _checking(conn, available=9000.0)
    # A successful daily run that finished 30h ago: past the 26h staleness bar.
    finished = (datetime.now().astimezone() - timedelta(hours=30)).isoformat()
    conn.execute(
        "INSERT INTO background_runs (id,trace_id,run_type,trigger_type,status,as_of_date,started_at,finished_at,created_at) "
        "VALUES ('run_stale','t','daily_sync','manual','succeeded',?,?,?,?)",
        (AS_OF, finished, finished, finished),
    )
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    stale = [i for i in q["items"] if i["type"] == "stale_job"]
    assert len(stale) == 1
    assert stale[0]["severity"] == "high"
    # It ranks first: a dead job means every other item may be out of date.
    assert q["items"][0]["type"] == "stale_job"


def test_gracefully_skips_when_snapshot_tables_missing(tmp_path):
    # Only app schema; no source tables at all.
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    _seed_needs_review_match(conn, "ap", "Apple paydown", due_date="2026-06-10", amount=-300.0, txn_id="TRN-x")
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    # No crash; the match confirmation still surfaces, snapshots are skipped.
    assert any(i["type"] == "match_confirmation" for i in q["items"])
    assert not [i for i in q["items"] if i["type"] == "snapshot_refresh"]


# --- determinism & schema --------------------------------------------------


def test_same_as_of_date_and_db_produces_same_result(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn, available=500.0)
    _fresh_sync(conn)
    _seed_needs_review_match(conn, "ap", "Apple paydown", due_date="2026-06-10", amount=-300.0, txn_id="TRN-x")
    _seed_stale_snapshot(conn, account_id="apple", name="Apple Card", days_old=40)
    conn.commit()

    first = get_surface_queue(conn, as_of_date=AS_OF)
    second = get_surface_queue(conn, as_of_date=AS_OF)
    assert _strip(first) == _strip(second)


def test_item_drops_after_underlying_data_changes(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_needs_review_match(conn, "ap", "Apple paydown", due_date="2026-06-10", amount=-300.0, txn_id="TRN-x")
    conn.commit()

    before = get_surface_queue(conn, as_of_date=AS_OF)
    assert any(i["id"] == "match:ap:2026-06-10" for i in before["items"])

    # Confirming the match removes it from the reconciliation review set.
    conn.execute("DELETE FROM transaction_obligation_matches WHERE obligation_instance_id = 'ap:2026-06-10'")
    conn.commit()

    after = get_surface_queue(conn, as_of_date=AS_OF)
    assert not any(i["id"] == "match:ap:2026-06-10" for i in after["items"])


def test_every_item_has_required_fields(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn, available=500.0)
    _fresh_sync(conn)
    _seed_needs_review_match(conn, "ap", "Apple paydown", due_date="2026-06-10", amount=-300.0, txn_id="TRN-x")
    _seed_estimate(conn, "elec", "Electric", due_date="2026-06-28", amount=-140.0, review_after="2026-06-20")
    _seed_stale_snapshot(conn, account_id="apple", name="Apple Card", days_old=40)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    seen_ids = set()
    known_types = {"stale_job", "match_confirmation", "estimate_review", "snapshot_refresh", "guardrail_warning", "goal_review"}
    known_sev = {"critical", "high", "medium", "low"}
    for item in q["items"]:
        for field in ("id", "type", "severity", "message", "suggested_todoist_due", "related_ids", "evidence"):
            assert field in item, f"missing {field}"
        assert item["type"] in known_types
        assert item["severity"] in known_sev
        assert item["id"] not in seen_ids
        seen_ids.add(item["id"])


# --- comprehensive scenario ------------------------------------------------


def test_comprehensive_scenario_all_item_types_with_prioritization(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn, available=500.0)  # cash-floor guardrail
    _fresh_sync(conn)
    _seed_needs_review_match(conn, "ap", "Apple paydown", due_date="2026-06-10", amount=-300.0, txn_id="TRN-x")
    _seed_estimate(conn, "elec", "Electric", due_date="2026-06-28", amount=-140.0, review_after="2026-06-20")
    _seed_stale_snapshot(conn, account_id="apple", name="Apple Card", days_old=40)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    types = {i["type"] for i in q["items"]}
    assert {"match_confirmation", "estimate_review", "snapshot_refresh", "guardrail_warning"} <= types

    # The highest-priority item is a high-severity match or guardrail; the lowest
    # tier never outranks it.
    ranks = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    sev_seq = [ranks[i["severity"]] for i in q["items"]]
    assert sev_seq == sorted(sev_seq, reverse=True)
    for item in q["items"]:
        assert item["message"]  # human-readable, non-empty
        assert item["suggested_todoist_due"]


# --- manual obligation due (non-autopay) -----------------------------------


def test_manual_obligation_due_within_window_surfaces_with_stable_key(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    # Rent check (manual) due in 4 days, within the 5-day lead window.
    _seed_manual_bill(conn, "rent_check", "Rent check", due_date="2026-06-28", amount=-3000.0)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    due = [i for i in q["items"] if i["type"] == "obligation_due"]
    assert len(due) == 1
    item = due[0]
    # Stable key dedups + updates in place via the emissions ledger.
    assert item["id"] == "obligation-due:rent_check:2026-06-28"
    assert "Rent check due 2026-06-28" in item["message"]
    assert "$3,000.00" in item["message"] and "(manual)" in item["message"]
    # Suggested Todoist due is ~2 days before the obligation is due.
    assert item["suggested_todoist_due"] == "2026-06-26"
    assert item["evidence"]["autopay"] is False
    assert item["evidence"]["obligation_id"] == "rent_check"
    assert item["evidence"]["days_until_due"] == 4


def test_autopay_obligation_due_in_same_window_does_not_surface(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    # Same window, but autopay=True: it pays itself, so it stays quiet.
    _seed_manual_bill(conn, "spotify", "Spotify", due_date="2026-06-28", amount=-12.0, autopay=True)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    assert not [i for i in q["items"] if i["type"] == "obligation_due"]


def test_manual_obligation_due_beyond_window_does_not_surface(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    # Manual, but due 10 days out: beyond the 5-day lead window.
    _seed_manual_bill(conn, "rent_check", "Rent check", due_date="2026-07-04", amount=-3000.0)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    assert not [i for i in q["items"] if i["type"] == "obligation_due"]


def test_cleared_manual_instance_does_not_surface(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    # Manual + in window, but already cleared/reconciled (status 'paid').
    _seed_manual_bill(conn, "rent_check", "Rent check", due_date="2026-06-28", amount=-3000.0, status="paid")
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    assert not [i for i in q["items"] if i["type"] == "obligation_due"]


def test_manual_due_severity_rises_as_due_date_nears(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_manual_bill(conn, "today_bill", "Due today", due_date=AS_OF, amount=-100.0)
    _seed_manual_bill(conn, "soon_bill", "Due soon", due_date="2026-06-25", amount=-100.0)
    _seed_manual_bill(conn, "later_bill", "Due later", due_date="2026-06-29", amount=-100.0)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    by_id = {i["id"]: i for i in q["items"] if i["type"] == "obligation_due"}
    assert by_id["obligation-due:today_bill:2026-06-24"]["severity"] == "critical"
    assert by_id["obligation-due:soon_bill:2026-06-25"]["severity"] == "high"
    assert by_id["obligation-due:later_bill:2026-06-29"]["severity"] == "medium"


def test_non_discretionary_obligation_renders_fixed_amount_wording(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    # A normal manual bill: the modeled amount IS the amount to pay.
    _seed_manual_bill(conn, "rent_check", "Rent check", due_date="2026-06-28", amount=-3000.0)
    conn.commit()

    q = get_surface_queue(conn, as_of_date=AS_OF)
    item = next(i for i in q["items"] if i["type"] == "obligation_due")
    # Fixed-amount wording: "<name> due <date>: $<amount> (manual)".
    assert item["message"] == "Rent check due 2026-06-28: $3,000.00 (manual)"
    assert "decide amount" not in item["message"]
    assert item["evidence"]["amount_discretionary"] is False


def test_discretionary_obligation_renders_decide_amount_wording(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    # The user decides the amount each time; the modeled figure is only a floor.
    _seed_manual_bill(
        conn, "apple_card_minimum_payments", "Apple Card payment",
        due_date="2026-06-30", amount=-196.58, amount_discretionary=True,
    )
    conn.commit()

    q = get_surface_queue(conn, as_of_date="2026-06-28")
    item = next(i for i in q["items"] if i["type"] == "obligation_due")
    # Decision wording: frames it as decide-amount, with the modeled min as guidance.
    assert item["message"] == (
        "Apple Card payment due 2026-06-30 - decide amount + pay "
        "(modeled min ~$196.58) (manual)"
    )
    assert "decide amount + pay" in item["message"]
    assert item["evidence"]["amount_discretionary"] is True
    # Stable key is unchanged by discretionary framing.
    assert item["id"] == "obligation-due:apple_card_minimum_payments:2026-06-30"


def test_discretionary_obligation_keeps_surface_key_stable(tmp_path):
    # The surface_key must be identical whether or not the obligation is
    # discretionary, so the emissions ledger keeps mapping to the same task.
    from financial_agent.surface_queue import build_surface_items

    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_manual_bill(
        conn, "apple_card_minimum_payments", "Apple Card payment",
        due_date="2026-06-30", amount=-196.58, amount_discretionary=True,
    )
    conn.commit()

    items = build_surface_items(conn, as_of_date="2026-06-28")
    keys = [it["surface_key"] for it in items]
    assert "obligation-due:apple_card_minimum_payments:2026-06-30" in keys


def test_discretionary_item_round_trips_through_surface_to_todoist_idempotently(tmp_path):
    from financial_agent.surface_queue import build_surface_items
    from financial_agent.todoist_outbox import surface_to_todoist

    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_manual_bill(
        conn, "apple_card_minimum_payments", "Apple Card payment",
        due_date="2026-06-30", amount=-196.58, amount_discretionary=True,
    )
    conn.commit()

    spy = _Spy()
    items = build_surface_items(conn, as_of_date="2026-06-28")
    first = surface_to_todoist(
        conn, items, "2026-06-28", write_enabled=True, token="tok", project_id="proj", send_func=spy
    )
    assert first["created"] == 1
    assert len(spy.creates) == 1
    # The task title is a plain action (no date/amount noise); the modeled
    # minimum and due date live in the description. The [fa:<key>] marker MUST
    # survive in the description so reconciliation can adopt the task.
    assert spy.creates[0]["body"]["content"] == "Decide amount + pay Apple Card payment"
    assert (
        "[fa:obligation-due:apple_card_minimum_payments:2026-06-30]"
        in spy.creates[0]["body"]["description"]
    )

    # Re-running with the same items skips: no duplicate create/update.
    items_again = build_surface_items(conn, as_of_date="2026-06-28")
    second = surface_to_todoist(
        conn, items_again, "2026-06-28", write_enabled=True, token="tok", project_id="proj", send_func=spy
    )
    assert second["created"] == 0
    assert second["skipped"] == 1
    assert len(spy.creates) == 1  # still one task; no duplicate


def test_manual_due_item_round_trips_through_surface_to_todoist_idempotently(tmp_path):
    from financial_agent.surface_queue import build_surface_items
    from financial_agent.todoist_outbox import surface_to_todoist

    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _fresh_sync(conn)
    _seed_manual_bill(conn, "rent_check", "Rent check", due_date="2026-06-28", amount=-3000.0)
    conn.commit()

    spy = _Spy()
    items = build_surface_items(conn, as_of_date=AS_OF)
    keys = [it["surface_key"] for it in items]
    assert "obligation-due:rent_check:2026-06-28" in keys

    # First push creates exactly one task.
    first = surface_to_todoist(
        conn, items, AS_OF, write_enabled=True, token="tok", project_id="proj", send_func=spy
    )
    assert first["created"] == 1
    assert len(spy.creates) == 1
    # Title is a plain "Pay <name> $<amount>" action; the due date, account, and
    # manual detail move into the description. The [fa:<key>] marker MUST survive
    # in the description so reconciliation can adopt the task.
    assert spy.creates[0]["body"]["content"] == "Pay Rent check $3,000.00"
    assert (
        "[fa:obligation-due:rent_check:2026-06-28]"
        in spy.creates[0]["body"]["description"]
    )

    # Re-running with the same (unchanged) items skips: no duplicate create/update.
    items_again = build_surface_items(conn, as_of_date=AS_OF)
    second = surface_to_todoist(
        conn, items_again, AS_OF, write_enabled=True, token="tok", project_id="proj", send_func=spy
    )
    assert second["created"] == 0
    assert second["skipped"] == 1
    assert len(spy.creates) == 1  # still one task; no duplicate


class _Spy:
    """Records every HTTP send; assigns incrementing task ids on create.

    Mirrors the spy in test_todoist_emissions so no real network call is made.
    """

    def __init__(self):
        self.calls = []
        self._next = 0

    def __call__(self, token, path, body, **kwargs):
        self.calls.append({"token": token, "path": path, "body": body})
        if path == "/tasks":  # create
            self._next += 1
            return {"id": f"T{self._next}", "url": f"https://todoist.com/showTask?id=T{self._next}"}
        return {}  # update returns empty body in the real client

    @property
    def creates(self):
        return [c for c in self.calls if c["path"] == "/tasks"]

    @property
    def updates(self):
        return [c for c in self.calls if c["path"].startswith("/tasks/")]


def _strip(result):
    """Drop the per-call trace_id so two runs can be compared."""

    return {k: v for k, v in result.items() if k != "trace_id"}
