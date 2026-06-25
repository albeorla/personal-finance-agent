"""Tests for Todoist read sync (slice L). No network: fetch is fixture/monkeypatched."""

import sqlite3

from financial_agent import sync_todoist as tsync
from financial_agent.config import ensure_source_tables
from financial_agent.schema import ensure_app_schema
from financial_agent.sync_todoist import (
    BASE_URL,
    fetch_project,
    normalize_task_for_storage,
    store_todoist,
    sync_todoist,
)
from financial_agent.todoist_input import import_todoist_obligations


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_source_tables(conn)
    ensure_app_schema(conn)
    return conn


def _task(tid, content, *, section_id="S1", due="2026-07-15", checked=False, labels=None, description="", is_deleted=False):
    return {
        "id": tid, "project_id": "P1", "section_id": section_id, "content": content, "description": description,
        "labels": labels or [], "due": ({"date": due, "string": due, "is_recurring": False} if due else None),
        "checked": checked, "is_deleted": is_deleted, "priority": 1,
    }


_BILLS = [{"id": "S1", "project_id": "P1", "name": "Bills & Transfers", "section_order": 1}]


def test_normalize_bills_outflow_matches_legacy():
    n = normalize_task_for_storage(_task("t1", "Pay federal tax $2,969"), "Bills & Transfers")
    assert n["amount_value"] == 2969.0
    assert n["amount_direction"] == -1
    assert n["signed_amount"] == -2969.0
    assert n["cashflow_candidate"] is True
    assert n["due_date"] == "2026-07-15"


def test_normalize_inflow_transfer():
    n = normalize_task_for_storage(_task("t2", "Transfer Owner pay to joint $3,781"), "Bills & Transfers")
    assert n["amount_direction"] == 1  # strong inflow term "transfer"
    assert n["signed_amount"] == 3781.0
    assert n["cashflow_candidate"] is True


def test_checked_or_no_amount_is_not_cashflow():
    assert normalize_task_for_storage(_task("t3", "Pay rent $3,000", checked=True), "Bills & Transfers")["cashflow_candidate"] is False
    assert normalize_task_for_storage(_task("t4", "Review the budget"), "Bills & Transfers")["cashflow_candidate"] is False


def test_negative_signal_excludes_cashflow():
    n = normalize_task_for_storage(_task("t5", "Ritual review of $500 plan", section_id=None), "")
    assert n["cashflow_candidate"] is False  # "ritual"/"review" negative signal


def test_store_is_idempotent_and_counts(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    tasks = [_task("t1", "Pay federal tax $2,969"), _task("t2", "Transfer to joint $3,781")]
    first = store_todoist(conn, tasks=tasks, sections=_BILLS, project_id="P1", fetched_at="2026-06-21T08:00:00")
    second = store_todoist(conn, tasks=tasks, sections=_BILLS, project_id="P1", fetched_at="2026-06-21T09:00:00")
    assert first["inserted"] == 2 and first["cashflow_tasks_seen"] == 2
    assert second["inserted"] == 0 and second["updated"] == 2
    assert conn.execute("SELECT COUNT(*) FROM todoist_tasks").fetchone()[0] == 2


def test_missing_task_is_marked_deleted(tmp_path):
    conn = _db(tmp_path / "t.sqlite")
    store_todoist(conn, tasks=[_task("t1", "Pay tax $100"), _task("t2", "Pay rent $3,000")],
                  sections=_BILLS, project_id="P1", fetched_at="2026-06-21T08:00:00")
    # A later sync no longer sees t2.
    res = store_todoist(conn, tasks=[_task("t1", "Pay tax $100")], sections=_BILLS, project_id="P1",
                        fetched_at="2026-06-22T08:00:00")
    assert res["missing_marked_deleted"] == 1
    assert conn.execute("SELECT is_deleted FROM todoist_tasks WHERE id='t2'").fetchone()[0] == 1


def test_sync_records_run_and_handles_fetch_error(tmp_path, monkeypatch):
    conn = _db(tmp_path / "t.sqlite")
    monkeypatch.setattr(tsync, "fetch_project", lambda *a, **k: ([_task("t1", "Pay tax $100")], _BILLS))
    res = sync_todoist(conn, token="x", project_id="P1")
    assert res["tasks_seen"] == 1 and res["error"] is None
    assert conn.execute("SELECT tasks_seen FROM todoist_sync_runs").fetchone()[0] == 1

    def _boom(*a, **k):
        raise RuntimeError("todoist down")
    monkeypatch.setattr(tsync, "fetch_project", _boom)
    res2 = sync_todoist(conn, token="x", project_id="P1")
    assert res2["error"] == "todoist down"


# --- v1 endpoint regression safeguards -------------------------------------
# Todoist retired the v9/v2 REST endpoints (HTTP 410). These tests pin the read
# sync to the current /api/v1 sections + tasks paths. The HTTP layer is mocked;
# no real network call is made.


def test_base_url_is_api_v1():
    assert BASE_URL == "https://api.todoist.com/api/v1"


def test_fetch_project_reads_v1_sections_and_tasks_paths(monkeypatch):
    paths = []

    def fake_paged(token, path, params=None, *, timeout=30):
        paths.append(path)
        return []

    monkeypatch.setattr(tsync, "_paged_request", fake_paged)
    fetch_project("tok", "P1")
    assert paths == ["/sections", "/tasks"]


def test_request_builds_v1_url(monkeypatch):
    """The real _request must GET {base}/api/v1{path}?query."""
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"results": [], "next_cursor": null}'

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp()

    monkeypatch.setattr(tsync, "urlopen", fake_urlopen)
    tsync._request("tok", "/tasks", {"project_id": "P1"})
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks?project_id=P1"
    assert captured["method"] == "GET"


def test_synced_cashflow_task_feeds_slice_g_importer(tmp_path):
    # End-to-end: L stores a cashflow task -> G imports it as a one-off obligation.
    conn = _db(tmp_path / "t.sqlite")
    store_todoist(conn, tasks=[_task("t-tax", "Pay federal tax $2,969")], sections=_BILLS,
                  project_id="P1", fetched_at="2026-06-21T08:00:00")
    result = import_todoist_obligations(conn)  # reads todoist_tasks (cashflow_candidate=1)
    assert result["imported"] == 1
    row = conn.execute("SELECT amount, direction FROM obligation_instances WHERE obligation_id='todoist_oneoff_t-tax'").fetchone()
    assert (row["amount"], row["direction"]) == (2969.0, "outflow")
