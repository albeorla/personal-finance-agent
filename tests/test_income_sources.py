import sqlite3

from financial_agent.income import (
    apply_income_source,
    generate_income_instances,
    list_income_sources,
)
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def _single_semimonthly_due_dates(
    tmp_path,
    *,
    days,
    start_date,
    through_date,
    extra_closure_dates=None,
):
    db_path = tmp_path / "income.sqlite"
    conn = _db(db_path)

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
            "active_from": start_date,
            "active_until": through_date,
            "review_by": None,
            "notes": None,
            "schedule": {
                "id": "schedule_test_v1",
                "schedule_type": "semi_monthly_days",
                "rule": {"days": days, "business_day_adjustment": "previous"},
                "valid_from": start_date,
                "valid_until": through_date,
                "confidence": "high",
                "source": "test",
                "status": "active",
                "review_by": None,
            },
        },
    )
    generate_income_instances(
        conn,
        start_date=start_date,
        through_date=through_date,
        extra_closure_dates=extra_closure_dates,
    )

    rows = conn.execute(
        """
        SELECT due_date
        FROM obligation_instances
        ORDER BY due_date
        """
    ).fetchall()
    return [row["due_date"] for row in rows]


def test_ensure_app_schema_migrates_existing_obligation_instances(tmp_path):
    db_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE obligation_instances (
            id TEXT PRIMARY KEY,
            obligation_id TEXT NOT NULL,
            due_date TEXT NOT NULL,
            amount REAL NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('inflow', 'outflow')),
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    ensure_app_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(obligation_instances)")}
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(obligation_instances)")}

    assert "generated_from_income_source_id" in columns
    assert "generated_from_schedule_version_id" in columns
    assert "idx_obligation_instances_income_source" in indexes


def test_income_source_generation_creates_dated_instances_with_business_day_rules(tmp_path):
    db_path = tmp_path / "income.sqlite"
    conn = _db(db_path)

    apply_income_source(
        conn,
        {
            "id": "income_owner_intellibridge",
            "person": "Owner",
            "employer": "IntelliBridge",
            "display_name": "Owner pay transfer",
            "status": "active",
            "default_amount": 3781.15,
            "deposit_account_id": "ACT-0d080ab4-5c7c-48af-b20c-35b4a1bf3019",
            "working_account_id": "ACT-073d560e-d421-4718-a9c3-1c88a4b6618f",
            "source": "working_rule",
            "confidence": "medium",
            "active_from": "2026-06-20",
            "active_until": "2026-12-31",
            "review_by": "2026-09-01",
            "notes": "Semi-monthly 10th/25th, working-cash event is transfer into XXXX.",
            "schedule": {
                "id": "schedule_owner_intellibridge_v1",
                "schedule_type": "semi_monthly_days",
                "rule": {"days": [10, 25], "business_day_adjustment": "previous"},
                "valid_from": "2026-06-20",
                "valid_until": "2026-12-31",
                "confidence": "medium",
                "source": "working_rule",
                "status": "active",
                "review_by": "2026-09-01",
            },
        },
    )
    apply_income_source(
        conn,
        {
            "id": "income_partner_town_greenwich",
            "person": "Partner",
            "employer": "Town of Greenwich",
            "display_name": "Partner pay",
            "status": "active",
            "default_amount": 2011.67,
            "deposit_account_id": "ACT-073d560e-d421-4718-a9c3-1c88a4b6618f",
            "working_account_id": "ACT-073d560e-d421-4718-a9c3-1c88a4b6618f",
            "source": "payroll_pattern",
            "confidence": "high",
            "active_from": "2026-06-01",
            "active_until": "2026-12-31",
            "review_by": "2026-11-30",
            "notes": "Biweekly Friday payroll, direct deposit into XXXX.",
            "schedule": {
                "id": "schedule_partner_town_greenwich_v1",
                "schedule_type": "biweekly_weekday",
                "rule": {
                    "anchor_date": "2026-06-05",
                    "weekday": "friday",
                    "business_day_adjustment": "previous",
                },
                "valid_from": "2026-06-01",
                "valid_until": "2026-12-31",
                "confidence": "high",
                "source": "payroll_pattern",
                "status": "active",
                "review_by": "2026-11-30",
            },
        },
    )
    first_run = generate_income_instances(conn, start_date="2026-06-18", through_date="2026-07-25")
    second_run = generate_income_instances(conn, start_date="2026-06-18", through_date="2026-07-25")

    assert first_run["created"] == 6
    assert first_run["unchanged"] == 0
    assert second_run["created"] == 0
    assert second_run["unchanged"] == 6

    rows = conn.execute(
        """
        SELECT
            id, obligation_id, due_date, amount, direction, source,
            confidence, generated_from_income_source_id,
            generated_from_schedule_version_id
        FROM obligation_instances
        ORDER BY due_date, id
        """
    ).fetchall()

    assert [(row["obligation_id"], row["due_date"], row["amount"]) for row in rows] == [
        ("income_partner_town_greenwich", "2026-06-18", 2011.67),
        ("income_owner_intellibridge", "2026-06-25", 3781.15),
        ("income_partner_town_greenwich", "2026-07-02", 2011.67),
        ("income_owner_intellibridge", "2026-07-10", 3781.15),
        ("income_partner_town_greenwich", "2026-07-17", 2011.67),
        ("income_owner_intellibridge", "2026-07-24", 3781.15),
    ]
    assert all(row["direction"] == "inflow" for row in rows)
    assert all(row["source"].startswith("income_schedule:") for row in rows)
    assert rows[1]["generated_from_income_source_id"] == "income_owner_intellibridge"
    assert rows[1]["generated_from_schedule_version_id"] == "schedule_owner_intellibridge_v1"


