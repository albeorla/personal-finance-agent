"""Tests for statement-cycle aggregation (slice B).

Card charges roll into the statement cycle that pays them; a future statement
estimate can be built from real modeled card spend, but a portal/confirmed
amount must never be overwritten by a rollup guess.
"""

import sqlite3

from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent.statements import (
    _min_confidence,
    aggregate_statement_inputs,
    list_statement_cycles,
    recompute_statement_estimates,
)


def test_min_confidence_keeps_none_when_no_confidence_present():
    assert _min_confidence([None, None]) is None  # absent, not "low"
    assert _min_confidence(["low", None]) == "low"
    assert _min_confidence(["high", "medium"]) == "medium"


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def _seed(conn):
    # The Amex statement-payment obligation: two monthly statements with known
    # close dates. July is a protected portal estimate; August is an unconfirmed
    # manual projection (eligible for rollup).
    apply_obligation_instances(
        conn,
        obligation={
            "id": "amex_statement_payment",
            "name": "Amex statement payment",
            "kind": "credit_card_statement",
            "status": "active",
            "source": "seed",
        },
        instances=[
            {
                "id": "amex_statement_payment:2026-07-16",
                "due_date": "2026-07-16",
                "amount": -5400.0,
                "source": "seed",
                "amount_status": "estimated",
                "amount_source": "portal_current_balance_estimate",
                "statement_close_date": "2026-06-21",
            },
            {
                "id": "amex_statement_payment:2026-08-16",
                "due_date": "2026-08-16",
                "amount": -6000.0,
                "source": "seed",
                "amount_status": "estimated",
                "amount_source": "manual_projection",
                "statement_close_date": "2026-07-21",
            },
        ],
    )
    # Gault card charges that feed the Amex statement.
    apply_obligation_instances(
        conn,
        obligation={
            "id": "gault_card_spend",
            "name": "Gault Energy",
            "kind": "card_spend_input",
            "status": "active",
            "source": "seed",
        },
        instances=[
            _gault("2026-06-10", -532.10),  # cycle closing 2026-06-21
            _gault("2026-07-05", -600.00),  # cycle closing 2026-07-21
            _gault("2026-07-25", -499.00),  # after last close -> unrolled
        ],
    )


def _gault(due_date, amount):
    return {
        "id": f"gault_card_spend:{due_date}",
        "due_date": due_date,
        "amount": amount,
        "source": "seed",
        "confidence": "low",
        "cash_flow_treatment": "card_statement_input",
        "statement_target_obligation_id": "amex_statement_payment",
    }


def test_aggregate_assigns_card_inputs_to_the_right_cycle(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed(conn)

    result = aggregate_statement_inputs(conn, target_obligation_id="amex_statement_payment")
    assert result["cycles"] == 2
    assert result["inputs_assigned"] == 2
    assert result["unrolled_inputs"] == 1
    assert result["unrolled_instance_ids"] == ["gault_card_spend:2026-07-25"]

    cycles = list_statement_cycles(conn, target_obligation_id="amex_statement_payment")
    by_close = {c["cycle_close_date"]: c for c in cycles}
    assert by_close["2026-06-21"]["input_sum"] == 532.10
    assert by_close["2026-06-21"]["input_count"] == 1
    assert by_close["2026-07-21"]["input_sum"] == 600.00
    # The second cycle opens the day after the first closes.
    assert by_close["2026-07-21"]["cycle_open_date"] == "2026-06-22"
    assert by_close["2026-07-21"]["confidence"] == "low"


def test_aggregate_is_idempotent(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed(conn)
    aggregate_statement_inputs(conn, target_obligation_id="amex_statement_payment")
    aggregate_statement_inputs(conn, target_obligation_id="amex_statement_payment")

    cycles = conn.execute("SELECT COUNT(*) FROM statement_cycles").fetchone()[0]
    inputs = conn.execute("SELECT COUNT(*) FROM statement_cycle_inputs").fetchone()[0]
    assert cycles == 2
    assert inputs == 2  # no duplicate join rows


def test_recompute_never_overwrites_a_portal_estimate(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed(conn)

    result = recompute_statement_estimates(
        conn, target_obligation_id="amex_statement_payment", baseline=2000.0
    )
    assert result["skipped_protected"] == 1  # July portal estimate untouched
    assert result["updated"] == 1  # August manual projection recomputed

    july = conn.execute(
        "SELECT amount, amount_source FROM obligation_instances WHERE id = 'amex_statement_payment:2026-07-16'"
    ).fetchone()
    assert july["amount"] == 5400.0
    assert july["amount_source"] == "portal_current_balance_estimate"

    august = conn.execute(
        "SELECT amount, amount_source, estimation_method FROM obligation_instances "
        "WHERE id = 'amex_statement_payment:2026-08-16'"
    ).fetchone()
    # baseline 2000 + cycle 2 rollup 600.00
    assert august["amount"] == 2600.0
    assert august["amount_source"] == "statement_input_rollup"
    assert august["estimation_method"] == "statement_input_rollup"


def test_recompute_is_idempotent(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed(conn)
    first = recompute_statement_estimates(conn, target_obligation_id="amex_statement_payment", baseline=2000.0)
    second = recompute_statement_estimates(conn, target_obligation_id="amex_statement_payment", baseline=2000.0)
    assert first["updated"] == 1
    assert second["updated"] == 1  # still recomputes, to the same value
    august = conn.execute(
        "SELECT amount FROM obligation_instances WHERE id = 'amex_statement_payment:2026-08-16'"
    ).fetchone()
    assert august["amount"] == 2600.0


def test_recompute_with_zero_baseline_warns(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed(conn)
    result = recompute_statement_estimates(conn, target_obligation_id="amex_statement_payment", baseline=0.0)
    assert result["updated"] == 1
    assert any("baseline is 0" in w for w in result["warnings"])
    august = conn.execute(
        "SELECT amount FROM obligation_instances WHERE id = 'amex_statement_payment:2026-08-16'"
    ).fetchone()
    assert august["amount"] == 600.0  # only the modeled card input
