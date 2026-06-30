"""Follow-ups / triggers: the dated-reminder backbone the daily job fires on.

This module is the STORE for time-based reminders ("surface this on this
date"). It is intentionally a plain DB record, not a Todoist push: the daily job
reads due follow-ups from here and decides what to surface. Todoist is only an
output channel downstream; ``capture_followup`` writes to the local DB and never
touches Todoist.

Schema (see ``schema.py`` ``follow_ups`` table):
- ``surface_when`` is an ISO date string. The field is named for *when* to
  surface so a future "condition" form (surface when balance < X) can be layered
  on without renaming the column.
- ``status`` is one of ``pending`` / ``surfaced`` / ``resolved``.
- ``linked_obligation_id`` is a nullable hint, not an enforced foreign key.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

from .schema import ensure_app_schema

# Priorities map to a numeric rank for deterministic ordering (high first). An
# unset priority sorts last among same-date follow-ups.
_PRIORITY_RANK: dict[str | None, int] = {"high": 3, "normal": 2, "low": 1}


def capture_followup(
    conn: sqlite3.Connection,
    text: str,
    surface_when: str,
    priority: str | None = None,
    linked_obligation_id: str | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """Store a follow-up reminder in the DB only (no Todoist push).

    The id is derived deterministically from the content, so re-capturing the
    same follow-up updates the existing row instead of creating a duplicate.
    """

    ensure_app_schema(conn)

    if not text or not text.strip():
        raise ValueError("Follow-up text must be non-empty.")
    if not surface_when or not surface_when.strip():
        raise ValueError("Follow-up surface_when must be non-empty.")
    surface_when_iso = _coerce_date(surface_when)

    followup_id = _followup_id(text, surface_when_iso, priority, source)
    now = _now()

    existing = conn.execute(
        "SELECT created_at FROM follow_ups WHERE id = ?",
        (followup_id,),
    ).fetchone()
    created = existing is None
    created_at = now if created else existing["created_at"]

    conn.execute(
        """
        INSERT INTO follow_ups (
            id, text, surface_when, priority, status, linked_obligation_id,
            source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            text = excluded.text,
            surface_when = excluded.surface_when,
            priority = excluded.priority,
            linked_obligation_id = excluded.linked_obligation_id,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (
            followup_id,
            text.strip(),
            surface_when_iso,
            priority,
            linked_obligation_id,
            source,
            created_at,
            now,
        ),
    )

    return {
        "id": followup_id,
        "text": text.strip(),
        "surface_when": surface_when_iso,
        "priority": priority,
        "status": "pending",
        "linked_obligation_id": linked_obligation_id,
        "source": source,
        "created": created,
        "updated": not created,
    }


def list_due_followups(
    conn: sqlite3.Connection,
    as_of_date: str,
) -> list[dict[str, Any]]:
    """Return pending follow-ups due on or before ``as_of_date``.

    Filters to ``status = 'pending'`` and ``surface_when <= as_of_date`` (ISO
    string comparison). Ordered by surface_when ascending, then priority
    descending (high first), then created_at ascending for stable ties.
    """

    ensure_app_schema(conn)
    as_of = _coerce_date(as_of_date)

    rows = conn.execute(
        """
        SELECT id, text, surface_when, priority, status, linked_obligation_id,
               source, created_at, updated_at
        FROM follow_ups
        WHERE status = 'pending' AND surface_when <= ?
        """,
        (as_of,),
    ).fetchall()

    items = [_row_to_dict(row) for row in rows]
    items.sort(
        key=lambda r: (
            r["surface_when"],
            -_PRIORITY_RANK.get(r["priority"], 0),
            r["created_at"],
            r["id"],
        )
    )
    return items


def resolve_followup(
    conn: sqlite3.Connection,
    followup_id: str,
) -> dict[str, Any]:
    """Mark a follow-up resolved. Idempotent; reports whether a row was found."""

    ensure_app_schema(conn)
    now = _now()
    cur = conn.execute(
        "UPDATE follow_ups SET status = 'resolved', updated_at = ? WHERE id = ?",
        (now, followup_id),
    )
    return {"id": followup_id, "resolved": cur.rowcount > 0}


def update_followup(
    conn: sqlite3.Connection,
    followup_id: str,
    *,
    text: str | None = None,
    surface_when: str | None = None,
    priority: str | None = None,
    linked_obligation_id: str | None = None,
) -> dict[str, Any]:
    """Edit a follow-up in place by id (reschedule, reword, re-prioritize, relink).

    Only the fields you pass change; the id stays stable. This is the edit path:
    re-running capture_followup with new text would mint a NEW row (the capture id
    is content-derived), so use this to change an existing reminder.
    """

    ensure_app_schema(conn)
    fields: list[str] = []
    params: list[Any] = []
    if text is not None:
        if not text.strip():
            raise ValueError("Follow-up text must be non-empty.")
        fields.append("text = ?")
        params.append(text.strip())
    if surface_when is not None:
        if not surface_when.strip():
            raise ValueError("Follow-up surface_when must be non-empty.")
        fields.append("surface_when = ?")
        params.append(_coerce_date(surface_when))
    if priority is not None:
        fields.append("priority = ?")
        params.append(priority)
    if linked_obligation_id is not None:
        fields.append("linked_obligation_id = ?")
        params.append(linked_obligation_id)
    if not fields:
        raise ValueError("update_followup needs at least one field to change.")

    fields.append("updated_at = ?")
    params.append(_now())
    params.append(followup_id)
    cur = conn.execute(f"UPDATE follow_ups SET {', '.join(fields)} WHERE id = ?", params)
    if cur.rowcount == 0:
        return {"id": followup_id, "updated": False}
    row = conn.execute("SELECT * FROM follow_ups WHERE id = ?", (followup_id,)).fetchone()
    return {"id": followup_id, "updated": True, "followup": _row_to_dict(row)}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "text": row["text"],
        "surface_when": row["surface_when"],
        "priority": row["priority"],
        "status": row["status"],
        "linked_obligation_id": row["linked_obligation_id"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _followup_id(
    text: str,
    surface_when: str,
    priority: str | None,
    source: str,
) -> str:
    raw = json.dumps(
        {
            "text": text.strip(),
            "surface_when": surface_when,
            "priority": priority,
            "source": source,
        },
        sort_keys=True,
    )
    return f"fup_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _coerce_date(value: str) -> str:
    """Normalize an ISO date string to YYYY-MM-DD, validating the format."""
    from datetime import date

    return date.fromisoformat(value[:10]).isoformat()


def _now() -> str:
    return datetime.now().astimezone().isoformat()
