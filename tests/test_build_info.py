"""Tests for build/version metadata and code-staleness detection.

Why this matters: the MCP server is a long-running process, so it can keep
executing code from an older commit than what is checked out, and nothing makes
that gap visible. These tests cover the version surface (``get_version``), the
graceful fallback when there is no git repo, and the staleness flag wired into
``get_job_health``. They are hermetic: git is mocked, never shelled out to.
"""

import sqlite3

import pytest

import financial_agent.background as background
import financial_agent.build_info as build_info
from financial_agent.schema import ensure_app_schema


# --- get_version tool shape -------------------------------------------------


def test_get_version_shape(monkeypatch):
    """get_version returns exactly the running-process identity fields."""

    pytest.importorskip("mcp", reason="MCP server deps not installed")
    from financial_agent import server

    monkeypatch.setattr(build_info, "VERSION", "0.2.0")
    monkeypatch.setattr(build_info, "RUNNING_COMMIT", "abc1234")
    monkeypatch.setattr(build_info, "RUNNING_DIRTY", False)
    monkeypatch.setattr(build_info, "STARTED_AT", "2026-06-25T00:00:00+00:00")

    result = server.get_version()
    assert set(result) == {"version", "running_commit", "running_dirty", "started_at"}
    assert result["version"] == "0.2.0"
    assert result["running_commit"] == "abc1234"
    assert result["running_dirty"] is False
    assert result["started_at"] == "2026-06-25T00:00:00+00:00"


# --- no-git graceful fallback ----------------------------------------------


def test_no_git_falls_back_to_unknown(monkeypatch):
    """With no git repo, commit reads degrade to 'unknown'/False and never raise."""

    monkeypatch.setattr(build_info, "_git", lambda args: None)
    assert build_info._read_running_commit() == "unknown"
    assert build_info._read_running_dirty() is False
    assert build_info.current_repo_head() == "unknown"


def test_git_helper_returns_none_when_git_binary_missing(monkeypatch):
    """The git helper swallows a missing-binary error rather than raising."""

    def boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(build_info.subprocess, "run", boom)
    assert build_info._git(["rev-parse", "--short", "HEAD"]) is None
    assert build_info._read_running_commit() == "unknown"
    assert build_info.current_repo_head() == "unknown"
    assert build_info._read_running_dirty() is False


# --- staleness wired into get_job_health -----------------------------------


def _health_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def test_server_job_health_defaults_as_of_to_today(tmp_path):
    """The MCP get_job_health tool defaults as_of_date to today, so the daily
    health-check can call it with no arguments instead of failing validation."""

    import datetime as _dt

    pytest.importorskip("mcp", reason="MCP server deps not installed")
    from financial_agent import server

    db = tmp_path / "fa.sqlite"
    ensure_app_schema(sqlite3.connect(str(db)))

    health = server.get_job_health(db_path=str(db))
    assert health["as_of_date"] == _dt.date.today().isoformat()
    assert health["is_stale"] is True  # no completed runs in a fresh db


def test_job_health_code_stale_when_running_behind_repo(monkeypatch):
    """Running commit differs from live repo HEAD: code_stale True + reload message."""

    monkeypatch.setattr(build_info, "VERSION", "0.2.0")
    monkeypatch.setattr(build_info, "RUNNING_COMMIT", "aaaaaaa")
    monkeypatch.setattr(build_info, "STARTED_AT", "2026-06-25T00:00:00+00:00")
    monkeypatch.setattr(build_info, "current_repo_head", lambda: "bbbbbbb")

    health = background.get_job_health(_health_conn(), as_of_date="2026-06-30")

    assert health["server"] == {
        "version": "0.2.0",
        "running_commit": "aaaaaaa",
        "started_at": "2026-06-25T00:00:00+00:00",
    }
    assert health["repo_head"] == "bbbbbbb"
    assert health["code_stale"] is True
    assert health["code_stale_message"] == (
        "Running aaaaaaa; repo at bbbbbbb. Reload the MCP server to apply the newer code."
    )


def test_job_health_not_stale_when_running_matches_repo(monkeypatch):
    """Running commit equals live repo HEAD: not stale, no reload message."""

    monkeypatch.setattr(build_info, "RUNNING_COMMIT", "abc1234")
    monkeypatch.setattr(build_info, "current_repo_head", lambda: "abc1234")

    health = background.get_job_health(_health_conn(), as_of_date="2026-06-30")

    assert health["repo_head"] == "abc1234"
    assert health["code_stale"] is False
    assert "code_stale_message" not in health


def test_job_health_not_stale_when_commit_unknown(monkeypatch):
    """No git (running or repo 'unknown'): code_stale stays False, never a false alarm."""

    monkeypatch.setattr(build_info, "RUNNING_COMMIT", "unknown")
    monkeypatch.setattr(build_info, "current_repo_head", lambda: "unknown")

    health = background.get_job_health(_health_conn(), as_of_date="2026-06-30")

    assert health["repo_head"] == "unknown"
    assert health["code_stale"] is False
    assert "code_stale_message" not in health
