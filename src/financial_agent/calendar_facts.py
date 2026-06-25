from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime
from typing import Any

from .schema import ensure_app_schema


ACTIVE_STATUS = "active"
BUSINESS_CLOSURE = "business_closure"
INCOME_PAY_DATE = "income_pay_date"


def import_calendar_facts(conn: sqlite3.Connection, facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Import normalized calendar facts into local storage.

    Callers should do source-specific work before this layer. For example,
    Google Calendar import should fetch events and convert them into typed facts
    before calling this function.
    """

    ensure_app_schema(conn)
    created = 0
    updated = 0
    imported_ids: list[str] = []
    now = _now()

    for fact in facts:
        normalized = _normalize_fact(fact)
        fact_id = normalized["id"]
        before = conn.execute("SELECT 1 FROM calendar_facts WHERE id = ?", (fact_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO calendar_facts (
                id, fact_type, fact_date, source, external_id, calendar_id,
                related_entity_type, related_entity_id, title, confidence,
                status, notes, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                fact_type = excluded.fact_type,
                fact_date = excluded.fact_date,
                source = excluded.source,
                external_id = excluded.external_id,
                calendar_id = excluded.calendar_id,
                related_entity_type = excluded.related_entity_type,
                related_entity_id = excluded.related_entity_id,
                title = excluded.title,
                confidence = excluded.confidence,
                status = excluded.status,
                notes = excluded.notes,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                fact_id,
                normalized["fact_type"],
                normalized["date"],
                normalized["source"],
                normalized.get("external_id"),
                normalized.get("calendar_id"),
                normalized.get("related_entity_type"),
                normalized.get("related_entity_id"),
                normalized.get("title"),
                normalized["confidence"],
                normalized["status"],
                normalized.get("notes"),
                normalized.get("payload_json"),
                now,
                now,
            ),
        )
        imported_ids.append(fact_id)
        if before:
            updated += 1
        else:
            created += 1

    return {"created": created, "updated": updated, "calendar_fact_ids": imported_ids}


def list_calendar_facts(
    conn: sqlite3.Connection,
    *,
    fact_type: str | None = None,
    start_date: str | None = None,
    through_date: str | None = None,
    status: str | None = ACTIVE_STATUS,
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where = []
    params: list[Any] = []
    if fact_type is not None:
        where.append("fact_type = ?")
        params.append(fact_type)
    if start_date is not None:
        where.append("fact_date >= ?")
        params.append(start_date)
    if through_date is not None:
        where.append("fact_date <= ?")
        params.append(through_date)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if related_entity_type is not None:
        where.append("related_entity_type = ?")
        params.append(related_entity_type)
    if related_entity_id is not None:
        where.append("related_entity_id = ?")
        params.append(related_entity_id)

    query = """
        SELECT
            id, fact_type, fact_date, source, external_id, calendar_id,
            related_entity_type, related_entity_id, title, confidence,
            status, notes, payload_json
        FROM calendar_facts
    """
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY fact_date, fact_type, id"

    rows = conn.execute(query, params).fetchall()
    return [_row_to_fact(row) for row in rows]


def closure_dates_for_range(
    conn: sqlite3.Connection,
    *,
    start: date,
    through: date,
) -> list[date]:
    facts = list_calendar_facts(
        conn,
        fact_type=BUSINESS_CLOSURE,
        start_date=start.isoformat(),
        through_date=through.isoformat(),
        status=ACTIVE_STATUS,
    )
    return [date.fromisoformat(fact["date"]) for fact in facts]


def income_pay_dates_for_source(
    conn: sqlite3.Connection,
    *,
    income_source_id: str,
    start: date,
    through: date,
    fact_type: str = INCOME_PAY_DATE,
) -> list[date]:
    facts = list_calendar_facts(
        conn,
        fact_type=fact_type,
        start_date=start.isoformat(),
        through_date=through.isoformat(),
        status=ACTIVE_STATUS,
        related_entity_type="income_source",
        related_entity_id=income_source_id,
    )
    return [date.fromisoformat(fact["date"]) for fact in facts]


def _normalize_fact(fact: dict[str, Any]) -> dict[str, Any]:
    if "fact_type" not in fact:
        raise ValueError("calendar fact requires fact_type")
    if "date" not in fact:
        raise ValueError("calendar fact requires date")
    if "source" not in fact:
        raise ValueError("calendar fact requires source")

    normalized = {
        "fact_type": fact["fact_type"],
        "date": _coerce_date(fact["date"]).isoformat(),
        "source": fact["source"],
        "external_id": fact.get("external_id"),
        "calendar_id": fact.get("calendar_id"),
        "related_entity_type": fact.get("related_entity_type"),
        "related_entity_id": fact.get("related_entity_id"),
        "title": fact.get("title"),
        "confidence": fact.get("confidence") or "medium",
        "status": fact.get("status") or ACTIVE_STATUS,
        "notes": fact.get("notes"),
    }
    payload = fact.get("payload")
    if payload is not None:
        normalized["payload_json"] = json.dumps(payload, sort_keys=True)
    fact_id = fact.get("id") or _derive_fact_id(normalized)
    normalized["id"] = fact_id
    return normalized


def _derive_fact_id(fact: dict[str, Any]) -> str:
    identity = {
        "calendar_id": fact.get("calendar_id"),
        "date": fact["date"],
        "external_id": fact.get("external_id"),
        "fact_type": fact["fact_type"],
        "related_entity_id": fact.get("related_entity_id"),
        "related_entity_type": fact.get("related_entity_type"),
        "source": fact["source"],
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return f"calendar_fact_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _row_to_fact(row: sqlite3.Row) -> dict[str, Any]:
    payload_json = row["payload_json"]
    return {
        "id": row["id"],
        "fact_type": row["fact_type"],
        "date": row["fact_date"],
        "source": row["source"],
        "external_id": row["external_id"],
        "calendar_id": row["calendar_id"],
        "related_entity_type": row["related_entity_type"],
        "related_entity_id": row["related_entity_id"],
        "title": row["title"],
        "confidence": row["confidence"],
        "status": row["status"],
        "notes": row["notes"],
        "payload": json.loads(payload_json) if payload_json else None,
    }


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _now() -> str:
    return datetime.now().astimezone().isoformat()
