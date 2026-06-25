import sqlite3

from financial_agent.calendar_facts import import_calendar_facts, list_calendar_facts
from financial_agent.income import apply_income_source, generate_income_instances
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def test_import_calendar_facts_is_idempotent_and_listable(tmp_path):
    conn = _db(tmp_path / "calendar.sqlite")

    first_run = import_calendar_facts(
        conn,
        [
            {
                "fact_type": "business_closure",
                "date": "2026-06-10",
                "source": "google_calendar",
                "external_id": "event-1",
                "calendar_id": "payroll",
                "title": "Payroll office closed",
                "confidence": "high",
            }
        ],
    )
    second_run = import_calendar_facts(
        conn,
        [
            {
                "fact_type": "business_closure",
                "date": "2026-06-10",
                "source": "google_calendar",
                "external_id": "event-1",
                "calendar_id": "payroll",
                "title": "Payroll office closed",
                "confidence": "high",
            }
        ],
    )

    facts = list_calendar_facts(
        conn,
        fact_type="business_closure",
        start_date="2026-06-01",
        through_date="2026-06-30",
    )

    assert first_run["created"] == 1
    assert first_run["updated"] == 0
    assert second_run["created"] == 0
    assert second_run["updated"] == 1
    assert first_run["calendar_fact_ids"] == second_run["calendar_fact_ids"]
    fact_id = first_run["calendar_fact_ids"][0]
    assert fact_id.startswith("calendar_fact_")
    assert facts == [
        {
            "id": fact_id,
            "fact_type": "business_closure",
            "date": "2026-06-10",
            "source": "google_calendar",
            "external_id": "event-1",
            "calendar_id": "payroll",
            "related_entity_type": None,
            "related_entity_id": None,
            "title": "Payroll office closed",
            "confidence": "high",
            "status": "active",
            "notes": None,
            "payload": None,
        }
    ]


def test_income_generation_uses_stored_business_closure_facts(tmp_path):
    conn = _db(tmp_path / "calendar.sqlite")
    import_calendar_facts(
        conn,
        [
            {
                "fact_type": "business_closure",
                "date": "2026-06-10",
                "source": "google_calendar",
                "external_id": "closure-1",
            }
        ],
    )
    apply_income_source(
        conn,
        {
            "id": "income_test",
            "person": "Test",
            "employer": "Example",
            "display_name": "Test pay",
            "status": "active",
            "default_amount": 100.0,
            "deposit_account_id": "checking",
            "working_account_id": "checking",
            "source": "test",
            "confidence": "high",
            "active_from": "2026-06-01",
            "active_until": "2026-06-30",
            "review_by": None,
            "notes": None,
            "schedule": {
                "id": "schedule_test_v1",
                "schedule_type": "semi_monthly_days",
                "rule": {"days": [10], "business_day_adjustment": "previous"},
                "valid_from": "2026-06-01",
                "valid_until": "2026-06-30",
                "confidence": "high",
                "source": "test",
                "status": "active",
                "review_by": None,
            },
        },
    )

    generate_income_instances(conn, start_date="2026-06-01", through_date="2026-06-30")

    rows = conn.execute(
        "SELECT due_date FROM obligation_instances ORDER BY due_date"
    ).fetchall()
    assert [row["due_date"] for row in rows] == ["2026-06-09"]


def test_calendar_date_income_schedule_uses_stored_pay_date_facts(tmp_path):
    conn = _db(tmp_path / "calendar.sqlite")
    import_calendar_facts(
        conn,
        [
            {
                "fact_type": "income_pay_date",
                "date": "2026-06-12",
                "source": "google_calendar",
                "external_id": "pay-1",
                "related_entity_type": "income_source",
                "related_entity_id": "income_calendar",
                "title": "Confirmed pay date",
            },
            {
                "fact_type": "income_pay_date",
                "date": "2026-06-26",
                "source": "google_calendar",
                "external_id": "pay-2",
                "related_entity_type": "income_source",
                "related_entity_id": "income_calendar",
                "title": "Confirmed pay date",
            },
        ],
    )
    apply_income_source(
        conn,
        {
            "id": "income_calendar",
            "person": "Test",
            "employer": "Example",
            "display_name": "Calendar pay",
            "status": "active",
            "default_amount": 100.0,
            "deposit_account_id": "checking",
            "working_account_id": "checking",
            "source": "calendar",
            "confidence": "high",
            "active_from": "2026-06-01",
            "active_until": "2026-06-30",
            "review_by": None,
            "notes": None,
            "schedule": {
                "id": "schedule_calendar_v1",
                "schedule_type": "calendar_dates",
                "rule": {"fact_type": "income_pay_date"},
                "valid_from": "2026-06-01",
                "valid_until": "2026-06-30",
                "confidence": "high",
                "source": "google_calendar",
                "status": "active",
                "review_by": None,
            },
        },
    )

    result = generate_income_instances(
        conn,
        start_date="2026-06-01",
        through_date="2026-06-30",
    )

    rows = conn.execute(
        "SELECT due_date, amount, direction FROM obligation_instances ORDER BY due_date"
    ).fetchall()
    assert result["created"] == 2
    assert [(row["due_date"], row["amount"], row["direction"]) for row in rows] == [
        ("2026-06-12", 100.0, "inflow"),
        ("2026-06-26", 100.0, "inflow"),
    ]
