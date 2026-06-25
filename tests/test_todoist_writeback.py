"""Tests for the gated Todoist write-back sender (slice U). HTTP is mocked; no live calls."""

import json
import sqlite3

import pytest

import financial_agent.todoist_outbox as tb
from financial_agent.config import get_finance_config
from financial_agent.schema import ensure_app_schema
from financial_agent.todoist_outbox import (
    TODOIST_BASE_URL,
    execute_action_outbox,
    send_review_batch,
)

_KEY = "todoist_review_batch:2026-06-21"
_NOW = "2026-06-21T10:00:00+00:00"
_PAYLOAD = {"parent_task": {"content": "Finance review 2026-06-21", "description": "2 items"},
            "subtasks": [{"content": "drift: rent"}, {"content": "confirm: nyt"}]}


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def _insert(conn, *, dry_run=False, payload_hash="h1", status="pending", external_task_id=None, last_pushed_hash=None):
    conn.execute(
        "INSERT INTO action_outbox (id,idempotency_key,action_type,target_type,target_id,payload_json,payload_hash,"
        "dry_run,status,attempts,item_count,created_at,updated_at,external_task_id,last_pushed_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_KEY, _KEY, "todoist_review_batch", "review_batch", _KEY, json.dumps(_PAYLOAD), payload_hash,
         1 if dry_run else 0, status, 0, 2, _NOW, _NOW, external_task_id, last_pushed_hash),
    )
    conn.commit()


class _Spy:
    def __init__(self, result=None, raise_exc=None):
        self.calls = []
        self.result = result or {"task_id": "T1", "action": "created"}
        self.raise_exc = raise_exc

    def __call__(self, token, project_id, payload, *, existing_task_id=None, timeout=30):
        self.calls.append({"token": token, "project_id": project_id, "existing_task_id": existing_task_id})
        if self.raise_exc:
            raise self.raise_exc
        return self.result


def _row(conn):
    return conn.execute("SELECT status, external_task_id, last_pushed_hash, last_error FROM action_outbox WHERE id = ?", (_KEY,)).fetchone()


def test_no_send_when_write_disabled(tmp_path):
    conn = _db(tmp_path / "o.sqlite")
    _insert(conn)
    spy = _Spy()
    result = execute_action_outbox(conn, write_enabled=False, token="t", project_id="p", send_func=spy)
    assert spy.calls == []  # never touched the network
    assert result["sent"] == 0 and result["awaiting_integration"] == 1
    assert _row(conn)["status"] == "no_integration_configured"


def test_dry_run_is_simulated_not_sent(tmp_path):
    conn = _db(tmp_path / "o.sqlite")
    _insert(conn, dry_run=True)
    spy = _Spy()
    result = execute_action_outbox(conn, write_enabled=True, token="t", project_id="p", send_func=spy)
    assert spy.calls == []
    assert result["simulated"] == 1
    assert _row(conn)["status"] == "simulated"


def test_send_creates_task_when_enabled(tmp_path):
    conn = _db(tmp_path / "o.sqlite")
    _insert(conn)
    spy = _Spy({"task_id": "T1", "action": "created"})
    result = execute_action_outbox(conn, write_enabled=True, token="t", project_id="p", send_func=spy)
    assert len(spy.calls) == 1 and spy.calls[0]["existing_task_id"] is None
    assert result["sent"] == 1
    row = _row(conn)
    assert row["status"] == "succeeded" and row["external_task_id"] == "T1" and row["last_pushed_hash"] == "h1"


def test_send_is_idempotent_skip_when_unchanged(tmp_path):
    conn = _db(tmp_path / "o.sqlite")
    _insert(conn, external_task_id="T1", last_pushed_hash="h1", payload_hash="h1")
    spy = _Spy()
    result = execute_action_outbox(conn, write_enabled=True, token="t", project_id="p", send_func=spy)
    assert spy.calls == []  # already up to date -> no duplicate task
    assert result["skipped_unchanged"] == 1
    assert _row(conn)["status"] == "succeeded"