def test_list_income_sources_reports_review_and_horizon(tmp_path):
    db_path = tmp_path / "income.sqlite"
    conn = _db(db_path)
    apply_income_source(
        conn,
        {
            "id": "income_owner_intellibridge",
            "person": "Owner",
            "employer": "IntelliBridge",
            "display_name": "Owner pay transfer",
            "status": "active",
            "default_amount": 3781.15,
            "deposit_account_id": "1793",
            "working_account_id": "XXXX",
            "source": "working_rule",
            "confidence": "medium",
            "active_from": "2026-06-20",
            "active_until": "2026-12-31",
            "review_by": "2026-09-01",
            "notes": None,
            "schedule": {
                "id": "schedule_owner_intellibridge_v1",
                "schedule_type": "semi_monthly_days",
                "rule": {"days": [10, 25], "business_day_adjustment": "previous"},
                "valid_from": "2026-06-20",
                "valid_until": "2026-12-31",
                "confidence": "medium",
                "source": "working_rule",
                "status": "active",
                "review_by": "2026-09-01",
            },
        },
    )
    generate_income_instances(conn, start_date="2026-06-20", through_date="2026-12-31")

    sources = list_income_sources(conn)

    assert sources == [
        {
            "id": "income_owner_intellibridge",
            "person": "Owner",
            "employer": "IntelliBridge",
            "display_name": "Owner pay transfer",
            "status": "active",
            "default_amount": 3781.15,
            "deposit_account_id": "1793",
            "working_account_id": "XXXX",
            "source": "working_rule",
            "confidence": "medium",
            "active_from": "2026-06-20",
            "active_until": "2026-12-31",
            "review_by": "2026-09-01",
            "generated_through": "2026-12-24",
            "schedule_versions": [
                {
                    "id": "schedule_owner_intellibridge_v1",
                    "schedule_type": "semi_monthly_days",
                    "rule": {"days": [10, 25], "business_day_adjustment": "previous"},
                    "valid_from": "2026-06-20",
                    "valid_until": "2026-12-31",
                    "confidence": "medium",
                    "source": "working_rule",
                    "status": "active",
                    "review_by": "2026-09-01",
                }
            ],
        }
    ]


def test_semi_monthly_previous_business_day_rolls_back_for_memorial_day_2026(tmp_path):
    due_dates = _single_semimonthly_due_dates(
        tmp_path,
        days=[25],
        start_date="2026-05-01",
        through_date="2026-05-31",
    )

    assert due_dates == ["2026-05-22"]


def test_semi_monthly_previous_business_day_rolls_back_for_thanksgiving_2026(tmp_path):
    due_dates = _single_semimonthly_due_dates(
        tmp_path,
        days=[26],
        start_date="2026-11-01",
        through_date="2026-11-30",
    )

    assert due_dates == ["2026-11-25"]


def test_semi_monthly_previous_business_day_uses_extra_closure_dates(tmp_path):
    due_dates = _single_semimonthly_due_dates(
        tmp_path,
        days=[10],
        start_date="2026-06-01",
        through_date="2026-06-30",
        extra_closure_dates=["2026-06-10"],
    )

    assert due_dates == ["2026-06-09"]


def test_generation_cancels_obsolete_expected_instances_after_calendar_change(tmp_path):
    db_path = tmp_path / "income.sqlite"
    conn = _db(db_path)
    source = {
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
    }

    apply_income_source(conn, source)
    first_run = generate_income_instances(
        conn,
        start_date="2026-06-01",
        through_date="2026-06-30",
    )
    source["schedule"]["rule"] = {
        "days": [10],
        "business_day_adjustment": "previous",
        "extra_closure_dates": ["2026-06-10"],
    }
    apply_income_source(conn, source)
    second_run = generate_income_instances(
        conn,
        start_date="2026-06-01",
        through_date="2026-06-30",
    )

    rows = conn.execute(
        """
        SELECT due_date, status
        FROM obligation_instances
        ORDER BY due_date
        """
    ).fetchall()

    assert first_run["canceled_obsolete"] == 0
    assert second_run["canceled_obsolete"] == 1
    assert [(row["due_date"], row["status"]) for row in rows] == [
        ("2026-06-09", "expected"),
        ("2026-06-10", "canceled"),
    ]
