"""Tests for the gated Todoist task-edit tools (update/complete/reopen/delete).

HTTP is mocked the same way the create_todoist_task tests mock it; no live calls.
Each tool is covered for (a) gated-OFF -> no external call, awaiting-integration
and (b) gated-ON -> correct HTTP verb/URL/payload and success shape.
"""

import financial_agent.todoist_outbox as tb
from financial_agent.todoist_outbox import (
    complete_todoist_task,
    delete_todoist_task,
    reopen_todoist_task,
    update_todoist_task,
)


class _Spy:
    """Captures (token, path, body) per _write_request-style call."""

    def __init__(self, result=None, raise_exc=None):
        self.calls = []
        self.result = result if result is not None else {"id": "T1", "url": "https://todoist.com/showTask?id=T1"}
        self.raise_exc = raise_exc

    def __call__(self, token, path, body, **kwargs):
        self.calls.append({"token": token, "path": path, "body": body})
        if self.raise_exc:
            raise self.raise_exc
        return self.result


class _DeleteSpy:
    """Captures (token, task_id) per _delete_request-style call (returns bool)."""

    def __init__(self, raise_exc=None):
        self.calls = []
        self.raise_exc = raise_exc

    def __call__(self, token, task_id, **kwargs):
        self.calls.append({"token": token, "task_id": task_id})
        if self.raise_exc:
            raise self.raise_exc
        return True


# --- update_todoist_task ----------------------------------------------------


def test_update_awaiting_integration_when_write_disabled():
    spy = _Spy()
    result = update_todoist_task("T1", content="New", write_enabled=False, token="t", send_func=spy)
    assert spy.calls == []  # never touched the network
    assert result["status"] == "awaiting-integration"
    assert result["sent"] is False
    assert "reason" in result


def test_update_awaiting_integration_when_no_token():
    # An explicit empty token (not None) is treated as "no credential" without any
    # config fallback, so the gate stays closed and nothing is sent.
    spy = _Spy()
    result = update_todoist_task("T1", content="New", write_enabled=True, token="", send_func=spy)
    assert spy.calls == []
    assert result["status"] == "awaiting-integration" and result["sent"] is False


def test_update_sends_only_provided_fields_when_enabled():
    spy = _Spy({"id": "T1", "url": "https://todoist.com/showTask?id=T1"})
    result = update_todoist_task(
        "T1", content="Pay rent", description="July", priority=2,
        write_enabled=True, token="tok", send_func=spy,
    )
    assert len(spy.calls) == 1
    assert spy.calls[0]["path"] == "/tasks/T1"
    assert spy.calls[0]["body"] == {"content": "Pay rent", "description": "July", "priority": 2}
    assert result == {
        "status": "updated", "sent": True, "task_id": "T1",
        "url": "https://todoist.com/showTask?id=T1",
    }


def test_update_omits_unset_fields():
    spy = _Spy()
    update_todoist_task("T1", content="Only content", write_enabled=True, token="t", send_func=spy)
    body = spy.calls[0]["body"]
    assert body == {"content": "Only content"}
    assert "description" not in body and "priority" not in body and "due_date" not in body


def test_update_due_date_wins_over_due_string():
    spy = _Spy()
    update_todoist_task(
        "T1", due_string="today", due_date="2026-07-28",
        write_enabled=True, token="t", send_func=spy,
    )
    body = spy.calls[0]["body"]
    assert body["due_date"] == "2026-07-28"
    assert "due_string" not in body


def test_update_due_string_used_when_no_due_date():
    spy = _Spy()
    update_todoist_task("T1", due_string="Jul 28", write_enabled=True, token="t", send_func=spy)
    body = spy.calls[0]["body"]
    assert body["due_string"] == "Jul 28"
    assert "due_date" not in body


def test_update_project_id_moves_task():
    spy = _Spy()
    update_todoist_task("T1", project_id="proj2", write_enabled=True, token="t", send_func=spy)
    assert spy.calls[0]["body"] == {"project_id": "proj2"}


def test_update_empty_task_id_rejected_without_call():
    spy = _Spy()
    result = update_todoist_task("   ", content="x", write_enabled=True, token="t", send_func=spy)
    assert spy.calls == []
    assert result["status"] == "awaiting-integration" and result["sent"] is False


def test_update_no_fields_rejected_without_call():
    spy = _Spy()
    result = update_todoist_task("T1", write_enabled=True, token="t", send_func=spy)
    assert spy.calls == []
    assert result["status"] == "awaiting-integration" and result["sent"] is False
    assert "no fields" in result["reason"]


def test_update_failure_is_recorded_not_raised():
    spy = _Spy(raise_exc=RuntimeError("boom"))
    result = update_todoist_task("T1", content="x", write_enabled=True, token="t", send_func=spy)
    assert result["status"] == "failed" and result["sent"] is False
    assert "boom" in result["reason"]


