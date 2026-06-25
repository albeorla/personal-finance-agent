"""Todoist as an INPUT source for one-off obligations (cutover slice G).

The local DB stays canonical; Todoist is the *origin* for one-off obligations
only (recurring obligations stay model-driven). This module reads
``cashflow_candidate`` tasks and turns each into a canonical one-off obligation
plus a single dated instance, tracking the link in ``todoist_sync_records`` so
re-import is idempotent and task lifecycle changes are handled.

Guardrails honored:
- A checked / completed task is NOT proof of payment: it sets ``review_after``
  for human/bank confirmation, it never auto-marks the instance paid.
- A deleted task cancels its instance.
- A task that collides with an existing recurring obligation is flagged
  ``needs_review_dedup_conflict`` and NOT imported, pending a human decision.
- Write-back to Todoist (flagging a task with its obligation) is recorded as a
  dry-run ``todoist_flag_task`` action in the existing outbox; nothing is sent.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .obligations import apply_obligation_instances
from .schema import ensure_app_schema


ONE_OFF_KIND = "one_off"
ONE_OFF_PREFIX = "todoist_oneoff_"
FLAG_ACTION_TYPE = "todoist_flag_task"
DEDUP_DATE_WINDOW_DAYS = 7

# Generic action + category words that do NOT identify a distinct obligation, so
# they must not, on their own, trigger a dedup match (e.g. "Federal tax" vs
# "State tax" share only "tax"). Entity words (gault, partner, volvo, ...) stay.
_NAME_STOPWORDS: frozenset[str] = frozenset(
    {"pay", "payment", "payments", "manual", "autopay", "auto", "the", "for", "and",
     "joint", "via", "est", "estimate", "bill", "from", "into", "transfer", "card",
     "tax", "taxes", "premium", "premiums", "insurance", "subscription", "subscriptions",
     "utility", "utilities", "rent", "mortgage", "loan", "loans", "credit", "debit",
     "service", "services", "fee", "fees", "charge", "charges", "interest", "deposit",
     "refund", "statement", "monthly", "annual", "weekly", "biweekly", "plan",
     "account", "balance", "minimum", "online", "recurring"}
)


def import_todoist_obligations(
    conn: sqlite3.Connection,
    *,
    tasks: list[Any] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Import cashflow-candidate Todoist tasks as canonical one-off obligations."""

    ensure_app_schema(conn)
    opts = options or {}
    as_of = _coerce_date(opts.get("as_of_date") or date.today())
    do_dedup = opts.get("dedup", True)
    now = _now()

    rows = tasks if tasks is not None else _read_cashflow_candidate_tasks(conn)

    scanned = imported = skipped = dedup_conflicts = needs_review = canceled = 0
    errors: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for raw in rows:
        scanned += 1
        task = _normalize_task(raw)
        task_id = task["id"]
        if task["amount"] is None or not task["due_date"]:
            skipped += 1
            needs_review += 1
            _record_sync(conn, task, sync_status="needs_review_missing_fields", now=now,
                         obligation_id=None, instance_id=None,
                         error_notes="missing amount and/or due_date")
            results.append({"task_id": task_id, "result": "skipped_missing_fields"})
            continue

        obligation_id = f"{ONE_OFF_PREFIX}{task_id}"
        instance_id = f"{obligation_id}:{task['due_date']}"

        if task["is_deleted"]:
            # Previously real, now removed in Todoist -> cancel the instance.
            _apply_one_off(conn, task, obligation_id, instance_id, status="canceled", review_after=None)
            _record_sync(conn, task, sync_status="imported_canceled", now=now,
                         obligation_id=obligation_id, instance_id=instance_id)
            canceled += 1
            results.append({"task_id": task_id, "result": "canceled"})
            continue

        if do_dedup:
            conflict = _find_dedup_conflict(conn, task, obligation_id)
            if conflict is not None:
                dedup_conflicts += 1
                needs_review += 1
                _record_sync(conn, task, sync_status="needs_review_dedup_conflict", now=now,
                             obligation_id=obligation_id, instance_id=instance_id,
                             error_notes=f"resembles recurring obligation {conflict['obligation_id']}")
                results.append({"task_id": task_id, "result": "needs_review_dedup_conflict",
                                "conflicts_with": conflict["obligation_id"]})
                continue

        # checked/completed -> flag for review, but never auto-mark paid.
        review_after = as_of.isoformat() if (task["checked"] or task["completed_at"]) else None
        _apply_one_off(conn, task, obligation_id, instance_id, status="expected", review_after=review_after)
        _record_sync(conn, task, sync_status="imported", now=now,
                     obligation_id=obligation_id, instance_id=instance_id)
        enqueue_todoist_flag_task(conn, obligation_instance_id=instance_id, external_task_id=task_id, dry_run=True)
        imported += 1
        results.append({"task_id": task_id, "result": "imported", "obligation_id": obligation_id})

    log_id = f"todoist_import_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO todoist_import_log (
            id, run_timestamp, tasks_scanned, tasks_imported, tasks_skipped,
            dedup_conflicts, needs_review_count, errors_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (log_id, now, scanned, imported, skipped, dedup_conflicts, needs_review,
         json.dumps(errors, sort_keys=True) if errors else None),
    )

    return {
        "scanned": scanned,
        "imported": imported,
        "canceled": canceled,
        "skipped": skipped,
        "dedup_conflicts": dedup_conflicts,
        "needs_review": needs_review,
        "import_log_id": log_id,
        "results": results,
    }


