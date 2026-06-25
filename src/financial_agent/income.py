from __future__ import annotations

import calendar
import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from .calendar import BusinessCalendar
from .calendar_facts import closure_dates_for_range, income_pay_dates_for_source
from .schema import ensure_app_schema


WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def apply_income_source(conn: sqlite3.Connection, source: dict[str, Any]) -> dict[str, Any]:
    """Create or update an income source and its active schedule version."""

    ensure_app_schema(conn)
    now = _now()
    schedule = source["schedule"]
    conn.execute(
        """
        INSERT INTO obligations (
            id, name, kind, cadence, status, source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            kind = excluded.kind,
            cadence = excluded.cadence,
            status = excluded.status,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (
            source["id"],
            source["display_name"],
            "income",
            schedule["schedule_type"],
            source["status"],
            source["source"],
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO income_sources (
            id, person, employer, display_name, status, default_amount,
            deposit_account_id, working_account_id, source, confidence,
            active_from, active_until, review_by, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            person = excluded.person,
            employer = excluded.employer,
            display_name = excluded.display_name,
            status = excluded.status,
            default_amount = excluded.default_amount,
            deposit_account_id = excluded.deposit_account_id,
            working_account_id = excluded.working_account_id,
            source = excluded.source,
            confidence = excluded.confidence,
            active_from = excluded.active_from,
            active_until = excluded.active_until,
            review_by = excluded.review_by,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            source["id"],
            source["person"],
            source["employer"],
            source["display_name"],
            source["status"],
            source["default_amount"],
            source.get("deposit_account_id"),
            source.get("working_account_id"),
            source["source"],
            source["confidence"],
            source["active_from"],
            source.get("active_until"),
            source.get("review_by"),
            source.get("notes"),
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO income_schedule_versions (
            id, income_source_id, schedule_type, rule_json, valid_from,
            valid_until, confidence, source, status, review_by, created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            income_source_id = excluded.income_source_id,
            schedule_type = excluded.schedule_type,
            rule_json = excluded.rule_json,
            valid_from = excluded.valid_from,
            valid_until = excluded.valid_until,
            confidence = excluded.confidence,
            source = excluded.source,
            status = excluded.status,
            review_by = excluded.review_by,
            updated_at = excluded.updated_at
        """,
        (
            schedule["id"],
            source["id"],
            schedule["schedule_type"],
            json.dumps(schedule["rule"], sort_keys=True),
            schedule["valid_from"],
            schedule.get("valid_until"),
            schedule["confidence"],
            schedule["source"],
            schedule["status"],
            schedule.get("review_by"),
            now,
            now,
        ),
    )
    return {"income_source_id": source["id"], "schedule_version_id": schedule["id"]}


