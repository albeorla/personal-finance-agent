"""Tests for the standardized task next-action line (``#8``).

Every surface builder must end its body with one dated action line, rendered by
``render_next_action`` in the documented shape
``Action: {verb} ${amount}{(est)} {from|to} {account} by {by}.``, and must always
set ``due_date`` from the same ``by`` value (so a surfaced task is never dateless).

These are hermetic: each test seeds a SQLite db (or, for the goal builder, injects
a controlled ``list_goals``) and calls ONE builder directly, asserting the exact
action line and that ``due_date`` is set.
"""

import sqlite3
from datetime import date, timedelta

import pytest

from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent import surface_queue
from financial_agent.surface_queue import (
    NextAction,
    _estimate_review_surface_items,
    _followup_surface_items,
    _goal_behind_surface_items,
    _manual_obligation_due_surface_items,
    _onboarding_digest_surface_item,
    _operating_account_name,
    _snapshot_due_surface_items,
    render_next_action,
)

AS_OF = date(2026, 6, 24)
_NOW = "2026-06-01T00:00:00+00:00"


def _db(path):
    """Fresh db with app + source tables, ready to seed."""

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
        """
    )
    return conn


def _checking(conn):
    conn.execute(
        "INSERT INTO accounts (id,name,org,kind,currency) "
        "VALUES ('chk','Checking 4321','Chase','checking','USD')"
    )


def _seed_manual_bill(conn, oid, name, *, due_date, amount, amount_discretionary=False):
    apply_obligation_instances(
        conn,
        obligation={
            "id": oid, "name": name, "kind": "housing", "status": "active",
            "source": "seed", "autopay": False,
            "amount_discretionary": amount_discretionary,
        },
        instances=[
            {"id": f"{oid}:{due_date}", "due_date": due_date, "amount": amount, "status": "expected", "source": "seed"},
        ],
    )


def _seed_estimate(conn, oid, name, *, due_date, amount, review_after):
    apply_obligation_instances(
        conn,
        obligation={
            "id": oid, "name": name, "kind": "utility", "status": "active",
            "source": "seed", "autopay": True,
        },
        instances=[
            {
                "id": f"{oid}:{due_date}", "due_date": due_date, "amount": amount, "source": "seed",
                "amount_status": "estimated", "review_after": review_after, "estimation_method": "average",
            },
        ],
    )


def _seed_stale_snapshot(conn, *, account_id, name, days_old):
    recorded = (AS_OF - timedelta(days=days_old)).isoformat()
    conn.execute(
        "INSERT INTO accounts (id,name,org,kind,currency) VALUES (?,?,?,'','USD')",
        (account_id, name, "Apple Card (Updated Monthly)"),
    )
    conn.execute(
        "INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) "
        "VALUES (?,?,?,?,'manual')",
        (account_id, -1200.0, -1200.0, f"{recorded}T12:00:00+00:00"),
    )


def _seed_candidate(conn, cid, display_name, *, status="discovered"):
    conn.execute(
        "INSERT INTO charge_onboarding_candidates "
        "(id, merchant_key, display_name, direction, status, evidence_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (cid, display_name.lower(), display_name, "outflow", status, 1, _NOW, _NOW),
    )


# --- render_next_action unit ------------------------------------------------


def test_render_next_action_unit():
    """The renderer's full contract in one place: shape, optional pieces, the
    estimate/modeled-min framings, the direction-driven preposition, and the
    required-field guards."""

    # Outflow money move: default preposition is "from".
    assert (
        render_next_action(
            NextAction(verb="Pay", by="2026-06-26", amount=3000.0, direction="outflow", account="Checking 4321")
        )
        == "Action: Pay $3,000.00 from Checking 4321 by 2026-06-26."
    )
    # Inflow flips the preposition to "to".
    assert (
        render_next_action(
            NextAction(verb="Move", by="2026-07-30", amount=475.0, direction="inflow", account='"Emergency fund"')
        )
        == 'Action: Move $475.00 to "Emergency fund" by 2026-07-30.'
    )
    # An explicit preposition overrides direction (e.g. "in the review").
    assert (
        render_next_action(
            NextAction(verb="Triage", by="2026-06-26", account="the charge-onboarding review", preposition="in")
        )
        == "Action: Triage in the charge-onboarding review by 2026-06-26."
    )
    # An estimated amount reads as an estimate, never a fixed figure.
    assert (
        render_next_action(
            NextAction(verb="Pay", by="2026-06-26", amount=140.0, direction="outflow", account="X", amount_status="estimated")
        )
        == "Action: Pay $140.00 (est) from X by 2026-06-26."
    )
    # A discretionary amount renders the modeled-min framing instead.
    assert (
        render_next_action(
            NextAction(verb="Decide amount + pay", by="2026-06-28", amount=196.58, direction="outflow", account="X", modeled_min=True)
        )
        == "Action: Decide amount + pay (modeled min ~$196.58) from X by 2026-06-28."
    )
    # No amount -> no money clause at all (never a bare "$-").
    line = render_next_action(NextAction(verb="Call Anthem", by="2026-06-24"))
    assert line == "Action: Call Anthem by 2026-06-24."
    assert "$" not in line

    # verb and by are required.
    with pytest.raises(ValueError):
        render_next_action(NextAction(verb="", by="2026-06-24"))
    with pytest.raises(ValueError):
        render_next_action(NextAction(verb="Pay", by=""))


# --- one focused test per builder type -------------------------------------


def test_manual_obligation_due_builder_emits_dated_action_and_due_date(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _seed_manual_bill(conn, "rent_check", "Rent check", due_date="2026-06-28", amount=-3000.0)
    conn.commit()

    acct = _operating_account_name(conn)
    items = _manual_obligation_due_surface_items(conn, AS_OF)
    assert len(items) == 1
    item = items[0]
    # Suggested Todoist due is 2 days before the bill; due_date mirrors it.
    assert item["due_date"] == "2026-06-26"
    assert item["description"].endswith(
        f"Action: Pay $3,000.00 from {acct} by 2026-06-26."
    )


def test_manual_obligation_due_builder_discretionary_uses_modeled_min(tmp_path):
    conn = _db(tmp_path / "t.db")
    _checking(conn)
    _seed_manual_bill(
        conn, "apple_card_minimum_payments", "Apple Card payment",
        due_date="2026-06-30", amount=-196.58, amount_discretionary=True,
    )
    conn.commit()

    acct = _operating_account_name(conn)
    items = _manual_obligation_due_surface_items(conn, date(2026, 6, 28))
    assert len(items) == 1
    item = items[0]
    assert item["due_date"] == "2026-06-28"
    assert item["description"].endswith(
        f"Action: Decide amount + pay (modeled min ~$196.58) from {acct} by 2026-06-28."
    )


def test_onboarding_digest_builder_emits_dated_action_and_due_date(tmp_path):
    conn = _db(tmp_path / "t.db")
    _seed_candidate(conn, "cand_acme", "Acme")
    conn.commit()

    by = (AS_OF + timedelta(days=2)).isoformat()  # _ONBOARDING_TRIAGE_LEAD_DAYS
    items = _onboarding_digest_surface_item(conn, AS_OF)
    assert len(items) == 1
    item = items[0]
    assert item["due_date"] == by  # was previously missing
    assert item["description"].endswith(
        f"Action: Triage in the charge-onboarding review by {by}."
    )


def test_followup_builder_emits_dated_action_and_due_date(tmp_path):
    from financial_agent.follow_ups import capture_followup

    conn = _db(tmp_path / "t.db")
    capture_followup(conn, text="Call Anthem about reimbursement", surface_when="2026-06-24")
    conn.commit()

    items = _followup_surface_items(conn, AS_OF)
    assert len(items) == 1
    item = items[0]
    assert item["due_date"] == "2026-06-24"
    assert item["description"] == "Action: Call Anthem about reimbursement by 2026-06-24."


def test_goal_behind_builder_emits_dated_action_and_due_date(tmp_path, monkeypatch):
    # A "behind" goal needs real elapsed-pace data; inject a controlled goal so the
    # builder's rendering contract is what is under test, not the pace classifier.
    conn = _db(tmp_path / "t.db")

    def fake_list_goals(_conn, as_of_date):
        return [
            {
                "goal_id": "g1", "name": "Emergency fund", "status": "behind",
                "deadline": "2026-07-30", "required_monthly_rate": 475.0,
                "remaining_amount": 4900.0, "current_progress": 100.0,
                "target_amount": 5000.0,
            }
        ]

    monkeypatch.setattr(surface_queue, "list_goals", fake_list_goals)

    items = _goal_behind_surface_items(conn, AS_OF)
    assert len(items) == 1
    item = items[0]
    assert item["due_date"] == "2026-07-30"  # goal deadline
    # Move the catch-up monthly rate INTO the goal account (inflow -> "to").
    assert item["description"].endswith(
        'Action: Move $475.00 to "Emergency fund" by 2026-07-30.'
    )


def test_estimate_review_builder_emits_dated_action_and_due_date(tmp_path):
    conn = _db(tmp_path / "t.db")
    _seed_estimate(conn, "elec", "Electric bill", due_date="2026-06-28", amount=-140.0, review_after="2026-06-20")
    conn.commit()

    items = _estimate_review_surface_items(conn, AS_OF)
    assert len(items) == 1
    item = items[0]
    assert item["due_date"] == "2026-06-24"  # actionable now (review_after has passed)
    assert item["description"].endswith(
        "Action: Refresh estimate from the statement by 2026-06-24."
    )


def test_snapshot_due_builder_emits_dated_action_and_due_date(tmp_path):
    conn = _db(tmp_path / "t.db")
    _seed_stale_snapshot(conn, account_id="apple", name="Apple Card", days_old=35)
    conn.commit()

    items = _snapshot_due_surface_items(conn, AS_OF)
    assert len(items) == 1
    item = items[0]
    assert item["due_date"] == "2026-06-24"  # stale now -> due today
    assert item["description"].endswith(
        "Action: Update balance from the Apple Card portal by 2026-06-24."
    )
