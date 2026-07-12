"""Integration contract for reviewing generic-check payment suggestions."""

import importlib
import sqlite3
from datetime import date

import pytest

from financial_agent.obligations import apply_obligation_instances
from financial_agent.reconciliation import confirm_reconciliation_match
from financial_agent.release_gate import promote_release
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT,
            first_seen_at TEXT, last_seen_at TEXT
        );
        CREATE TABLE transactions (
            id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,
            amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT,
            first_seen_at TEXT, last_seen_at TEXT, fetched_at TEXT
        );
        CREATE TABLE balance_snapshots (
            id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL,
            recorded_at TEXT, source TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO accounts (id,name,org,kind,currency) VALUES (?,?,?,?,?)",
        [
            ("ACT-chk", "PREMIER PLUS CKG (4321)", "Chase Bank", "checking", "USD"),
            ("ACT-card", "Chase Amazon", "Chase Bank", "credit_card", "USD"),
        ],
    )
    ensure_app_schema(conn)
    return conn


def _bill(
    conn,
    obligation_id,
    name,
    due_date,
    amount,
    *,
    status="expected",
    treatment="direct_checking",
):
    instance_id = f"{obligation_id}:{due_date}"
    apply_obligation_instances(
        conn,
        obligation={
            "id": obligation_id,
            "name": name,
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "source": "seed",
        },
        instances=[
            {
                "id": instance_id,
                "due_date": due_date,
                "amount": amount,
                "status": status,
                "source": "seed",
                "cash_flow_treatment": treatment,
            }
        ],
    )
    return instance_id


