"""Tests for recurring-instance backfill and extension. No network."""

import sqlite3
from datetime import date, timedelta

import pytest

import financial_agent.backfill as backfill
from financial_agent.backfill import backfill_recurring_instances, list_recently_cleared
from financial_agent.background import run_background_sync
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent.verification import (
    COVERAGE_HORIZON_DAYS,
    list_verification_findings,
)


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
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('ACT-chk','PREMIER PLUS CKG (4321)','Chase','checking','USD')")
    conn.executemany(
        "INSERT INTO transactions (id,account_id,posted,amount,payee,description,pending,source) VALUES (?,?,?,?,?,?,0,'simplefin')",
        transactions,
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    conn.commit()
    return conn


def _rent(conn):
    apply_obligation_instances(
        conn,
        obligation={"id": "rent_check", "name": "Rent check", "kind": "housing", "cadence": "monthly", "status": "active", "source": "seed"},
        instances=[{"id": "rent_check:2026-07-03", "due_date": "2026-07-03", "amount": 3000.0, "direction": "outflow", "source": "seed"}],
    )


def _manual_recurring(
    conn,
    obligation_id,
    *,
    due_date,
    cadence="monthly",
    status="expected",
    source="obligations_yaml_manual",
    amount=125.0,
    active_until=None,
    instance_id=None,
):
    apply_obligation_instances(
        conn,
        obligation={
            "id": obligation_id,
            "name": obligation_id,
            "kind": "bill",
            "cadence": cadence,
            "status": "active",
            "source": "obligations_yaml_manual",
            "active_until": active_until,
        },
        instances=[
            {
                "id": instance_id or f"{obligation_id}:{due_date}",
                "due_date": due_date,
                "amount": amount,
                "direction": "outflow",
                "status": status,
                "source": source,
            }
        ],
    )


def test_backfill_creates_past_instances_and_reconciles_exact_rent(tmp_path):
    conn = _db(tmp_path / "b.sqlite", transactions=[
        ("c1", "ACT-chk", "2026-06-03T08:00:00", -3000.0, "Check #1229", ""),
        ("c2", "ACT-chk", "2026-05-04T08:00:00", -3000.0, "Check #1227", ""),
    ])
    _rent(conn)
    res = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90)
    assert res["instances_created"] >= 2  # ~2026-06-03 and 2026-05-03 fall in the trailing 90d
    # the future instance is untouched, and past instances exist
    past = conn.execute("SELECT due_date FROM obligation_instances WHERE obligation_id='rent_check' AND due_date < '2026-06-21' ORDER BY due_date").fetchall()
    assert len(past) >= 2
    # exact $3000 checks reconciled -> show as cleared
    cleared = list_recently_cleared(conn, as_of_date="2026-06-21")
    assert any(c["obligation_name"] == "Rent check" and c["cleared"] for c in cleared)


def test_backfill_is_idempotent(tmp_path):
    conn = _db(tmp_path / "b.sqlite", transactions=[("c1", "ACT-chk", "2026-06-03T08:00:00", -3000.0, "Check #1229", "")])
    _rent(conn)
    first = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90)["instances_created"]
    second = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90)["instances_created"]
    assert first >= 1 and second == 0  # nothing new on the second pass


def test_backfill_skips_unknown_cadence(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "odd", "name": "Irregular thing", "kind": "misc", "cadence": "irregular", "status": "active", "source": "seed"},
        instances=[{"id": "odd:2026-07-01", "due_date": "2026-07-01", "amount": 50.0, "direction": "outflow", "source": "seed"}],
    )
    res = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90, reconcile=False)
    assert res["instances_created"] == 0


