"""Todoist reflection: review-batch preview and a durable action outbox.

Todoist is a reflection/action surface, not the source of financial truth. This
module renders the day's review items (drift findings) into a Todoist task /
subtask payload and records intended writes in a durable, idempotent outbox.

Live Todoist write-back is GATED OFF by default. The sender exists
(``send_review_batch`` + the live path in ``execute_action_outbox``) but only
fires when ``TODOIST_WRITE_ENABLED`` is set AND a token/project are configured.
With the flag off (the default), the outbox simulates dry-run items and marks
pending items ``no_integration_configured`` - no external side effect. When on,
sending is idempotent: one task per outbox key, updated in place on rerun.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import Any, Callable

from .config import get_finance_config
from .drift import detect_drift
from .follow_ups import resolve_followup
from .schema import ensure_app_schema


ACTION_TYPE = "todoist_review_batch"
TODOIST_BASE_URL = "https://api.todoist.com/api/v1"

DEFAULT_OPTIONS: dict[str, Any] = {
    "min_severity": "low",          # include findings at or above this severity
    "include_recurring": True,      # include unexpected-recurring (onboarding) items
}

_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def preview_todoist_review_batch(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render the day's review batch as a Todoist task + subtasks. Read-only."""

    ensure_app_schema(conn)
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    as_of = _coerce_date(as_of_date)
    return _build_batch(conn, as_of, opts)