def generate_income_instances(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    through_date: str,
    extra_closure_dates: Iterable[date | str] | None = None,
) -> dict[str, Any]:
    ensure_app_schema(conn)
    start = date.fromisoformat(start_date)
    through = date.fromisoformat(through_date)
    stored_closure_dates = closure_dates_for_range(conn, start=start, through=through)
    business_calendar = BusinessCalendar([*(extra_closure_dates or ()), *stored_closure_dates])
    created = 0
    unchanged = 0
    generated_ids: list[str] = []

    rows = conn.execute(
        """
        SELECT
            src.id AS income_source_id,
            src.display_name,
            src.status AS income_source_status,
            src.default_amount,
            src.active_from AS income_source_active_from,
            src.active_until AS income_source_active_until,
            src.confidence AS income_source_confidence,
            src.source AS income_source_record_source,
            sched.id AS schedule_version_id,
            sched.schedule_type,
            sched.rule_json,
            sched.valid_from,
            sched.valid_until,
            sched.confidence AS schedule_confidence,
            sched.source AS schedule_source
        FROM income_sources src
        JOIN income_schedule_versions sched ON sched.income_source_id = src.id
        WHERE src.status = 'active'
          AND sched.status = 'active'
        ORDER BY src.id, sched.valid_from, sched.id
        """
    ).fetchall()

    now = _now()
    for row in rows:
        valid_from = max(
            start,
            date.fromisoformat(row["income_source_active_from"]),
            date.fromisoformat(row["valid_from"]),
        )
        valid_until = through
        if row["income_source_active_until"]:
            valid_until = min(valid_until, date.fromisoformat(row["income_source_active_until"]))
        if row["valid_until"]:
            valid_until = min(valid_until, date.fromisoformat(row["valid_until"]))
        rule = json.loads(row["rule_json"])
        if row["schedule_type"] == "calendar_dates":
            due_dates = _calendar_dates(
                conn,
                income_source_id=row["income_source_id"],
                rule=rule,
                start=valid_from,
                through=valid_until,
            )
        else:
            due_dates = _schedule_dates(
                row["schedule_type"],
                rule,
                valid_from,
                valid_until,
                business_calendar,
            )
        for due_date in due_dates:
            instance_id = f"{row['income_source_id']}:{due_date.isoformat()}"
            before = conn.execute(
                "SELECT 1 FROM obligation_instances WHERE id = ?",
                (instance_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO obligation_instances (
                    id, obligation_id, due_date, amount, direction, status,
                    source, confidence, notes, generated_from_income_source_id,
                    generated_from_schedule_version_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    obligation_id = excluded.obligation_id,
                    due_date = excluded.due_date,
                    amount = excluded.amount,
                    direction = excluded.direction,
                    status = excluded.status,
                    source = excluded.source,
                    confidence = excluded.confidence,
                    notes = excluded.notes,
                    generated_from_income_source_id = excluded.generated_from_income_source_id,
                    generated_from_schedule_version_id = excluded.generated_from_schedule_version_id,
                    updated_at = excluded.updated_at
                """,
                (
                    instance_id,
                    row["income_source_id"],
                    due_date.isoformat(),
                    row["default_amount"],
                    "inflow",
                    "expected",
                    f"income_schedule:{row['schedule_source']}",
                    row["schedule_confidence"],
                    f"Generated from {row['display_name']} schedule.",
                    row["income_source_id"],
                    row["schedule_version_id"],
                    now,
                    now,
                ),
            )
            generated_ids.append(instance_id)
            if before:
                unchanged += 1
            else:
                created += 1

    canceled_obsolete = _cancel_obsolete_expected_instances(
        conn,
        start=start,
        through=through,
        generated_ids=set(generated_ids),
        updated_at=now,
    )

    return {
        "created": created,
        "unchanged": unchanged,
        "canceled_obsolete": canceled_obsolete,
        "generated_instance_ids": generated_ids,
        "start_date": start.isoformat(),
        "through_date": through.isoformat(),
    }


def list_income_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    sources = conn.execute(
        """
        SELECT
            id, person, employer, display_name, status, default_amount,
            deposit_account_id, working_account_id, source, confidence,
            active_from, active_until, review_by
        FROM income_sources
        ORDER BY person, employer, id
        """
    ).fetchall()
    result = []
    for source in sources:
        schedules = conn.execute(
            """
            SELECT
                id, schedule_type, rule_json, valid_from, valid_until,
                confidence, source, status, review_by
            FROM income_schedule_versions
            WHERE income_source_id = ?
            ORDER BY valid_from, id
            """,
            (source["id"],),
        ).fetchall()
        horizon = conn.execute(
            """
            SELECT MAX(due_date) AS generated_through
            FROM obligation_instances
            WHERE generated_from_income_source_id = ?
            """,
            (source["id"],),
        ).fetchone()
        result.append(
            {
                "id": source["id"],
                "person": source["person"],
                "employer": source["employer"],
                "display_name": source["display_name"],
                "status": source["status"],
                "default_amount": round(float(source["default_amount"]), 2),
                "deposit_account_id": source["deposit_account_id"],
                "working_account_id": source["working_account_id"],
                "source": source["source"],
                "confidence": source["confidence"],
                "active_from": source["active_from"],
                "active_until": source["active_until"],
                "review_by": source["review_by"],
                "generated_through": horizon["generated_through"],
                "schedule_versions": [
                    {
                        "id": schedule["id"],
                        "schedule_type": schedule["schedule_type"],
                        "rule": json.loads(schedule["rule_json"]),
                        "valid_from": schedule["valid_from"],
                        "valid_until": schedule["valid_until"],
                        "confidence": schedule["confidence"],
                        "source": schedule["source"],
                        "status": schedule["status"],
                        "review_by": schedule["review_by"],
                    }
                    for schedule in schedules
                ],
            }
        )
    return result


def _schedule_dates(
    schedule_type: str,
    rule: dict[str, Any],
    start: date,
    through: date,
    business_calendar: BusinessCalendar,
) -> list[date]:
    if start > through:
        return []
    rule_calendar = business_calendar.with_extra_closure_dates(rule.get("extra_closure_dates"))
    if schedule_type == "semi_monthly_days":
        return _semi_monthly_dates(rule, start, through, rule_calendar)
    if schedule_type == "biweekly_weekday":
        return _biweekly_weekday_dates(rule, start, through, rule_calendar)
    raise ValueError(f"Unsupported income schedule type: {schedule_type}")


def _calendar_dates(
    conn: sqlite3.Connection,
    *,
    income_source_id: str,
    rule: dict[str, Any],
    start: date,
    through: date,
) -> list[date]:
    fact_type = rule.get("fact_type", "income_pay_date")
    return income_pay_dates_for_source(
        conn,
        income_source_id=income_source_id,
        start=start,
        through=through,
        fact_type=fact_type,
    )


def _semi_monthly_dates(
    rule: dict[str, Any],
    start: date,
    through: date,
    business_calendar: BusinessCalendar,
) -> list[date]:
    dates = []
    current = date(start.year, start.month, 1)
    while current <= through:
        _, last_day = calendar.monthrange(current.year, current.month)
        for day in rule["days"]:
            nominal = date(current.year, current.month, min(int(day), last_day))
            adjusted = _adjust_business_day(
                nominal, rule.get("business_day_adjustment"), business_calendar
            )
            if start <= adjusted <= through:
                dates.append(adjusted)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return sorted(set(dates))


def _biweekly_weekday_dates(
    rule: dict[str, Any],
    start: date,
    through: date,
    business_calendar: BusinessCalendar,
) -> list[date]:
    anchor = date.fromisoformat(rule["anchor_date"])
    weekday = WEEKDAYS[rule["weekday"].lower()]
    if anchor.weekday() != weekday:
        raise ValueError("biweekly_weekday anchor_date does not match weekday")
    current = anchor
    while current < start:
        current += timedelta(days=14)
    dates = []
    while current <= through:
        adjusted = _adjust_business_day(
            current, rule.get("business_day_adjustment"), business_calendar
        )
        if start <= adjusted <= through:
            dates.append(adjusted)
        current += timedelta(days=14)
    return dates


def _adjust_business_day(
    value: date,
    adjustment: str | None,
    business_calendar: BusinessCalendar,
) -> date:
    if adjustment is None:
        return value
    if adjustment != "previous":
        raise ValueError(f"Unsupported business day adjustment: {adjustment}")
    return business_calendar.previous_business_day(value)


def _cancel_obsolete_expected_instances(
    conn: sqlite3.Connection,
    *,
    start: date,
    through: date,
    generated_ids: set[str],
    updated_at: str,
) -> int:
    rows = conn.execute(
        """
        SELECT id
        FROM obligation_instances
        WHERE generated_from_income_source_id IS NOT NULL
          AND due_date >= ?
          AND due_date <= ?
          AND status = 'expected'
        """,
        (start.isoformat(), through.isoformat()),
    ).fetchall()
    obsolete_ids = [row["id"] for row in rows if row["id"] not in generated_ids]
    for instance_id in obsolete_ids:
        conn.execute(
            """
            UPDATE obligation_instances
            SET status = 'canceled',
                notes = 'Canceled because the generated income schedule no longer includes this date.',
                updated_at = ?
            WHERE id = ?
            """,
            (updated_at, instance_id),
        )
    return len(obsolete_ids)


def _now() -> str:
    return datetime.now().astimezone().isoformat()