def test_generic_check_suggestion_reject_confirm_and_projection_lifecycle(tmp_path):
    """A generic posted check stays advisory until an explicit durable decision."""
    db_path = tmp_path / "checks.sqlite"
    conn = _db(db_path)
    june_rent = _bill(conn, "rent", "Rent", "2026-06-01", -3000.0)
    july_rent = _bill(conn, "rent", "Rent", "2026-07-01", -3000.0)
    august_rent = _bill(conn, "rent", "Rent", "2026-08-01", -3000.0)
    september_rent = _bill(conn, "rent", "Rent", "2026-09-01", -3000.0)
    october_rent = _bill(conn, "rent", "Rent", "2026-10-05", -3000.0)
    competing_bill = _bill(conn, "studio", "Studio rent", "2026-08-02", -2999.0)
    card_bill = _bill(
        conn,
        "card_rent",
        "Card-funded rent",
        "2026-08-01",
        -3000.0,
        treatment="card_statement_input",
    )
    paid_bill = _bill(
        conn,
        "paid_rent",
        "Already paid rent",
        "2026-08-01",
        -3000.0,
        status="paid",
    )
    conn.executemany(
        """
        INSERT INTO transactions
            (id,account_id,posted,amount,payee,description,pending,source)
        VALUES (?,?,?,?,?,?,?, 'simplefin')
        """,
        [
            ("check-june", "ACT-chk", "2026-06-05T09:00:00", -3000.0, "CHECK 1231", "CHECK 1231", 0),
            ("check-july", "ACT-chk", "2026-07-05T09:00:00", -3000.0, "CHECK 1232", "CHECK 1232", 0),
            ("check-august", "ACT-chk", "2026-08-05T09:00:00", -3000.0, "CHECK 1233", "CHECK 1233", 0),
            ("check-september", "ACT-chk", "2026-09-05T09:00:00", -3000.0, "CHECK 1237", "CHECK 1237", 0),
            ("check-october", "ACT-chk", "2026-10-01T09:00:00", -3000.0, "CHECK 1238", "CHECK 1238", 0),
            ("pending-check", "ACT-chk", "2026-08-04T09:00:00", -3000.0, "CHECK 1234", "CHECK 1234", 1),
            ("named-ach", "ACT-chk", "2026-08-04T09:00:00", -3000.0, "LANDLORD LLC", "RENT ACH", 0),
            ("matched-check", "ACT-chk", "2026-08-03T09:00:00", -3000.0, "CHECK 1235", "CHECK 1235", 0),
            ("card-check", "ACT-card", "2026-08-05T09:00:00", -3000.0, "CHECK 1236", "CHECK 1236", 0),
        ],
    )
    confirm_reconciliation_match(conn, june_rent, "check-june")
    conn.execute(
        """
        INSERT INTO balance_snapshots
            (account_id,balance,available,recorded_at,source)
        VALUES ('ACT-chk',9000.0,9000.0,'2026-08-06T00:00:00+00:00','simplefin')
        """
    )
    conn.execute(
        """
        INSERT INTO transaction_obligation_matches
            (obligation_instance_id,transaction_id,match_type,match_score,created_at,updated_at)
        VALUES ('historical:2026-08-01','matched-check','manual',1.0,?,?)
        """,
        ("2026-08-06T00:00:00+00:00", "2026-08-06T00:00:00+00:00"),
    )
    conn.commit()

    check_suggestions = importlib.import_module("financial_agent.check_suggestions")

    before_listing = "\n".join(conn.iterdump())
    suggestions = check_suggestions.list_check_suggestions(conn, as_of_date="2026-08-06")
    assert "\n".join(conn.iterdump()) == before_listing

    allowed_bills = {july_rent, august_rent, competing_bill}
    assert suggestions
    assert {item["bill"]["instance_id"] for item in suggestions} <= allowed_bills
    assert {item["transaction"]["id"] for item in suggestions} <= {"check-july", "check-august"}
    assert card_bill not in {item["bill"]["instance_id"] for item in suggestions}
    assert paid_bill not in {item["bill"]["instance_id"] for item in suggestions}

    july = next(
        item
        for item in suggestions
        if item["bill"]["instance_id"] == july_rent
        and item["transaction"]["id"] == "check-july"
    )
    assert july["bill"] == {
        "obligation_id": "rent",
        "instance_id": july_rent,
        "name": "Rent",
        "due_date": "2026-07-01",
        "amount": -3000.0,
        "recurrence": "monthly",
    }
    assert july["transaction"] == {
        "id": "check-july",
        "date": "2026-07-05",
        "amount": -3000.0,
        "account_id": "ACT-chk",
        "account_name": "PREMIER PLUS CKG (4321)",
        "check_identifier": "1232",
    }
    assert len(suggestions) == 3
    eligible_by_check = {
        "check-july": [july_rent],
        "check-august": [august_rent, competing_bill],
    }
    for item in suggestions:
        assert isinstance(item["score"], (int, float)) and not isinstance(item["score"], bool)
        assert 0.0 <= item["score"] <= 1.0
        assert set(item["reasons"]) == {
            "amount",
            "date",
            "account",
            "recurrence",
            "competition",
        }
        assert all(
            isinstance(reason, str) and reason.strip()
            for reason in item["reasons"].values()
        )

        evidence = item["evidence"]
        amount_delta = abs(abs(item["transaction"]["amount"]) - abs(item["bill"]["amount"]))
        expected_tolerance = max(2.0, abs(item["bill"]["amount"]) * 0.025)
        assert evidence["amount"] == {
            "delta": amount_delta,
            "tolerance": expected_tolerance,
        }
        assert evidence["date"] == {
            "delta_days": (
                date.fromisoformat(item["transaction"]["date"])
                - date.fromisoformat(item["bill"]["due_date"])
            ).days,
            "window_days": 7,
        }
        is_rent = item["bill"]["obligation_id"] == "rent"
        assert evidence["account"] == {
            "current_account_id": "ACT-chk",
            "confirmed_prior_payment_account_ids": ["ACT-chk"] if is_rent else [],
            "matches_confirmed_history": is_rent,
        }
        assert evidence["recurrence"] == {
            "confirmed_payment_count": 1 if is_rent else 0
        }
        assert evidence["competition"] == {
            "eligible_bill_count": len(eligible_by_check[item["transaction"]["id"]]),
            "eligible_bill_instance_ids": eligible_by_check[item["transaction"]["id"]],
            "score": 1.0 / len(eligible_by_check[item["transaction"]["id"]]),
        }

    assert july["ambiguous"] is False

    rejected = check_suggestions.reject_check_suggestion(conn, july["suggestion_id"])
    assert rejected["status"] == "rejected"
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    suggestions = check_suggestions.list_check_suggestions(conn, as_of_date="2026-08-06")
    assert july["suggestion_id"] not in {item["suggestion_id"] for item in suggestions}
    assert conn.execute(
        "SELECT status FROM obligation_instances WHERE id = ?", (july_rent,)
    ).fetchone()["status"] == "expected"

    august = next(
        item
        for item in suggestions
        if item["bill"]["instance_id"] == august_rent
        and item["transaction"]["id"] == "check-august"
    )
    competition = next(
        item
        for item in suggestions
        if item["bill"]["instance_id"] == competing_bill
        and item["transaction"]["id"] == "check-august"
    )
    assert august["ambiguous"] is True
    assert competition["ambiguous"] is True
    assert august["score"] < july["score"]

    future = next(
        item
        for item in check_suggestions.list_check_suggestions(
            conn, as_of_date="2026-09-06"
        )
        if item["bill"]["instance_id"] == september_rent
        and item["transaction"]["id"] == "check-september"
    )
    pre_due = next(
        item
        for item in check_suggestions.list_check_suggestions(
            conn, as_of_date="2026-10-06"
        )
        if item["bill"]["instance_id"] == october_rent
        and item["transaction"]["id"] == "check-october"
    )
    assert pre_due["evidence"]["date"]["delta_days"] == -4
    conn.close()
    promote_release(str(db_path))

    server = importlib.import_module("financial_agent.server")
    listed = server.list_check_suggestions(
        as_of_date="2026-08-06", db_path=str(db_path)
    )
    assert listed["release_warning"] is None
    assert august["suggestion_id"] in {
        item["suggestion_id"] for item in listed["items"]
    }

    with pytest.raises(ValueError, match="no longer eligible"):
        server.confirm_check_suggestion(
            suggestion_id=future["suggestion_id"],
            as_of_date="2026-08-06",
            db_path=str(db_path),
        )

    confirmed = server.confirm_check_suggestion(
        suggestion_id=august["suggestion_id"],
        as_of_date="2026-08-06",
        db_path=str(db_path),
    )
    assert confirmed["status"] == "paid"
    assert confirmed["matched_transaction_id"] == "check-august"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert tuple(
        conn.execute(
            "SELECT status, matched_transaction_id FROM obligation_instances WHERE id = ?",
            (august_rent,),
        ).fetchone()
    ) == ("paid", "check-august")
    assert august_rent not in {
        event["instance_id"] for event in confirmed["projection"]["events"]
    }

    with pytest.raises(ValueError, match="transaction.*already confirmed"):
        check_suggestions.confirm_check_suggestion(
            conn,
            competition["suggestion_id"],
            as_of_date="2026-08-06",
        )
