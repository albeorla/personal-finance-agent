"""Tests for Todoist one-off obligation import (slice G)."""

import sqlite3
from datetime import date

import pytest

from financial_agent.cashflow import build_cash_flow_projections
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent.todoist_input import (
    import_todoist_obligations,
    list_todoist_sync_records,
    resolve_todoist_dedup_conflict,
)


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def _task(tid, content, signed, due, checked=0, is_deleted=0, completed_at=None):
    return {
        "id": tid, "content": content, "signed_amount": signed, "amount_value": abs(signed) if signed is not None else None,
        "amount_direction": (1 if (signed or 0) >= 0 else -1), "due_date": due,
        "checked": checked, "is_deleted": is_deleted, "completed_at": completed_at,
    }


def test_imports_one_off_with_sign_normalization(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    res = import_todoist_obligations(conn, tasks=[
        _task("t-tax", "Pay federal tax $2,969", -2969.0, "2026-04-15"),
        _task("t-in", "Transfer Owner pay to joint", 3781.0, "2026-04-24"),
    ])
    assert res["imported"] == 2

    rows = conn.execute(
        "SELECT obligation_id, due_date, amount, direction, kind FROM obligation_instances oi "
        "JOIN obligations o ON o.id=oi.obligation_id ORDER BY due_date"
    ).fetchall()
    assert [(r["amount"], r["direction"], r["kind"]) for r in rows] == [
        (2969.0, "outflow", "one_off"),
        (3781.0, "inflow", "one_off"),
    ]


def test_missing_amount_or_date_is_skipped_to_needs_review(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    res = import_todoist_obligations(conn, tasks=[
        _task("t-noamt", "Mystery bill", None, "2026-05-01"),
        _task("t-nodate", "Floating task", -50.0, None),
    ])
    assert res["imported"] == 0
    assert res["skipped"] == 2
    assert conn.execute("SELECT COUNT(*) FROM obligation_instances").fetchone()[0] == 0
    statuses = {r["sync_status"] for r in list_todoist_sync_records(conn)}
    assert statuses == {"needs_review_missing_fields"}


def test_dedup_conflict_against_recurring_obligation(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    # An existing recurring obligation that the task resembles.
    apply_obligation_instances(
        conn,
        obligation={"id": "partner_pay", "name": "Partner pay", "kind": "income",
                    "cadence": "biweekly", "status": "active", "source": "seed"},
        instances=[{"id": "partner_pay:2026-07-02", "due_date": "2026-07-02", "amount": 2011.67,
                    "direction": "inflow", "source": "seed"}],
    )
    res = import_todoist_obligations(conn, tasks=[
        _task("t-cait", "Partner pay (July)", 2011.67, "2026-07-03"),
    ])
    assert res["dedup_conflicts"] == 1
    assert res["imported"] == 0
    # No one-off instance was created for the conflicting task.
    assert conn.execute(
        "SELECT COUNT(*) FROM obligation_instances WHERE obligation_id LIKE 'todoist_oneoff_%'"
    ).fetchone()[0] == 0
    rec = list_todoist_sync_records(conn, external_task_id="t-cait")[0]
    assert rec["sync_status"] == "needs_review_dedup_conflict"


def test_distinct_merchant_does_not_false_dedup(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "partner_pay", "name": "Partner pay", "kind": "income",
                    "cadence": "biweekly", "status": "active", "source": "seed"},
        instances=[{"id": "partner_pay:2026-07-02", "due_date": "2026-07-02", "amount": 2000.0,
                    "direction": "outflow", "source": "seed"}],
    )
    # Same amount bucket + date window but a totally different merchant -> import.
    res = import_todoist_obligations(conn, tasks=[
        _task("t-tax", "Pay federal tax", -2000.0, "2026-07-03"),
    ])
    assert res["imported"] == 1
    assert res["dedup_conflicts"] == 0


def test_generic_token_does_not_false_dedup(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "federal_tax", "name": "Federal tax", "kind": "tax",
                    "cadence": "annual", "status": "active", "source": "seed"},
        instances=[{"id": "federal_tax:2026-04-15", "due_date": "2026-04-15", "amount": -2969.0,
                    "direction": "outflow", "source": "seed"}],
    )
    # "State tax" shares only the generic token "tax" -> must NOT be a conflict.
    res = import_todoist_obligations(conn, tasks=[_task("t-state", "State tax payment", -2969.0, "2026-04-16")])
    assert res["dedup_conflicts"] == 0
    assert res["imported"] == 1


def test_small_amounts_do_not_collide(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "gym", "name": "Gym membership", "kind": "subscription",
                    "cadence": "monthly", "status": "active", "source": "seed"},
        instances=[{"id": "gym:2026-06-10", "due_date": "2026-06-10", "amount": -15.0,
                    "direction": "outflow", "source": "seed"}],
    )
    # Same merchant token but a very different small amount -> not the same charge.
    res = import_todoist_obligations(conn, tasks=[_task("t-gym2", "Gym membership annual", -89.0, "2026-06-11")])
    assert res["dedup_conflicts"] == 0
    assert res["imported"] == 1