def test_backfill_does_not_approximate_semimonthly_as_every_15_days(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _manual_recurring(
        conn,
        "twice_monthly",
        due_date="2026-07-15",
        cadence="semimonthly",
    )

    result = backfill_recurring_instances(
        conn,
        as_of_date="2026-07-01",
        lookback_days=90,
        reconcile=False,
    )

    assert result["instances_created"] == 0
    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM obligation_instances
        WHERE obligation_id = 'twice_monthly' AND source = 'backfill'
        """
    ).fetchone()[0] == 0


def test_backfill_does_not_create_future_instances(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _rent(conn)
    backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90, reconcile=False)
    # only the original future instance is on/after as_of
    future = conn.execute("SELECT COUNT(*) FROM obligation_instances WHERE obligation_id='rent_check' AND due_date >= '2026-06-21'").fetchone()[0]
    assert future == 1


def test_backfill_skips_inflows(tmp_path):
    # income/reimbursements must never be backfilled (they'd read as bogus "missing"/"owe")
    conn = _db(tmp_path / "b.sqlite")
    apply_obligation_instances(conn,
        obligation={"id": "anthem", "name": "Anthem reimbursement", "kind": "income", "cadence": "monthly", "status": "active", "source": "seed"},
        instances=[{"id": "anthem:2026-07-15", "due_date": "2026-07-15", "amount": 440.0, "direction": "inflow", "source": "seed"}])
    res = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90, reconcile=False)
    assert res["instances_created"] == 0


def test_backfill_cancels_unmatched_history_to_avoid_false_missing(tmp_path):
    # A monthly bill whose only posted payment is off-cadence/different-amount: the
    # backfilled past instances that can't be matched must be canceled, not left
    # 'expected' (which would become a false CRITICAL "missing payment" in drift).
    conn = _db(tmp_path / "b.sqlite", transactions=[
        ("c1", "ACT-chk", "2026-06-03T08:00:00", -3000.0, "Check #1229", ""),  # matches June rent exactly
    ])
    _rent(conn)
    res = backfill_recurring_instances(conn, as_of_date="2026-06-21", lookback_days=90)
    assert res["unmatched_canceled"] >= 1  # the older months with no matching check
    # no backfilled past instance is left 'expected' without a match
    leftover = conn.execute(
        "SELECT COUNT(*) FROM obligation_instances WHERE source='backfill' AND status='expected' "
        "AND id NOT IN (SELECT obligation_instance_id FROM transaction_obligation_matches)"
    ).fetchone()[0]
    assert leftover == 0
    # the matched June rent is kept and shows cleared
    assert any(c["obligation_name"] == "Rent check" for c in list_recently_cleared(conn, as_of_date="2026-06-21"))


def test_backfill_rolls_back_created_instances_when_reconciliation_raises(
    tmp_path, monkeypatch
):
    db = tmp_path / "b.sqlite"
    conn = _db(db)
    _rent(conn)
    conn.commit()

    def fail_reconciliation(conn, *, as_of_date):
        raise RuntimeError("reconciliation failed")

    monkeypatch.setattr(
        backfill, "reconcile_obligation_instances", fail_reconciliation
    )

    with pytest.raises(RuntimeError, match="reconciliation failed"):
        with conn:
            backfill_recurring_instances(
                conn, as_of_date="2026-06-21", lookback_days=90
            )

    persisted = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM obligation_instances WHERE source = 'backfill'"
    ).fetchone()[0]
    assert persisted == 0


def test_daily_sync_extends_manual_recurring_obligation_through_coverage_horizon(
    tmp_path,
):
    conn = _db(tmp_path / "b.sqlite")
    apply_obligation_instances(
        conn,
        obligation={
            "id": "manual_card",
            "name": "Manual card payment",
            "kind": "credit_card",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
            "autopay": False,
            "amount_discretionary": True,
        },
        instances=[
            {
                "id": "manual_card:2026-07-30",
                "due_date": "2026-07-30",
                "amount": 125.0,
                "direction": "outflow",
                "status": "expected",
                "source": "obligations_yaml_manual",
                "confidence": "medium",
                "amount_status": "estimated",
                "amount_source": "manual_floor",
                "estimation_method": "manual_floor",
                "estimation_inputs": {"required_minimum": 25.0},
                "cash_flow_treatment": "direct_checking",
            }
        ],
    )

    run_background_sync(conn, as_of_date="2026-07-23")
    first_rows = conn.execute(
        """
        SELECT due_date, amount, direction, status, source, confidence,
               amount_status, amount_source, estimation_method,
               estimation_inputs_json, cash_flow_treatment
        FROM obligation_instances
        WHERE obligation_id = 'manual_card'
        ORDER BY due_date, id
        """
    ).fetchall()
    run_background_sync(conn, as_of_date="2026-07-23")
    second_rows = conn.execute(
        """
        SELECT due_date, amount, direction, status, source, confidence,
               amount_status, amount_source, estimation_method,
               estimation_inputs_json, cash_flow_treatment
        FROM obligation_instances
        WHERE obligation_id = 'manual_card'
        ORDER BY due_date, id
        """
    ).fetchall()

    obligation = conn.execute(
        """
        SELECT source, autopay, amount_discretionary
        FROM obligations
        WHERE id = 'manual_card'
        """
    ).fetchone()
    assert dict(obligation) == {
        "source": "obligations_yaml_manual",
        "autopay": 0,
        "amount_discretionary": 1,
    }
    assert len(second_rows) == len(first_rows)

    horizon = date.fromisoformat("2026-07-23") + timedelta(
        days=COVERAGE_HORIZON_DAYS
    )
    assert second_rows[-1]["due_date"] >= horizon.isoformat()

    generated = [
        dict(row) for row in second_rows if row["due_date"] > "2026-07-30"
    ]
    assert generated
    for row in generated:
        assert row == {
            "due_date": row["due_date"],
            "amount": 125.0,
            "direction": "outflow",
            "status": "expected",
            "source": "obligations_yaml_manual",
            "confidence": "medium",
            "amount_status": "estimated",
            "amount_source": "manual_floor",
            "estimation_method": "manual_floor",
            "estimation_inputs_json": '{"required_minimum": 25.0}',
            "cash_flow_treatment": "direct_checking",
        }


def test_paid_manual_occurrence_can_seed_future_expected_occurrences(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _manual_recurring(
        conn,
        "paid_card",
        due_date="2026-01-31",
        status="paid",
    )

    backfill.extend_manual_recurring_instances(
        conn,
        as_of_date="2026-02-01",
        horizon_days=60,
    )

    rows = conn.execute(
        """
        SELECT due_date, status
        FROM obligation_instances
        WHERE obligation_id = 'paid_card'
        ORDER BY due_date
        """
    ).fetchall()
    future = [row for row in rows if row["due_date"] > "2026-01-31"]
    assert future
    assert future[0]["due_date"] == "2026-02-28"
    assert all(row["status"] == "expected" for row in future)


def test_month_end_recurrence_does_not_drift_across_successive_runs(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _manual_recurring(conn, "month_end", due_date="2026-01-31")

    backfill.extend_manual_recurring_instances(
        conn,
        as_of_date="2026-01-31",
        horizon_days=1,
    )
    backfill.extend_manual_recurring_instances(
        conn,
        as_of_date="2026-02-28",
        horizon_days=1,
    )

    due_dates = [
        row["due_date"]
        for row in conn.execute(
            """
            SELECT due_date
            FROM obligation_instances
            WHERE obligation_id = 'month_end'
            ORDER BY due_date
            """
        )
    ]
    assert due_dates == ["2026-01-31", "2026-02-28", "2026-03-31"]


def test_semimonthly_two_anchors_extend_with_short_month_clamping(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _manual_recurring(
        conn,
        "twice_monthly",
        due_date="2026-01-15",
        cadence="semimonthly",
    )
    _manual_recurring(
        conn,
        "twice_monthly",
        due_date="2026-01-31",
        cadence="semimonthly",
    )

    first = backfill.extend_manual_recurring_instances(
        conn,
        as_of_date="2026-01-31",
        horizon_days=45,
    )
    second = backfill.extend_manual_recurring_instances(
        conn,
        as_of_date="2026-01-31",
        horizon_days=45,
    )

    rows = conn.execute(
        """
        SELECT due_date, source
        FROM obligation_instances
        WHERE obligation_id = 'twice_monthly'
        ORDER BY due_date
        """
    ).fetchall()
    assert [row["due_date"] for row in rows] == [
        "2026-01-15",
        "2026-01-31",
        "2026-02-15",
        "2026-02-28",
        "2026-03-15",
        "2026-03-31",
    ]
    assert all(row["source"] == "obligations_yaml_manual" for row in rows)
    assert first == {"instances_created": 4, "obligations_touched": 1}
    assert second == {"instances_created": 0, "obligations_touched": 0}


def test_semimonthly_extension_respects_active_until(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    for due_date in ("2026-01-15", "2026-01-31"):
        _manual_recurring(
            conn,
            "ending_twice_monthly",
            due_date=due_date,
            cadence="semimonthly",
            active_until="2026-03-15",
        )

    backfill.extend_manual_recurring_instances(
        conn,
        as_of_date="2026-01-31",
        horizon_days=90,
    )

    due_dates = [
        row["due_date"]
        for row in conn.execute(
            """
            SELECT due_date
            FROM obligation_instances
            WHERE obligation_id = 'ending_twice_monthly'
            ORDER BY due_date
            """
        )
    ]
    assert due_dates == [
        "2026-01-15",
        "2026-01-31",
        "2026-02-15",
        "2026-02-28",
        "2026-03-15",
    ]


def test_semimonthly_one_anchor_does_not_extend_and_warning_remains(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _manual_recurring(
        conn,
        "ambiguous_twice_monthly",
        due_date="2026-07-31",
        cadence="semimonthly",
    )

    run_background_sync(conn, as_of_date="2026-07-23")

    due_dates = [
        row["due_date"]
        for row in conn.execute(
            """
            SELECT due_date
            FROM obligation_instances
            WHERE obligation_id = 'ambiguous_twice_monthly'
            ORDER BY due_date
            """
        )
    ]
    findings = list_verification_findings(
        conn,
        check_id="coverage_horizon",
        status="open",
    )
    assert due_dates == ["2026-07-31"]
    assert any(
        finding["evidence"]["obligation_id"] == "ambiguous_twice_monthly"
        for finding in findings
    )


def test_semimonthly_extension_rolls_back_when_a_later_insert_fails(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _manual_recurring(
        conn,
        "twice_monthly",
        due_date="2026-01-15",
        cadence="semimonthly",
    )
    _manual_recurring(
        conn,
        "twice_monthly",
        due_date="2026-01-31",
        cadence="semimonthly",
    )
    apply_obligation_instances(
        conn,
        obligation={
            "id": "collision_owner",
            "name": "collision_owner",
            "kind": "bill",
            "status": "active",
            "source": "statement_projection",
        },
        instances=[
            {
                "id": "twice_monthly:2026-02-28",
                "due_date": "2026-01-01",
                "amount": 1.0,
                "direction": "outflow",
                "status": "expected",
                "source": "statement_projection",
            }
        ],
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        backfill.extend_manual_recurring_instances(
            conn,
            as_of_date="2026-01-31",
            horizon_days=31,
        )

    due_dates = [
        row["due_date"]
        for row in conn.execute(
            """
            SELECT due_date
            FROM obligation_instances
            WHERE obligation_id = 'twice_monthly'
            ORDER BY due_date
            """
        )
    ]
    assert due_dates == ["2026-01-15", "2026-01-31"]


def test_source_owned_instance_is_not_used_as_manual_recurrence_template(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _manual_recurring(
        conn,
        "mixed_owner",
        due_date="2026-01-31",
        amount=125.0,
    )
    _manual_recurring(
        conn,
        "mixed_owner",
        due_date="2026-02-15",
        source="statement_projection",
        amount=999.0,
    )

    backfill.extend_manual_recurring_instances(
        conn,
        as_of_date="2026-02-15",
        horizon_days=45,
    )

    generated = conn.execute(
        """
        SELECT due_date, amount, source
        FROM obligation_instances
        WHERE obligation_id = 'mixed_owner' AND due_date > '2026-02-15'
        ORDER BY due_date
        """
    ).fetchall()
    assert generated
    assert generated[0]["due_date"] == "2026-02-28"
    assert all(row["amount"] == 125.0 for row in generated)
    assert all(row["source"] == "obligations_yaml_manual" for row in generated)


def test_extension_error_rolls_back_all_writes_from_the_step(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _manual_recurring(
        conn,
        "a_first",
        due_date="2026-01-01",
        cadence="weekly",
    )
    apply_obligation_instances(
        conn,
        obligation={
            "id": "collision_owner",
            "name": "collision_owner",
            "kind": "bill",
            "cadence": "weekly",
            "status": "active",
            "source": "statement_projection",
        },
        instances=[
            {
                "id": "z_later:2026-01-08",
                "due_date": "2026-01-09",
                "amount": 1.0,
                "direction": "outflow",
                "status": "expected",
                "source": "statement_projection",
            }
        ],
    )
    _manual_recurring(
        conn,
        "z_later",
        due_date="2026-01-01",
        cadence="weekly",
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        backfill.extend_manual_recurring_instances(
            conn,
            as_of_date="2026-01-01",
            horizon_days=7,
        )

    first_dates = [
        row["due_date"]
        for row in conn.execute(
            """
            SELECT due_date
            FROM obligation_instances
            WHERE obligation_id = 'a_first'
            ORDER BY due_date
            """
        )
    ]
    assert first_dates == ["2026-01-01"]


def test_manual_extension_stops_at_active_until(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    _manual_recurring(
        conn,
        "ending_bill",
        due_date="2026-01-31",
        active_until="2026-03-15",
    )

    backfill.extend_manual_recurring_instances(
        conn,
        as_of_date="2026-01-31",
        horizon_days=120,
    )

    due_dates = [
        row["due_date"]
        for row in conn.execute(
            """
            SELECT due_date
            FROM obligation_instances
            WHERE obligation_id = 'ending_bill'
            ORDER BY due_date
            """
        )
    ]
    assert due_dates == ["2026-01-31", "2026-02-28"]