def enqueue_todoist_flag_task(
    conn: sqlite3.Connection,
    *,
    obligation_instance_id: str,
    external_task_id: str,
    action: str = "add_obligation_label",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Record an idempotent, dry-run write-back to flag a Todoist task with its obligation.

    Reuses the existing action_outbox. Nothing is sent to Todoist here; the agent
    or a future configured sender performs the actual write.
    """

    ensure_app_schema(conn)
    key = f"todoist_flag:{external_task_id}"
    payload = {"obligation_instance_id": obligation_instance_id, "external_task_id": external_task_id, "action": action}
    payload_json = json.dumps(payload, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()[:16]
    status = "dry_run" if dry_run else "pending"
    now = _now()
    conn.execute(
        """
        INSERT INTO action_outbox (
            id, idempotency_key, action_type, target_type, target_id, payload_json,
            payload_hash, dry_run, status, attempts, item_count, created_at, updated_at
        ) VALUES (?, ?, ?, 'todoist_task', ?, ?, ?, ?, ?, 0, 1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            payload_json = excluded.payload_json,
            payload_hash = excluded.payload_hash,
            dry_run = excluded.dry_run,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (key, key, FLAG_ACTION_TYPE, external_task_id, payload_json, payload_hash, 1 if dry_run else 0, status, now, now),
    )
    return {"idempotency_key": key, "status": status, "dry_run": dry_run, "external_task_id": external_task_id}


def list_todoist_sync_records(
    conn: sqlite3.Connection,
    *,
    sync_status: str | None = None,
    external_task_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where: list[str] = []
    params: list[Any] = []
    if sync_status is not None:
        where.append("sync_status = ?")
        params.append(sync_status)
    if external_task_id is not None:
        where.append("external_task_id = ?")
        params.append(external_task_id)
    query = "SELECT * FROM todoist_sync_records"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY updated_at DESC, external_task_id"
    return [_row_to_sync(r) for r in conn.execute(query, params).fetchall()]


def resolve_todoist_dedup_conflict(
    conn: sqlite3.Connection,
    *,
    external_task_id: str,
    decision: str,
    merge_with_obligation_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a flagged dedup conflict: import_anyway, skip, or merge."""

    ensure_app_schema(conn)
    row = conn.execute(
        "SELECT * FROM todoist_sync_records WHERE external_task_id = ?", (external_task_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown todoist sync record: {external_task_id}")
    if decision not in {"import_anyway", "skip", "merge"}:
        raise ValueError(f"unsupported decision: {decision!r}; use import_anyway, skip, or merge")
    state = json.loads(row["last_observed_state_json"]) if row["last_observed_state_json"] else {}
    now = _now()

    if decision == "skip":
        conn.execute(
            "UPDATE todoist_sync_records SET sync_status='skipped', updated_at=? WHERE external_task_id=?",
            (now, external_task_id),
        )
        return {"external_task_id": external_task_id, "decision": "skip", "sync_status": "skipped"}

    if decision == "merge":
        if not merge_with_obligation_id:
            raise ValueError("merge requires merge_with_obligation_id")
        conn.execute(
            "UPDATE todoist_sync_records SET sync_status='merged', obligation_id=?, updated_at=? WHERE external_task_id=?",
            (merge_with_obligation_id, now, external_task_id),
        )
        return {"external_task_id": external_task_id, "decision": "merge",
                "obligation_id": merge_with_obligation_id, "sync_status": "merged"}

    # import_anyway: build the one-off from the recorded state and create it.
    task = _normalize_task({
        "id": external_task_id,
        "content": state.get("content", external_task_id),
        "due_date": state.get("due_date"),
        "signed_amount": state.get("signed_amount"),
        "amount_value": state.get("amount_value"),
        "amount_direction": state.get("amount_direction"),
        "checked": state.get("checked", 0),
        "is_deleted": 0,
        "completed_at": state.get("completed_at"),
    })
    obligation_id = row["obligation_id"] or f"{ONE_OFF_PREFIX}{external_task_id}"
    instance_id = f"{obligation_id}:{task['due_date']}"
    _apply_one_off(conn, task, obligation_id, instance_id, status="expected", review_after=None)
    conn.execute(
        "UPDATE todoist_sync_records SET sync_status='imported', obligation_id=?, obligation_instance_id=?, updated_at=? WHERE external_task_id=?",
        (obligation_id, instance_id, now, external_task_id),
    )
    enqueue_todoist_flag_task(conn, obligation_instance_id=instance_id, external_task_id=external_task_id, dry_run=True)
    return {"external_task_id": external_task_id, "decision": "import_anyway", "obligation_id": obligation_id, "sync_status": "imported"}


# --- internals -------------------------------------------------------------


def _apply_one_off(conn, task, obligation_id, instance_id, *, status, review_after) -> None:
    apply_obligation_instances(
        conn,
        obligation={
            "id": obligation_id,
            "name": task["content"],
            "kind": ONE_OFF_KIND,
            "cadence": None,
            "status": "active",
            "source": "todoist",
        },
        instances=[
            {
                "id": instance_id,
                "due_date": task["due_date"],
                "amount": task["amount"],
                "direction": task["direction"],
                "status": status,
                "source": "todoist",
                "confidence": "medium",
                "review_after": review_after,
                "notes": f"Imported from Todoist task {task['id']}: {task['content'][:80]}",
            }
        ],
    )


def _find_dedup_conflict(conn, task, self_obligation_id) -> dict[str, Any] | None:
    due = _coerce_date(task["due_date"])
    lo = (due - timedelta(days=DEDUP_DATE_WINDOW_DAYS)).isoformat()
    hi = (due + timedelta(days=DEDUP_DATE_WINDOW_DAYS)).isoformat()
    candidate_tokens = _name_tokens(task["content"])
    if not candidate_tokens:
        return None
    rows = conn.execute(
        """
        SELECT oi.id AS instance_id, oi.amount, oi.due_date, o.id AS obligation_id, o.name AS obligation_name
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE o.cadence IS NOT NULL
          AND o.status = 'active'
          AND oi.direction = ?
          AND oi.due_date BETWEEN ? AND ?
        """,
        (task["direction"], lo, hi),
    ).fetchall()
    for r in rows:
        if r["obligation_id"] == self_obligation_id:
            continue
        if not _amount_close(task["amount"], float(r["amount"])):
            continue
        if candidate_tokens & _name_tokens(r["obligation_name"]):
            return {"obligation_id": r["obligation_id"], "instance_id": r["instance_id"]}
    return None


def _amount_close(a: float, b: float, abs_tol: float = 5.0, pct_tol: float = 0.05) -> bool:
    # Tolerance-based so small amounts ($15 vs $25) do not collide the way a
    # round-to-100 bucket did, while near-equal large amounts still match.
    return abs(a - b) <= max(abs_tol, pct_tol * max(abs(a), abs(b)))


def _record_sync(conn, task, *, sync_status, now, obligation_id, instance_id, error_notes=None) -> None:
    state = {
        "content": task["content"],
        "checked": task["checked"],
        "is_deleted": task["is_deleted"],
        "due_date": task["due_date"],
        "amount_value": task["amount_value"],
        "signed_amount": task["signed_amount"],
        "amount_direction": task["amount_direction"],
        "completed_at": task["completed_at"],
    }
    conn.execute(
        """
        INSERT INTO todoist_sync_records (
            external_task_id, obligation_id, obligation_instance_id, content_hash,
            last_observed_state_json, sync_status, is_deleted_in_source,
            checked_in_source, completed_at_in_source, error_notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(external_task_id) DO UPDATE SET
            obligation_id = excluded.obligation_id,
            obligation_instance_id = excluded.obligation_instance_id,
            content_hash = excluded.content_hash,
            last_observed_state_json = excluded.last_observed_state_json,
            sync_status = excluded.sync_status,
            is_deleted_in_source = excluded.is_deleted_in_source,
            checked_in_source = excluded.checked_in_source,
            completed_at_in_source = excluded.completed_at_in_source,
            error_notes = excluded.error_notes,
            updated_at = excluded.updated_at
        """,
        (
            task["id"], obligation_id, instance_id, _content_hash(task["content"]),
            json.dumps(state, sort_keys=True), sync_status, 1 if task["is_deleted"] else 0,
            1 if task["checked"] else 0, task["completed_at"], error_notes, now, now,
        ),
    )


def _read_cashflow_candidate_tasks(conn) -> list[Any]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='todoist_tasks' LIMIT 1"
    ).fetchone()
    if not exists:
        raise ValueError("no todoist_tasks table in this database; pass tasks explicitly")
    return conn.execute(
        """
        SELECT id, content, labels_json, due_date, amount_value, amount_direction,
               signed_amount, checked, is_deleted, completed_at
        FROM todoist_tasks
        WHERE cashflow_candidate = 1
        ORDER BY due_date, id
        """
    ).fetchall()


def _normalize_task(raw: Any) -> dict[str, Any]:
    def g(key, default=None):
        try:
            value = raw[key]
        except (KeyError, IndexError, TypeError):
            value = raw.get(key, default) if isinstance(raw, dict) else default
        return value if value is not None else default

    signed = g("signed_amount")
    amount_value = g("amount_value")
    amount_direction = g("amount_direction")
    if signed is None and amount_value is not None:
        # signed_amount is the source of truth; fall back to amount + direction.
        signed = float(amount_value) if (amount_direction is None or float(amount_direction) >= 0) else -float(amount_value)
    amount = abs(float(signed)) if signed is not None else None
    direction = None
    if signed is not None:
        direction = "inflow" if float(signed) >= 0 else "outflow"
    due = g("due_date")
    return {
        "id": str(g("id")),
        "content": g("content", "") or "",
        "due_date": str(due)[:10] if due else None,
        "amount": round(amount, 2) if amount is not None else None,
        "direction": direction,
        "signed_amount": float(signed) if signed is not None else None,
        "amount_value": float(amount_value) if amount_value is not None else None,
        "amount_direction": amount_direction,
        "checked": bool(g("checked", 0)),
        "is_deleted": bool(g("is_deleted", 0)),
        "completed_at": g("completed_at"),
    }


def _name_tokens(text: str) -> set[str]:
    toks = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {t for t in toks if len(t) >= 3 and t not in _NAME_STOPWORDS and not t.isdigit()}


def _row_to_sync(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "external_task_id": row["external_task_id"],
        "obligation_id": row["obligation_id"],
        "obligation_instance_id": row["obligation_instance_id"],
        "content_hash": row["content_hash"],
        "last_observed_state": json.loads(row["last_observed_state_json"]) if row["last_observed_state_json"] else None,
        "sync_status": row["sync_status"],
        "is_deleted_in_source": bool(row["is_deleted_in_source"]),
        "checked_in_source": bool(row["checked_in_source"]),
        "completed_at_in_source": row["completed_at_in_source"],
        "error_notes": row["error_notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode()).hexdigest()[:16]


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _now() -> str:
    return datetime.now().astimezone().isoformat()