def test_merge_resolution_returns_sync_status(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "partner_pay", "name": "Partner pay", "kind": "income",
                    "cadence": "biweekly", "status": "active", "source": "seed"},
        instances=[{"id": "partner_pay:2026-07-02", "due_date": "2026-07-02", "amount": 2011.67,
                    "direction": "inflow", "source": "seed"}],
    )
    import_todoist_obligations(conn, tasks=[_task("t-cait", "Partner pay (July)", 2011.67, "2026-07-03")])
    out = resolve_todoist_dedup_conflict(conn, external_task_id="t-cait", decision="merge",
                                         merge_with_obligation_id="partner_pay")
    assert out["sync_status"] == "merged"
    assert out["obligation_id"] == "partner_pay"


def test_idempotent_reimport(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    task = _task("t-tax", "Pay federal tax", -2969.0, "2026-04-15")
    import_todoist_obligations(conn, tasks=[task])
    import_todoist_obligations(conn, tasks=[task])
    assert conn.execute("SELECT COUNT(*) FROM obligation_instances").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM todoist_sync_records").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM action_outbox WHERE action_type='todoist_flag_task'").fetchone()[0] == 1


def test_checked_task_sets_review_after_not_paid(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    import_todoist_obligations(
        conn,
        tasks=[_task("t-chk", "Oil refill spring", -387.0, "2026-05-01", checked=1, completed_at="2026-05-02T00:00:00Z")],
        options={"as_of_date": "2026-05-03"},
    )
    row = conn.execute(
        "SELECT status, review_after FROM obligation_instances WHERE id='todoist_oneoff_t-chk:2026-05-01'"
    ).fetchone()
    assert row["status"] == "expected"  # NOT 'paid'
    assert row["review_after"] == "2026-05-03"
    assert list_todoist_sync_records(conn, external_task_id="t-chk")[0]["checked_in_source"] is True


def test_deleted_task_cancels_instance(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    res = import_todoist_obligations(conn, tasks=[_task("t-del", "Pay federal tax", -2969.0, "2026-04-15", is_deleted=1)])
    assert res["canceled"] == 1
    row = conn.execute("SELECT status FROM obligation_instances WHERE id='todoist_oneoff_t-del:2026-04-15'").fetchone()
    assert row["status"] == "canceled"
    # Canceled tasks are not flagged back to Todoist (the task is gone).
    assert conn.execute("SELECT COUNT(*) FROM action_outbox WHERE action_type='todoist_flag_task'").fetchone()[0] == 0


def test_flag_action_is_dry_run(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    import_todoist_obligations(conn, tasks=[_task("t-tax", "Pay federal tax", -2969.0, "2026-04-15")])
    flag = conn.execute(
        "SELECT idempotency_key, status, dry_run FROM action_outbox WHERE action_type='todoist_flag_task'"
    ).fetchone()
    assert flag["idempotency_key"] == "todoist_flag:t-tax"
    assert flag["status"] == "dry_run"
    assert flag["dry_run"] == 1


def test_resolve_dedup_import_anyway_creates_obligation(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "partner_pay", "name": "Partner pay", "kind": "income",
                    "cadence": "biweekly", "status": "active", "source": "seed"},
        instances=[{"id": "partner_pay:2026-07-02", "due_date": "2026-07-02", "amount": 2011.67,
                    "direction": "inflow", "source": "seed"}],
    )
    import_todoist_obligations(conn, tasks=[_task("t-cait", "Partner pay (July)", 2011.67, "2026-07-03")])
    out = resolve_todoist_dedup_conflict(conn, external_task_id="t-cait", decision="import_anyway")
    assert out["sync_status"] == "imported"
    assert conn.execute(
        "SELECT COUNT(*) FROM obligation_instances WHERE obligation_id='todoist_oneoff_t-cait'"
    ).fetchone()[0] == 1


def test_imported_one_off_projects_into_cash_flow(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    import_todoist_obligations(conn, tasks=[_task("t-tax", "Pay federal tax", -2969.0, "2026-07-10")])
    accounts = [{"account_id": "chk", "account_name": "Checking XXXX", "kind": "checking",
                 "available": 10000.0, "recorded_at": "2026-07-01T00:00:00+00:00"}]
    projections, _ = build_cash_flow_projections(conn, accounts=accounts, windows=[30], start_date=date(2026, 7, 1))
    assert projections[0]["ending_balance"] == 7031.0  # 10000 - 2969
    assert projections[0]["events"][0]["obligation_id"] == "todoist_oneoff_t-tax"


def test_import_log_records_counts(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    import_todoist_obligations(conn, tasks=[
        _task("t1", "Pay federal tax", -2969.0, "2026-04-15"),
        _task("t2", "Mystery", None, "2026-04-16"),
    ])
    log = conn.execute("SELECT tasks_scanned, tasks_imported, tasks_skipped FROM todoist_import_log").fetchone()
    assert (log["tasks_scanned"], log["tasks_imported"], log["tasks_skipped"]) == (2, 1, 1)
