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


def _digest_db(tmp_path):
    import sqlite3

    from financial_agent.schema import ensure_app_schema

    db = tmp_path / "fa.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL, recorded_at TEXT, source TEXT);
        CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, accounts_seen INT, transactions_inserted INT, transactions_updated INT, error TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT, amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);
        """
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('chk','CKG (4321)','Chase','checking','USD')")
    conn.execute("INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) VALUES ('chk',9000,9000,'2026-06-20T00:00:00+00:00','simplefin')")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    conn.commit()
    conn.close()
    return str(db)


def test_get_daily_digest_defaults_to_compact_summary(tmp_path):
    import json

    db = _digest_db(tmp_path)
    compact = server.get_daily_digest(as_of_date="2026-06-20", db_path=db)
    assert compact["mode"] == "summary"
    assert "markdown" not in compact
    assert "upcoming_obligations" not in compact
    # Well under the ~10k char churn target on a small fixture; the real guard is
    # that the unbounded arrays (events, findings, markdown) are gone.
    assert len(json.dumps(compact)) < 10_000

    verbose = server.get_daily_digest(as_of_date="2026-06-20", db_path=db, verbose=True)
    assert "markdown" in verbose
    assert "upcoming_obligations" in verbose


def test_list_tools_default_to_compact_rows(tmp_path):
    import sqlite3

    from financial_agent.obligations import apply_obligation_instances

    db = _digest_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    apply_obligation_instances(
        conn,
        obligation={"id": "rent", "name": "Rent check", "kind": "housing", "status": "active", "source": "seed"},
        instances=[{"due_date": "2026-07-03", "amount": -3000.0, "source": "seed", "notes": "big blob"}],
    )
    conn.commit()
    conn.close()

    # Default: instance rows are compact; full=True restores provenance fields.
    compact = server.list_obligations(db_path=db, name_contains="Rent")
    assert compact["count"] == 1
    inst = compact["items"][0]["instances"][0]
    assert "notes" not in inst and "estimation_inputs" not in inst
    full = server.list_obligations(db_path=db, name_contains="Rent", full=True)
    assert full["items"][0]["instances"][0]["notes"] == "big blob"
