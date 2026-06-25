"""Tests for semantic finance memory (M4): interface + context-control policy."""

import sqlite3

import pytest

from financial_agent.memory import (
    delete_memory,
    list_memories,
    search_memory,
    write_memory,
)
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


_FACTS = [
    "Gault Energy heating oil is paid on the Amex Platinum card, not from checking.",
    "Eversource electric is a direct checking utility with summer air conditioning spikes.",
    "Rent check is three thousand dollars on the third of the month.",
    "Partner payroll from the Town of Greenwich deposits biweekly on Fridays.",
    "Cash Magnet card is paid off, at zero, and not being used, so do not seed it.",
]


def _seed(conn, facts=_FACTS, kind="decision"):
    for fact in facts:
        write_memory(conn, text=fact, kind=kind)


def test_search_ranks_the_relevant_memory_first(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    _seed(conn)
    result = search_memory(conn, query="How is Gault heating oil paid?")
    assert result["returned"][0]["text"].startswith("Gault Energy heating oil")
    assert result["returned"][0]["score"] > 0


def test_top_k_caps_returned_count(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    _seed(conn)
    # A broad query that matches several memories; k bounds how many enter.
    result = search_memory(conn, query="paid checking card payroll rent", k=2, min_score=0.0)
    assert result["returned_count"] == 2
    assert result["dropped_by_k"] >= 1


def test_threshold_filters_weak_matches(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    _seed(conn)
    strict = search_memory(conn, query="Gault heating oil Amex", min_score=0.9, k=10)
    # Almost nothing clears a 0.9 cosine; the rest are dropped by threshold.
    assert strict["dropped_by_threshold"] >= 1
    assert all(r["score"] >= 0.9 for r in strict["returned"])


def test_token_budget_bounds_context(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    _seed(conn)
    tiny = search_memory(conn, query="paid checking card payroll rent", k=10, min_score=0.0, max_tokens=1)
    # At least one record always enters; the budget drops the rest.
    assert tiny["returned_count"] == 1
    assert tiny["dropped_by_budget"] >= 1
    assert tiny["used_tokens"] <= tiny["returned"][0]["token_count"]


def test_search_is_deterministic(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    _seed(conn)
    a = search_memory(conn, query="how is rent paid", k=3, min_score=0.0)
    b = search_memory(conn, query="how is rent paid", k=3, min_score=0.0)
    assert [r["id"] for r in a["returned"]] == [r["id"] for r in b["returned"]]
    assert [r["score"] for r in a["returned"]] == [r["score"] for r in b["returned"]]


def test_write_is_idempotent(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    first = write_memory(conn, text="Rent is 3000 on the third.", kind="fact")
    second = write_memory(conn, text="Rent is 3000 on the third.", kind="fact")
    assert first["created"] is True
    assert second["created"] is False
    assert first["id"] == second["id"]
    assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 1


def test_kind_filter_scopes_search(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    write_memory(conn, text="Gault is paid on Amex.", kind="decision")
    write_memory(conn, text="Gault invoice arrived.", kind="note")
    result = search_memory(conn, query="Gault", kind="decision", min_score=0.0)
    assert all(r["kind"] == "decision" for r in result["returned"])
    assert result["considered"] == 1


def test_policy_reports_all_drop_reasons(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    _seed(conn)
    result = search_memory(conn, query="Gault checking rent payroll card", k=10, min_score=0.0, max_tokens=10000)
    # The drop counters and returned set must reconcile with considered.
    assert (
        result["returned_count"]
        + result["dropped_by_threshold"]
        + result["dropped_by_k"]
        + result["dropped_by_budget"]
        == result["considered"]
    )


def test_write_rejects_empty_text(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    with pytest.raises(ValueError):
        write_memory(conn, text="   ")


def test_list_and_delete(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    rec = write_memory(conn, text="Eversource is direct checking.", kind="decision")
    assert len(list_memories(conn, kind="decision")) == 1
    assert delete_memory(conn, memory_id=rec["id"])["deleted"] is True
    assert list_memories(conn) == []