def test_send_updates_same_task_when_payload_changed(tmp_path):
    conn = _db(tmp_path / "o.sqlite")
    _insert(conn, external_task_id="T1", last_pushed_hash="h1", payload_hash="h2")  # content changed
    spy = _Spy({"task_id": "T1", "action": "updated"})
    result = execute_action_outbox(conn, write_enabled=True, token="t", project_id="p", send_func=spy)
    assert len(spy.calls) == 1 and spy.calls[0]["existing_task_id"] == "T1"  # update, not create
    assert result["updated"] == 1
    assert _row(conn)["last_pushed_hash"] == "h2"


def test_send_failure_is_recorded_not_raised(tmp_path):
    conn = _db(tmp_path / "o.sqlite")
    _insert(conn)
    spy = _Spy(raise_exc=RuntimeError("boom"))
    result = execute_action_outbox(conn, write_enabled=True, token="t", project_id="p", send_func=spy)
    assert result["failed"] == 1
    row = _row(conn)
    assert row["status"] == "failed" and "boom" in row["last_error"]


def test_outbox_send_updates_same_task_on_payload_change(tmp_path):
    """A pending row with an existing task id and a changed payload UPDATES the
    same task (preserving external_task_id) instead of creating a duplicate."""

    conn = _db(tmp_path / "o.sqlite")
    _insert(conn, external_task_id="T1", last_pushed_hash="h0", payload_hash="h1")  # content changed
    spy = _Spy({"task_id": "T1", "action": "updated"})
    result = execute_action_outbox(conn, write_enabled=True, token="t", project_id="p", send_func=spy)
    assert result["updated"] == 1 and spy.calls[0]["existing_task_id"] == "T1"
    row = _row(conn)
    assert row["external_task_id"] == "T1" and row["last_pushed_hash"] == "h1"


def test_finance_agent_env_override(tmp_path, monkeypatch):
    from financial_agent.config import load_env_file, resolve_env_path

    sandbox = tmp_path / "sandbox.env"
    sandbox.write_text("TODOIST_WRITE_ENABLED=1\nTODOIST_API_TOKEN=tok\n")
    monkeypatch.setenv("FINANCE_AGENT_ENV", str(sandbox))
    assert resolve_env_path() == sandbox
    assert load_env_file()["TODOIST_WRITE_ENABLED"] == "1"
    # an explicit path still wins over the env override
    other = tmp_path / "other.env"
    other.write_text("TODOIST_WRITE_ENABLED=0\n")
    assert resolve_env_path(other) == other


# --- v1 endpoint regression safeguards -------------------------------------
# Todoist retired the v9/v2 REST endpoints (HTTP 410). These tests pin the
# write-back sender to the current /api/v1 task create + task update paths.
# The HTTP send is mocked; no real network call is made.


class _WriteSpy:
    """Captures (token, path, body) per _write_request call."""

    def __init__(self):
        self.calls = []

    def __call__(self, token, path, body, *, timeout=30):
        self.calls.append({"token": token, "path": path, "body": body})
        return {"id": "T-new"}


def test_send_review_batch_creates_with_v1_tasks_path(monkeypatch):
    spy = _WriteSpy()
    monkeypatch.setattr(tb, "_write_request", spy)
    payload = {"parent_task": {"content": "Finance review", "description": "d"},
               "subtasks": [{"content": "sub a"}, {"content": "sub b"}]}
    result = send_review_batch("tok", "proj", payload)
    # parent create + 2 subtask creates, all POST /tasks
    assert [c["path"] for c in spy.calls] == ["/tasks", "/tasks", "/tasks"]
    assert spy.calls[0]["body"]["project_id"] == "proj"
    assert spy.calls[1]["body"]["parent_id"] == "T-new"
    assert result == {"task_id": "T-new", "action": "created"}