def enqueue_todoist_review_batch(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
    options: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Record the day's review batch in the durable outbox. No live write.

    Idempotent: one batch per day keyed by ``todoist_review_batch:<date>``. A
    succeeded item is never re-queued; a dry-run or pending item is updated in
    place when the payload changes.
    """

    ensure_app_schema(conn)
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    as_of = _coerce_date(as_of_date)
    batch = _build_batch(conn, as_of, opts)
    idempotency_key = batch["idempotency_key"]
    payload = {"parent_task": batch["parent_task"], "subtasks": batch["subtasks"]}
    payload_json = json.dumps(payload, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()[:16]
    now = _now()

    existing = conn.execute(
        "SELECT status, payload_hash FROM action_outbox WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()

    # A succeeded batch is skipped only when nothing changed. When the day's
    # findings change, re-queue it: execute() then UPDATES the same task (the
    # external_task_id is preserved) instead of leaving a stale board task.
    if existing is not None and existing["status"] == "succeeded" and existing["payload_hash"] == payload_hash:
        return {"idempotency_key": idempotency_key, "status": "succeeded", "action": "skipped_already_sent", "item_count": batch["item_count"]}

    target_status = "dry_run" if dry_run else "pending"
    if existing is None:
        conn.execute(
            """
            INSERT INTO action_outbox (
                id, idempotency_key, action_type, target_type, target_id,
                payload_json, payload_hash, dry_run, status, attempts, item_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'review_batch', ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (idempotency_key, idempotency_key, ACTION_TYPE, batch["batch_id"], payload_json,
             payload_hash, 1 if dry_run else 0, target_status, batch["item_count"], now, now),
        )
        action = "created"
    else:
        conn.execute(
            """
            UPDATE action_outbox
            SET payload_json = ?, payload_hash = ?, dry_run = ?, status = ?,
                item_count = ?, updated_at = ?
            WHERE idempotency_key = ?
            """,
            (payload_json, payload_hash, 1 if dry_run else 0, target_status, batch["item_count"], now, idempotency_key),
        )
        action = "updated" if existing["payload_hash"] != payload_hash else "unchanged"

    return {
        "idempotency_key": idempotency_key,
        "status": target_status,
        "action": action,
        "dry_run": dry_run,
        "item_count": batch["item_count"],
        "payload_hash": payload_hash,
    }


def _write_request(token: str, path: str, body: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    """POST JSON to the Todoist REST API. Only called on the gated live path."""

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - token is the user's own .env credential
        f"{TODOIST_BASE_URL}{path}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "financial-agent-mcp/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


# Sentinel returned by the read client when a task no longer exists (HTTP 404).
# A completed or deleted task drops out of the active-tasks endpoint, so a 404 is
# treated as "the user resolved it" rather than a hard error.
TASK_NOT_FOUND = "__not_found__"


def _read_task(token: str, task_id: str, *, timeout: int = 30) -> dict[str, Any] | str:
    """GET a single active task. Returns the task dict, or ``TASK_NOT_FOUND`` on 404.

    Only called on the gated read path. A 404 means the task is completed or
    deleted (it left the active-tasks endpoint); any other HTTP error propagates
    so the caller records it and never silently resolves a still-open item.
    """

    req = urllib.request.Request(  # noqa: S310 - token is the user's own .env credential
        f"{TODOIST_BASE_URL}/tasks/{task_id}",
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "financial-agent-mcp/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return TASK_NOT_FOUND
        raise
    return json.loads(raw) if raw else {}


def send_review_batch(
    token: str,
    project_id: str,
    payload: dict[str, Any],
    *,
    existing_task_id: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Create or update the day's review task in Todoist. Idempotent at the call
    site: pass ``existing_task_id`` to update the same task instead of creating a
    new one. Subtasks are only created on first send.
    """

    parent = payload.get("parent_task", {})
    content = parent.get("content", "Finance review")
    description = parent.get("description", "")

    if existing_task_id:
        _write_request(token, f"/tasks/{existing_task_id}", {"content": content, "description": description}, timeout=timeout)
        return {"task_id": existing_task_id, "action": "updated"}

    created = _write_request(token, "/tasks", {"project_id": project_id, "content": content, "description": description}, timeout=timeout)
    task_id = created.get("id")
    # A malformed-but-200 create can omit the id. Without a parent id every
    # subtask would be created orphaned at the project root, and the caller would
    # persist a null external_task_id it can never update. Fail loudly instead so
    # execute_action_outbox records this row failed and retries cleanly.
    if not task_id:
        raise ValueError("Todoist create returned no task id")
    for sub in payload.get("subtasks", []):
        _write_request(token, "/tasks", {"project_id": project_id, "parent_id": task_id, "content": sub.get("content", "")}, timeout=timeout)
    return {"task_id": task_id, "action": "created"}


def create_todoist_task(
    content: str,
    *,
    due_string: str | None = None,
    due_date: str | None = None,
    description: str | None = None,
    priority: int | None = None,
    project_id: str | None = None,
    write_enabled: bool | None = None,
    token: str | None = None,
    env_path: str | None = None,
    send_func: Callable[..., dict[str, Any]] = _write_request,
) -> dict[str, Any]:
    """Create a free-form Todoist task. Live write is GATED OFF by default.

    For one-off reminders (e.g. "call the bank Jul 28"). This is a direct create,
    not an idempotent outbox row: a one-off reminder has no stable key and re-running
    would mean re-creating the same intent, so it is the caller's responsibility not
    to call twice. (The drift/review batch is the path that needs idempotency; this
    one does not, by design.)

    The live write fires ONLY when ``write_enabled`` is true (resolved from
    TODOIST_WRITE_ENABLED in the finances .env when not passed) AND a token + a
    project id are available. Otherwise this makes NO external HTTP call and returns
    ``{"status": "awaiting-integration", "sent": False, "reason": ...}``. Secrets are
    never logged or returned.

    Due handling: if both ``due_date`` (ISO yyyy-mm-dd) and ``due_string`` are given,
    ``due_date`` wins. ``priority`` is the Todoist 1-4 scale. ``project_id`` defaults
    to the configured finance project.
    """

    content = (content or "").strip()
    if not content:
        return {"status": "awaiting-integration", "sent": False, "reason": "content is required"}

    # Resolve the gate. When write_enabled is explicitly False, do NOT read config
    # so tests stay hermetic and can never fire a live send. Config only fills gaps.
    if write_enabled is None:
        cfg = get_finance_config(env_path=env_path)
        write_enabled = cfg["todoist_write_enabled"]
        token = token if token is not None else cfg["todoist_api_token"]
        project_id = project_id if project_id is not None else cfg["todoist_project_id"]
    elif write_enabled and (token is None or project_id is None):
        cfg = get_finance_config(env_path=env_path)
        token = token if token is not None else cfg["todoist_api_token"]
        project_id = project_id if project_id is not None else cfg["todoist_project_id"]

    if not write_enabled:
        return {"status": "awaiting-integration", "sent": False,
                "reason": "Todoist write-back disabled (TODOIST_WRITE_ENABLED off)"}
    if not token or not project_id:
        return {"status": "awaiting-integration", "sent": False,
                "reason": "Todoist token or project id not configured"}

    body: dict[str, Any] = {"content": content, "project_id": project_id}
    if description:
        body["description"] = description
    if priority is not None:
        body["priority"] = priority
    # due_date (ISO) wins over due_string when both are supplied.
    if due_date:
        body["due_date"] = due_date
    elif due_string:
        body["due_string"] = due_string

    try:
        created = send_func(token, "/tasks", body)
    except Exception as exc:  # noqa: BLE001 - surface a clean failure, never crash the tool
        return {"status": "failed", "sent": False,
                "reason": f"create failed: {type(exc).__name__}: {exc}"[:300]}

    return {
        "status": "created",
        "sent": True,
        "task_id": created.get("id"),
        "url": created.get("url"),
        "content": content,
        "project_id": project_id,
    }


# --- idempotent surfacing ledger -------------------------------------------
# The daily job pushes due items (follow-ups, behind goals, estimate reviews,
# stale snapshots) to Todoist and MUST NEVER duplicate them: not across days,
# not on a re-run, and not when the user already created the task by hand. The
# todoist_emissions ledger (schema.py) is the single source of truth keyed by a
# stable surface_key. surface_to_todoist UPSERTS each item against the ledger;
# reconcile_emission adopts a task that already carries the [fa:<key>] marker.

# Label attached to every auto-surfaced task. Prefixed/versioned so it does not
# collide with a label the user may already own and can be widened later.
FA_AUTO_LABEL = "fa-auto"


def surface_marker(surface_key: str) -> str:
    """The visible, parseable marker embedded in a task description."""

    return f"[fa:{surface_key}]"


def extract_surface_key(description: str | None) -> str | None:
    """Pull the surface_key out of a task description carrying ``[fa:<key>]``.

    Used when scanning Todoist for manually-created (or prior) tasks so they can
    be adopted via ``reconcile_emission`` instead of duplicated.
    """

    if not description:
        return None
    start = description.find("[fa:")
    if start == -1:
        return None
    end = description.find("]", start)
    if end == -1:
        return None
    key = description[start + 4 : end].strip()
    return key or None


def content_hash_for(content: str, description: str) -> str:
    """Stable 16-char hash of the surfaced content, for change detection."""

    raw = f"{content}\n{description}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _emission_description(description: str, surface_key: str) -> str:
    """Append the ``[fa:<key>]`` marker to a description (idempotently)."""

    base = (description or "").rstrip()
    marker = surface_marker(surface_key)
    if marker in base:
        return base
    return f"{base}\n\n{marker}" if base else marker


def _create_surface_task(
    token: str,
    project_id: str,
    content: str,
    description: str,
    surface_key: str,
    *,
    due_date: str | None,
    priority: int | None,
    send_func: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "project_id": project_id,
        "content": content,
        "description": _emission_description(description, surface_key),
        "labels": [FA_AUTO_LABEL],
    }
    if due_date:
        body["due_date"] = due_date
    if priority is not None:
        body["priority"] = priority
    return send_func(token, "/tasks", body)


def _update_surface_task(
    token: str,
    task_id: str,
    content: str,
    description: str,
    surface_key: str,
    *,
    due_date: str | None,
    priority: int | None,
    send_func: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "content": content,
        "description": _emission_description(description, surface_key),
    }
    if due_date:
        body["due_date"] = due_date
    if priority is not None:
        body["priority"] = priority
    return send_func(token, f"/tasks/{task_id}", body)


def reconcile_emission(
    conn: sqlite3.Connection,
    surface_key: str,
    todoist_task_id: str,
    content_hash: str,
) -> dict[str, Any]:
    """Adopt an existing Todoist task that carries a ``[fa:<key>]`` marker.

    Called when scanning Todoist tasks and finding a marker for a key that has no
    ledger row yet (a manually-created task, or a task from a prior install).
    Inserting the ledger row makes future ``surface_to_todoist`` runs skip the
    task instead of creating a duplicate.
    """

    ensure_app_schema(conn)
    now = _now()
    existing = conn.execute(
        "SELECT surface_key FROM todoist_emissions WHERE surface_key = ?", (surface_key,)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO todoist_emissions (
            surface_key, todoist_task_id, status, content_hash, created_at, last_seen
        ) VALUES (?, ?, 'open', ?, ?, ?)
        ON CONFLICT(surface_key) DO UPDATE SET
            todoist_task_id = excluded.todoist_task_id,
            last_seen = excluded.last_seen
        """,
        (surface_key, todoist_task_id, content_hash, now, now),
    )
    return {
        "surface_key": surface_key,
        "todoist_task_id": todoist_task_id,
        "action": "updated" if existing is not None else "adopted",
    }


def mark_emission_status(
    conn: sqlite3.Connection,
    surface_key: str,
    status: str,
) -> dict[str, Any]:
    """Record that the user completed or deleted the task for ``surface_key``.

    When a surfaced task is closed in Todoist, the source item is considered
    resolved by the user; ``surface_to_todoist`` will then NOT recreate it.
    """

    ensure_app_schema(conn)
    now = _now()
    cur = conn.execute(
        "UPDATE todoist_emissions SET status = ?, last_seen = ? WHERE surface_key = ?",
        (status, now, surface_key),
    )
    return {"surface_key": surface_key, "status": status, "updated": cur.rowcount}


def _task_is_completed(task: dict[str, Any]) -> bool:
    """Whether a fetched v1 task object represents a completed/closed task.

    The v1 task carries ``checked`` (the completion boolean); ``is_completed`` is
    the older alias some clients still return. ``completed_at`` being set is a
    third signal. Any one means the user checked it off.
    """

    return bool(task.get("checked") or task.get("is_completed") or task.get("completed_at"))


def reconcile_todoist_completions(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
    write_enabled: bool | None = None,
    token: str | None = None,
    env_path: str | None = None,
    read_func: Callable[..., dict[str, Any] | str] = _read_task,
) -> dict[str, Any]:
    """Map user-completed/deleted Todoist tasks back to the emissions ledger.

    For every open ``todoist_emissions`` row, fetch the task's current state from
    Todoist (GET ``/tasks/<id>``). A 404 means the task was completed or deleted
    (it left the active-tasks endpoint); a 200 whose ``checked``/``is_completed``/
    ``completed_at`` is set means the user checked it off. Either way the emission
    is marked resolved so ``surface_to_todoist`` never recreates it, and any
    follow-up linked by a ``followup:<id>`` surface_key is resolved too.

    Gated by ``TODOIST_WRITE_ENABLED`` (and a configured token), exactly like the
    other Todoist calls: when the gate is closed this makes NO external call and
    no-ops. The read itself is read-only against Todoist; the only writes are to
    the LOCAL ledger / follow-ups. Secrets are never logged or returned.
    """

    ensure_app_schema(conn)
    _coerce_date(as_of_date)  # validate shape; reconciliation is date-agnostic per row

    # Resolve the gate. When write_enabled is explicitly False, do NOT read config
    # so tests stay hermetic and can never fire a live read. Config fills gaps only.
    if write_enabled is None:
        cfg = get_finance_config(env_path=env_path)
        write_enabled = cfg["todoist_write_enabled"]
        token = token if token is not None else cfg["todoist_api_token"]
    elif write_enabled and token is None:
        cfg = get_finance_config(env_path=env_path)
        token = token if token is not None else cfg["todoist_api_token"]
    live = bool(write_enabled and token)

    summary: dict[str, Any] = {
        "status": "ok" if live else "awaiting-integration",
        "integration_enabled": live,
        "checked": 0,
        "resolved": 0,
        "followups_resolved": 0,
        "still_open": 0,
        "failed": 0,
        "items": [],
    }

    if not live:
        summary["reason"] = "Todoist write-back disabled (TODOIST_WRITE_ENABLED off or no token)"
        return summary

    rows = conn.execute(
        "SELECT surface_key, todoist_task_id FROM todoist_emissions WHERE status = 'open'"
    ).fetchall()

    for row in rows:
        key = row["surface_key"]
        task_id = row["todoist_task_id"]
        summary["checked"] += 1
        try:
            task = read_func(token, task_id)
        except Exception as exc:  # noqa: BLE001 - record, never crash the batch
            summary["failed"] += 1
            summary["items"].append({"surface_key": key, "action": "failed", "reason": f"{type(exc).__name__}: {exc}"[:200]})
            continue

        if task == TASK_NOT_FOUND:
            disposition = "deleted_by_user"
        elif isinstance(task, dict) and _task_is_completed(task):
            disposition = "completed"
        else:
            summary["still_open"] += 1
            summary["items"].append({"surface_key": key, "action": "unchanged"})
            continue

        mark_emission_status(conn, key, disposition)
        summary["resolved"] += 1
        item: dict[str, Any] = {"surface_key": key, "action": "resolved", "status": disposition}

        # Resolve a linked follow-up so it does not surface again either. The
        # follow-up id is carried in the surface_key (followup:<id>).
        if key.startswith("followup:"):
            followup_id = key[len("followup:") :]
            res = resolve_followup(conn, followup_id)
            if res.get("resolved"):
                summary["followups_resolved"] += 1
                item["followup_resolved"] = followup_id

        summary["items"].append(item)

    return summary


def surface_to_todoist(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
    as_of_date: date | str,
    *,
    write_enabled: bool | None = None,
    token: str | None = None,
    project_id: str | None = None,
    env_path: str | None = None,
    send_func: Callable[..., dict[str, Any]] = _write_request,
) -> dict[str, Any]:
    """Push due items to Todoist with automatic de-duplication via the ledger.

    Each item is ``{surface_key, content, description?, due_date?, priority?}``.
    For every item, against the ``todoist_emissions`` ledger:
    - no ledger row -> create task (with ``[fa:<key>]`` marker + ``fa-auto``
      label), insert ledger row (status='open')
    - row exists + open + content_hash unchanged -> skip (idempotent)
    - row exists + open + content_hash changed -> update the same task in place,
      refresh the ledger hash (never recreate)
    - row exists + completed / deleted_by_user -> the user resolved it; mark the
      source item resolved and do NOT recreate

    Gated by ``TODOIST_WRITE_ENABLED`` exactly like ``create_todoist_task``: when
    the gate is closed (flag off or no token/project) NO external HTTP call is
    made and the ledger is left untouched, so a later enabled run is a clean
    create. Returns a summary with per-item dispositions. Secrets are never
    logged or returned.
    """

    ensure_app_schema(conn)
    _coerce_date(as_of_date)  # validate shape; surfacing is date-agnostic per item

    # Resolve the gate. When write_enabled is explicitly False, do NOT read config
    # so tests stay hermetic and can never fire a live send. Config fills gaps only.
    if write_enabled is None:
        cfg = get_finance_config(env_path=env_path)
        write_enabled = cfg["todoist_write_enabled"]
        token = token if token is not None else cfg["todoist_api_token"]
        project_id = project_id if project_id is not None else cfg["todoist_project_id"]
    elif write_enabled and (token is None or project_id is None):
        cfg = get_finance_config(env_path=env_path)
        token = token if token is not None else cfg["todoist_api_token"]
        project_id = project_id if project_id is not None else cfg["todoist_project_id"]
    live = bool(write_enabled and token and project_id)

    summary: dict[str, Any] = {
        "status": "ok" if live else "awaiting-integration",
        "integration_enabled": live,
        "sent": live,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "resolved": 0,
        "failed": 0,
        "items": [],
    }

    if not live:
        # Gate closed: no external call, no ledger mutation. Report what WOULD
        # happen so the caller still has visibility, but treat all as awaiting.
        summary["reason"] = "Todoist write-back disabled (TODOIST_WRITE_ENABLED off or no token/project)"
        for item in items:
            summary["items"].append({"surface_key": item.get("surface_key"), "action": "awaiting-integration"})
        return summary

    for item in items:
        key = item.get("surface_key")
        if not key:
            summary["failed"] += 1
            summary["items"].append({"surface_key": None, "action": "failed", "reason": "missing surface_key"})
            continue
        content = (item.get("content") or "").strip()
        description = item.get("description") or ""
        due_date = item.get("due_date")
        priority = item.get("priority")
        new_hash = content_hash_for(content, description)

        row = conn.execute(
            "SELECT surface_key, todoist_task_id, status, content_hash FROM todoist_emissions WHERE surface_key = ?",
            (key,),
        ).fetchone()

        # User already resolved this item by closing the task: never recreate.
        if row is not None and row["status"] in ("completed", "deleted_by_user"):
            summary["resolved"] += 1
            summary["items"].append({"surface_key": key, "action": "resolved", "status": row["status"]})
            continue

        # Open + unchanged: idempotent skip, no HTTP.
        if row is not None and row["status"] == "open" and row["content_hash"] == new_hash:
            now = _now()
            conn.execute(
                "UPDATE todoist_emissions SET last_seen = ? WHERE surface_key = ?", (now, key)
            )
            summary["skipped"] += 1
            summary["items"].append({"surface_key": key, "action": "skipped", "todoist_task_id": row["todoist_task_id"]})
            continue

        # Open + changed: update the SAME task in place, refresh the hash.
        if row is not None and row["status"] == "open":
            try:
                _update_surface_task(
                    token, row["todoist_task_id"], content, description, key,
                    due_date=due_date, priority=priority, send_func=send_func,
                )
            except Exception as exc:  # noqa: BLE001 - record, never crash the batch
                summary["failed"] += 1
                summary["items"].append({"surface_key": key, "action": "failed", "reason": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            now = _now()
            conn.execute(
                "UPDATE todoist_emissions SET content_hash = ?, last_seen = ? WHERE surface_key = ?",
                (new_hash, now, key),
            )
            summary["updated"] += 1
            summary["items"].append({"surface_key": key, "action": "updated", "todoist_task_id": row["todoist_task_id"]})
            continue

        # No ledger row: create the task, then insert the ledger row.
        try:
            created = _create_surface_task(
                token, project_id, content, description, key,
                due_date=due_date, priority=priority, send_func=send_func,
            )
        except Exception as exc:  # noqa: BLE001 - record, never crash the batch
            summary["failed"] += 1
            summary["items"].append({"surface_key": key, "action": "failed", "reason": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        task_id = created.get("id")
        # A malformed-but-200 response can omit the id. The ledger's
        # todoist_task_id is NOT NULL, so inserting None would raise IntegrityError
        # and abort the whole batch. Record this item failed (no ledger row, so a
        # later run cleanly retries the create) and keep processing the rest.
        if not task_id:
            summary["failed"] += 1
            summary["items"].append({"surface_key": key, "action": "failed", "reason": "create returned no task id"})
            continue
        now = _now()
        conn.execute(
            """
            INSERT INTO todoist_emissions (
                surface_key, todoist_task_id, status, content_hash, created_at, last_seen
            ) VALUES (?, ?, 'open', ?, ?, ?)
            """,
            (key, task_id, new_hash, now, now),
        )
        summary["created"] += 1
        summary["items"].append({"surface_key": key, "action": "created", "todoist_task_id": task_id})

    return summary


def execute_action_outbox(
    conn: sqlite3.Connection,
    *,
    options: dict[str, Any] | None = None,
    write_enabled: bool | None = None,
    token: str | None = None,
    project_id: str | None = None,
    env_path: str | None = None,
    send_func: Callable[..., dict[str, Any]] = send_review_batch,
) -> dict[str, Any]:
    """Process outbox items. Live sending is gated OFF by default.

    Dry-run items are always simulated. Pending items send to Todoist ONLY when
    ``write_enabled`` is true and a token + project id are available (resolved
    from config when not passed). With the gate off, pending items are marked
    ``no_integration_configured`` - no external call. Sending is idempotent: a
    row with an ``external_task_id`` and unchanged ``last_pushed_hash`` is skipped;
    a changed hash updates the same task.
    """

    ensure_app_schema(conn)
    # Resolve the gate. When write_enabled is explicitly False, do NOT read config
    # at all - this keeps tests hermetic so `pytest` under a write-enabled env can
    # never fire a live send. Config is consulted only to fill missing values.
    if write_enabled is None:
        cfg = get_finance_config(env_path=env_path)
        write_enabled = cfg["todoist_write_enabled"]
        token = token if token is not None else cfg["todoist_api_token"]
        project_id = project_id if project_id is not None else cfg["todoist_project_id"]
    elif write_enabled and (token is None or project_id is None):
        cfg = get_finance_config(env_path=env_path)
        token = token if token is not None else cfg["todoist_api_token"]
        project_id = project_id if project_id is not None else cfg["todoist_project_id"]
    live = bool(write_enabled and token and project_id)

    now = _now()
    rows = conn.execute(
        "SELECT id, idempotency_key, status, dry_run, payload_json, payload_hash, external_task_id, last_pushed_hash "
        "FROM action_outbox WHERE status IN ('pending', 'dry_run')"
    ).fetchall()

    simulated = awaiting_integration = sent = updated = skipped = failed = 0
    for row in rows:
        if row["dry_run"]:
            conn.execute(
                "UPDATE action_outbox SET status = 'simulated', last_error = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                ("dry run: payload validated, not sent", now, row["id"]),
            )
            simulated += 1
            continue
        if not live:
            conn.execute(
                "UPDATE action_outbox SET status = 'no_integration_configured', last_error = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                ("Todoist write-back disabled (TODOIST_WRITE_ENABLED off or no token/project); not sent", now, row["id"]),
            )
            awaiting_integration += 1
            continue

        # Already up to date: nothing to push.
        if row["external_task_id"] and row["last_pushed_hash"] == row["payload_hash"]:
            conn.execute(
                "UPDATE action_outbox SET status = 'succeeded', last_observed_state = 'unchanged', updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            skipped += 1
            continue

        conn.execute("UPDATE action_outbox SET status = 'in_progress', attempts = attempts + 1, updated_at = ? WHERE id = ?", (now, row["id"]))
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            result = send_func(token, project_id, payload, existing_task_id=row["external_task_id"])
            conn.execute(
                "UPDATE action_outbox SET status = 'succeeded', external_task_id = ?, last_pushed_hash = ?, "
                "last_observed_state = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (result.get("task_id"), row["payload_hash"], result.get("action"), _now(), row["id"]),
            )
            if result.get("action") == "updated":
                updated += 1
            else:
                sent += 1
        except Exception as exc:  # noqa: BLE001 - record and continue; never crash the batch
            conn.execute(
                "UPDATE action_outbox SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                (f"send failed: {type(exc).__name__}: {exc}"[:300], _now(), row["id"]),
            )
            failed += 1

    return {
        "processed": len(rows),
        "simulated": simulated,
        "awaiting_integration": awaiting_integration,
        "sent": sent,
        "updated": updated,
        "skipped_unchanged": skipped,
        "failed": failed,
        "integration_enabled": live,
    }


def list_action_outbox(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where = ""
    params: list[Any] = []
    if status is not None:
        where = "WHERE status = ?"
        params.append(status)
    rows = conn.execute(
        f"""
        SELECT id, idempotency_key, action_type, target_type, target_id,
               payload_json, payload_hash, dry_run, status, attempts, last_error,
               item_count, created_at, updated_at
        FROM action_outbox {where}
        ORDER BY created_at DESC, id
        """,
        params,
    ).fetchall()
    return [
        {
            "idempotency_key": r["idempotency_key"],
            "action_type": r["action_type"],
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "dry_run": bool(r["dry_run"]),
            "status": r["status"],
            "attempts": r["attempts"],
            "last_error": r["last_error"],
            "item_count": r["item_count"],
            "payload": json.loads(r["payload_json"]) if r["payload_json"] else None,
            "payload_hash": r["payload_hash"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


# --- batch rendering -------------------------------------------------------


def _build_batch(conn: sqlite3.Connection, as_of: date, opts: dict[str, Any]) -> dict[str, Any]:
    min_rank = _SEVERITY_RANK.get(opts["min_severity"], 1)
    drift = detect_drift(conn, as_of_date=as_of, persist=False)

    subtasks: list[dict[str, Any]] = []

    # Make a silently-stopped daily job visible. If the last successful daily run
    # is stale, surface a HIGH-priority alert at the top of the batch so a stopped
    # scheduler becomes visible the next time anything runs (not only when the
    # daily job itself runs, which by definition it no longer does).
    stale_finding = _stale_job_finding(conn, as_of)
    if stale_finding is not None and _SEVERITY_RANK.get(stale_finding["severity"], 0) >= min_rank:
        subtasks.append(
            {
                "finding_id": stale_finding["id"],
                "finding_type": stale_finding["finding_type"],
                "severity": stale_finding["severity"],
                "content": stale_finding["message"],
                "obligation_instance_id": None,
                "cash_flow_impact": None,
            }
        )

    for finding in drift["findings"]:
        if _SEVERITY_RANK.get(finding["severity"], 0) < min_rank:
            continue
        if finding["finding_type"] == "unexpected_recurring" and not opts["include_recurring"]:
            continue
        subtasks.append(
            {
                "finding_id": finding["id"],
                "finding_type": finding["finding_type"],
                "severity": finding["severity"],
                "content": _subtask_content(finding),
                "obligation_instance_id": finding["obligation_instance_id"],
                "cash_flow_impact": finding["cash_flow_impact"],
            }
        )

    batch_id = f"finance-review-{as_of.isoformat()}"
    return {
        "batch_id": batch_id,
        "as_of_date": as_of.isoformat(),
        "idempotency_key": f"{ACTION_TYPE}:{as_of.isoformat()}",
        "parent_task": {
            "content": f"Finance review {as_of.isoformat()}",
            "description": f"{len(subtasks)} item(s) need review. This task reflects the review workflow; it is not bank evidence.",
        },
        "subtasks": subtasks,
        "item_count": len(subtasks),
    }


def _stale_job_finding(conn: sqlite3.Connection, as_of: date) -> dict[str, Any] | None:
    """Synthesize a HIGH-severity finding when the daily job has gone stale.

    Returns None when the daily job is healthy. Imported lazily because
    ``background`` imports this module (the daily run enqueues the review batch),
    so a top-level import would be circular.
    """

    from .background import get_job_health  # local import: avoids import cycle

    health = get_job_health(conn, as_of_date=as_of.isoformat())
    if not health["is_stale"]:
        return None

    hours = health["hours_since_last_run"]
    threshold = health["stale_threshold_hours"]
    if hours is None:
        detail = (
            f"no successful daily sync on record (threshold: {threshold}h) - "
            "the job may never have run"
        )
    else:
        detail = (
            f"last completed {hours:.1f}h ago (threshold: {threshold}h) - "
            "job may be stopped"
        )
    return {
        "id": "drift:stale_daily_job",
        "finding_type": "stale_daily_job",
        "severity": "high",
        "status": "active",
        "message": (
            f"Daily sync is stale: {detail}. Check cron/scheduler logs and "
            "restart the daily runner."
        ),
        "recommended_action": "Check cron/scheduler logs; restart the daily runner",
    }


def _subtask_content(finding: dict[str, Any]) -> str:
    ev = finding.get("evidence") or {}
    ftype = finding["finding_type"]
    if ftype == "missing_expected":
        return (
            f"Confirm payment: {ev.get('obligation_name')} expected "
            f"${_money(ev.get('expected_amount'))} on {ev.get('due_date')} "
            f"({ev.get('age_days')}d past due, no matching transaction found)."
        )
    if ftype == "stale_estimate":
        return (
            f"Refresh estimate: {ev.get('obligation_name')} ${_money(ev.get('current_estimate'))} "
            f"estimate is past its review date ({ev.get('review_after')}); update from the portal/bill."
        )
    if ftype == "amount_changed":
        return (
            f"Review amount change: {ev.get('obligation_name')} expected "
            f"${_money(ev.get('expected_amount'))}, charged ${_money(ev.get('observed_amount'))} "
            f"({_pct(ev.get('pct_change'))})."
        )
    if ftype == "unexpected_recurring":
        return (
            f"Onboard charge: {ev.get('merchant')} recurs "
            f"(~${_money(ev.get('estimated_monthly_impact'))}/mo) but is not modeled yet; "
            f"review it in the onboarding queue."
        )
    return f"Review: {ftype} ({finding['severity']})."


def _money(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "?"


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "?"


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _now() -> str:
    return datetime.now().astimezone().isoformat()
