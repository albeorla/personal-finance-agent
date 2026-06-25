import json
import sqlite3
from datetime import date

from financial_agent.cashflow import build_cash_flow_projections
from financial_agent.obligations import (
    apply_obligation_instances,
    list_obligation_review_candidates,
    list_obligations,
    list_statement_input_estimates,
    suppress_dormant_avg_estimates,
)
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def _db_with_source(path):
    """App schema plus the SimpleFIN source tables the suppression logic reads."""
    conn = _db(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT
        );
        CREATE TABLE IF NOT EXISTS balance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
            balance REAL NOT NULL, available REAL NOT NULL,
            recorded_at TEXT NOT NULL, source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL, posted TEXT,
            transacted_at TEXT, amount REAL NOT NULL, payee TEXT, description TEXT
        );
        """
    )
    return conn


def _seed_account(conn, account_id="chase_amazon", name="Chase Amazon", org="Chase"):
    conn.execute(
        "INSERT INTO accounts (id, name, org, kind, currency) VALUES (?, ?, ?, 'credit_card', 'USD')",
        (account_id, name, org),
    )


def _seed_balance(conn, account_id, balance, available, recorded_at):
    conn.execute(
        "INSERT INTO balance_snapshots (account_id, balance, available, recorded_at, source) "
        "VALUES (?, ?, ?, ?, 'simplefin')",
        (account_id, balance, available, recorded_at),
    )


def _seed_candidate(conn, candidate_id, obligation_id, account_ids):
    """Mirror an applied onboarding candidate: links obligation -> source account."""
    conn.execute(
        """
        INSERT INTO charge_onboarding_candidates (
            id, merchant_key, display_name, direction, status, candidate_type,
            cash_flow_treatment, proposed_cash_impact_policy_json, evidence_count,
            existing_obligation_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'outflow', 'applied', 'card_statement_input',
                  'direct_checking', ?, 3, ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (
            candidate_id,
            candidate_id,
            "Chase Card Payment",
            json.dumps({"evidence_account_ids": account_ids}, sort_keys=True),
            obligation_id,
        ),
    )


def _seed_avg_estimate_obligation(
    conn,
    *,
    obligation_id="onboarded_chase_card_payment",
    candidate_id="cand_chase",
    account_ids=("chase_amazon",),
    source=None,
    estimation_method="average",
    amount_status="estimated",
    due_date="2026-07-10",
):
    """Create an onboarded avg-estimate obligation linked to a source account."""
    source = source or f"charge_onboarding:{candidate_id}"
    _seed_candidate(conn, candidate_id, obligation_id, list(account_ids))
    apply_obligation_instances(
        conn,
        obligation={
            "id": obligation_id,
            "name": "Chase card payment estimate",
            "kind": "bill",
            "cadence": "monthly",
            "status": "active",
            "source": source,
        },
        instances=[
            {
                "id": f"{obligation_id}:{due_date}",
                "due_date": due_date,
                "amount": -1162.0,
                "direction": "outflow",
                "status": "expected",
                "source": source,
                "amount_status": amount_status,
                "estimation_method": estimation_method,
            }
        ],
    )
    return obligation_id


