import sqlite3

from financial_agent.follow_ups import (
    capture_followup,
    list_due_followups,
    resolve_followup,
)
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def test_capture_followup_writes_to_db_only(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = capture_followup(
        conn,
        text="Review credit card statement",
        surface_when="2026-06-28",
        priority="high",
        linked_obligation_id="obl_12345",
        source="manual",
    )
    assert result["created"] is True
    assert result["updated"] is False
    assert result["status"] == "pending"
    assert result["id"].startswith("fup_")

    row = conn.execute(
        "SELECT * FROM follow_ups WHERE id = ?", (result["id"],)
    ).fetchone()
    assert row["text"] == "Review credit card statement"
    assert row["surface_when"] == "2026-06-28"
    assert row["priority"] == "high"
    assert row["linked_obligation_id"] == "obl_12345"
    assert row["source"] == "manual"
    assert row["status"] == "pending"
    assert row["created_at"] == row["updated_at"]


def test_capture_followup_idempotent(tmp_path):
    conn = _db(tmp_path / "t.db")
    first = capture_followup(
        conn, text="Pay rent", surface_when="2026-07-01", priority="normal"
    )
    second = capture_followup(
        conn, text="Pay rent", surface_when="2026-07-01", priority="normal"
    )
    assert first["id"] == second["id"]
    assert second["created"] is False
    assert second["updated"] is True

    count = conn.execute("SELECT COUNT(*) FROM follow_ups").fetchone()[0]
    assert count == 1


def test_capture_followup_rejects_empty_text(tmp_path):
    conn = _db(tmp_path / "t.db")
    try:
        capture_followup(conn, text="   ", surface_when="2026-07-01")
        assert False, "expected ValueError"
    except ValueError:
        pass
    count = conn.execute("SELECT COUNT(*) FROM follow_ups").fetchone()[0]
    assert count == 0


def test_capture_followup_nullable_linked_obligation(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = capture_followup(
        conn, text="Generic reminder", surface_when="2026-07-01"
    )
    assert result["linked_obligation_id"] is None
    assert result["priority"] is None
    row = conn.execute(
        "SELECT * FROM follow_ups WHERE id = ?", (result["id"],)
    ).fetchone()
    assert row["linked_obligation_id"] is None


def test_list_due_followups_filters_by_date(tmp_path):
    conn = _db(tmp_path / "t.db")
    for d in ("2026-06-20", "2026-06-22", "2026-06-25", "2026-07-01"):
        capture_followup(conn, text=f"item {d}", surface_when=d)

    due = list_due_followups(conn, as_of_date="2026-06-24")
    dates = [r["surface_when"] for r in due]
    assert dates == ["2026-06-20", "2026-06-22"]


def test_list_due_followups_boundary_inclusive(tmp_path):
    conn = _db(tmp_path / "t.db")
    capture_followup(conn, text="on the day", surface_when="2026-06-24")
    capture_followup(conn, text="next day", surface_when="2026-06-25")
    due = list_due_followups(conn, as_of_date="2026-06-24")
    assert [r["text"] for r in due] == ["on the day"]


def test_list_due_followups_filters_by_status(tmp_path):
    conn = _db(tmp_path / "t.db")
    pending = capture_followup(conn, text="pending one", surface_when="2026-06-24")
    resolved = capture_followup(conn, text="resolved one", surface_when="2026-06-24")
    resolve_followup(conn, followup_id=resolved["id"])

    due = list_due_followups(conn, as_of_date="2026-06-24")
    assert [r["id"] for r in due] == [pending["id"]]


def test_list_due_followups_ordering_by_priority(tmp_path):
    conn = _db(tmp_path / "t.db")
    low = capture_followup(
        conn, text="low item", surface_when="2026-06-24", priority="low"
    )
    high = capture_followup(
        conn, text="high item", surface_when="2026-06-24", priority="high"
    )
    normal = capture_followup(
        conn, text="normal item", surface_when="2026-06-24", priority="normal"
    )
    due = list_due_followups(conn, as_of_date="2026-06-24")
    assert [r["id"] for r in due] == [high["id"], normal["id"], low["id"]]


def test_list_due_followups_empty(tmp_path):
    conn = _db(tmp_path / "t.db")
    assert list_due_followups(conn, as_of_date="2026-06-24") == []


def test_resolve_followup_updates_status(tmp_path):
    conn = _db(tmp_path / "t.db")
    captured = capture_followup(conn, text="resolve me", surface_when="2026-06-24")
    created_at = conn.execute(
        "SELECT created_at, updated_at FROM follow_ups WHERE id = ?",
        (captured["id"],),
    ).fetchone()

    result = resolve_followup(conn, followup_id=captured["id"])
    assert result == {"id": captured["id"], "resolved": True}

    row = conn.execute(
        "SELECT status, created_at, updated_at FROM follow_ups WHERE id = ?",
        (captured["id"],),
    ).fetchone()
    assert row["status"] == "resolved"
    assert row["created_at"] == created_at["created_at"]
    assert row["updated_at"] >= created_at["updated_at"]


def test_resolve_followup_nonexistent(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = resolve_followup(conn, followup_id="fup_does_not_exist")
    assert result == {"id": "fup_does_not_exist", "resolved": False}


def test_resolve_followup_idempotent(tmp_path):
    conn = _db(tmp_path / "t.db")
    captured = capture_followup(conn, text="twice", surface_when="2026-06-24")
    first = resolve_followup(conn, followup_id=captured["id"])
    second = resolve_followup(conn, followup_id=captured["id"])
    assert first["resolved"] is True
    assert second["resolved"] is True
    row = conn.execute(
        "SELECT status FROM follow_ups WHERE id = ?", (captured["id"],)
    ).fetchone()
    assert row["status"] == "resolved"
