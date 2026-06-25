"""Tests for the gated create_todoist_task tool. HTTP is mocked; no live calls."""

import financial_agent.todoist_outbox as tb
from financial_agent.todoist_outbox import TODOIST_BASE_URL, create_todoist_task


class _Spy:
    def __init__(self, result=None, raise_exc=None):
        self.calls = []
        self.result = result or {"id": "T1", "url": "https://todoist.com/showTask?id=T1"}
        self.raise_exc = raise_exc

    def __call__(self, token, path, body, **kwargs):
        self.calls.append({"token": token, "path": path, "body": body})
        if self.raise_exc:
            raise self.raise_exc
        return self.result


def test_awaiting_integration_when_write_disabled():
    spy = _Spy()
    result = create_todoist_task("Call the bank", write_enabled=False, token="t", project_id="p", send_func=spy)
    assert spy.calls == []  # never touched the network
    assert result["status"] == "awaiting-integration"
    assert result["sent"] is False
    assert "reason" in result


def test_awaiting_integration_when_no_token():
    # An explicit empty token (not None) is treated as "no credential" without any
    # config fallback, so the gate stays closed and nothing is sent.
    spy = _Spy()
    result = create_todoist_task("Call the bank", write_enabled=True, token="", project_id="p", send_func=spy)
    assert spy.calls == []
    assert result["status"] == "awaiting-integration" and result["sent"] is False


def test_creates_task_when_enabled():
    spy = _Spy({"id": "T9", "url": "https://todoist.com/showTask?id=T9"})
    result = create_todoist_task(
        "Pay electric bill", description="Eversource", priority=3,
        write_enabled=True, token="tok", project_id="proj", send_func=spy,
    )
    assert len(spy.calls) == 1
    body = spy.calls[0]["body"]
    assert spy.calls[0]["path"] == "/tasks"
    assert body["content"] == "Pay electric bill"
    assert body["project_id"] == "proj"
    assert body["description"] == "Eversource"
    assert body["priority"] == 3
    assert result == {
        "status": "created", "sent": True, "task_id": "T9",
        "url": "https://todoist.com/showTask?id=T9",
        "content": "Pay electric bill", "project_id": "proj",
    }


def test_due_date_wins_over_due_string():
    spy = _Spy()
    create_todoist_task(
        "Reminder", due_string="today", due_date="2026-07-28",
        write_enabled=True, token="t", project_id="p", send_func=spy,
    )
    body = spy.calls[0]["body"]
    assert body["due_date"] == "2026-07-28"
    assert "due_string" not in body


def test_due_string_used_when_no_due_date():
    spy = _Spy()
    create_todoist_task(
        "Reminder", due_string="Jul 28",
        write_enabled=True, token="t", project_id="p", send_func=spy,
    )
    body = spy.calls[0]["body"]
    assert body["due_string"] == "Jul 28"
    assert "due_date" not in body


def test_default_project_id_from_config(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TODOIST_WRITE_ENABLED=1\nTODOIST_API_TOKEN=tok\nTODOIST_PROJECT_ID=cfgproj\n")
    spy = _Spy()
    result = create_todoist_task("Reminder", env_path=str(env), send_func=spy)
    assert spy.calls[0]["body"]["project_id"] == "cfgproj"
    assert spy.calls[0]["token"] == "tok"
    assert result["status"] == "created" and result["project_id"] == "cfgproj"


def test_empty_content_is_rejected_without_call():
    spy = _Spy()
    result = create_todoist_task("   ", write_enabled=True, token="t", project_id="p", send_func=spy)
    assert spy.calls == []
    assert result["status"] == "awaiting-integration" and result["sent"] is False


def test_create_failure_is_recorded_not_raised():
    spy = _Spy(raise_exc=RuntimeError("boom"))
    result = create_todoist_task("Reminder", write_enabled=True, token="t", project_id="p", send_func=spy)
    assert result["status"] == "failed" and result["sent"] is False
    assert "boom" in result["reason"]


# --- v1 endpoint regression safeguards -------------------------------------
# Todoist retired the v9/v2 REST endpoints (they now return HTTP 410). These
# tests pin the live HTTP layer to the current https://api.todoist.com/api/v1
# base so a regression to a deprecated host/path fails loudly. The HTTP send is
# mocked at urllib; no real network call is made.


def test_base_url_is_api_v1():
    assert TODOIST_BASE_URL == "https://api.todoist.com/api/v1"


def test_create_task_calls_v1_tasks_url(monkeypatch):
    """The real _write_request must build a POST to {base}/api/v1/tasks."""
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": "T1", "url": "https://todoist.com/showTask?id=T1"}'

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp()

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    # Drive the real _write_request through create_todoist_task (default send_func).
    result = create_todoist_task("Call the bank", write_enabled=True, token="tok", project_id="proj")
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks"
    assert captured["method"] == "POST"
    assert result["status"] == "created" and result["task_id"] == "T1"
