"""Todoist output: push due-item reminders and a durable action outbox.

Todoist is an OUTPUT/action surface, not the source of financial truth. This
module pushes the day's due items to Todoist (idempotently, via the emissions
ledger), reads back completions of tasks we pushed, and records intended writes
in a durable action outbox.

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
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Callable

from .config import get_finance_config
from .follow_ups import resolve_followup
from .onboarding import DECIDED_STATUSES
from .schema import ensure_app_schema


TODOIST_BASE_URL = "https://api.todoist.com/api/v1"


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


def _list_tasks_request(
    token: str,
    project_id: str,
    *,
    cursor: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET one page of active tasks for a project. Only called on the gated path.

    Todoist unified v1 list endpoints are paginated: the response envelope is
    ``{"results": [ {task}, ... ], "next_cursor": <str|None>}``. The caller loops,
    passing ``cursor=next_cursor`` until ``next_cursor`` is null/empty. Headers
    match ``_read_task`` (Bearer + Content-Type + User-Agent). The active-tasks
    list returns ACTIVE tasks only (completed tasks live on a separate endpoint),
    so there is no completed bucket to filter here.
    """

    url = f"{TODOIST_BASE_URL}/tasks?project_id={project_id}"
    if cursor:
        url = f"{url}&cursor={cursor}"
    req = urllib.request.Request(  # noqa: S310 - token is the user's own .env credential
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "financial-agent-mcp/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {"results": [], "next_cursor": None}


def _delete_request(
    token: str,
    task_id: str,
    *,
    timeout: int = 30,
    max_retries: int = 5,
) -> bool:
    """Hard-DELETE a single task. Only called on the gated apply path.

    Returns True on success. ``DELETE /api/v1/tasks/<id>`` returns 204 (200 on
    some clients); a 404 means the task is already gone, which is treated as
    success so the cleanup is idempotent. An HTTP 429 (rate limited) is retried up
    to ``max_retries`` times, honoring the ``Retry-After`` header when present and
    otherwise backing off on an exponential schedule (0.5, 1, 2, 4, 8s, capped).
    Any other error, or a 429 that survives the retries, propagates so the caller
    records the task failed without aborting the whole run.
    """

    req = urllib.request.Request(  # noqa: S310 - token is the user's own .env credential
        f"{TODOIST_BASE_URL}/tasks/{task_id}",
        method="DELETE",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "financial-agent-mcp/0.1",
        },
    )
    backoffs = [0.5, 1.0, 2.0, 4.0, 8.0]
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                resp.read()
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return True
            if exc.code == 429 and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                default_delay = backoffs[min(attempt, len(backoffs) - 1)]
                try:
                    delay = float(retry_after) if retry_after else default_delay
                except (TypeError, ValueError):
                    delay = default_delay
                time.sleep(delay)
                attempt += 1
                continue
            raise


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


