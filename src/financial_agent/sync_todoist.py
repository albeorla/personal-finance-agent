"""Todoist read sync: pull project tasks + sections (cutover slice L).

READ-ONLY against Todoist (no task create/update here; the write-back flag stays
a dry-run outbox action in todoist_input). A faithful port of
`~/dev/areas/finances/finance/todoist.py` + the Todoist upserts in
`finance/db.py`. The normalization (cashflow_candidate, amount_value,
signed_amount, amount_direction, due_date, checked, is_deleted) must match the
legacy exactly, because slice G's importer reads those fields. Idempotent: tasks
upsert by id, and tasks no longer seen are marked deleted.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import ensure_source_tables, get_finance_config


BASE_URL = "https://api.todoist.com/api/v1"
AMOUNT_RE = re.compile(r"\$([0-9][0-9,]*\.?[0-9]{0,2})")
FORECAST_AMOUNT_RE = re.compile(r"forecast amount:\s*\$([0-9][0-9,]*\.?[0-9]{0,2})", re.IGNORECASE)

_INFLOW_TERMS = ["reimbursement", "transfer", "payday", "deposit", "income", "refund"]
_STRONG_INFLOW_TERMS = ["transfer", "reimbursement", "deposit", "refund"]
_OUTFLOW_TERMS = [
    "pay ", "payment", "autopay", "bill", "rent", "tax", "statement", "minimum",
    "lease", "electric", "oil", "spotify", "water", "garbage", "fee", "check",
]
_CASHFLOW_SECTIONS = {"Bills & Transfers", "Taxes & Retirement"}
_NEGATIVE_SIGNALS = ["ritual", "review", "verify", "revisit", "call ", "execute avalanche", "use v0 credits", "roll over"]


def sync_todoist(
    conn: sqlite3.Connection,
    *,
    token: str | None = None,
    project_id: str | None = None,
    env_path: str | None = None,
    fetched_at: str | None = None,
    record_run: bool = True,
    timeout: int = 30,
) -> dict[str, Any]:
    """Fetch Todoist tasks + sections and upsert them into the local DB."""

    ensure_source_tables(conn)
    if token is None or project_id is None:
        cfg = get_finance_config(env_path=env_path)
        token = token or cfg["todoist_api_token"]
        project_id = project_id or cfg["todoist_project_id"]
    if not token or not project_id:
        raise ValueError("no TODOIST_API_TOKEN / project id configured (.env or environment)")

    started_at = _now()
    fetched_at = fetched_at or started_at
    error: str | None = None
    tasks: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    try:
        tasks, sections = fetch_project(token, project_id, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - record as a sync run
        error = str(exc)

    stored = store_todoist(conn, tasks=tasks, sections=sections, project_id=project_id, fetched_at=fetched_at)
    finished_at = _now()
    if record_run:
        _record_todoist_sync_run(
            conn, started_at=started_at, finished_at=finished_at, project_id=project_id,
            sections_seen=stored["sections_seen"], tasks_seen=stored["tasks_seen"],
            cashflow_tasks_seen=stored["cashflow_tasks_seen"], inserted=stored["inserted"],
            updated=stored["updated"], missing_marked_deleted=stored["missing_marked_deleted"], error=error,
        )
    return {**stored, "project_id": project_id, "error": error, "started_at": started_at, "finished_at": finished_at}


def fetch_project(token: str, project_id: str, *, timeout: int = 30) -> tuple[list[dict], list[dict]]:
    sections = _paged_request(token, "/sections", {"project_id": project_id}, timeout=timeout)
    tasks = _paged_request(token, "/tasks", {"project_id": project_id, "limit": 200}, timeout=timeout)
    return tasks, sections


def store_todoist(
    conn: sqlite3.Connection, *, tasks: list[dict], sections: list[dict], project_id: str, fetched_at: str
) -> dict[str, int]:
    ensure_source_tables(conn)
    section_map = {s["id"]: s["name"] for s in sections}
    for section in sections:
        _upsert_todoist_section(conn, section, fetched_at)

    inserted = updated = cashflow = 0
    seen_ids: set[str] = set()
    for task in tasks:
        section_name = section_map.get(task.get("section_id"), "")
        normalized = normalize_task_for_storage(task, section_name)
        if _upsert_todoist_task(conn, normalized, fetched_at) == "inserted":
            inserted += 1
        else:
            updated += 1
        if normalized["cashflow_candidate"]:
            cashflow += 1
        seen_ids.add(task["id"])

    missing = _mark_missing_todoist_tasks_deleted(conn, project_id=project_id, seen_ids=seen_ids, fetched_at=fetched_at)
    return {
        "sections_seen": len(sections), "tasks_seen": len(tasks), "cashflow_tasks_seen": cashflow,
        "inserted": inserted, "updated": updated, "missing_marked_deleted": missing,
    }


# --- normalization (exact mirror of finance/todoist.py) --------------------


def normalize_task_for_storage(task: dict[str, Any], section_name: str) -> dict[str, Any]:
    due = task.get("due") or {}
    deadline = task.get("deadline") or {}
    amount = _task_amount(task)
    direction = _infer_direction(task) if amount is not None else 0
    if direction == 0 and amount is not None and section_name in _CASHFLOW_SECTIONS:
        direction = -1
    cashflow_candidate = _is_cashflow_candidate(task, section_name)
    signed_amount = amount * direction if amount is not None and direction != 0 else None
    return {
        "id": task["id"],
        "project_id": task["project_id"],
        "section_id": task.get("section_id"),
        "parent_id": task.get("parent_id"),
        "content": task["content"],
        "description": task.get("description"),
        "labels_json": json.dumps(task.get("labels", [])),
        "due_date": due.get("date"),
        "due_string": due.get("string"),
        "due_is_recurring": due.get("is_recurring", False),
        "deadline_date": deadline.get("date"),
        "amount_value": amount,
        "amount_direction": direction,
        "signed_amount": signed_amount,
        "cashflow_candidate": cashflow_candidate,
        "checked": task.get("checked", False),
        "is_deleted": task.get("is_deleted", False),
        "added_at": task.get("added_at"),
        "updated_at": task.get("updated_at"),
        "completed_at": task.get("completed_at"),
        "priority": task.get("priority"),
    }


def _parse_amount(text: str) -> float | None:
    match = AMOUNT_RE.search(text or "")
    return float(match.group(1).replace(",", "")) if match else None


def _task_amount(task: dict[str, Any]) -> float | None:
    amount = _parse_amount(task["content"])
    if amount is not None:
        return amount
    match = FORECAST_AMOUNT_RE.search(task.get("description", "") or "")
    return float(match.group(1).replace(",", "")) if match else None


def _infer_direction(task: dict[str, Any]) -> int:
    title = task["content"].lower()
    text = f"{task['content']} {task.get('description', '')}".lower()
    if any(t in title for t in _STRONG_INFLOW_TERMS):
        return 1
    if any(t in title for t in _OUTFLOW_TERMS):
        return -1
    if any(t in title for t in _INFLOW_TERMS):
        return 1
    if any(t in text for t in _OUTFLOW_TERMS):
        return -1
    if any(t in text for t in _INFLOW_TERMS):
        return 1
    return 0


def _is_cashflow_candidate(task: dict[str, Any], section_name: str) -> bool:
    if task.get("checked") or not task.get("due"):
        return False
    if _task_amount(task) is None:
        return False
    labels = set(task.get("labels", []))
    if section_name in _CASHFLOW_SECTIONS:
        return True
    text = f"{task['content']} {task.get('description', '')}".lower()
    if any(signal in text for signal in _NEGATIVE_SIGNALS):
        return False
    if "cashflow" in labels:
        return True
    return _infer_direction(task) != 0


# --- http (stdlib urllib) --------------------------------------------------


def _request(token: str, path: str, params: dict[str, Any] | None = None, *, timeout: int = 30) -> dict[str, Any]:
    query = urlencode(params or {})
    url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
    req = Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "financial-agent-mcp/0.1",
    })
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - token comes from the user's own .env
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _paged_request(token: str, path: str, params: dict[str, Any] | None = None, *, timeout: int = 30) -> list[dict]:
    merged: list[dict] = []
    cursor: str | None = None
    while True:
        request_params = dict(params or {})
        if cursor:
            request_params["cursor"] = cursor
        data = _request(token, path, request_params, timeout=timeout)
        merged.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            return merged


# --- upserts (mirror finance/db.py) ----------------------------------------


def _upsert_todoist_section(conn: sqlite3.Connection, section: dict[str, Any], fetched_at: str) -> None:
    existing = conn.execute("SELECT id FROM todoist_sections WHERE id = ?", (section["id"],)).fetchone()
    values = (
        section["project_id"], section["name"], section.get("section_order"),
        int(bool(section.get("is_archived"))), int(bool(section.get("is_deleted"))),
        section.get("added_at"), section.get("updated_at"),
    )
    if existing:
        conn.execute(
            "UPDATE todoist_sections SET project_id=?, name=?, section_order=?, is_archived=?, is_deleted=?, "
            "added_at=?, updated_at=?, last_seen_at=?, fetched_at=? WHERE id=?",
            (*values, fetched_at, fetched_at, section["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO todoist_sections (id, project_id, name, section_order, is_archived, is_deleted, "
            "added_at, updated_at, first_seen_at, last_seen_at, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (section["id"], *values, fetched_at, fetched_at, fetched_at),
        )


def _upsert_todoist_task(conn: sqlite3.Connection, task: dict[str, Any], fetched_at: str) -> str:
    existing = conn.execute("SELECT id FROM todoist_tasks WHERE id = ?", (task["id"],)).fetchone()
    values = (
        task["project_id"], task.get("section_id"), task.get("parent_id"), task["content"], task.get("description"),
        task["labels_json"], task.get("due_date"), task.get("due_string"), int(bool(task.get("due_is_recurring"))),
        task.get("deadline_date"), task.get("amount_value"), int(task.get("amount_direction", 0)),
        task.get("signed_amount"), int(bool(task.get("cashflow_candidate"))), int(bool(task.get("checked"))),
        int(bool(task.get("is_deleted"))), task.get("added_at"), task.get("updated_at"), task.get("completed_at"),
        task.get("priority"),
    )
    if existing:
        conn.execute(
            "UPDATE todoist_tasks SET project_id=?, section_id=?, parent_id=?, content=?, description=?, labels_json=?, "
            "due_date=?, due_string=?, due_is_recurring=?, deadline_date=?, amount_value=?, amount_direction=?, "
            "signed_amount=?, cashflow_candidate=?, checked=?, is_deleted=?, added_at=?, updated_at=?, completed_at=?, "
            "priority=?, last_seen_at=?, fetched_at=? WHERE id=?",
            (*values, fetched_at, fetched_at, task["id"]),
        )
        return "updated"
    conn.execute(
        "INSERT INTO todoist_tasks (id, project_id, section_id, parent_id, content, description, labels_json, "
        "due_date, due_string, due_is_recurring, deadline_date, amount_value, amount_direction, signed_amount, "
        "cashflow_candidate, checked, is_deleted, added_at, updated_at, completed_at, priority, first_seen_at, "
        "last_seen_at, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task["id"], *values, fetched_at, fetched_at, fetched_at),
    )
    return "inserted"


def _mark_missing_todoist_tasks_deleted(conn, *, project_id, seen_ids, fetched_at) -> int:
    rows = conn.execute(
        "SELECT id FROM todoist_tasks WHERE project_id=? AND last_seen_at < ? AND is_deleted = 0",
        (project_id, fetched_at),
    ).fetchall()
    missing = [r["id"] for r in rows if r["id"] not in seen_ids]
    if missing:
        conn.executemany(
            "UPDATE todoist_tasks SET is_deleted=1, last_seen_at=?, fetched_at=? WHERE id=?",
            [(fetched_at, fetched_at, tid) for tid in missing],
        )
    return len(missing)


def _record_todoist_sync_run(conn, *, started_at, finished_at, project_id, sections_seen, tasks_seen,
                             cashflow_tasks_seen, inserted, updated, missing_marked_deleted, error) -> None:
    conn.execute(
        "INSERT INTO todoist_sync_runs (started_at, finished_at, project_id, sections_seen, tasks_seen, "
        "cashflow_tasks_seen, inserted, updated, missing_marked_deleted, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (started_at, finished_at, project_id, sections_seen, tasks_seen, cashflow_tasks_seen, inserted, updated,
         missing_marked_deleted, error),
    )


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")