def test_update_uses_config_token(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TODOIST_WRITE_ENABLED=1\nTODOIST_API_TOKEN=tok\n")
    spy = _Spy()
    result = update_todoist_task("T1", content="x", env_path=str(env), send_func=spy)
    assert spy.calls[0]["token"] == "tok"
    assert result["status"] == "updated"


# --- complete_todoist_task --------------------------------------------------


def test_complete_awaiting_integration_when_write_disabled():
    spy = _Spy()
    result = complete_todoist_task("T1", write_enabled=False, token="t", send_func=spy)
    assert spy.calls == []
    assert result["status"] == "awaiting-integration" and result["sent"] is False


def test_complete_posts_close_endpoint_when_enabled():
    spy = _Spy({})  # 204 No Content -> _write_request returns {}
    result = complete_todoist_task("T1", write_enabled=True, token="tok", send_func=spy)
    assert len(spy.calls) == 1
    assert spy.calls[0]["path"] == "/tasks/T1/close"
    assert spy.calls[0]["body"] == {}
    assert result == {"status": "completed", "sent": True, "task_id": "T1"}


def test_complete_empty_task_id_rejected_without_call():
    spy = _Spy()
    result = complete_todoist_task("  ", write_enabled=True, token="t", send_func=spy)
    assert spy.calls == []
    assert result["status"] == "awaiting-integration" and result["sent"] is False


def test_complete_failure_is_recorded_not_raised():
    spy = _Spy(raise_exc=RuntimeError("boom"))
    result = complete_todoist_task("T1", write_enabled=True, token="t", send_func=spy)
    assert result["status"] == "failed" and result["sent"] is False
    assert "boom" in result["reason"]


# --- reopen_todoist_task ----------------------------------------------------


def test_reopen_awaiting_integration_when_write_disabled():
    spy = _Spy()
    result = reopen_todoist_task("T1", write_enabled=False, token="t", send_func=spy)
    assert spy.calls == []
    assert result["status"] == "awaiting-integration" and result["sent"] is False


def test_reopen_posts_reopen_endpoint_when_enabled():
    spy = _Spy({})  # 204 No Content -> _write_request returns {}
    result = reopen_todoist_task("T1", write_enabled=True, token="tok", send_func=spy)
    assert len(spy.calls) == 1
    assert spy.calls[0]["path"] == "/tasks/T1/reopen"
    assert spy.calls[0]["body"] == {}
    assert result == {"status": "reopened", "sent": True, "task_id": "T1"}


def test_reopen_failure_is_recorded_not_raised():
    spy = _Spy(raise_exc=RuntimeError("boom"))
    result = reopen_todoist_task("T1", write_enabled=True, token="t", send_func=spy)
    assert result["status"] == "failed" and result["sent"] is False
    assert "boom" in result["reason"]


# --- delete_todoist_task ----------------------------------------------------


def test_delete_awaiting_integration_when_write_disabled():
    spy = _DeleteSpy()
    result = delete_todoist_task("T1", write_enabled=False, token="t", delete_func=spy)
    assert spy.calls == []
    assert result["status"] == "awaiting-integration" and result["sent"] is False


def test_delete_calls_delete_func_when_enabled():
    spy = _DeleteSpy()
    result = delete_todoist_task("T1", write_enabled=True, token="tok", delete_func=spy)
    assert spy.calls == [{"token": "tok", "task_id": "T1"}]
    assert result == {"status": "deleted", "sent": True, "task_id": "T1"}


def test_delete_empty_task_id_rejected_without_call():
    spy = _DeleteSpy()
    result = delete_todoist_task("   ", write_enabled=True, token="t", delete_func=spy)
    assert spy.calls == []
    assert result["status"] == "awaiting-integration" and result["sent"] is False


def test_delete_failure_is_recorded_not_raised():
    spy = _DeleteSpy(raise_exc=RuntimeError("boom"))
    result = delete_todoist_task("T1", write_enabled=True, token="t", delete_func=spy)
    assert result["status"] == "failed" and result["sent"] is False
    assert "boom" in result["reason"]


# --- v1 endpoint regression safeguards -------------------------------------
# Todoist retired the v9/v2 REST endpoints (they now return HTTP 410). These
# tests drive the REAL _write_request / _delete_request through create-style
# urllib mocking to pin every task-edit tool to the current
# https://api.todoist.com/api/v1 base and the correct HTTP verb. No real network
# call is made.


class _FakeResp:
    def __init__(self, body=b""):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_update_calls_v1_tasks_id_url(monkeypatch):
    """The real _write_request must build a POST to {base}/api/v1/tasks/<id>."""
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp(b'{"id": "T1", "url": "https://todoist.com/showTask?id=T1"}')

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    result = update_todoist_task("T1", content="x", write_enabled=True, token="tok")
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks/T1"
    assert captured["method"] == "POST"
    assert result["status"] == "updated" and result["task_id"] == "T1"


def test_complete_calls_v1_close_url(monkeypatch):
    """Complete must POST to {base}/api/v1/tasks/<id>/close and handle 204."""
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp(b"")  # 204 No Content

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    result = complete_todoist_task("T1", write_enabled=True, token="tok")
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks/T1/close"
    assert captured["method"] == "POST"
    assert result == {"status": "completed", "sent": True, "task_id": "T1"}


def test_reopen_calls_v1_reopen_url(monkeypatch):
    """Reopen must POST to {base}/api/v1/tasks/<id>/reopen and handle 204."""
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp(b"")  # 204 No Content

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    result = reopen_todoist_task("T1", write_enabled=True, token="tok")
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks/T1/reopen"
    assert captured["method"] == "POST"
    assert result == {"status": "reopened", "sent": True, "task_id": "T1"}


def test_delete_calls_v1_tasks_id_url_with_delete_verb(monkeypatch):
    """Delete must DELETE {base}/api/v1/tasks/<id> and handle 204."""
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp(b"")  # 204 No Content

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    result = delete_todoist_task("T1", write_enabled=True, token="tok")
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks/T1"
    assert captured["method"] == "DELETE"
    assert result == {"status": "deleted", "sent": True, "task_id": "T1"}