def update_todoist_task(
    task_id: str,
    *,
    content: str | None = None,
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
    """Update an existing Todoist task in place. Live write is GATED OFF by default.

    Edits ONLY the fields that are passed; any argument left ``None`` is omitted
    from the request body, so a partial update never clears an untouched field.
    ``description`` and ``project_id`` are sent whenever not ``None`` (so passing
    ``project_id`` moves the task to another project). ``due_date`` (ISO yyyy-mm-dd)
    wins over ``due_string`` when both are supplied, matching ``create_todoist_task``.

    Targets a task by id (Todoist POST ``/tasks/<id>``), so the gate needs only a
    token, not a configured project (mirrors ``reconcile_todoist_completions``).
    The live write fires ONLY when ``write_enabled`` is true (resolved from
    TODOIST_WRITE_ENABLED in the finances .env when not passed) AND a token is
    available. Otherwise this makes NO external HTTP call and returns
    ``{"status": "awaiting-integration", "sent": False, "reason": ...}``. Secrets
    are never logged or returned.

    Returns on success: ``{"status": "updated", "sent": True, "task_id", "url"}``.
    """

    task_id = (task_id or "").strip()
    if not task_id:
        return {"status": "awaiting-integration", "sent": False, "reason": "task_id is required"}

    body: dict[str, Any] = {}
    if content is not None:
        body["content"] = content
    if description is not None:
        body["description"] = description
    if priority is not None:
        body["priority"] = priority
    if project_id is not None:
        body["project_id"] = project_id
    # due_date (ISO) wins over due_string when both are supplied.
    if due_date:
        body["due_date"] = due_date
    elif due_string:
        body["due_string"] = due_string
    if not body:
        return {"status": "awaiting-integration", "sent": False, "reason": "no fields to update"}

    # Resolve the gate. When write_enabled is explicitly False, do NOT read config
    # so tests stay hermetic and can never fire a live send. Config only fills gaps.
    if write_enabled is None:
        cfg = get_finance_config(env_path=env_path)
        write_enabled = cfg["todoist_write_enabled"]
        token = token if token is not None else cfg["todoist_api_token"]
    elif write_enabled and token is None:
        cfg = get_finance_config(env_path=env_path)
        token = token if token is not None else cfg["todoist_api_token"]

    if not write_enabled:
        return {"status": "awaiting-integration", "sent": False,
                "reason": "Todoist write-back disabled (TODOIST_WRITE_ENABLED off)"}
    if not token:
        return {"status": "awaiting-integration", "sent": False,
                "reason": "Todoist token not configured"}

    try:
        updated = send_func(token, f"/tasks/{task_id}", body)
    except Exception as exc:  # noqa: BLE001 - surface a clean failure, never crash the tool
        return {"status": "failed", "sent": False,
                "reason": f"update failed: {type(exc).__name__}: {exc}"[:300]}

    return {"status": "updated", "sent": True, "task_id": task_id, "url": updated.get("url")}


def complete_todoist_task(
    task_id: str,
    *,
    write_enabled: bool | None = None,
    token: str | None = None,
    env_path: str | None = None,
    send_func: Callable[..., dict[str, Any]] = _write_request,
) -> dict[str, Any]:
    """Complete (close) an existing Todoist task. Live write is GATED OFF by default.

    Closes the task by id (Todoist POST ``/tasks/<id>/close``), which returns 204
    No Content on success; the empty body is handled cleanly. Targets a task by id,
    so the gate needs only a token, not a configured project (mirrors
    ``reconcile_todoist_completions``). The live write fires ONLY when
    ``write_enabled`` is true (resolved from TODOIST_WRITE_ENABLED in the finances
    .env when not passed) AND a token is available. Otherwise this makes NO
    external HTTP call and returns ``{"status": "awaiting-integration", "sent":
    False, "reason": ...}``. Secrets are never logged or returned.

    Returns on success: ``{"status": "completed", "sent": True, "task_id"}``.
    """

    task_id = (task_id or "").strip()
    if not task_id:
        return {"status": "awaiting-integration", "sent": False, "reason": "task_id is required"}

    # Resolve the gate. When write_enabled is explicitly False, do NOT read config
    # so tests stay hermetic and can never fire a live send. Config only fills gaps.
    if write_enabled is None:
        cfg = get_finance_config(env_path=env_path)
        write_enabled = cfg["todoist_write_enabled"]
        token = token if token is not None else cfg["todoist_api_token"]
    elif write_enabled and token is None:
        cfg = get_finance_config(env_path=env_path)
        token = token if token is not None else cfg["todoist_api_token"]

    if not write_enabled:
        return {"status": "awaiting-integration", "sent": False,
                "reason": "Todoist write-back disabled (TODOIST_WRITE_ENABLED off)"}
    if not token:
        return {"status": "awaiting-integration", "sent": False,
                "reason": "Todoist token not configured"}

    try:
        # Close returns 204 No Content; _write_request returns {} for an empty body.
        send_func(token, f"/tasks/{task_id}/close", {})
    except Exception as exc:  # noqa: BLE001 - surface a clean failure, never crash the tool
        return {"status": "failed", "sent": False,
                "reason": f"complete failed: {type(exc).__name__}: {exc}"[:300]}

    return {"status": "completed", "sent": True, "task_id": task_id}


def reopen_todoist_task(
    task_id: str,
    *,
    write_enabled: bool | None = None,
    token: str | None = None,
    env_path: str | None = None,
    send_func: Callable[..., dict[str, Any]] = _write_request,
) -> dict[str, Any]:
    """Reopen (un-complete) an existing Todoist task. Live write is GATED OFF by default.

    Reopens the task by id (Todoist POST ``/tasks/<id>/reopen``), which returns 204
    No Content on success; the empty body is handled cleanly. Targets a task by id,
    so the gate needs only a token, not a configured project (mirrors
    ``reconcile_todoist_completions``). The live write fires ONLY when
    ``write_enabled`` is true (resolved from TODOIST_WRITE_ENABLED in the finances
    .env when not passed) AND a token is available. Otherwise this makes NO
    external HTTP call and returns ``{"status": "awaiting-integration", "sent":
    False, "reason": ...}``. Secrets are never logged or returned.

    Returns on success: ``{"status": "reopened", "sent": True, "task_id"}``.
    """

    task_id = (task_id or "").strip()
    if not task_id:
        return {"status": "awaiting-integration", "sent": False, "reason": "task_id is required"}

    # Resolve the gate. When write_enabled is explicitly False, do NOT read config
    # so tests stay hermetic and can never fire a live send. Config only fills gaps.
    if write_enabled is None:
        cfg = get_finance_config(env_path=env_path)
        write_enabled = cfg["todoist_write_enabled"]
        token = token if token is not None else cfg["todoist_api_token"]
    elif write_enabled and token is None:
        cfg = get_finance_config(env_path=env_path)
        token = token if token is not None else cfg["todoist_api_token"]

    if not write_enabled:
        return {"status": "awaiting-integration", "sent": False,
                "reason": "Todoist write-back disabled (TODOIST_WRITE_ENABLED off)"}
    if not token:
        return {"status": "awaiting-integration", "sent": False,
                "reason": "Todoist token not configured"}

    try:
        # Reopen returns 204 No Content; _write_request returns {} for an empty body.
        send_func(token, f"/tasks/{task_id}/reopen", {})
    except Exception as exc:  # noqa: BLE001 - surface a clean failure, never crash the tool
        return {"status": "failed", "sent": False,
                "reason": f"reopen failed: {type(exc).__name__}: {exc}"[:300]}

    return {"status": "reopened", "sent": True, "task_id": task_id}


def delete_todoist_task(
    task_id: str,
    *,
    write_enabled: bool | None = None,
    token: str | None = None,
    env_path: str | None = None,
    delete_func: Callable[..., bool] = _delete_request,
) -> dict[str, Any]:
    """Delete an existing Todoist task. Live write is GATED OFF by default.

    Hard-deletes the task by id (Todoist DELETE ``/tasks/<id>``), which returns 204
    No Content on success. Reuses ``_delete_request`` (the existing DELETE helper),
    so a 404 is treated as already-gone (idempotent) and a 429 is retried. Targets
    a task by id, so the gate needs only a token, not a configured project (mirrors
    ``reconcile_todoist_completions``). The live delete fires ONLY when
    ``write_enabled`` is true (resolved from TODOIST_WRITE_ENABLED in the finances
    .env when not passed) AND a token is available. Otherwise this makes NO
    external HTTP call and returns ``{"status": "awaiting-integration", "sent":
    False, "reason": ...}``. Secrets are never logged or returned.

    Returns on success: ``{"status": "deleted", "sent": True, "task_id"}``.
    """

    task_id = (task_id or "").strip()
    if not task_id:
        return {"status": "awaiting-integration", "sent": False, "reason": "task_id is required"}

    # Resolve the gate. When write_enabled is explicitly False, do NOT read config
    # so tests stay hermetic and can never fire a live delete. Config only fills gaps.
    if write_enabled is None:
        cfg = get_finance_config(env_path=env_path)
        write_enabled = cfg["todoist_write_enabled"]
        token = token if token is not None else cfg["todoist_api_token"]
    elif write_enabled and token is None:
        cfg = get_finance_config(env_path=env_path)
        token = token if token is not None else cfg["todoist_api_token"]

    if not write_enabled:
        return {"status": "awaiting-integration", "sent": False,
                "reason": "Todoist write-back disabled (TODOIST_WRITE_ENABLED off)"}
    if not token:
        return {"status": "awaiting-integration", "sent": False,
                "reason": "Todoist token not configured"}

    try:
        delete_func(token, task_id)
    except Exception as exc:  # noqa: BLE001 - surface a clean failure, never crash the tool
        return {"status": "failed", "sent": False,
                "reason": f"delete failed: {type(exc).__name__}: {exc}"[:300]}

    return {"status": "deleted", "sent": True, "task_id": task_id}


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


def request_emission_retire(conn: sqlite3.Connection, surface_key: str) -> dict[str, Any]:
    """Mark a single open emission for removal on the next live surface run.

    Sets the ``retire_requested_at`` tombstone on the matching open emission. This
    is a pure local UPDATE (no Todoist call), so it is safe in dry-run and runs
    regardless of ``todoist_write_enabled``: it records intent only. The actual
    delete + status flip to ``retired`` happens later in ``surface_to_todoist``'s
    drain. Use this for singleton surface keys (e.g. ``onboarding-digest``).
    """

    ensure_app_schema(conn)
    cur = conn.execute(
        "UPDATE todoist_emissions SET retire_requested_at = ? "
        "WHERE surface_key = ? AND status = 'open'",
        (_now(), surface_key),
    )
    return {"matched": surface_key, "retire_requested": cur.rowcount}


def request_emission_retire_prefix(
    conn: sqlite3.Connection, surface_key_prefix: str
) -> dict[str, Any]:
    """Mark every open emission whose surface_key starts with ``surface_key_prefix``.

    ``obligation-due`` keys carry a per-instance date suffix
    (``obligation-due:<obligation_id>:<due_date>``), so a single obligation has one
    emission per due date. Passing ``"obligation-due:<obligation_id>:"`` retires
    every due-date instance of that obligation in one call. Like
    ``request_emission_retire`` this only sets intent on ``status='open'`` rows.
    """

    ensure_app_schema(conn)
    cur = conn.execute(
        "UPDATE todoist_emissions SET retire_requested_at = ? "
        "WHERE surface_key LIKE ? || '%' AND status = 'open'",
        (_now(), surface_key_prefix),
    )
    return {"prefix": surface_key_prefix, "retire_requested": cur.rowcount}


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
    delete_func: Callable[..., bool] = _delete_request,
    retire_keys: list[str] | None = None,
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

    ``retire_keys``: surface_keys to flag for retire before the drain (the
    read-only builder no longer writes the digest retire; it reports it here so
    the mutation lives on the write path with the other ledger writes).
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
        "retired": 0,
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

    # Apply retire intent reported by the read-only builder (e.g. the singleton
    # onboarding-digest when its queue is empty). Flag the open rows here so the
    # drain below removes them, keeping all ledger writes on the write path.
    for key in retire_keys or ():
        request_emission_retire(conn, key)

    # Drain retire tombstones first. A candidate/obligation decision may have
    # flagged open emissions for removal (retire_requested_at set). Delete each
    # task in Todoist, then flip the ledger row to 'retired' (NOT
    # deleted_by_user, so a recurring surface_key can resurface when next due).
    retire_rows = conn.execute(
        "SELECT surface_key, todoist_task_id FROM todoist_emissions "
        "WHERE status = 'open' AND retire_requested_at IS NOT NULL"
    ).fetchall()
    for r in retire_rows:
        try:
            delete_func(token, r["todoist_task_id"])
        except Exception as exc:  # noqa: BLE001 - record, never crash the batch
            summary["failed"] += 1
            summary["items"].append({"surface_key": r["surface_key"], "action": "failed", "reason": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        mark_emission_status(conn, r["surface_key"], "retired")
        summary["retired"] += 1
        summary["items"].append({"surface_key": r["surface_key"], "action": "retired", "todoist_task_id": r["todoist_task_id"]})

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
        # INSERT OR REPLACE so a singleton surface_key (e.g. onboarding-digest)
        # that was previously flipped to 'retired' resurfaces cleanly: the stale
        # ledger row is replaced with a fresh open one rather than colliding on
        # the surface_key primary key (rev 3 - retired must allow recreation). A
        # genuinely new key is a plain insert. completed/deleted_by_user rows are
        # already short-circuited above, so they never reach here.
        conn.execute(
            """
            INSERT OR REPLACE INTO todoist_emissions (
                surface_key, todoist_task_id, status, content_hash, created_at, last_seen
            ) VALUES (?, ?, 'open', ?, ?, ?)
            """,
            (key, task_id, new_hash, now, now),
        )
        summary["created"] += 1
        summary["items"].append({"surface_key": key, "action": "created", "todoist_task_id": task_id})

    return summary


# --- whole-project reconcile + cleanup -------------------------------------
# Tasks are created but never retired, so the project can accumulate stale
# leftovers over time. reconcile_todoist_project_for_db
# adds the missing server-side LIST capability and a classify/clean pass over the
# WHOLE Finance project. The safety invariant is hard: a task is deletable ONLY if
# it matches one of three explicit positive rules; everything else is `kept` and
# is NEVER passed to delete_func.

# Literal title-prefix allowlist for legacy-mess cleanup (delete rule b). This is
# the ONLY rule that can delete a task lacking the fa-auto label, so it is kept
# deliberately narrow and confirmed against a live sample (V-LIVE): the 56
# onboarding tasks carry no label and no marker, only this title prefix.
LEGACY_CLEANUP_PREFIXES: list[str] = ["Onboard charge:"]

# Page cap: drain at most this many LIST pages. Hitting it forces report-only
# because duplicate/ledger-orphan inference is unsound on a partial view.
MAX_LIST_PAGES = 50

# Per-run delete ceiling: protects against a runaway loop and against tripping a
# hard rate-limit ban mid-cleanup. Once hit, stop deleting and require a re-run.
MAX_DELETES_PER_RUN = 200


def _parse_legacy_display_name(content: str, prefix: str) -> str:
    """Pull the merchant/display name out of a legacy ``"Onboard charge: <name>
    not modeled"`` title. Heuristic and lossy; used only for the candidate join."""

    rest = (content or "")[len(prefix):].strip()
    low = rest.lower()
    if low.endswith("not modeled"):
        rest = rest[: len(rest) - len("not modeled")].strip()
    return rest


def reconcile_todoist_project_for_db(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    apply: bool = False,
    write_enabled: bool | None = None,
    token: str | None = None,
    project_id: str | None = None,
    env_path: str | None = None,
    list_func: Callable[..., dict[str, Any]] = _list_tasks_request,
    delete_func: Callable[..., bool] = _delete_request,
) -> dict[str, Any]:
    """LIST the whole Finance project, classify every task, and (in apply mode)
    delete only the explicit delete set.

    Classification precedence (first match wins):
    ``managed > stale_applied > duplicate > fa_auto_orphan > kept``. The delete
    set is the union of exactly three rules: (a) fa-auto orphan, (b) a
    ``LEGACY_CLEANUP_PREFIXES``-matched task whose underlying candidate is decided,
    (c) a duplicate extra copy whose surviving copy is itself managed or fa-auto.
    Anything else is ``kept`` and is NEVER deleted (this protects the ritual
    reminders and every hand-made task, which carry no marker and no fa-auto
    label).

    Each entry in the returned ``tasks`` list carries the task's
    ``task_id``, ``content``, ``surface_key``, ``has_fa_auto``,
    ``classification``, ``action``, ``reason``, plus ``due_date`` (the task's
    due date string, or None when the task has no due date) and ``description``
    (the raw Todoist description, "" when empty).

    Gating mirrors ``surface_to_todoist``: the dry-run report needs only
    token+project (reads are allowed with write-back off); apply (delete/resolve)
    requires ``live`` = write_enabled AND token AND project. A truncated or
    failed LIST forces report-only (zero deletes, zero ledger resolutions),
    because duplicate and ledger-orphan inference are unsound on a partial view.
    """

    ensure_app_schema(conn)
    _coerce_date(as_of_date)  # validate shape

    # Resolve the gate. When write_enabled is explicitly False, do NOT read config
    # so tests stay hermetic. Config only fills gaps.
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

    report: dict[str, Any] = {
        "status": "ok",
        "integration_enabled": live,
        "applied": False,
        "truncated": False,
        "truncated_deletes": False,
        "as_of_date": str(as_of_date)[:10],
        "project_id": project_id,
        "listed": 0,
        "counts": {
            "managed": 0,
            "stale_applied": 0,
            "duplicate": 0,
            "fa_auto_orphan": 0,
            "kept": 0,
            "needs_review": 0,
        },
        "ledger_findings": {"ledger_orphan": 0},
        "actions": {"deleted": 0, "ledger_resolved": 0, "skipped_not_live": 0, "failed": 0},
        "tasks": [],
    }

    # LIST requires token + project. Reads are allowed even with write-back off.
    if not token or not project_id:
        report["status"] = "awaiting-integration"
        report["reason"] = "missing project_id" if (token and not project_id) else "awaiting-integration"
        return report

    # --- drain all pages -----------------------------------------------------
    tasks: list[dict[str, Any]] = []
    truncated = False
    list_failed = False
    cursor: str | None = None
    pages = 0
    while True:
        if pages >= MAX_LIST_PAGES:
            truncated = True
            break
        try:
            resp = list_func(token, project_id, cursor=cursor)
        except Exception:  # noqa: BLE001 - a failed page makes the view partial
            list_failed = True
            break
        pages += 1
        tasks.extend(resp.get("results") or [])
        cursor = resp.get("next_cursor")
        if not cursor:
            break

    incomplete = truncated or list_failed

    # --- load ledger + candidates -------------------------------------------
    emissions = conn.execute(
        "SELECT surface_key, todoist_task_id, status FROM todoist_emissions"
    ).fetchall()
    emission_by_key = {r["surface_key"]: r for r in emissions}
    open_task_ids = {r["todoist_task_id"] for r in emissions if r["status"] == "open"}

    candidates = conn.execute(
        "SELECT id, display_name, merchant_key, status FROM charge_onboarding_candidates"
    ).fetchall()
    cand_by_display: dict[str, Any] = {}
    cand_by_merchant: dict[str, Any] = {}
    for c in candidates:
        if c["display_name"]:
            cand_by_display[c["display_name"].strip().lower()] = c
        if c["merchant_key"]:
            cand_by_merchant[c["merchant_key"].strip().lower()] = c

    # --- per-task raw attributes --------------------------------------------
    attrs: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    groups: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        tid = str(t["id"])
        content = t.get("content") or ""
        description = t.get("description") or ""
        sk = extract_surface_key(description)
        labels = t.get("labels") or []
        has_fa_auto = FA_AUTO_LABEL in labels
        chash = content_hash_for(content, description)
        attrs[tid] = {
            "tid": tid,
            "content": content,
            "surface_key": sk,
            "has_fa_auto": has_fa_auto,
            "due_date": (t.get("due") or {}).get("date"),
            "description": description,
        }
        order.append(tid)
        dup_key = sk if sk else f"hash:{chash}"
        groups[dup_key].append(tid)

    def _is_managed(tid: str, sk: str | None) -> bool:
        if not sk:
            return False
        row = emission_by_key.get(sk)
        return bool(row and row["status"] == "open" and row["todoist_task_id"] == tid)

    # --- duplicate survivors -------------------------------------------------
    # Survivor: the copy matching an open emission id; else the NUMERICALLY
    # smallest id (Todoist ids are large numeric strings -> numeric, not lexical).
    duplicate_extra: set[str] = set()
    survivor_of_extra: dict[str, str] = {}
    duplicate_survivor: set[str] = set()
    for dup_key, ids in groups.items():
        if len(ids) < 2:
            continue
        matched = [i for i in ids if i in open_task_ids]
        survivor = matched[0] if matched else min(ids, key=lambda x: int(x))
        duplicate_survivor.add(survivor)
        for i in ids:
            if i != survivor:
                duplicate_extra.add(i)
                survivor_of_extra[i] = survivor

    # --- classify ------------------------------------------------------------
    deletable: list[str] = []
    task_rows: dict[str, dict[str, Any]] = {}
    for tid in order:
        a = attrs[tid]
        sk = a["surface_key"]
        content = a["content"]
        has_fa_auto = a["has_fa_auto"]

        entry: dict[str, Any] = {
            "task_id": tid,
            "content": content,
            "surface_key": sk,
            "has_fa_auto": has_fa_auto,
            "due_date": a["due_date"],
            "description": a["description"],
        }

        # 1. managed
        if _is_managed(tid, sk):
            entry["classification"] = "managed"
            entry["action"] = "kept"
            entry["reason"] = "managed by open emission"
            task_rows[tid] = entry
            continue

        # 2. stale_applied (legacy title prefix)
        matched_prefix = next((p for p in LEGACY_CLEANUP_PREFIXES if content.startswith(p)), None)
        if matched_prefix is not None:
            name = _parse_legacy_display_name(content, matched_prefix)
            cand = cand_by_display.get(name.lower()) or cand_by_merchant.get(name.lower())
            entry["classification"] = "stale_applied"
            entry["match_confidence"] = "heuristic"
            entry["candidate_id"] = cand["id"] if cand else None
            entry["candidate_status"] = cand["status"] if cand else None
            if cand is not None and cand["status"] in DECIDED_STATUSES:
                entry["action"] = "would_delete"
                entry["reason"] = f"candidate {cand['status']}"
                deletable.append(tid)
            else:
                entry["action"] = "needs_review"
                entry["reason"] = (
                    "legacy prefix but candidate not decided"
                    if cand is not None
                    else "legacy prefix with no candidate match"
                )
            task_rows[tid] = entry
            continue

        # 3. duplicate (non-survivor copy)
        if tid in duplicate_extra:
            survivor = survivor_of_extra[tid]
            s = attrs[survivor]
            survivor_managed = _is_managed(survivor, s["surface_key"])
            survivor_fa_auto = s["has_fa_auto"]
            if survivor_managed or survivor_fa_auto:
                entry["classification"] = "duplicate"
                entry["action"] = "would_delete"
                entry["reason"] = f"duplicate of {survivor}"
                entry["survivor_task_id"] = survivor
                deletable.append(tid)
            else:
                # Survivor is a kept user/ritual task -> the duplicates are kept too.
                entry["classification"] = "kept"
                entry["action"] = "kept"
                entry["reason"] = "duplicate of a kept task"
                entry["survivor_task_id"] = survivor
            task_rows[tid] = entry
            continue

        # Canonical duplicate survivor: keep exactly one copy this run. Even if it
        # is itself a fa-auto orphan, it is protected here so we never delete every
        # copy in one pass; a later run (now with no duplicate) removes a lone
        # orphan via rule (a).
        if tid in duplicate_survivor:
            entry["classification"] = "kept"
            entry["action"] = "kept"
            entry["reason"] = "canonical surviving copy of a duplicate group"
            task_rows[tid] = entry
            continue

        # 4. fa_auto_orphan
        if has_fa_auto:
            entry["classification"] = "fa_auto_orphan"
            entry["had_marker"] = sk is not None
            entry["action"] = "would_delete"
            entry["reason"] = "fa-auto label with no open emission"
            deletable.append(tid)
            task_rows[tid] = entry
            continue

        # 5. kept
        entry["classification"] = "kept"
        entry["action"] = "kept"
        entry["reason"] = "no fa-auto, no managed emission, no legacy prefix"
        task_rows[tid] = entry

    # --- counts + invariant --------------------------------------------------
    for tid in order:
        entry = task_rows[tid]
        report["counts"][entry["classification"]] += 1
        if entry["action"] == "needs_review":
            report["counts"]["needs_review"] += 1
    report["listed"] = len(order)
    report["tasks"] = [task_rows[tid] for tid in order]

    # --- ledger-orphan finding (only sound on a fully-drained, clean LIST) ----
    listed_ids = set(order)
    ledger_orphans: list[Any] = []
    if not incomplete:
        ledger_orphans = [
            r for r in emissions if r["status"] == "open" and r["todoist_task_id"] not in listed_ids
        ]
        report["ledger_findings"]["ledger_orphan"] = len(ledger_orphans)

    # --- gating outcome ------------------------------------------------------
    if incomplete:
        report["truncated"] = True
        report["status"] = "truncated"
        report["reason"] = "list truncated (page cap hit)" if truncated else "list page request failed"
    elif apply and not live:
        report["status"] = "awaiting-integration"
        report["reason"] = "awaiting-integration"

    can_apply = bool(apply and live and not incomplete)
    report["applied"] = can_apply

    # --- apply: deletes + ledger resolutions ---------------------------------
    if apply and not live and not incomplete:
        # Report-only: surface what WOULD be deleted, perform nothing.
        report["actions"]["skipped_not_live"] = len(deletable)
    elif can_apply:
        deleted_count = 0
        for tid in deletable:
            entry = task_rows[tid]
            if deleted_count >= MAX_DELETES_PER_RUN:
                report["truncated_deletes"] = True
                break  # leave the rest as would_delete; a re-run finishes them
            try:
                delete_func(token, tid)
            except Exception as exc:  # noqa: BLE001 - isolate one failure, keep going
                entry["action"] = "failed"
                entry["reason"] = f"delete failed: {type(exc).__name__}: {exc}"[:200]
                report["actions"]["failed"] += 1
                continue
            entry["action"] = "deleted"
            report["actions"]["deleted"] += 1
            deleted_count += 1
            # If this task's marker maps to an OPEN emission for THIS id, retire it.
            # (For the delete set this is effectively only the managed case, which
            # is never deleted; the id guard prevents retiring another task's row.)
            sk = entry["surface_key"]
            if sk is not None:
                row = emission_by_key.get(sk)
                if row is not None and row["status"] == "open" and row["todoist_task_id"] == tid:
                    mark_emission_status(conn, sk, "retired")
                    report["actions"]["ledger_resolved"] += 1
        # Ledger-orphans: the task is already gone, resolve the open row to
        # 'retired' (NOT deleted_by_user, so a recurring key can resurface).
        for r in ledger_orphans:
            mark_emission_status(conn, r["surface_key"], "retired")
            report["actions"]["ledger_resolved"] += 1

    return report


def _forbidden_delete(*_args: Any, **_kwargs: Any) -> bool:
    """Guard delete_func for the read-only LIST tool.

    ``list_todoist_project_for_db`` reuses the reconcile classifier with
    ``apply=False``, which never reaches the delete branch. This guard makes the
    invariant structural rather than incidental: if a future refactor ever wired a
    delete into the read path, it raises instead of silently mutating Todoist.
    """

    raise RuntimeError("list_todoist_project is read-only and must never delete a task")


def list_todoist_project_for_db(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    write_enabled: bool | None = None,
    token: str | None = None,
    project_id: str | None = None,
    env_path: str | None = None,
    list_func: Callable[..., dict[str, Any]] = _list_tasks_request,
) -> dict[str, Any]:
    """Read-only LIST + classify of the whole Finance project.

    Runs the SAME paginated LIST and per-task classification as
    ``reconcile_todoist_project_for_db`` but with NO delete capability and NO
    apply path: ``apply`` is hard-forced ``False`` and the delete hook is
    ``_forbidden_delete`` (raises if ever reached), so this can never mutate
    Todoist or the local ledger. The returned report is the reconcile report shape
    with ``applied`` always ``False`` and every entry in ``actions`` zero; tasks
    still carry their ``would_delete`` classification so a caller can see what a
    cleanup WOULD remove without performing it.

    The dry-run report needs only token+project (reads are allowed with write-back
    off); no commit is required. A truncated or failed LIST is reported via
    ``truncated`` / ``status`` exactly as the reconcile path reports it.
    """

    return reconcile_todoist_project_for_db(
        conn,
        as_of_date=as_of_date,
        apply=False,
        write_enabled=write_enabled,
        token=token,
        project_id=project_id,
        env_path=env_path,
        list_func=list_func,
        delete_func=_forbidden_delete,
    )


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


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _now() -> str:
    return datetime.now().astimezone().isoformat()
