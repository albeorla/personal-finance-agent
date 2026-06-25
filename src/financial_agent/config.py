"""Runtime config for live ingest: credentials and source-table schema.

Credentials live in the legacy finances `.env` (SIMPLEFIN_ACCESS_URL,
TODOIST_API_TOKEN, TODOIST_PROJECT_ID). This module reads them at runtime; it
never logs or persists the secret values. The
`has_simplefin` / `has_todoist` booleans are safe to surface (they say whether a
credential is present, not what it is).

It also owns `ensure_source_tables`, the DDL for the SimpleFIN/Todoist source
tables (accounts, balance_snapshots, transactions, sync_runs, todoist_*), so a
sync can populate a fresh database. This mirrors the legacy
`~/dev/areas/finances/finance/db.py` schema.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_FINANCE_ENV = Path("~/dev/areas/finances/.env").expanduser()
DEFAULT_OBLIGATIONS_YAML = Path("~/dev/areas/finances/obligations.yaml").expanduser()


def resolve_env_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the .env location: explicit arg, then FINANCE_AGENT_ENV, then default.

    The FINANCE_AGENT_ENV override lets a registered MCP server read a sandbox
    .env (set it in the server's env block) without touching the real workspace.
    """

    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get("FINANCE_AGENT_ENV")
    return Path(override).expanduser() if override else DEFAULT_FINANCE_ENV


def load_env_file(path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Parse a .env file into a dict. Never mutates os.environ."""

    target = resolve_env_path(path)
    values: dict[str, str] = {}
    if not target.exists():
        return values
    for raw in target.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def get_finance_config(
    *,
    env_path: str | os.PathLike[str] | None = None,
    obligations_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Resolve live-ingest credentials. The .env wins, then the process env."""

    env = load_env_file(env_path)

    def pick(key: str) -> str | None:
        return env.get(key) or os.environ.get(key)

    project_id = pick("TODOIST_PROJECT_ID")
    if not project_id:
        # DEPRECATED fallback: read the Todoist project id from the retired
        # obligations.yaml. obligations.yaml is no longer authoritative for
        # cash-flow events (those live in the obligation_instances table); only
        # this single id is still read for backward compatibility. Set
        # TODOIST_PROJECT_ID in the finances .env to drop this fallback.
        ob_path = Path(obligations_path).expanduser() if obligations_path else DEFAULT_OBLIGATIONS_YAML
        if ob_path.exists():
            try:
                project_id = json.loads(ob_path.read_text()).get("todoist_project_id")
            except (ValueError, OSError):
                project_id = None

    access_url = pick("SIMPLEFIN_ACCESS_URL")
    token = pick("TODOIST_API_TOKEN")
    # Live Todoist write-back is OFF unless explicitly enabled. Reads stay allowed.
    write_enabled = str(pick("TODOIST_WRITE_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "simplefin_access_url": access_url,
        "todoist_api_token": token,
        "todoist_project_id": project_id,
        "has_simplefin": bool(access_url),
        "has_todoist": bool(token and project_id),
        "todoist_write_enabled": write_enabled,
    }


SOURCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, org TEXT, kind TEXT, currency TEXT,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, balance REAL NOT NULL,
    available REAL NOT NULL, recorded_at TEXT NOT NULL, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_balance_snapshots_account_recorded
    ON balance_snapshots(account_id, recorded_at);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, posted TEXT, transacted_at TEXT,
    amount REAL NOT NULL, payee TEXT, description TEXT, pending INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transactions_account_posted ON transactions(account_id, posted);
CREATE INDEX IF NOT EXISTS idx_transactions_payee ON transactions(payee);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
    mode TEXT NOT NULL, accounts_seen INTEGER NOT NULL, transactions_inserted INTEGER NOT NULL,
    transactions_updated INTEGER NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS todoist_sections (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, section_order INTEGER,
    is_archived INTEGER NOT NULL DEFAULT 0, is_deleted INTEGER NOT NULL DEFAULT 0,
    added_at TEXT, updated_at TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todoist_tasks (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, section_id TEXT, parent_id TEXT,
    content TEXT NOT NULL, description TEXT, labels_json TEXT NOT NULL, due_date TEXT, due_string TEXT,
    due_is_recurring INTEGER NOT NULL DEFAULT 0, deadline_date TEXT, amount_value REAL,
    amount_direction INTEGER NOT NULL DEFAULT 0, signed_amount REAL, cashflow_candidate INTEGER NOT NULL DEFAULT 0,
    checked INTEGER NOT NULL DEFAULT 0, is_deleted INTEGER NOT NULL DEFAULT 0, added_at TEXT, updated_at TEXT,
    completed_at TEXT, priority INTEGER, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todoist_tasks_project_due ON todoist_tasks(project_id, due_date);
CREATE TABLE IF NOT EXISTS todoist_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
    project_id TEXT NOT NULL, sections_seen INTEGER NOT NULL, tasks_seen INTEGER NOT NULL,
    cashflow_tasks_seen INTEGER NOT NULL, inserted INTEGER NOT NULL, updated INTEGER NOT NULL,
    missing_marked_deleted INTEGER NOT NULL, error TEXT
);
"""


def ensure_source_tables(conn: sqlite3.Connection) -> None:
    """Create the SimpleFIN/Todoist source tables if absent (idempotent)."""

    conn.executescript(SOURCE_SCHEMA)