def test_send_review_batch_updates_with_v1_tasks_id_path(monkeypatch):
    spy = _WriteSpy()
    monkeypatch.setattr(tb, "_write_request", spy)
    payload = {"parent_task": {"content": "Finance review", "description": "d"}, "subtasks": []}
    result = send_review_batch("tok", "proj", payload, existing_task_id="T123")
    assert len(spy.calls) == 1
    assert spy.calls[0]["path"] == "/tasks/T123"  # update existing, not create
    assert result == {"task_id": "T123", "action": "updated"}


def test_send_review_batch_raises_on_null_task_id_and_skips_subtasks(monkeypatch):
    """A malformed-but-200 parent create (no id) must RAISE before any subtask is
    created, so subtasks are never orphaned at the project root and the caller
    never persists a null external_task_id. (The review batch is a single
    parent+subtasks unit, so it fails loudly here rather than partial-committing;
    execute_action_outbox catches the raise and records the row failed.)"""

    class _NoIdWriteSpy:
        def __init__(self):
            self.calls = []

        def __call__(self, token, path, body, *, timeout=30):
            self.calls.append({"path": path, "body": body})
            return {"url": "https://todoist.com/showTask"}  # 200 but no "id"

    spy = _NoIdWriteSpy()
    monkeypatch.setattr(tb, "_write_request", spy)
    payload = {"parent_task": {"content": "Finance review", "description": "d"},
               "subtasks": [{"content": "sub a"}, {"content": "sub b"}]}

    with pytest.raises(ValueError, match="no task id"):
        send_review_batch("tok", "proj", payload)

    # Only the parent create was attempted; the missing id stopped us before any
    # subtask POST, so nothing was orphaned.
    assert [c["path"] for c in spy.calls] == ["/tasks"]


def test_execute_action_outbox_records_failed_when_create_returns_no_id(tmp_path, monkeypatch):
    """End-to-end: a malformed parent create (no id) does not crash the run; the
    outbox row is marked failed with the error recorded, so a later run retries
    cleanly instead of the whole batch aborting."""

    conn = _db(tmp_path / "o.sqlite")
    _insert(conn)

    def no_id_write(token, path, body, *, timeout=30):
        return {"url": "https://todoist.com/showTask"}  # 200 but no "id"

    # The default send_func (send_review_batch) calls module-level _write_request;
    # patch it so the parent create comes back without an id.
    monkeypatch.setattr(tb, "_write_request", no_id_write)
    result = execute_action_outbox(
        conn, write_enabled=True, token="t", project_id="p", send_func=send_review_batch
    )

    assert result["failed"] == 1
    row = _row(conn)
    assert row["status"] == "failed" and "no task id" in row["last_error"]


def test_write_request_builds_v1_url(monkeypatch):
    """The real _write_request must POST to {base}/api/v1{path}."""
    assert TODOIST_BASE_URL == "https://api.todoist.com/api/v1"
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": "T1"}'

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp()

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    out = tb._write_request("tok", "/tasks/T1", {"content": "x"})
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks/T1"
    assert captured["method"] == "POST"
    assert out == {"id": "T1"}


def test_write_request_create_posts_v1_tasks(monkeypatch):
    """Task create must POST to /api/v1/tasks (not the deprecated REST v2/v9 path)."""
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": "T9"}'

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp()

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    out = tb._write_request("tok", "/tasks", {"content": "x"})
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks"
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer tok"
    assert out == {"id": "T9"}


def test_config_write_enabled_defaults_false_and_parses_env(tmp_path):
    assert get_finance_config(env_path=tmp_path / "missing.env")["todoist_write_enabled"] is False
    env = tmp_path / ".env"
    env.write_text("TODOIST_WRITE_ENABLED=true\nTODOIST_API_TOKEN=x\n")
    assert get_finance_config(env_path=env)["todoist_write_enabled"] is True
    env.write_text("TODOIST_WRITE_ENABLED=0\n")
    assert get_finance_config(env_path=env)["todoist_write_enabled"] is False