def test_apply_obligation_instances_normalizes_signed_outflows(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")

    result = apply_obligation_instances(
        conn,
        obligation={
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[
            {
                "due_date": "2026-07-03",
                "amount": -3000.0,
                "source": "obligations_yaml_manual",
                "confidence": "high",
                "notes": "Rent check (Jul).",
            }
        ],
    )
    second_result = apply_obligation_instances(
        conn,
        obligation={
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[
            {
                "due_date": "2026-07-03",
                "amount": -3000.0,
                "source": "obligations_yaml_manual",
                "confidence": "high",
                "notes": "Rent check (Jul).",
            }
        ],
    )

    rows = conn.execute(
        """
        SELECT obligation_id, due_date, amount, direction, status
        FROM obligation_instances
        ORDER BY due_date
        """
    ).fetchall()

    assert result["created"] == 1
    assert result["updated"] == 0
    assert second_result["created"] == 0
    assert second_result["updated"] == 1
    assert [(row["obligation_id"], row["due_date"], row["amount"], row["direction"], row["status"]) for row in rows] == [
        ("rent", "2026-07-03", 3000.0, "outflow", "expected")
    ]


def test_list_obligations_includes_instances(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")
    apply_obligation_instances(
        conn,
        obligation={
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[
            {"due_date": "2026-07-03", "amount": -3000.0, "source": "obligations_yaml_manual"}
        ],
    )

    assert list_obligations(conn, kind="housing") == [
        {
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
            "autopay": True,
            "amount_discretionary": False,
            "instances": [
                {
                    "id": "rent:2026-07-03",
                    "due_date": "2026-07-03",
                    "amount": 3000.0,
                    "direction": "outflow",
                    "status": "expected",
                    "source": "obligations_yaml_manual",
                    "confidence": None,
                    "notes": None,
                    "amount_status": None,
                    "amount_source": None,
                    "amount_observed_at": None,
                    "statement_close_date": None,
                    "review_after": None,
                    "estimation_method": None,
                    "estimation_inputs": None,
                    "cash_flow_treatment": None,
                    "statement_target_obligation_id": None,
                }
            ],
        }
    ]


def test_obligation_instances_preserve_amount_lifecycle_fields(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")
    apply_obligation_instances(
        conn,
        obligation={
            "id": "amex_statement_payment",
            "name": "Amex statement payment",
            "kind": "credit_card_statement",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[
            {
                "due_date": "2026-07-16",
                "amount": -5400.0,
                "source": "obligations_yaml_manual",
                "confidence": "medium",
                "notes": "Jun-cycle statement estimate from Amex portal screenshot.",
                "amount_status": "estimated",
                "amount_source": "portal_current_balance_estimate",
                "amount_observed_at": "2026-06-18T12:00:00+00:00",
                "statement_close_date": "2026-06-21",
                "review_after": "2026-06-22",
            }
        ],
    )

    obligation = list_obligations(conn, kind="credit_card_statement")[0]
    instance = obligation["instances"][0]

    assert instance["amount_status"] == "estimated"
    assert instance["amount_source"] == "portal_current_balance_estimate"
    assert instance["amount_observed_at"] == "2026-06-18T12:00:00+00:00"
    assert instance["statement_close_date"] == "2026-06-21"
    assert instance["review_after"] == "2026-06-22"

    projections, _ = build_cash_flow_projections(
        conn,
        accounts=[
            {
                "account_id": "checking-1",
                "account_name": "Checking 4321",
                "kind": "checking",
                "available": 10000.0,
                "recorded_at": "2026-06-20T00:00:00+00:00",
            }
        ],
        windows=[31],
        start_date=date(2026, 7, 1),
    )
    event = projections[0]["events"][0]

    assert event["amount_status"] == "estimated"
    assert event["amount_source"] == "portal_current_balance_estimate"
    assert event["statement_close_date"] == "2026-06-21"
    assert event["review_after"] == "2026-06-22"
    assert event["cash_flow_treatment"] is None


def test_list_obligation_review_candidates_finds_estimates_due_for_refresh(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")
    apply_obligation_instances(
        conn,
        obligation={
            "id": "amex_statement_payment",
            "name": "Amex statement payment",
            "kind": "credit_card_statement",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[
            {
                "due_date": "2026-07-16",
                "amount": -5400.0,
                "source": "obligations_yaml_manual",
                "confidence": "medium",
                "amount_status": "estimated",
                "amount_source": "portal_current_balance_estimate",
                "statement_close_date": "2026-06-21",
                "review_after": "2026-06-22",
            },
            {
                "due_date": "2026-08-16",
                "amount": -6715.0,
                "source": "obligations_yaml_manual",
                "confidence": "low",
                "amount_status": "estimated",
                "amount_source": "manual_projection_from_spend_cuts",
            },
        ],
    )

    assert list_obligation_review_candidates(conn, as_of_date="2026-06-21") == []

    candidates = list_obligation_review_candidates(conn, as_of_date="2026-06-22")

    assert candidates == [
        {
            "review_type": "estimated_amount_ready_for_refresh",
            "instance_id": "amex_statement_payment:2026-07-16",
            "obligation_id": "amex_statement_payment",
            "obligation_name": "Amex statement payment",
            "obligation_kind": "credit_card_statement",
            "due_date": "2026-07-16",
            "amount": 5400.0,
            "direction": "outflow",
            "status": "expected",
            "confidence": "medium",
            "amount_status": "estimated",
            "amount_source": "portal_current_balance_estimate",
            "amount_observed_at": None,
            "statement_close_date": "2026-06-21",
            "review_after": "2026-06-22",
            "estimation_method": None,
            "estimation_inputs": None,
            "cash_flow_treatment": None,
            "statement_target_obligation_id": None,
            "source": "obligations_yaml_manual",
            "notes": None,
            "recommended_action": "Refresh amount from source and replace the estimate with the statement amount.",
        }
    ]


def test_fixed_outflow_instances_project_into_cash_flow(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")
    apply_obligation_instances(
        conn,
        obligation={
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[
            {"due_date": "2026-07-03", "amount": -3000.0, "source": "obligations_yaml_manual"}
        ],
    )

    projections, warnings = build_cash_flow_projections(
        conn,
        accounts=[
            {
                "account_id": "checking-1",
                "account_name": "Checking 4321",
                "kind": "checking",
                "available": 5000.0,
                "recorded_at": "2026-06-20T00:00:00+00:00",
            }
        ],
        windows=[20],
        start_date=date(2026, 6, 20),
    )

    assert projections[0]["ending_balance"] == 2000.0
    assert projections[0]["events"][0]["obligation_id"] == "rent"
    assert "cash-flow projection includes only seeded local obligation instances" in warnings[0]


def test_seasonal_eversource_estimate_keeps_structured_model_metadata(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")
    apply_obligation_instances(
        conn,
        obligation={
            "id": "eversource_electric_estimates",
            "name": "Eversource electric estimates",
            "kind": "utility",
            "cadence": "monthly_estimate",
            "status": "active",
            "source": "user_estimate_policy",
        },
        instances=[
            {
                "due_date": "2026-07-27",
                "amount": -171.77,
                "direction": "outflow",
                "source": "user_estimate_policy",
                "confidence": "low",
                "amount_status": "estimated",
                "amount_source": "average_with_summer_multiplier",
                "estimation_method": "average_with_month_multiplier",
                "estimation_inputs": {
                    "base_average": 114.51,
                    "source_months": ["2025-11", "2025-12", "2026-02", "2026-03", "2026-04", "2026-05"],
                    "target_month": "2026-07",
                    "summer_multiplier": 1.5,
                },
                "cash_flow_treatment": "direct_checking",
                "notes": "Average observed Eversource usage, then apply 150% summer multiplier.",
            }
        ],
    )

    obligation = list_obligations(conn, kind="utility")[0]
    instance = obligation["instances"][0]

    assert instance["amount"] == 171.77
    assert instance["estimation_method"] == "average_with_month_multiplier"
    assert instance["estimation_inputs"]["summer_multiplier"] == 1.5
    assert instance["cash_flow_treatment"] == "direct_checking"

    projections, _ = build_cash_flow_projections(
        conn,
        accounts=[
            {
                "account_id": "checking-1",
                "account_name": "Checking 4321",
                "kind": "checking",
                "available": 1000.0,
                "recorded_at": "2026-06-20T00:00:00+00:00",
            }
        ],
        windows=[45],
        start_date=date(2026, 6, 20),
    )

    event = projections[0]["events"][0]
    assert event["obligation_id"] == "eversource_electric_estimates"
    assert event["signed_amount"] == -171.77
    assert event["estimation_inputs"]["target_month"] == "2026-07"


def test_gault_card_spend_input_does_not_project_as_direct_checking_outflow(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")
    apply_obligation_instances(
        conn,
        obligation={
            "id": "amex_statement_payment",
            "name": "Amex statement payment",
            "kind": "credit_card_statement",
            "cadence": "monthly",
            "status": "active",
            "source": "statement_projection",
        },
        instances=[
            {
                "due_date": "2026-08-16",
                "amount": -1000.0,
                "source": "statement_projection",
                "confidence": "low",
                "amount_status": "estimated",
                "amount_source": "statement_inputs",
                "cash_flow_treatment": "direct_checking",
            }
        ],
    )
    apply_obligation_instances(
        conn,
        obligation={
            "id": "gault_card_spend_estimates",
            "name": "Gault card spend estimates",
            "kind": "card_spend_input",
            "cadence": "seasonal_usage",
            "status": "active",
            "source": "user_estimate_policy",
        },
        instances=[
            {
                "due_date": "2026-08-01",
                "amount": -175.0,
                "source": "user_estimate_policy",
                "confidence": "low",
                "amount_status": "estimated",
                "amount_source": "seasonal_usage_estimate",
                "estimation_method": "seasonal_card_spend_pattern",
                "estimation_inputs": {
                    "summer_amount": 175.0,
                    "winter_observed_amounts": [532.10, 602.48],
                    "expected_pattern": "low summer/fall charge, larger winter charges",
                },
                "cash_flow_treatment": "card_statement_input",
                "statement_target_obligation_id": "amex_statement_payment",
                "notes": "Feeds the Amex statement estimate; it is not a direct checking outflow.",
            }
        ],
    )

    projections, _ = build_cash_flow_projections(
        conn,
        accounts=[
            {
                "account_id": "checking-1",
                "account_name": "Checking 4321",
                "kind": "checking",
                "available": 2000.0,
                "recorded_at": "2026-06-20T00:00:00+00:00",
            }
        ],
        windows=[70],
        start_date=date(2026, 6, 20),
    )

    assert [event["obligation_id"] for event in projections[0]["events"]] == ["amex_statement_payment"]
    assert projections[0]["ending_balance"] == 1000.0

    statement_inputs = list_statement_input_estimates(conn, target_obligation_id="amex_statement_payment")
    assert statement_inputs == [
        {
            "instance_id": "gault_card_spend_estimates:2026-08-01",
            "obligation_id": "gault_card_spend_estimates",
            "obligation_name": "Gault card spend estimates",
            "due_date": "2026-08-01",
            "amount": 175.0,
            "direction": "outflow",
            "status": "expected",
            "confidence": "low",
            "amount_status": "estimated",
            "amount_source": "seasonal_usage_estimate",
            "estimation_method": "seasonal_card_spend_pattern",
            "estimation_inputs": {
                "summer_amount": 175.0,
                "winter_observed_amounts": [532.10, 602.48],
                "expected_pattern": "low summer/fall charge, larger winter charges",
            },
            "cash_flow_treatment": "card_statement_input",
            "statement_target_obligation_id": "amex_statement_payment",
            "notes": "Feeds the Amex statement estimate; it is not a direct checking outflow.",
        }
    ]


def _taxes_obligation():
    return {
        "id": "taxes",
        "name": "Estimated taxes",
        "kind": "tax",
        "cadence": "quarterly",
        "status": "active",
        "source": "obligations_yaml_manual",
    }


def test_apply_obligation_instances_allows_multiple_same_date(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")

    result = apply_obligation_instances(
        conn,
        obligation=_taxes_obligation(),
        instances=[
            {"due_date": "2026-07-31", "amount": -500.0, "source": "obligations_yaml_manual", "notes": "estimated"},
            {"due_date": "2026-07-31", "amount": -500.0, "source": "obligations_yaml_manual", "notes": "payment"},
        ],
    )

    assert result["created"] == 2
    assert result["updated"] == 0
    assert result["instance_ids"] == ["taxes:2026-07-31", "taxes:2026-07-31:1"]

    ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM obligation_instances WHERE obligation_id = 'taxes' ORDER BY id"
        ).fetchall()
    ]
    assert ids == ["taxes:2026-07-31", "taxes:2026-07-31:1"]

    instances = list_obligations(conn, kind="tax")[0]["instances"]
    assert len(instances) == 2
    assert {i["notes"] for i in instances} == {"estimated", "payment"}


def test_composite_instance_id_upsert_idempotent(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")

    apply_obligation_instances(
        conn,
        obligation=_taxes_obligation(),
        instances=[{"due_date": "2026-07-31", "amount": -500.0, "source": "obligations_yaml_manual"}],
    )
    before = conn.execute(
        "SELECT updated_at FROM obligation_instances WHERE id = 'taxes:2026-07-31'"
    ).fetchone()[0]

    result = apply_obligation_instances(
        conn,
        obligation=_taxes_obligation(),
        instances=[{"due_date": "2026-07-31", "amount": -550.0, "source": "obligations_yaml_manual"}],
    )

    assert result["created"] == 0
    assert result["updated"] == 1
    rows = conn.execute(
        "SELECT id, amount, updated_at FROM obligation_instances WHERE obligation_id = 'taxes'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == "taxes:2026-07-31"
    assert rows[0]["amount"] == 550.0
    assert rows[0]["updated_at"] >= before


def test_add_third_instance_same_date_increments_index(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")

    apply_obligation_instances(
        conn,
        obligation=_taxes_obligation(),
        instances=[
            {"due_date": "2026-07-31", "amount": -100.0, "source": "obligations_yaml_manual"},
            {"due_date": "2026-07-31", "amount": -200.0, "source": "obligations_yaml_manual"},
        ],
    )
    result = apply_obligation_instances(
        conn,
        obligation=_taxes_obligation(),
        instances=[{"due_date": "2026-07-31", "amount": -300.0, "source": "obligations_yaml_manual"}],
    )

    assert result["created"] == 1
    assert result["instance_ids"] == ["taxes:2026-07-31:2"]


def test_backward_compat_old_id_format_no_index_suffix(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")

    apply_obligation_instances(
        conn,
        obligation={
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[{"id": "rent:2026-07-03", "due_date": "2026-07-03", "amount": -3000.0, "source": "obligations_yaml_manual"}],
    )

    result = apply_obligation_instances(
        conn,
        obligation={
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[{"due_date": "2026-07-03", "amount": -3100.0, "source": "obligations_yaml_manual"}],
    )

    assert result["created"] == 0
    assert result["updated"] == 1
    rows = conn.execute(
        "SELECT id, amount FROM obligation_instances WHERE obligation_id = 'rent'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == "rent:2026-07-03"
    assert rows[0]["amount"] == 3100.0


def test_multiple_instances_same_date_different_obligations_no_collision(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")

    for oid in ("tax_fed", "tax_state"):
        apply_obligation_instances(
            conn,
            obligation={
                "id": oid,
                "name": oid,
                "kind": "tax",
                "cadence": "quarterly",
                "status": "active",
                "source": "obligations_yaml_manual",
            },
            instances=[{"due_date": "2026-07-31", "amount": -500.0, "source": "obligations_yaml_manual"}],
        )

    fed = conn.execute("SELECT id FROM obligation_instances WHERE obligation_id = 'tax_fed'").fetchone()[0]
    state = conn.execute("SELECT id FROM obligation_instances WHERE obligation_id = 'tax_state'").fetchone()[0]
    assert fed == "tax_fed:2026-07-31"
    assert state == "tax_state:2026-07-31"


def test_delete_obligation_instance_soft_deletes(tmp_path):
    from financial_agent.obligations import delete_obligation_instance

    conn = _db(tmp_path / "obligations.sqlite")
    apply_obligation_instances(
        conn,
        obligation={
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[{"due_date": "2026-07-03", "amount": -3000.0, "source": "obligations_yaml_manual"}],
    )

    result = delete_obligation_instance(conn, "rent:2026-07-03")
    assert result["deleted"] is True
    assert result["previous_status"] == "expected"

    row = conn.execute(
        "SELECT status, updated_at FROM obligation_instances WHERE id = 'rent:2026-07-03'"
    ).fetchone()
    assert row["status"] == "deleted"
    assert row["updated_at"] is not None

    instances = list_obligations(conn, kind="housing")[0]["instances"]
    assert instances == []

    # Idempotent: deleting again is a no-op report.
    again = delete_obligation_instance(conn, "rent:2026-07-03")
    assert again["deleted"] is False
    assert again["reason"] == "already_deleted"

    # Missing instance reports not_found.
    missing = delete_obligation_instance(conn, "rent:2099-01-01")
    assert missing["deleted"] is False
    assert missing["reason"] == "not_found"


def test_deleted_instances_excluded_from_cash_flow_projection(tmp_path):
    from financial_agent.obligations import delete_obligation_instance

    conn = _db(tmp_path / "obligations.sqlite")
    apply_obligation_instances(
        conn,
        obligation={
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[{"due_date": "2026-07-03", "amount": -3000.0, "source": "obligations_yaml_manual"}],
    )

    accounts = [
        {
            "account_id": "checking-1",
            "account_name": "Checking 4321",
            "kind": "checking",
            "available": 5000.0,
            "recorded_at": "2026-06-20T00:00:00+00:00",
        }
    ]
    projections, _ = build_cash_flow_projections(conn, accounts=accounts, windows=[20], start_date=date(2026, 6, 20))
    assert projections[0]["ending_balance"] == 2000.0
    assert len(projections[0]["events"]) == 1

    delete_obligation_instance(conn, "rent:2026-07-03")

    projections, _ = build_cash_flow_projections(conn, accounts=accounts, windows=[20], start_date=date(2026, 6, 20))
    assert projections[0]["ending_balance"] == 5000.0
    assert projections[0]["events"] == []


def test_apply_with_status_deleted_marks_instance_deleted(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")
    apply_obligation_instances(
        conn,
        obligation={
            "id": "rent",
            "name": "Rent check",
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[
            {
                "id": "rent:2026-07-03",
                "due_date": "2026-07-03",
                "amount": -3000.0,
                "status": "deleted",
                "source": "obligations_yaml_manual",
            }
        ],
    )

    row = conn.execute(
        "SELECT status FROM obligation_instances WHERE id = 'rent:2026-07-03'"
    ).fetchone()
    assert row["status"] == "deleted"
    assert list_obligations(conn, kind="housing")[0]["instances"] == []


def _seed_compact_fixture(conn):
    """Create 3 obligations with 10 instances each (30 instances total)."""
    for ob in range(3):
        apply_obligation_instances(
            conn,
            obligation={
                "id": f"bill-{ob}",
                "name": f"Bill {ob}",
                "kind": "utilities",
                "cadence": "monthly",
                "status": "active",
                "source": "obligations_yaml_manual",
            },
            instances=[
                {
                    "due_date": f"2026-{month:02d}-15",
                    "amount": -100.0 - ob,
                    "source": "obligations_yaml_manual",
                    "confidence": "high",
                    "notes": f"Bill {ob} month {month}.",
                }
                for month in range(1, 11)
            ],
        )


def test_list_obligations_compact_mode_reduces_size(tmp_path):
    import json

    conn = _db(tmp_path / "obligations.sqlite")
    _seed_compact_fixture(conn)

    full = list_obligations(conn, kind="utilities", status="active", include_instances=True)
    compact = list_obligations(
        conn, kind="utilities", status="active", include_instances=True, compact=True
    )

    full_size = len(json.dumps(full))
    compact_size = len(json.dumps(compact))

    assert full_size > 10_000
    assert compact_size < 5_000
    # 70% reduction target
    assert compact_size < full_size * 0.3
    assert full_size > full_size * 0.9  # baseline sanity

    metadata_keys = {"id", "name", "kind", "cadence", "status", "source"}
    for full_ob, compact_ob in zip(full, compact, strict=True):
        assert "instances" not in compact_ob
        assert compact_ob["instance_count"] == len(full_ob["instances"])
        assert {k: full_ob[k] for k in metadata_keys} == {
            k: compact_ob[k] for k in metadata_keys
        }


def test_list_obligations_default_mode_unchanged(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")
    _seed_compact_fixture(conn)

    explicit_default = list_obligations(
        conn, kind="utilities", status="active", include_instances=True, compact=False
    )
    implicit_default = list_obligations(
        conn, kind="utilities", status="active", include_instances=True
    )

    assert explicit_default == implicit_default
    for ob in explicit_default:
        assert "instances" in ob
        assert "instance_count" not in ob
        assert len(ob["instances"]) == 10


def test_list_obligations_mixed_parameters(tmp_path):
    conn = _db(tmp_path / "obligations.sqlite")
    _seed_compact_fixture(conn)

    # include_instances=True + compact=False returns full data.
    full = list_obligations(
        conn, kind="utilities", include_instances=True, compact=False
    )
    assert all("instances" in ob and len(ob["instances"]) == 10 for ob in full)

    # compact mode is a no-op when there are no instances to drop.
    no_instances_default = list_obligations(
        conn, kind="utilities", include_instances=False, compact=False
    )
    no_instances_compact = list_obligations(
        conn, kind="utilities", include_instances=False, compact=True
    )
    assert no_instances_default == no_instances_compact
    for ob in no_instances_compact:
        assert "instances" not in ob
        assert "instance_count" not in ob


# --- dormant avg-estimate suppression --------------------------------------


def test_suppress_dormant_avg_estimates_deactivates_zero_balance_account(tmp_path):
    conn = _db_with_source(tmp_path / "obligations.sqlite")
    _seed_account(conn)
    # 2+ cycles of zero-balance snapshots, no transactions in the window.
    _seed_balance(conn, "chase_amazon", 0.0, 0.0, "2026-04-30T00:00:00+00:00")
    _seed_balance(conn, "chase_amazon", 0.0, 0.0, "2026-05-31T00:00:00+00:00")
    _seed_balance(conn, "chase_amazon", 0.0, 0.0, "2026-06-23T00:00:00+00:00")
    obligation_id = _seed_avg_estimate_obligation(conn)

    result = suppress_dormant_avg_estimates(
        conn, as_of_date="2026-06-24", options={"dormancy_cycles": 2, "lookback_days": 60}
    )

    assert result["suppressed_count"] == 1
    assert result["suppressed"][0]["obligation_id"] == obligation_id

    status = conn.execute(
        "SELECT status FROM obligations WHERE id = ?", (obligation_id,)
    ).fetchone()["status"]
    assert status == "dormant_suppressed"

    finding = conn.execute(
        "SELECT finding_type, severity, evidence_json, status FROM drift_findings "
        "WHERE obligation_id = ? AND finding_type = 'auto_suppressed_dormant_estimate'",
        (obligation_id,),
    ).fetchone()
    assert finding is not None
    assert finding["severity"] == "low"
    assert finding["status"] == "active"
    evidence = json.loads(finding["evidence_json"])
    assert "chase_amazon" in evidence["account_ids"]
    assert evidence["balance_history"][0]["balance"] == 0.0
    assert evidence["transactions_in_window"] == 0


def test_dormant_suppressed_excluded_from_cashflow_projection(tmp_path):
    conn = _db_with_source(tmp_path / "obligations.sqlite")
    _seed_account(conn)
    _seed_balance(conn, "chase_amazon", 0.0, 0.0, "2026-04-30T00:00:00+00:00")
    _seed_balance(conn, "chase_amazon", 0.0, 0.0, "2026-06-23T00:00:00+00:00")
    obligation_id = _seed_avg_estimate_obligation(conn, due_date="2026-07-10")

    # Before suppression the estimate projects.
    accounts = [
        {
            "account_id": "checking-1",
            "account_name": "Checking 4321",
            "kind": "checking",
            "available": 10000.0,
            "recorded_at": "2026-06-24T00:00:00+00:00",
        }
    ]
    before, _ = build_cash_flow_projections(
        conn, accounts=accounts, windows=[31], start_date=date(2026, 7, 1)
    )
    assert any(e["obligation_id"] == obligation_id for e in before[0]["events"])

    suppress_dormant_avg_estimates(
        conn, as_of_date="2026-06-24", options={"dormancy_cycles": 2, "lookback_days": 60}
    )

    after, _ = build_cash_flow_projections(
        conn, accounts=accounts, windows=[31], start_date=date(2026, 7, 1)
    )
    assert all(e["obligation_id"] != obligation_id for e in after[0]["events"])
    # Suppressing the only event leaves the runway untouched.
    assert after[0]["ending_balance"] == 10000.0


def test_suppress_dormant_does_not_touch_active_accounts(tmp_path):
    conn = _db_with_source(tmp_path / "obligations.sqlite")
    _seed_account(conn)
    # Non-zero latest balance => active, not dormant.
    _seed_balance(conn, "chase_amazon", -540.0, -540.0, "2026-06-23T00:00:00+00:00")
    obligation_id = _seed_avg_estimate_obligation(conn)

    result = suppress_dormant_avg_estimates(
        conn, as_of_date="2026-06-24", options={"dormancy_cycles": 2, "lookback_days": 60}
    )

    assert result["suppressed_count"] == 0
    status = conn.execute(
        "SELECT status FROM obligations WHERE id = ?", (obligation_id,)
    ).fetchone()["status"]
    assert status == "active"


def test_suppress_dormant_active_via_recent_transaction(tmp_path):
    conn = _db_with_source(tmp_path / "obligations.sqlite")
    _seed_account(conn)
    _seed_balance(conn, "chase_amazon", 0.0, 0.0, "2026-06-23T00:00:00+00:00")
    # A single posted transaction in the window resets dormancy.
    conn.execute(
        "INSERT INTO transactions (id, account_id, posted, amount, payee, description) "
        "VALUES ('t1', 'chase_amazon', '2026-06-10T00:00:00', -42.0, 'Amazon', 'order')"
    )
    obligation_id = _seed_avg_estimate_obligation(conn)

    result = suppress_dormant_avg_estimates(
        conn, as_of_date="2026-06-24", options={"dormancy_cycles": 2, "lookback_days": 60}
    )

    assert result["suppressed_count"] == 0
    status = conn.execute(
        "SELECT status FROM obligations WHERE id = ?", (obligation_id,)
    ).fetchone()["status"]
    assert status == "active"


def test_suppress_dormant_skips_manual_obligations(tmp_path):
    conn = _db_with_source(tmp_path / "obligations.sqlite")
    _seed_account(conn)
    _seed_balance(conn, "chase_amazon", 0.0, 0.0, "2026-06-23T00:00:00+00:00")

    # Auto-modeled (onboarding) obligation, dormant.
    onboarding_id = _seed_avg_estimate_obligation(
        conn, obligation_id="onboarded_chase", candidate_id="cand_chase"
    )
    # Manual obligation on the same dormant account: must never be auto-touched.
    _seed_candidate(conn, "cand_manual", "manual_chase", ["chase_amazon"])
    apply_obligation_instances(
        conn,
        obligation={
            "id": "manual_chase",
            "name": "Chase manual payment",
            "kind": "bill",
            "cadence": "monthly",
            "status": "active",
            "source": "obligations_yaml_manual",
        },
        instances=[
            {
                "id": "manual_chase:2026-07-10",
                "due_date": "2026-07-10",
                "amount": -500.0,
                "direction": "outflow",
                "status": "expected",
                "source": "obligations_yaml_manual",
                "amount_status": "estimated",
                "estimation_method": "average",
            }
        ],
    )

    result = suppress_dormant_avg_estimates(
        conn, as_of_date="2026-06-24", options={"dormancy_cycles": 2, "lookback_days": 60}
    )

    suppressed_ids = {s["obligation_id"] for s in result["suppressed"]}
    assert suppressed_ids == {onboarding_id}
    manual_status = conn.execute(
        "SELECT status FROM obligations WHERE id = 'manual_chase'"
    ).fetchone()["status"]
    assert manual_status == "active"


def test_suppress_dormant_skips_confirmed_amounts(tmp_path):
    conn = _db_with_source(tmp_path / "obligations.sqlite")
    _seed_account(conn)
    _seed_balance(conn, "chase_amazon", 0.0, 0.0, "2026-06-23T00:00:00+00:00")
    # estimation_method set, but the amount is statement-confirmed.
    obligation_id = _seed_avg_estimate_obligation(conn, amount_status="confirmed")

    result = suppress_dormant_avg_estimates(
        conn, as_of_date="2026-06-24", options={"dormancy_cycles": 2, "lookback_days": 60}
    )

    assert result["suppressed_count"] == 0
    status = conn.execute(
        "SELECT status FROM obligations WHERE id = ?", (obligation_id,)
    ).fetchone()["status"]
    assert status == "active"


def test_suppress_dormant_skips_null_estimation_method(tmp_path):
    conn = _db_with_source(tmp_path / "obligations.sqlite")
    _seed_account(conn)
    _seed_balance(conn, "chase_amazon", 0.0, 0.0, "2026-06-23T00:00:00+00:00")
    obligation_id = _seed_avg_estimate_obligation(conn, estimation_method=None)

    result = suppress_dormant_avg_estimates(
        conn, as_of_date="2026-06-24", options={"dormancy_cycles": 2, "lookback_days": 60}
    )

    assert result["suppressed_count"] == 0
    status = conn.execute(
        "SELECT status FROM obligations WHERE id = ?", (obligation_id,)
    ).fetchone()["status"]
    assert status == "active"


def test_suppress_dormant_is_reversible(tmp_path):
    conn = _db_with_source(tmp_path / "obligations.sqlite")
    _seed_account(conn)
    _seed_balance(conn, "chase_amazon", 0.0, 0.0, "2026-06-23T00:00:00+00:00")
    obligation_id = _seed_avg_estimate_obligation(conn, due_date="2026-07-10")

    suppress_dormant_avg_estimates(
        conn, as_of_date="2026-06-24", options={"dormancy_cycles": 2, "lookback_days": 60}
    )
    assert conn.execute(
        "SELECT status FROM obligations WHERE id = ?", (obligation_id,)
    ).fetchone()["status"] == "dormant_suppressed"

    # Manual reactivation: a plain status flip brings it back into projection.
    conn.execute(
        "UPDATE obligations SET status = 'active' WHERE id = ?", (obligation_id,)
    )
    accounts = [
        {
            "account_id": "checking-1",
            "account_name": "Checking 4321",
            "kind": "checking",
            "available": 10000.0,
            "recorded_at": "2026-06-24T00:00:00+00:00",
        }
    ]
    projections, _ = build_cash_flow_projections(
        conn, accounts=accounts, windows=[31], start_date=date(2026, 7, 1)
    )
    assert any(e["obligation_id"] == obligation_id for e in projections[0]["events"])
    # The audit finding is preserved for history.
    assert conn.execute(
        "SELECT COUNT(*) FROM drift_findings WHERE obligation_id = ? "
        "AND finding_type = 'auto_suppressed_dormant_estimate'",
        (obligation_id,),
    ).fetchone()[0] == 1


def test_suppress_dormant_noop_without_source_tables(tmp_path):
    # App schema only (no balance_snapshots): nothing can be proven dormant.
    conn = _db(tmp_path / "obligations.sqlite")
    result = suppress_dormant_avg_estimates(conn, as_of_date="2026-06-24")
    assert result["suppressed_count"] == 0
    assert result["evaluated"] == 0
