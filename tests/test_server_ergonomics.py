"""Validation-ergonomics guards on the MCP server tool layer.

Covers the churn fixes that keep first calls from bouncing: every tool's
as_of_date defaults to today when omitted, and write_finance_memory accepts
'content' as an alias for 'text'.
"""

import inspect

import pytest

pytest.importorskip("mcp", reason="MCP server deps not installed")

from financial_agent import server


def test_no_tool_requires_as_of_date():
    # Guard: every module-level tool that takes as_of_date must default it.
    offenders = []
    for name, func in vars(server).items():
        if not inspect.isfunction(func) or name.startswith("_"):
            continue
        if func.__module__ != server.__name__:
            continue
        params = inspect.signature(func).parameters
        param = params.get("as_of_date")
        if param is not None and param.default is inspect.Parameter.empty:
            offenders.append(name)
    assert offenders == []


def test_as_of_date_defaults_to_today(tmp_path):
    # Functional check on one pure-DB tool: omitting as_of_date must work.
    db = tmp_path / "fa.sqlite"
    result = server.list_due_followups(db_path=str(db))
    assert result["items"] == []


def test_write_finance_memory_accepts_content_alias(tmp_path):
    db = tmp_path / "fa.sqlite"
    result = server.write_finance_memory(
        content="Apple Card feed is balance-only",
        kind="fact",
        source="test",
        db_path=str(db),
    )
    stored = server.list_finance_memories(kind="fact", db_path=str(db))
    assert stored["count"] == 1
    assert stored["items"][0]["text"] == "Apple Card feed is balance-only"
    assert result is not None


def test_write_finance_memory_requires_text_or_content(tmp_path):
    db = tmp_path / "fa.sqlite"
    with pytest.raises(ValueError, match="content"):
        server.write_finance_memory(db_path=str(db))
