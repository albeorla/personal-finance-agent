"""Tests for reconciliation close-out (slice S): confirm/unconfirm/list."""

import sqlite3

import pytest

from financial_agent.obligations import apply_obligation_instances
from financial_agent.reconciliation import (
    confirm_reconciliation_match,
    list_reconciliation_review_items,
    unconfirm_reconciliation_match,
)
from financial_agent.schema import ensure_app_schema

_INSTANCE = "nyt:2026-06-23"


def _db(path, *, with_match=True, score=0.92):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    apply_obligation_instances(
        conn,
        obligation={"id": "nyt", "name": "New York Times", "kind": "subscription", "status": "active", "source": "seed"},
        instances=[{"id": _INSTANCE, "due_date": "2026-06-23", "amount": -28.62, "source": "seed"}],
    )
    if with_match:
        now = "2026-06-21T10:00:00+00:00"
        conn.execute(
            "INSERT INTO transaction_obligation_matches (obligation_instance_id,transaction_id,match_type,match_score,amount_delta,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            # needs_review = genuinely awaits confirmation (auto matches are "cleared")
            (_INSTANCE, "TRN-xyz", "needs_review", score, 0.0, now, now),
        )
    conn.commit()
    return conn


def _status(conn, instance_id=_INSTANCE):
    return conn.execute(
        "SELECT status, matched_transaction_id, match_confidence FROM obligation_instances WHERE id = ?", (instance_id,)
    ).fetchone()


def test_confirm_marks_paid_with_evidence(tmp_path):
    conn = _db(tmp_path / "r.sqlite")
    result = confirm_reconciliation_match(conn, _INSTANCE)
    assert result["status"] == "paid"
    row = _status(conn)
    assert row["status"] == "paid"
    assert row["matched_transaction_id"] == "TRN-xyz"
    assert abs(row["match_confidence"] - 0.92) < 1e-9


def test_confirm_requires_recorded_match(tmp_path):
    conn = _db(tmp_path / "r.sqlite", with_match=False)
    with pytest.raises(ValueError, match="no recorded transaction match"):
        confirm_reconciliation_match(conn, _INSTANCE)
    assert _status(conn)["status"] == "expected"  # unchanged, never auto-paid


def test_confirm_unknown_instance_raises(tmp_path):
    conn = _db(tmp_path / "r.sqlite")
    with pytest.raises(ValueError, match="unknown obligation instance"):
        confirm_reconciliation_match(conn, "does:not-exist")


def test_confirm_is_idempotent(tmp_path):
    conn = _db(tmp_path / "r.sqlite")
    confirm_reconciliation_match(conn, _INSTANCE)
    confirm_reconciliation_match(conn, _INSTANCE)  # again
    assert _status(conn)["status"] == "paid"


def test_unconfirm_reverts_to_expected(tmp_path):
    conn = _db(tmp_path / "r.sqlite")
    confirm_reconciliation_match(conn, _INSTANCE)
    result = unconfirm_reconciliation_match(conn, _INSTANCE)
    assert result["status"] == "expected"
    row = _status(conn)
    assert row["status"] == "expected"
    assert row["matched_transaction_id"] is None and row["match_confidence"] is None


def test_list_review_items_surfaces_then_clears_after_confirm(tmp_path):
    conn = _db(tmp_path / "r.sqlite")
    items = list_reconciliation_review_items(conn, as_of_date="2026-06-30")
    assert len(items) == 1
    assert items[0]["obligation_instance_id"] == _INSTANCE
    assert items[0]["transaction_id"] == "TRN-xyz"
    assert items[0]["match_score"] == 0.92

    confirm_reconciliation_match(conn, _INSTANCE)
    assert list_reconciliation_review_items(conn, as_of_date="2026-06-30") == []  # paid no longer awaits confirmation
