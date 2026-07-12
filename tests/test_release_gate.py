"""Release gate coverage for supported database writers."""

import ast
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import financial_agent.adversarial as adversarial
import financial_agent.backfill as backfill
import financial_agent.migration as migration
import financial_agent.onboarding as onboarding
import financial_agent.scheduled as scheduled
from financial_agent import build_info, release_gate, server
from financial_agent.config import ensure_source_tables
from financial_agent.schema import LATEST_SCHEMA_VERSION, ensure_app_schema


APPLE_CSV = """Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)
07/01/2026,07/02/2026,APPLE.COM/BILL,Apple Services,Subscriptions,Purchase,9.99
"""

CHECKING_CSV = """Date,Description,Amount
07/01/2026,PAYROLL DEPOSIT,2500.00
07/02/2026,GROCERY STORE,-42.10
"""

LOCAL_WRITERS = [
    (
        "set_goal_override",
        "set_goal_override_for_db",
        {"goal_id": "goal-test", "override_amount": 1},
    ),
    (
        "set_debt_terms",
        "set_debt_terms_for_db",
        {"id": "debt-test", "name": "Test debt", "apr": 1},
    ),
    (
        "capture_followup",
        "capture_followup_for_db",
        {"text": "Test", "surface_when": "2026-07-12"},
    ),
    (
        "resolve_followup",
        "resolve_followup_for_db",
        {"followup_id": "followup-test"},
    ),
    (
        "update_followup",
        "update_followup_for_db",
        {"followup_id": "followup-test", "priority": "high"},
    ),
    (
        "apply_income_source",
        "apply_income_source_config",
        {"source": {}},
    ),
    (
        "import_calendar_facts",
        "import_calendar_facts_for_db",
        {"facts": []},
    ),
]

LOCAL_WRITER_MODULE_REPRESENTATIVES = [
    LOCAL_WRITERS[index] for index in (0, 1, 2, 5, 6)
]

OBLIGATION_WRITERS = [
    (
        "apply_obligation_instances",
        "apply_obligation_instances_for_db",
        {
            "obligation": {
                "id": "obligation-test",
                "name": "Test obligation",
                "kind": "bill",
                "source": "test",
                "autopay": False,
            },
            "instances": [],
        },
    ),
    (
        "delete_obligation_instance",
        "delete_obligation_instance_for_db",
        {"instance_id": "instance-test"},
    ),
    (
        "set_obligation_end",
        "set_obligation_end_for_db",
        {"obligation_id": "obligation-test", "active_until": "2026-12-31"},
    ),
    (
        "deactivate_obligation",
        "deactivate_obligation_for_db",
        {"obligation_id": "obligation-test"},
    ),
]

OBLIGATION_WRITER_SCHEMA_REPRESENTATIVES = [
    (*OBLIGATION_WRITERS[0], LATEST_SCHEMA_VERSION - 1),
    (*OBLIGATION_WRITERS[1], LATEST_SCHEMA_VERSION - 1),
    (*OBLIGATION_WRITERS[2], LATEST_SCHEMA_VERSION + 1),
    (*OBLIGATION_WRITERS[3], LATEST_SCHEMA_VERSION + 1),
]

ONBOARDING_STATEMENT_RECONCILIATION_WRITERS = [
    (
        "scan_charge_onboarding_candidates",
        "scan_charge_onboarding_candidates_for_db",
        {"options": {}},
    ),
    (
        "record_charge_onboarding_decision",
        "_record_onboarding_decision",
        {"candidate_id": "candidate-test", "decision": "defer"},
    ),
    (
        "record_charge_onboarding_decisions",
        "_record_onboarding_decision",
        {
            "decisions": [
                {"candidate_id": "candidate-test", "decision": "defer"}
            ]
        },
    ),
    (
        "apply_charge_onboarding_candidate",
        "apply_charge_onboarding_candidate_for_db",
        {"candidate_id": "candidate-test"},
    ),
    (
        "aggregate_statement_inputs",
        "aggregate_statement_inputs_for_db",
        {"target_obligation_id": "statement-test"},
    ),
    (
        "recompute_statement_estimates",
        "recompute_statement_estimates_for_db",
        {"target_obligation_id": "statement-test"},
    ),
    (
        "set_statement_actual",
        "set_statement_actual_for_db",
        {
            "obligation_id": "statement-test",
            "amount": 1,
            "due_date": "2026-07-12",
        },
    ),
    (
        "reconcile_obligation_instances",
        "reconcile_obligation_instances_for_db",
        {"as_of_date": "2026-07-11"},
    ),
]

ONBOARDING_STATEMENT_RECONCILIATION_SCHEMA_REPRESENTATIVES = [
    ONBOARDING_STATEMENT_RECONCILIATION_WRITERS[index] for index in (0, 4, 7)
]

RECONCILIATION_CONFIRMATION_WRITERS = [
    (
        "confirm_reconciliation_match",
        "confirm_reconciliation_match_for_db",
        {"instance_id": "instance-test", "transaction_id": "transaction-test"},
        ("instance-test", "transaction-test"),
    ),
    (
        "unconfirm_reconciliation_match",
        "unconfirm_reconciliation_match_for_db",
        {"instance_id": "instance-test"},
        ("instance-test",),
    ),
]

RECONCILIATION_CONFIRMATION_SCHEMA_REPRESENTATIVES = [
    (*RECONCILIATION_CONFIRMATION_WRITERS[0], LATEST_SCHEMA_VERSION - 1),
    (*RECONCILIATION_CONFIRMATION_WRITERS[1], LATEST_SCHEMA_VERSION + 1),
]

IMP1_WRITERS = [
    (
        "generate_income_instances",
        "generate_income_instances_for_db",
        {
            "start_date": "2026-07-01",
            "through_date": "2026-07-31",
            "extra_closure_dates": ["2026-07-03"],
        },
    ),
    (
        "acknowledge_verification_findings",
        "acknowledge_verification_findings_for_db",
        {"finding_ids": ["finding-test"], "check_id": "projection_identity"},
    ),
    (
        "write_finance_memory",
        "write_memory_for_db",
        {
            "text": "Test memory",
            "metadata": {"source": "release-gate"},
            "kind": "fact",
            "source": "test",
        },
    ),
    (
        "delete_finance_memory",
        "delete_memory_for_db",
        {"memory_id": "memory-test"},
    ),
    (
        "apply_guardrail_rules",
        "apply_guardrail_rules_for_db",
        {},
    ),
]

IMP1_SCHEMA_REPRESENTATIVES = [
    IMP1_WRITERS[index] for index in (0, 1, 2, 4)
]

RECURRING_WRITERS = [
    (
        "backfill_recurring_instances",
        "backfill_recurring_instances_for_db",
        {"as_of_date": "2026-07-11", "lookback_days": 45},
    ),
    (
        "auto_model_high_confidence_recurring",
        "auto_model_high_confidence_recurring_for_db",
        {"as_of_date": "2026-07-11"},
    ),
]

MIXED_WRITE_BRANCHES = [
    (
        "suppress_contradicted_estimates",
        "suppress_contradicted_estimates_for_db",
        {
            "as_of_date": "2026-07-11",
            "mode": "report",
            "options": {"modeled_floor": 100},
        },
        {
            "as_of_date": "2026-07-11",
            "options": {"modeled_floor": 100, "mode": "report"},
        },
    ),
    (
        "evaluate_guardrails",
        "evaluate_guardrails_for_db",
        {"as_of_date": "2026-07-11", "persist": False},
        {"as_of_date": "2026-07-11", "persist": False},
    ),
    (
        "get_statement_status",
        "get_statement_status_for_db",
        {"obligation_id": "statement-test", "as_of_date": "2026-07-11"},
        {"obligation_id": "statement-test", "as_of_date": "2026-07-11"},
    ),
    (
        "detect_drift",
        "detect_drift_for_db",
        {
            "as_of_date": "2026-07-11",
            "options": {"grace_days": 3},
            "persist": True,
        },
        {
            "as_of_date": "2026-07-11",
            "options": {"grace_days": 3},
            "persist": True,
        },
    ),
    (
        "run_verification",
        "run_verification_for_db",
        {"as_of_date": "2026-07-11", "persist": True},
        {"as_of_date": "2026-07-11", "persist": True},
    ),
    (
        "import_checking_activity",
        "import_checking_activity_for_db",
        {
            "text": CHECKING_CSV,
            "account_query": "Apple Card",
            "as_of_date": "2026-07-11",
            "balance": 2500,
            "dry_run": False,
            "confirmed_source_hash": None,
        },
        {
            "text": CHECKING_CSV,
            "account_query": "Apple Card",
            "as_of_date": "2026-07-11",
            "balance": 2500,
            "dry_run": False,
            "confirmed_source_hash": None,
        },
    ),
]

MIXED_SAFE_READ_BRANCHES = [
    (
        "detect_drift",
        "detect_drift_for_db",
        {
            "as_of_date": "2026-07-11",
            "options": {"grace_days": 3},
            "persist": False,
        },
        {
            "as_of_date": "2026-07-11",
            "options": {"grace_days": 3},
            "persist": False,
        },
    ),
    (
        "run_verification",
        "run_verification_for_db",
        {"as_of_date": "2026-07-11", "persist": False},
        {"as_of_date": "2026-07-11", "persist": False},
    ),
    (
        "import_checking_activity",
        "import_checking_activity_for_db",
        {
            "text": CHECKING_CSV,
            "account_query": "Apple Card",
            "as_of_date": "2026-07-11",
            "balance": 2500,
            "dry_run": True,
        },
        {
            "text": CHECKING_CSV,
            "account_query": "Apple Card",
            "as_of_date": "2026-07-11",
            "balance": 2500,
            "dry_run": True,
        },
    ),
]

REMAINING_DB_MUTATION_WRITERS = [
    (
        "execute_action_outbox",
        "execute_action_outbox_for_db",
        {"options": {"write_enabled": False}},
        (),
        {"options": {"write_enabled": False}},
    ),
    (
        "surface_due_items_to_todoist",
        "surface_to_todoist_for_db",
        {"as_of_date": "2026-07-11", "sync_failed": False},
        ([{"surface_key": "test-item"}], "2026-07-11"),
        {"retire_keys": ["retire-test"]},
    ),
    (
        "reconcile_todoist_emission",
        "reconcile_emission_for_db",
        {
            "surface_key": "test-key",
            "todoist_task_id": "task-test",
            "content_hash": "hash-test",
        },
        ("test-key", "task-test", "hash-test"),
        {},
    ),
    (
        "reconcile_todoist_completions",
        "reconcile_todoist_completions_for_db",
        {"as_of_date": "2026-07-11"},
        (),
        {"as_of_date": "2026-07-11"},
    ),
    (
        "reconcile_todoist_project",
        "reconcile_todoist_project_for_db",
        {"as_of_date": "2026-07-11", "apply": True},
        (),
        {"as_of_date": "2026-07-11", "apply": True},
    ),
    (
        "run_background_sync",
        "run_background_sync_for_db",
        {
            "as_of_date": "2026-07-11",
            "options": {"surface": {"write_enabled": False}},
            "run_type": "acceptance",
            "trigger_type": "manual",
        },
        (),
        {
            "as_of_date": "2026-07-11",
            "options": {"surface": {"write_enabled": False}},
            "run_type": "acceptance",
            "trigger_type": "manual",
        },
    ),
    (
        "run_adversarial_review",
        "run_adversarial_review_for_db",
        {"as_of_date": "2026-07-11", "persist": True, "model": "test-model"},
        (),
        {"as_of_date": "2026-07-11", "persist": True, "model": "test-model"},
    ),
    (
        "sync_simplefin",
        "sync_simplefin_for_db",
        {
            "start_date": "2026-07-01",
            "end_date": "2026-07-11",
            "lookback_days": 10,
            "incremental": True,
        },
        (),
        {
            "start_date": "2026-07-01",
            "end_date": "2026-07-11",
            "lookback_days": 10,
            "incremental": True,
        },
    ),
    (
        "reject_check_suggestion",
        "reject_check_suggestion_for_db",
        {"suggestion_id": "suggestion-test"},
        ("suggestion-test",),
        {},
    ),
    (
        "confirm_check_suggestion",
        "confirm_check_suggestion_for_db",
        {
            "suggestion_id": "suggestion-test",
            "as_of_date": "2026-07-11",
        },
        ("suggestion-test",),
        {"as_of_date": date(2026, 7, 11), "accounts": []},
    ),
]

REMAINING_DB_MUTATION_SCHEMA_REPRESENTATIVES = [
    (*REMAINING_DB_MUTATION_WRITERS[0], LATEST_SCHEMA_VERSION - 1),
    (*REMAINING_DB_MUTATION_WRITERS[-1], LATEST_SCHEMA_VERSION + 1),
]

REMOTE_TODOIST_MUTATION_WRITERS = [
    (
        "create_todoist_task",
        "create_todoist_task_impl",
        {
            "content": "Test task",
            "due_string": "today",
            "due_date": "2026-07-11",
            "description": "Test description",
            "priority": 4,
            "project_id": "project-test",
        },
        ("Test task",),
        {
            "due_string": "today",
            "due_date": "2026-07-11",
            "description": "Test description",
            "priority": 4,
            "project_id": "project-test",
        },
    ),
    (
        "update_todoist_task",
        "update_todoist_task_impl",
        {
            "task_id": "task-test",
            "content": "Updated task",
            "due_string": "tomorrow",
            "due_date": "2026-07-12",
            "description": "Updated description",
            "priority": 3,
            "project_id": "project-test",
        },
        ("task-test",),
        {
            "content": "Updated task",
            "due_string": "tomorrow",
            "due_date": "2026-07-12",
            "description": "Updated description",
            "priority": 3,
            "project_id": "project-test",
        },
    ),
    (
        "complete_todoist_task",
        "complete_todoist_task_impl",
        {"task_id": "task-test"},
        ("task-test",),
        {},
    ),
    (
        "reopen_todoist_task",
        "reopen_todoist_task_impl",
        {"task_id": "task-test"},
        ("task-test",),
        {},
    ),
    (
        "delete_todoist_task",
        "delete_todoist_task_impl",
        {"task_id": "task-test"},
        ("task-test",),
        {},
    ),
]


def _prepare_database(db, release_state):
    conn = sqlite3.connect(db)
    ensure_app_schema(conn)
    if release_state != "missing_table":
        conn.execute(
            """
            CREATE TABLE finance_release (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version TEXT NOT NULL
            )
            """
        )
    if release_state == "mismatched":
        conn.execute(
            "INSERT INTO finance_release (id, version) VALUES (1, ?)",
            (f"not-{build_info.VERSION}",),
        )
    conn.commit()
    conn.close()
    if release_state == "current":
        release_gate.promote_release(str(db))


def _release_version(db):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT version FROM finance_release WHERE id = 1"
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _database_snapshot(db):
    conn = sqlite3.connect(db)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    snapshot = {
        "sqlite_master": conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall(),
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
    }
    for table in ("finance_release", "goals", "background_runs"):
        snapshot[table] = conn.execute(f"SELECT * FROM {table}").fetchall() if table in tables else None
    conn.close()
    return snapshot


def _full_database_snapshot(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0], tuple(conn.iterdump())
    finally:
        conn.close()


def _migration_yaml(path, items):
    path.write_text(json.dumps({"working_account_id": "4321", "items": items}))
    return str(path)


def _prepare_writer_database(db):
    conn = sqlite3.connect(db)
    ensure_source_tables(conn)
    conn.execute(
        "INSERT INTO accounts "
        "(id, name, org, kind, currency, first_seen_at, last_seen_at) "
        "VALUES ('apple-1', 'Apple Card', 'Goldman Sachs', 'credit_card', "
        "'USD', '2026-07-01', '2026-07-01')"
    )
    conn.commit()
    conn.close()
    _prepare_database(db, "current")


def test_promotion_refuses_missing_database_without_creating_file(tmp_path):
    db = tmp_path / "missing.sqlite"

    with pytest.raises(release_gate.StaleReleaseError, match="[Dd]atabase"):
        release_gate.promote_release(str(db))

    assert not db.exists()


def test_promotion_migrates_existing_empty_database_before_recording_release(tmp_path):
    db = tmp_path / "finance.sqlite"
    sqlite3.connect(db).close()

    release_gate.promote_release(str(db))

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'obligations'"
        ).fetchone() == (1,)
    finally:
        conn.close()
    assert _release_version(db) == build_info.VERSION


def test_promotion_refuses_newer_schema_before_migration(tmp_path, monkeypatch):
    db = tmp_path / "finance.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    before = _database_snapshot(db)
    migration_calls = []

    def migration_spy(conn):
        migration_calls.append(conn.in_transaction)

    monkeypatch.setattr(release_gate, "ensure_app_schema", migration_spy)

    with pytest.raises(release_gate.IncompatibleSchemaError):
        release_gate.promote_release(str(db))

    assert (migration_calls, _database_snapshot(db)) == ([], before)


def test_promotion_runs_migrations_inside_explicit_transaction(tmp_path, monkeypatch):
    db = tmp_path / "finance.sqlite"
    sqlite3.connect(db).close()
    real_ensure_app_schema = release_gate.ensure_app_schema

    def transaction_spy(conn):
        assert conn.in_transaction is True
        real_ensure_app_schema(conn)

    monkeypatch.setattr(release_gate, "ensure_app_schema", transaction_spy)

    release_gate.promote_release(str(db))

    assert _release_version(db) == build_info.VERSION


@pytest.mark.parametrize("path_character", ["?", "#", "%"])
def test_release_gate_supports_sqlite_paths_with_uri_characters(
    tmp_path, path_character
):
    db = tmp_path / f"finance{path_character}release.sqlite"
    sqlite3.connect(db).close()

    release_gate.promote_release(str(db))
    with release_gate.guarded_write(str(db)) as conn:
        conn.execute("CREATE TABLE path_probe (value TEXT)")
        conn.execute("INSERT INTO path_probe VALUES ('committed')")

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT value FROM path_probe").fetchone() == (
            "committed",
        )
    finally:
        conn.close()
    assert _release_version(db) == build_info.VERSION


def test_promotion_rolls_back_schema_and_release_when_migration_fails(
    tmp_path, monkeypatch
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "missing_row")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO finance_release (id, version) VALUES (1, '0.0.0')")
    conn.commit()
    conn.close()
    before = _database_snapshot(db)

    def failing_migration(conn):
        conn.execute("CREATE TABLE incomplete_migration (id INTEGER)")
        conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION - 1}")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(
        release_gate, "ensure_app_schema", failing_migration, raising=False
    )

    with pytest.raises(RuntimeError, match="migration failed"):
        release_gate.promote_release(str(db))

    assert _database_snapshot(db) == before
    assert _release_version(db) == "0.0.0"


def test_promotion_refuses_incomplete_schema_migration(tmp_path, monkeypatch):
    db = tmp_path / "finance.sqlite"
    sqlite3.connect(db).close()

    def incomplete_migration(conn):
        conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION - 1}")

    monkeypatch.setattr(
        release_gate, "ensure_app_schema", incomplete_migration, raising=False
    )

    with pytest.raises(release_gate.IncompatibleSchemaError):
        release_gate.promote_release(str(db))

    assert _database_snapshot(db)["finance_release"] is None


@pytest.mark.parametrize("release_state", ["missing_row", "mismatched"])
def test_guarded_write_rejects_release_before_mutation(tmp_path, release_state):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, release_state)
    before = _database_snapshot(db)
    body_entered = False

    with pytest.raises(release_gate.StaleReleaseError):
        with release_gate.guarded_write(str(db)) as conn:
            body_entered = True
            conn.execute("CREATE TABLE forbidden_mutation (id INTEGER)")

    assert not body_entered
    assert _database_snapshot(db) == before


@pytest.mark.parametrize("schema_version", [LATEST_SCHEMA_VERSION - 1, LATEST_SCHEMA_VERSION + 1])
def test_guarded_write_rejects_incompatible_schema(tmp_path, schema_version):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()

    with pytest.raises(release_gate.IncompatibleSchemaError):
        with release_gate.guarded_write(str(db)) as guarded:
            guarded.execute("CREATE TABLE forbidden_mutation (id INTEGER)")

    assert _release_version(db) == build_info.VERSION


def test_guarded_write_commits_with_row_factory_and_preserves_release(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")

    with release_gate.guarded_write(str(db)) as conn:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute(
            "SELECT version FROM finance_release WHERE id = 1"
        ).fetchone()["version"] == build_info.VERSION
        conn.execute("CREATE TABLE guarded_probe (value TEXT)")
        conn.execute("INSERT INTO guarded_probe (value) VALUES ('committed')")

    assert sqlite3.connect(db).execute(
        "SELECT value FROM guarded_probe"
    ).fetchone() == ("committed",)
    assert _release_version(db) == build_info.VERSION


def test_guarded_write_rolls_back_exception(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")

    with pytest.raises(RuntimeError, match="stop write"):
        with release_gate.guarded_write(str(db)) as conn:
            conn.execute("CREATE TABLE rolled_back_probe (value TEXT)")
            conn.execute("INSERT INTO rolled_back_probe VALUES ('no commit')")
            raise RuntimeError("stop write")

    assert "rolled_back_probe" not in {
        row[0]
        for row in sqlite3.connect(db).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert _release_version(db) == build_info.VERSION


def test_guarded_write_blocks_concurrent_release_advance_until_commit(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    contender = sqlite3.connect(db, timeout=0)

    with release_gate.guarded_write(str(db)) as conn:
        assert conn.execute(
            "SELECT version FROM finance_release WHERE id = 1"
        ).fetchone()["version"] == build_info.VERSION
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            contender.execute(
                "UPDATE finance_release SET version = '9999.0.0' WHERE id = 1"
            )
        contender.rollback()
        assert _release_version(db) == build_info.VERSION

    contender.execute(
        "UPDATE finance_release SET version = '9999.0.0' WHERE id = 1"
    )
    contender.commit()
    contender.close()
    assert _release_version(db) == "9999.0.0"


@pytest.mark.parametrize("release_state", ["missing_table", "missing_row"])
def test_promotion_bootstraps_release_record_at_current_version(tmp_path, release_state):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, release_state)

    release_gate.promote_release(str(db))

    conn = sqlite3.connect(db)
    try:
        columns = conn.execute("PRAGMA table_info(finance_release)").fetchall()
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'finance_release'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert [(column[1], column[2], column[5]) for column in columns] == [
        ("id", "INTEGER", 1),
        ("version", "TEXT", 0),
    ]
    assert "CHECK (id = 1)" in table_sql
    assert _release_version(db) == build_info.VERSION


def test_promotion_advances_older_semantic_version(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "missing_row")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO finance_release (id, version) VALUES (1, '0.0.0')")
    conn.commit()
    conn.close()

    release_gate.promote_release(str(db))

    assert _release_version(db) == build_info.VERSION


def test_promotion_refuses_rollback_and_preserves_newer_version(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "missing_row")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO finance_release (id, version) VALUES (1, '9999.0.0')")
    conn.commit()
    conn.close()

    with pytest.raises(release_gate.StaleReleaseError, match="[Rr]elease"):
        release_gate.promote_release(str(db))

    assert _release_version(db) == "9999.0.0"


def test_promotion_refuses_newer_release_before_migration(tmp_path, monkeypatch):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "missing_row")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO finance_release (id, version) VALUES (1, '9999.0.0')")
    conn.commit()
    conn.close()
    before = _database_snapshot(db)
    migration_calls = []

    def migration_spy(conn):
        migration_calls.append(conn.in_transaction)

    monkeypatch.setattr(release_gate, "ensure_app_schema", migration_spy)

    with pytest.raises(release_gate.StaleReleaseError, match="[Rr]elease"):
        release_gate.promote_release(str(db))

    assert (migration_calls, _database_snapshot(db)) == ([], before)


def test_promotion_at_current_version_does_not_update_row(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "missing_table")
    release_gate.promote_release(str(db))
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TRIGGER reject_release_update
        BEFORE UPDATE ON finance_release
        BEGIN
            SELECT RAISE(FAIL, 'current release row was updated');
        END
        """
    )
    conn.commit()
    conn.close()

    release_gate.promote_release(str(db))

    assert _release_version(db) == build_info.VERSION


@pytest.mark.parametrize("release_state", ["missing_table", "missing_row", "mismatched"])
def test_mcp_database_mutation_fails_closed_when_release_record_is_not_current(
    tmp_path, release_state
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, release_state)
    before = _database_snapshot(db)

    error = None
    try:
        server.set_goal("Emergency fund", 10_000, db_path=str(db))
    except release_gate.StaleReleaseError as exc:
        error = exc

    after = _database_snapshot(db)
    assert (error is not None, after) == (True, before)
    assert "release record" in str(error).lower()


def test_mcp_database_mutation_allows_current_release_record(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    release_before = _release_version(db)

    result = server.set_goal("Emergency fund", 10_000, db_path=str(db))

    assert result["name"] == "Emergency fund"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 1
    assert _release_version(db) == release_before


def test_mcp_database_mutation_rechecks_release_inside_write_window(tmp_path, monkeypatch):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "missing_row")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO finance_release (id, version) VALUES (1, ?)",
        (build_info.VERSION,),
    )
    conn.commit()
    conn.close()
    goals_before = _database_snapshot(db)["goals"]
    early_precheck = server.require_current_release

    def advance_release_after_precheck(db_path):
        early_precheck(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE finance_release SET version = '9999.0.0' WHERE id = 1"
        )
        conn.commit()
        conn.close()

    monkeypatch.setattr(server, "require_current_release", advance_release_after_precheck)

    with pytest.raises(release_gate.StaleReleaseError, match="[Rr]elease"):
        server.set_goal("Emergency fund", 10_000, db_path=str(db))

    assert _release_version(db) == "9999.0.0"
    assert _database_snapshot(db)["goals"] == goals_before


def test_manual_balance_rejects_stale_release_before_helper_mutation(
    tmp_path, monkeypatch
):
    db = tmp_path / "finance.sqlite"
    _prepare_writer_database(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = 0

    def staged_helper(conn, **kwargs):
        nonlocal helper_calls
        helper_calls += 1
        conn.execute(
            "INSERT INTO balance_snapshots "
            "(account_id, balance, available, recorded_at, source) "
            "VALUES ('apple-1', -99, -99, '2026-07-11', 'test')"
        )
        return {"status": "ok"}

    monkeypatch.setattr(server, "set_manual_balance_for_db", staged_helper)

    with pytest.raises(release_gate.StaleReleaseError):
        server.set_manual_balance("Apple Card", -99, db_path=str(db))

    assert helper_calls == 0
    assert _full_database_snapshot(db) == before


def test_manual_balance_current_release_commits_successful_helper_result(
    tmp_path, monkeypatch
):
    db = tmp_path / "finance.sqlite"
    _prepare_writer_database(db)
    expected = {"status": "ok", "account_id": "apple-1"}

    def staged_helper(conn, **kwargs):
        conn.execute(
            "INSERT INTO balance_snapshots "
            "(account_id, balance, available, recorded_at, source) "
            "VALUES ('apple-1', -99, -99, '2026-07-11', 'test')"
        )
        return expected

    monkeypatch.setattr(server, "set_manual_balance_for_db", staged_helper)

    result = server.set_manual_balance("Apple Card", -99, db_path=str(db))

    assert result is expected
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM balance_snapshots WHERE source = 'test'"
    ).fetchone()[0] == 1


def test_manual_balance_non_ok_result_is_unchanged_and_staged_write_is_discarded(
    tmp_path, monkeypatch
):
    db = tmp_path / "finance.sqlite"
    _prepare_writer_database(db)
    expected = {"status": "ambiguous", "candidates": ["Apple Card"]}

    def staged_helper(conn, **kwargs):
        conn.execute(
            "INSERT INTO balance_snapshots "
            "(account_id, balance, available, recorded_at, source) "
            "VALUES ('apple-1', -99, -99, '2026-07-11', 'test')"
        )
        return expected

    monkeypatch.setattr(server, "set_manual_balance_for_db", staged_helper)

    result = server.set_manual_balance("Apple Card", -99, db_path=str(db))

    assert result is expected
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM balance_snapshots WHERE source = 'test'"
    ).fetchone()[0] == 0


def test_card_import_dry_run_allows_stale_release_warning_without_database_changes(
    tmp_path,
):
    db = tmp_path / "finance.sqlite"
    _prepare_writer_database(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    with release_gate.guarded_read(str(db)) as (_, status):
        expected_warning = status.warning
    before = _full_database_snapshot(db)

    result = server.import_card_statement(
        APPLE_CSV,
        as_of_date="2026-07-11",
        dry_run=True,
        db_path=str(db),
    )

    assert result["status"] == "preview"
    assert result.get("release_warning") == expected_warning
    assert _full_database_snapshot(db) == before


@pytest.mark.parametrize(
    "schema_version", [LATEST_SCHEMA_VERSION - 1, LATEST_SCHEMA_VERSION + 1]
)
def test_card_import_dry_run_rejects_incompatible_schema_without_migration(
    tmp_path, schema_version
):
    db = tmp_path / "finance.sqlite"
    _prepare_writer_database(db)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)

    with pytest.raises(release_gate.IncompatibleSchemaError):
        server.import_card_statement(
            APPLE_CSV,
            as_of_date="2026-07-11",
            dry_run=True,
            db_path=str(db),
        )

    assert _full_database_snapshot(db) == before


def test_card_import_non_ok_result_is_unchanged_and_staged_write_is_discarded(
    tmp_path, monkeypatch
):
    db = tmp_path / "finance.sqlite"
    _prepare_writer_database(db)
    expected = {"status": "ambiguous", "candidates": ["Apple Card"]}

    def staged_helper(conn, **kwargs):
        conn.execute(
            "INSERT INTO card_import_runs "
            "(id, account_id, imported_at, txn_count, total_spend) "
            "VALUES ('test-run', 'apple-1', '2026-07-11', 1, -9.99)"
        )
        return expected

    monkeypatch.setattr(server, "import_card_statement_for_db", staged_helper)

    result = server.import_card_statement(
        APPLE_CSV,
        as_of_date="2026-07-11",
        dry_run=False,
        db_path=str(db),
    )

    assert result is expected
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM card_import_runs WHERE id = 'test-run'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    LOCAL_WRITERS,
    ids=[writer[0] for writer in LOCAL_WRITERS],
)
def test_local_writer_rejects_stale_release_before_helper_invocation(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(*args, **kwargs):
        helper_calls.append((args, kwargs))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)
    error = None
    try:
        getattr(server, entrypoint)(**arguments, db_path=str(db))
    except release_gate.StaleReleaseError as exc:
        error = exc

    assert (type(error), helper_calls, _full_database_snapshot(db)) == (
        release_gate.StaleReleaseError,
        [],
        before,
    )


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    LOCAL_WRITERS,
    ids=[writer[0] for writer in LOCAL_WRITERS],
)
def test_local_writer_commits_with_row_factory_and_preserves_release(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE writer_probe (entrypoint TEXT)")
    conn.commit()
    conn.close()
    expected = {"entrypoint": entrypoint}

    def helper_spy(conn, *args, **kwargs):
        assert conn.row_factory is sqlite3.Row
        conn.execute("INSERT INTO writer_probe VALUES (?)", (entrypoint,))
        return expected

    monkeypatch.setattr(server, helper_name, helper_spy)

    assert getattr(server, entrypoint)(**arguments, db_path=str(db)) == expected

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT entrypoint FROM writer_probe").fetchall() == [
            (entrypoint,)
        ]
        assert conn.execute(
            "SELECT version FROM finance_release WHERE id = 1"
        ).fetchone() == (build_info.VERSION,)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    LOCAL_WRITER_MODULE_REPRESENTATIVES,
    ids=[writer[0] for writer in LOCAL_WRITER_MODULE_REPRESENTATIVES],
)
@pytest.mark.parametrize(
    "schema_version", [LATEST_SCHEMA_VERSION - 1, LATEST_SCHEMA_VERSION + 1]
)
def test_local_writer_rejects_incompatible_schema_before_helper_invocation(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    schema_version,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(*args, **kwargs):
        helper_calls.append((args, kwargs))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)
    error = None
    try:
        getattr(server, entrypoint)(**arguments, db_path=str(db))
    except release_gate.IncompatibleSchemaError as exc:
        error = exc

    assert (type(error), helper_calls, _full_database_snapshot(db)) == (
        release_gate.IncompatibleSchemaError,
        [],
        before,
    )


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    OBLIGATION_WRITERS,
    ids=[writer[0] for writer in OBLIGATION_WRITERS],
)
def test_obligation_writer_rejects_stale_release_before_helper_invocation(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(*args, **kwargs):
        helper_calls.append((args, kwargs))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)

    with pytest.raises(release_gate.StaleReleaseError):
        getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert helper_calls == []
    assert _full_database_snapshot(db) == before


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments", "schema_version"),
    OBLIGATION_WRITER_SCHEMA_REPRESENTATIVES,
    ids=[writer[0] for writer in OBLIGATION_WRITER_SCHEMA_REPRESENTATIVES],
)
def test_obligation_writer_rejects_incompatible_schema_before_helper_invocation(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments, schema_version
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(*args, **kwargs):
        helper_calls.append((args, kwargs))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)

    with pytest.raises(release_gate.IncompatibleSchemaError):
        getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert helper_calls == []
    assert _full_database_snapshot(db) == before


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    OBLIGATION_WRITERS,
    ids=[writer[0] for writer in OBLIGATION_WRITERS],
)
def test_obligation_writer_commits_helper_result_with_row_factory(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE obligation_writer_probe (entrypoint TEXT)")
    conn.commit()
    conn.close()
    expected = {"entrypoint": entrypoint}

    def helper_spy(conn, *args, **kwargs):
        assert conn.row_factory is sqlite3.Row
        conn.execute("INSERT INTO obligation_writer_probe VALUES (?)", (entrypoint,))
        return expected

    monkeypatch.setattr(server, helper_name, helper_spy)

    result = getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert result is expected
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT entrypoint FROM obligation_writer_probe"
        ).fetchall() == [(entrypoint,)]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    ONBOARDING_STATEMENT_RECONCILIATION_WRITERS,
    ids=[writer[0] for writer in ONBOARDING_STATEMENT_RECONCILIATION_WRITERS],
)
def test_onboarding_statement_reconciliation_writer_rejects_stale_release_before_helper(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(*args, **kwargs):
        helper_calls.append((args, kwargs))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)
    error = None
    try:
        getattr(server, entrypoint)(**arguments, db_path=str(db))
    except release_gate.StaleReleaseError as exc:
        error = exc

    assert (type(error), helper_calls, _full_database_snapshot(db)) == (
        release_gate.StaleReleaseError,
        [],
        before,
    )


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    ONBOARDING_STATEMENT_RECONCILIATION_SCHEMA_REPRESENTATIVES,
    ids=[
        writer[0]
        for writer in ONBOARDING_STATEMENT_RECONCILIATION_SCHEMA_REPRESENTATIVES
    ],
)
@pytest.mark.parametrize(
    "schema_version", [LATEST_SCHEMA_VERSION - 1, LATEST_SCHEMA_VERSION + 1]
)
def test_onboarding_statement_reconciliation_writer_rejects_incompatible_schema_before_helper(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    schema_version,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(*args, **kwargs):
        helper_calls.append((args, kwargs))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)
    error = None
    try:
        getattr(server, entrypoint)(**arguments, db_path=str(db))
    except release_gate.IncompatibleSchemaError as exc:
        error = exc

    assert (type(error), helper_calls, _full_database_snapshot(db)) == (
        release_gate.IncompatibleSchemaError,
        [],
        before,
    )


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    ONBOARDING_STATEMENT_RECONCILIATION_WRITERS,
    ids=[writer[0] for writer in ONBOARDING_STATEMENT_RECONCILIATION_WRITERS],
)
def test_onboarding_statement_reconciliation_writer_commits_exact_helper_result_with_row_factory(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE finance_writer_probe (entrypoint TEXT)")
    conn.commit()
    conn.close()
    expected = {"entrypoint": entrypoint}
    if entrypoint == "record_charge_onboarding_decisions":
        expected["status"] = expected

    def helper_spy(conn, *args, **kwargs):
        assert conn.row_factory is sqlite3.Row
        conn.execute("INSERT INTO finance_writer_probe VALUES (?)", (entrypoint,))
        return expected

    monkeypatch.setattr(server, helper_name, helper_spy)

    result = getattr(server, entrypoint)(**arguments, db_path=str(db))
    helper_result = (
        result["results"][0]["status"]
        if entrypoint == "record_charge_onboarding_decisions"
        else result
    )

    assert helper_result is expected
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT entrypoint FROM finance_writer_probe"
        ).fetchall() == [(entrypoint,)]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments", "expected_helper_arguments"),
    RECONCILIATION_CONFIRMATION_WRITERS,
    ids=[writer[0] for writer in RECONCILIATION_CONFIRMATION_WRITERS],
)
def test_reconciliation_confirmation_writer_rejects_stale_release_before_helper(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_helper_arguments,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reconciliation_confirmation_probe (entrypoint TEXT)")
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(conn, *args):
        helper_calls.append(args)
        conn.execute(
            "INSERT INTO reconciliation_confirmation_probe VALUES (?)",
            (entrypoint,),
        )
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)
    error = None
    try:
        getattr(server, entrypoint)(**arguments, db_path=str(db))
    except release_gate.StaleReleaseError as exc:
        error = exc

    assert (type(error), helper_calls, _full_database_snapshot(db)) == (
        release_gate.StaleReleaseError,
        [],
        before,
    )


@pytest.mark.parametrize(
    (
        "entrypoint",
        "helper_name",
        "arguments",
        "expected_helper_arguments",
        "schema_version",
    ),
    RECONCILIATION_CONFIRMATION_SCHEMA_REPRESENTATIVES,
    ids=[
        writer[0]
        for writer in RECONCILIATION_CONFIRMATION_SCHEMA_REPRESENTATIVES
    ],
)
def test_reconciliation_confirmation_writer_rejects_incompatible_schema_before_helper(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_helper_arguments,
    schema_version,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reconciliation_confirmation_probe (entrypoint TEXT)")
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(conn, *args):
        helper_calls.append(args)
        conn.execute(
            "INSERT INTO reconciliation_confirmation_probe VALUES (?)",
            (entrypoint,),
        )
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)
    error = None
    try:
        getattr(server, entrypoint)(**arguments, db_path=str(db))
    except release_gate.IncompatibleSchemaError as exc:
        error = exc

    assert (type(error), helper_calls, _full_database_snapshot(db)) == (
        release_gate.IncompatibleSchemaError,
        [],
        before,
    )


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments", "expected_helper_arguments"),
    RECONCILIATION_CONFIRMATION_WRITERS,
    ids=[writer[0] for writer in RECONCILIATION_CONFIRMATION_WRITERS],
)
def test_reconciliation_confirmation_writer_commits_exact_helper_result_with_row_factory(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_helper_arguments,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reconciliation_confirmation_probe (entrypoint TEXT)")
    conn.commit()
    conn.close()
    expected = {"entrypoint": entrypoint}
    helper_calls = []

    def helper_spy(conn, *args):
        assert conn.row_factory is sqlite3.Row
        helper_calls.append(args)
        conn.execute(
            "INSERT INTO reconciliation_confirmation_probe VALUES (?)",
            (entrypoint,),
        )
        return expected

    monkeypatch.setattr(server, helper_name, helper_spy)

    result = getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert result is expected
    assert helper_calls == [expected_helper_arguments]
    assert sqlite3.connect(db).execute(
        "SELECT entrypoint FROM reconciliation_confirmation_probe"
    ).fetchall() == [(entrypoint,)]


def test_calendar_fact_batch_rolls_back_when_later_fact_is_malformed(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")

    with pytest.raises(ValueError, match="requires date"):
        server.import_calendar_facts(
            [
                {
                    "fact_type": "business_closure",
                    "date": "2026-07-12",
                    "source": "test",
                },
                {"fact_type": "business_closure"},
            ],
            db_path=str(db),
        )

    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM calendar_facts"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    IMP1_WRITERS,
    ids=[writer[0] for writer in IMP1_WRITERS],
)
def test_imp1_writer_rejects_stale_release_before_helper_and_preserves_database(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE imp1_writer_probe (entrypoint TEXT)")
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(conn, **kwargs):
        helper_calls.append(kwargs)
        conn.execute("INSERT INTO imp1_writer_probe VALUES (?)", (entrypoint,))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)
    error = None
    try:
        getattr(server, entrypoint)(**arguments, db_path=str(db))
    except release_gate.StaleReleaseError as exc:
        error = exc

    assert (type(error), helper_calls, _full_database_snapshot(db)) == (
        release_gate.StaleReleaseError,
        [],
        before,
    )


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    IMP1_SCHEMA_REPRESENTATIVES,
    ids=[writer[0] for writer in IMP1_SCHEMA_REPRESENTATIVES],
)
@pytest.mark.parametrize(
    "schema_version", [LATEST_SCHEMA_VERSION - 1, LATEST_SCHEMA_VERSION + 1]
)
def test_imp1_writer_rejects_incompatible_schema_before_helper(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    schema_version,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(conn, **kwargs):
        helper_calls.append(kwargs)
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)
    error = None
    try:
        getattr(server, entrypoint)(**arguments, db_path=str(db))
    except release_gate.IncompatibleSchemaError as exc:
        error = exc

    assert (type(error), helper_calls, _full_database_snapshot(db)) == (
        release_gate.IncompatibleSchemaError,
        [],
        before,
    )


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    IMP1_WRITERS,
    ids=[writer[0] for writer in IMP1_WRITERS],
)
def test_imp1_writer_forwards_arguments_and_commits_exact_helper_result(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE imp1_writer_probe (entrypoint TEXT)")
    conn.commit()
    conn.close()
    expected = {"entrypoint": entrypoint}
    helper_calls = []

    def helper_spy(conn, **kwargs):
        assert conn.row_factory is sqlite3.Row
        helper_calls.append(kwargs)
        conn.execute("INSERT INTO imp1_writer_probe VALUES (?)", (entrypoint,))
        return expected

    monkeypatch.setattr(server, helper_name, helper_spy)

    result = getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert result is expected
    assert helper_calls == [arguments]
    assert sqlite3.connect(db).execute(
        "SELECT entrypoint FROM imp1_writer_probe"
    ).fetchall() == [(entrypoint,)]


def test_imp1_write_finance_memory_validates_before_release_gate(tmp_path, monkeypatch):
    db = tmp_path / "missing.sqlite"
    gate_calls = []

    def stale_gate(*args, **kwargs):
        gate_calls.append((args, kwargs))
        raise release_gate.StaleReleaseError("release gate consulted")

    monkeypatch.setattr(server, "require_current_release", stale_gate)
    monkeypatch.setattr(server, "guarded_write", stale_gate)

    with pytest.raises(ValueError, match="requires 'text'"):
        server.write_finance_memory(db_path=str(db))

    assert gate_calls == []
    assert not db.exists()


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    RECURRING_WRITERS,
    ids=[writer[0] for writer in RECURRING_WRITERS],
)
def test_recurring_writer_rejects_stale_release_before_helper_and_preserves_database(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE recurring_writer_probe (entrypoint TEXT)")
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(conn, **kwargs):
        helper_calls.append(kwargs)
        conn.execute("INSERT INTO recurring_writer_probe VALUES (?)", (entrypoint,))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)

    with pytest.raises(release_gate.StaleReleaseError):
        getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert helper_calls == []
    assert _full_database_snapshot(db) == before


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    RECURRING_WRITERS,
    ids=[writer[0] for writer in RECURRING_WRITERS],
)
@pytest.mark.parametrize(
    "schema_version", [LATEST_SCHEMA_VERSION - 1, LATEST_SCHEMA_VERSION + 1]
)
def test_recurring_writer_rejects_incompatible_schema_before_helper(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments, schema_version
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE recurring_writer_probe (entrypoint TEXT)")
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(conn, **kwargs):
        helper_calls.append(kwargs)
        conn.execute("INSERT INTO recurring_writer_probe VALUES (?)", (entrypoint,))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)

    with pytest.raises(release_gate.IncompatibleSchemaError):
        getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert helper_calls == []
    assert _full_database_snapshot(db) == before


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments"),
    RECURRING_WRITERS,
    ids=[writer[0] for writer in RECURRING_WRITERS],
)
def test_recurring_writer_forwards_arguments_and_commits_exact_helper_result(
    tmp_path, monkeypatch, entrypoint, helper_name, arguments
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE recurring_writer_probe (entrypoint TEXT)")
    conn.commit()
    conn.close()
    expected = {"entrypoint": entrypoint}
    helper_calls = []

    def helper_spy(conn, **kwargs):
        assert conn.row_factory is sqlite3.Row
        helper_calls.append(kwargs)
        conn.execute("INSERT INTO recurring_writer_probe VALUES (?)", (entrypoint,))
        return expected

    monkeypatch.setattr(server, helper_name, helper_spy)

    result = getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert result is expected
    assert helper_calls == [arguments]
    assert sqlite3.connect(db).execute(
        "SELECT entrypoint FROM recurring_writer_probe"
    ).fetchall() == [(entrypoint,)]


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments", "expected_helper_arguments"),
    MIXED_WRITE_BRANCHES,
    ids=[branch[0] for branch in MIXED_WRITE_BRANCHES],
)
@pytest.mark.parametrize(
    ("gate_state", "expected_error"),
    [
        ("stale", release_gate.StaleReleaseError),
        ("older_schema", release_gate.IncompatibleSchemaError),
        ("newer_schema", release_gate.IncompatibleSchemaError),
    ],
)
def test_mixed_write_branch_rejects_release_before_helper_and_preserves_database(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_helper_arguments,
    gate_state,
    expected_error,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE mixed_endpoint_probe (entrypoint TEXT)")
    if gate_state == "stale":
        conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    else:
        schema_version = (
            LATEST_SCHEMA_VERSION - 1
            if gate_state == "older_schema"
            else LATEST_SCHEMA_VERSION + 1
        )
        conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []

    def helper_spy(conn, **kwargs):
        helper_calls.append(kwargs)
        conn.execute("INSERT INTO mixed_endpoint_probe VALUES (?)", (entrypoint,))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)

    with pytest.raises(expected_error):
        getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert helper_calls == []
    assert _full_database_snapshot(db) == before


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments", "expected_helper_arguments"),
    MIXED_WRITE_BRANCHES,
    ids=[branch[0] for branch in MIXED_WRITE_BRANCHES],
)
def test_mixed_write_branch_forwards_arguments_and_commits_exact_helper_result(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_helper_arguments,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE mixed_endpoint_probe (entrypoint TEXT)")
    conn.commit()
    conn.close()
    expected = {"status": "ok", "entrypoint": entrypoint}
    helper_calls = []

    def helper_spy(conn, **kwargs):
        assert conn.row_factory is sqlite3.Row
        helper_calls.append(kwargs)
        conn.execute("INSERT INTO mixed_endpoint_probe VALUES (?)", (entrypoint,))
        return expected

    monkeypatch.setattr(server, helper_name, helper_spy)

    result = getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert result is expected
    assert helper_calls == [expected_helper_arguments]
    assert sqlite3.connect(db).execute(
        "SELECT entrypoint FROM mixed_endpoint_probe"
    ).fetchall() == [(entrypoint,)]


def test_checking_import_non_ok_result_discards_staged_writes(tmp_path, monkeypatch):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE mixed_endpoint_probe (entrypoint TEXT)")
    conn.commit()
    conn.close()
    expected = {"status": "ambiguous", "candidates": ["Checking"]}

    def helper_spy(conn, **kwargs):
        conn.execute("INSERT INTO mixed_endpoint_probe VALUES ('checking')")
        return expected

    monkeypatch.setattr(server, "import_checking_activity_for_db", helper_spy)

    result = server.import_checking_activity(
        CHECKING_CSV,
        as_of_date="2026-07-11",
        dry_run=False,
        db_path=str(db),
    )

    assert result is expected
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM mixed_endpoint_probe"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments", "expected_helper_arguments"),
    MIXED_SAFE_READ_BRANCHES,
    ids=[branch[0] for branch in MIXED_SAFE_READ_BRANCHES],
)
@pytest.mark.parametrize("release_state", ["current", "stale"])
def test_mixed_safe_read_is_read_only_and_reports_release_warning(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_helper_arguments,
    release_state,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE mixed_endpoint_probe (entrypoint TEXT)")
    if release_state == "stale":
        conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    with release_gate.guarded_read(str(db)) as (_, status):
        expected_warning = status.warning
    before = _full_database_snapshot(db)
    expected = {"entrypoint": entrypoint}
    helper_calls = []

    def helper_spy(conn, **kwargs):
        assert conn.row_factory is sqlite3.Row
        helper_calls.append(kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO mixed_endpoint_probe VALUES (?)", (entrypoint,))
        return expected

    monkeypatch.setattr(server, helper_name, helper_spy)

    result = getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert result == {**expected, "release_warning": expected_warning}
    assert helper_calls == [expected_helper_arguments]
    assert _full_database_snapshot(db) == before
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM mixed_endpoint_probe"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("release_state", ["current", "stale"])
def test_check_suggestion_list_is_read_only_and_reports_release_warning(
    tmp_path, monkeypatch, release_state
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE check_suggestion_read_probe (value TEXT)")
    if release_state == "stale":
        conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    with release_gate.guarded_read(str(db)) as (_, status):
        expected_warning = status.warning
    before = _full_database_snapshot(db)
    expected = {"suggestion_id": "suggestion-test"}
    helper_calls = []

    def helper_spy(conn, *, as_of_date=None):
        assert conn.row_factory is sqlite3.Row
        helper_calls.append(as_of_date)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO check_suggestion_read_probe VALUES ('write')")
        return [expected]

    monkeypatch.setattr(server, "list_check_suggestions_for_db", helper_spy)

    result = server.list_check_suggestions(
        as_of_date="2026-07-11", db_path=str(db)
    )

    assert result == {
        "items": [expected],
        "count": 1,
        "release_warning": expected_warning,
    }
    assert helper_calls == ["2026-07-11"]
    assert _full_database_snapshot(db) == before


def test_obligation_migration_dry_run_allows_stale_release_without_any_writes(
    tmp_path,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    with release_gate.guarded_read(str(db)) as (_, status):
        expected_warning = status.warning
    before = _full_database_snapshot(db)
    path = _migration_yaml(
        tmp_path / "obligations.yaml",
        [
            {
                "date": "2026-07-31",
                "label": "Volvo wear and tear",
                "amount": -712.0,
                "source": "verbal",
            }
        ],
    )

    result = server.apply_obligation_migration(
        path, dry_run=True, db_path=str(db)
    )

    assert result == {
        "source": "obligations_yaml",
        "dry_run": True,
        "parsed": 1,
        "obligations_to_create": 1,
        "created_obligations": 0,
        "created_instances": 0,
        "skipped_already_modeled": 0,
        "needs_review": 0,
        "plan": [
            {
                "date": "2026-07-31",
                "label": "Volvo wear and tear",
                "amount": 712.0,
                "direction": "outflow",
                "decision": "new",
                "existing_obligation_id": None,
            }
        ],
        "migration_log_id": None,
        "release_warning": expected_warning,
    }
    assert _full_database_snapshot(db) == before
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM obligation_migration_log"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "schema_version", [LATEST_SCHEMA_VERSION - 1, LATEST_SCHEMA_VERSION + 1]
)
def test_obligation_migration_dry_run_rejects_incompatible_schema_without_changes(
    tmp_path, schema_version
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    path = _migration_yaml(tmp_path / "obligations.yaml", [])

    with pytest.raises(release_gate.IncompatibleSchemaError):
        server.apply_obligation_migration(path, dry_run=True, db_path=str(db))

    assert _full_database_snapshot(db) == before


def test_obligation_migration_live_run_uses_guarded_write_and_commits_all_artifacts(
    tmp_path, monkeypatch
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    path = _migration_yaml(
        tmp_path / "obligations.yaml",
        [
            {
                "date": "2026-07-31",
                "label": "Volvo wear and tear",
                "amount": -712.0,
                "source": "verbal",
            }
        ],
    )
    guarded_write_calls = []
    real_guarded_write = server.guarded_write

    @contextmanager
    def guarded_write_spy(db_path):
        guarded_write_calls.append(db_path)
        with real_guarded_write(db_path) as conn:
            assert conn.in_transaction is True
            yield conn

    monkeypatch.setattr(server, "guarded_write", guarded_write_spy)

    result = server.apply_obligation_migration(
        path, dry_run=False, db_path=str(db)
    )

    assert set(result) == {
        "source",
        "dry_run",
        "parsed",
        "obligations_to_create",
        "created_obligations",
        "created_instances",
        "skipped_already_modeled",
        "needs_review",
        "plan",
        "migration_log_id",
    }
    assert (
        result["created_obligations"],
        result["created_instances"],
        bool(result["migration_log_id"]),
    ) == (1, 1, True)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM obligation_instances"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM obligation_migration_log"
        ).fetchone()[0] == 1
    finally:
        conn.close()
    assert guarded_write_calls == [str(db)]


def test_obligation_migration_live_run_parses_before_rechecking_release(
    tmp_path, monkeypatch
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    path = _migration_yaml(
        tmp_path / "obligations.yaml",
        [
            {
                "date": "2026-07-31",
                "label": "Volvo wear and tear",
                "amount": -712.0,
                "source": "verbal",
            }
        ],
    )
    before = _full_database_snapshot(db)
    events = []
    real_parser = migration.parse_obligations_yaml
    real_guarded_write = server.guarded_write

    def parser_spy(source_path):
        rows = real_parser(source_path)
        events.append("parsed")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE finance_release SET version = '9999.0.0' WHERE id = 1")
        conn.commit()
        conn.close()
        return rows

    @contextmanager
    def guarded_write_spy(db_path):
        events.append("guarded_write")
        with real_guarded_write(db_path) as conn:
            yield conn

    monkeypatch.setattr(migration, "parse_obligations_yaml", parser_spy)
    monkeypatch.setattr(server, "guarded_write", guarded_write_spy)

    with pytest.raises(release_gate.StaleReleaseError):
        server.apply_obligation_migration(path, dry_run=False, db_path=str(db))

    assert events == ["parsed", "guarded_write"]
    after = _full_database_snapshot(db)
    assert after != before
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT version FROM finance_release WHERE id = 1"
        ).fetchone()[0] == "9999.0.0"
        assert conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM obligation_instances"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM obligation_migration_log"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_obligation_migration_live_run_rolls_back_first_group_when_second_fails(
    tmp_path, monkeypatch
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    path = _migration_yaml(
        tmp_path / "obligations.yaml",
        [
            {
                "date": "2026-07-31",
                "label": "Volvo wear and tear",
                "amount": -712.0,
                "source": "verbal",
            },
            {
                "date": "2026-08-01",
                "label": "School deposit",
                "amount": -500.0,
                "source": "invoice",
            },
        ],
    )
    before = _full_database_snapshot(db)
    real_apply = migration.apply_obligation_instances
    apply_calls = 0

    def fail_after_first_group(conn, *, obligation, instances):
        nonlocal apply_calls
        apply_calls += 1
        if apply_calls == 2:
            raise RuntimeError("second group failed")
        return real_apply(conn, obligation=obligation, instances=instances)

    monkeypatch.setattr(
        migration, "apply_obligation_instances", fail_after_first_group
    )

    with pytest.raises(RuntimeError, match="second group failed"):
        server.apply_obligation_migration(path, dry_run=False, db_path=str(db))

    assert apply_calls == 2
    assert _full_database_snapshot(db) == before


def test_obligation_migration_wrapper_does_not_manage_sqlite_transactions_directly():
    functions, direct_calls = _direct_sqlite_transaction_calls(
        {"apply_obligation_migration"}, {"commit", "rollback"}
    )

    assert set(functions) == {"apply_obligation_migration"}
    assert direct_calls == {"apply_obligation_migration": []}


def _stub_surface_due_item_inputs(monkeypatch):
    monkeypatch.setattr(server, "_has_synced_sources", lambda conn: False)
    monkeypatch.setattr(
        server,
        "build_surface_items_for_db",
        lambda conn, **kwargs: [{"surface_key": "test-item"}],
    )
    monkeypatch.setattr(
        server,
        "build_surface_retire_keys_for_db",
        lambda conn, **kwargs: ["retire-test"],
    )


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments", "expected_args", "expected_kwargs"),
    REMAINING_DB_MUTATION_WRITERS,
    ids=[writer[0] for writer in REMAINING_DB_MUTATION_WRITERS],
)
def test_remaining_db_mutation_rejects_stale_release_before_helper_invocation(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_args,
    expected_kwargs,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE remaining_writer_probe (entrypoint TEXT)")
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []
    _stub_surface_due_item_inputs(monkeypatch)

    def helper_spy(conn, *args, **kwargs):
        helper_calls.append((args, kwargs))
        conn.execute("INSERT INTO remaining_writer_probe VALUES (?)", (entrypoint,))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)

    with pytest.raises(release_gate.StaleReleaseError):
        getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert helper_calls == []
    assert _full_database_snapshot(db) == before


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments", "expected_args", "expected_kwargs"),
    REMAINING_DB_MUTATION_WRITERS,
    ids=[writer[0] for writer in REMAINING_DB_MUTATION_WRITERS],
)
def test_remaining_db_mutation_forwards_exact_helper_call_and_commits_row_connection(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_args,
    expected_kwargs,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE remaining_writer_probe (entrypoint TEXT)")
    conn.commit()
    conn.close()
    expected = {"entrypoint": entrypoint}
    helper_calls = []
    _stub_surface_due_item_inputs(monkeypatch)

    def helper_spy(conn, *args, **kwargs):
        assert conn.row_factory is sqlite3.Row
        helper_calls.append((args, kwargs))
        conn.execute("INSERT INTO remaining_writer_probe VALUES (?)", (entrypoint,))
        return expected

    monkeypatch.setattr(server, helper_name, helper_spy)

    result = getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert result is expected
    assert helper_calls == [(expected_args, expected_kwargs)]
    assert sqlite3.connect(db).execute(
        "SELECT entrypoint FROM remaining_writer_probe"
    ).fetchall() == [(entrypoint,)]


@pytest.mark.parametrize(
    (
        "entrypoint",
        "helper_name",
        "arguments",
        "expected_args",
        "expected_kwargs",
        "schema_version",
    ),
    REMAINING_DB_MUTATION_SCHEMA_REPRESENTATIVES,
    ids=[writer[0] for writer in REMAINING_DB_MUTATION_SCHEMA_REPRESENTATIVES],
)
def test_remaining_db_mutation_rejects_incompatible_schema_before_helper_invocation(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_args,
    expected_kwargs,
    schema_version,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)
    helper_calls = []
    _stub_surface_due_item_inputs(monkeypatch)

    def helper_spy(*args, **kwargs):
        helper_calls.append((args, kwargs))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)

    with pytest.raises(release_gate.IncompatibleSchemaError):
        getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert helper_calls == []
    assert _full_database_snapshot(db) == before


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments", "expected_args", "expected_kwargs"),
    REMOTE_TODOIST_MUTATION_WRITERS,
    ids=[writer[0] for writer in REMOTE_TODOIST_MUTATION_WRITERS],
)
def test_remote_todoist_mutation_rejects_stale_release_before_imported_impl(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_args,
    expected_kwargs,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    helper_calls = []

    def helper_spy(*args, **kwargs):
        helper_calls.append((args, kwargs))
        return {"status": "unexpected"}

    monkeypatch.setattr(server, helper_name, helper_spy)

    with pytest.raises(release_gate.StaleReleaseError):
        getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert helper_calls == []


@pytest.mark.parametrize(
    ("entrypoint", "helper_name", "arguments", "expected_args", "expected_kwargs"),
    REMOTE_TODOIST_MUTATION_WRITERS,
    ids=[writer[0] for writer in REMOTE_TODOIST_MUTATION_WRITERS],
)
def test_remote_todoist_mutation_forwards_existing_arguments_and_result_identity(
    tmp_path,
    monkeypatch,
    entrypoint,
    helper_name,
    arguments,
    expected_args,
    expected_kwargs,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    expected = {"entrypoint": entrypoint}
    helper_calls = []

    def helper_spy(*args, **kwargs):
        helper_calls.append((args, kwargs))
        return expected

    monkeypatch.setattr(server, helper_name, helper_spy)

    result = getattr(server, entrypoint)(**arguments, db_path=str(db))

    assert result is expected
    assert helper_calls == [(expected_args, expected_kwargs)]


def test_stale_release_error_tells_writer_to_reload(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()

    with pytest.raises(release_gate.StaleReleaseError) as exc_info:
        release_gate.require_current_release(str(db))

    assert "reload" in str(exc_info.value).lower()


def _direct_sqlite_transaction_calls(function_names, transaction_methods, module=server):
    tree = ast.parse(Path(module.__file__).read_text())
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in function_names
    }
    direct_calls = {}
    for name, function in functions.items():
        direct_calls[name] = [
            call.func.attr
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and (
                call.func.attr in transaction_methods
                or (
                    call.func.attr == "connect"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "sqlite3"
                )
            )
        ]

    return functions, direct_calls


def test_adversarial_main_uses_guarded_release_checked_transaction(
    tmp_path, monkeypatch, capsys
):
    stale_db = tmp_path / "stale.sqlite"
    _prepare_database(stale_db, "current")
    conn = sqlite3.connect(stale_db)
    conn.execute("CREATE TABLE adversarial_probe (as_of_date TEXT, model TEXT)")
    conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")
    conn.commit()
    conn.close()
    stale_before = _full_database_snapshot(stale_db)
    calls = []

    def review_spy(conn, *, as_of_date, model):
        calls.append((as_of_date, model))
        conn.execute(
            "INSERT INTO adversarial_probe VALUES (?, ?)", (as_of_date, model)
        )
        return {
            "available": True,
            "reviewed_count": 2,
            "findings_total": 0,
            "by_severity": {},
        }

    monkeypatch.setattr(adversarial, "adversarial_review_enabled", lambda: True)
    monkeypatch.setattr(adversarial, "run_adversarial_review", review_spy)

    with pytest.raises(release_gate.StaleReleaseError):
        adversarial.main(
            [
                "--as-of",
                "2026-07-11",
                "--db",
                str(stale_db),
                "--model",
                "test-model",
            ]
        )

    assert calls == []
    assert _full_database_snapshot(stale_db) == stale_before

    current_db = tmp_path / "current.sqlite"
    _prepare_database(current_db, "current")
    conn = sqlite3.connect(current_db)
    conn.execute("CREATE TABLE adversarial_probe (as_of_date TEXT, model TEXT)")
    conn.commit()
    conn.close()

    result = adversarial.main(
        [
            "--as-of",
            "2026-07-12",
            "--db",
            str(current_db),
            "--model",
            "current-model",
        ]
    )

    assert result == 0
    assert calls == [("2026-07-12", "current-model")]
    assert sqlite3.connect(current_db).execute(
        "SELECT as_of_date, model FROM adversarial_probe"
    ).fetchall() == [("2026-07-12", "current-model")]
    assert capsys.readouterr().out == (
        "adversarial review complete: reviewed 2 item(s), no advisory flags.\n"
    )

    functions, direct_calls = _direct_sqlite_transaction_calls(
        {"main"}, {"commit", "rollback"}, adversarial
    )
    assert set(functions) == {"main"}
    assert direct_calls == {"main": []}


def test_migrated_writer_functions_do_not_manage_sqlite_transactions_directly():
    migrated_writers = {
        "set_manual_balance",
        "import_card_statement",
        *(writer[0] for writer in LOCAL_WRITERS),
    }
    functions, direct_calls = _direct_sqlite_transaction_calls(
        migrated_writers, {"commit"}
    )

    assert set(functions) == migrated_writers
    assert direct_calls == {name: [] for name in migrated_writers}


def test_obligation_writers_do_not_manage_sqlite_transactions_directly():
    obligation_writers = {writer[0] for writer in OBLIGATION_WRITERS}
    functions, direct_calls = _direct_sqlite_transaction_calls(
        obligation_writers, {"commit", "rollback"}
    )

    assert set(functions) == obligation_writers
    assert direct_calls == {name: [] for name in obligation_writers}


def test_onboarding_statement_reconciliation_writers_do_not_manage_sqlite_transactions_directly():
    writers = {
        writer[0] for writer in ONBOARDING_STATEMENT_RECONCILIATION_WRITERS
    }
    functions, direct_calls = _direct_sqlite_transaction_calls(
        writers, {"commit", "rollback"}
    )

    assert set(functions) == writers
    assert direct_calls == {name: [] for name in writers}


def test_reconciliation_confirmation_writers_do_not_manage_sqlite_transactions_directly():
    writers = {writer[0] for writer in RECONCILIATION_CONFIRMATION_WRITERS}
    functions, direct_calls = _direct_sqlite_transaction_calls(
        writers, {"commit", "rollback"}
    )

    assert set(functions) == writers
    assert direct_calls == {name: [] for name in writers}


def test_imp1_writers_do_not_manage_sqlite_transactions_directly():
    writers = {writer[0] for writer in IMP1_WRITERS}
    functions, direct_calls = _direct_sqlite_transaction_calls(
        writers, {"commit", "rollback"}
    )

    assert set(functions) == writers
    assert direct_calls == {name: [] for name in writers}


def test_recurring_writers_and_helpers_do_not_manage_sqlite_transactions_directly():
    wrapper_names = {writer[0] for writer in RECURRING_WRITERS}
    wrapper_functions, wrapper_calls = _direct_sqlite_transaction_calls(
        wrapper_names, {"commit", "rollback"}
    )
    backfill_functions, backfill_calls = _direct_sqlite_transaction_calls(
        {"backfill_recurring_instances"}, {"commit", "rollback"}, backfill
    )
    onboarding_functions, onboarding_calls = _direct_sqlite_transaction_calls(
        {"auto_model_high_confidence_recurring"},
        {"commit", "rollback"},
        onboarding,
    )

    assert set(wrapper_functions) == wrapper_names
    assert set(backfill_functions) == {"backfill_recurring_instances"}
    assert set(onboarding_functions) == {"auto_model_high_confidence_recurring"}
    direct_calls = {
        **{f"server.{name}": calls for name, calls in wrapper_calls.items()},
        **{f"backfill.{name}": calls for name, calls in backfill_calls.items()},
        **{f"onboarding.{name}": calls for name, calls in onboarding_calls.items()},
    }
    assert direct_calls == {name: [] for name in direct_calls}


def test_mixed_endpoints_do_not_manage_sqlite_transactions_directly():
    entrypoints = {
        "suppress_contradicted_estimates",
        "evaluate_guardrails",
        "get_statement_status",
        "detect_drift",
        "run_verification",
        "import_checking_activity",
    }
    functions, direct_calls = _direct_sqlite_transaction_calls(
        entrypoints, {"commit"}
    )

    assert set(functions) == entrypoints
    assert direct_calls == {name: [] for name in entrypoints}


def test_remaining_db_mutations_do_not_manage_sqlite_transactions_directly():
    entrypoints = {writer[0] for writer in REMAINING_DB_MUTATION_WRITERS}
    functions, direct_calls = _direct_sqlite_transaction_calls(
        entrypoints, {"commit", "rollback"}
    )

    assert set(functions) == entrypoints
    assert direct_calls == {name: [] for name in entrypoints}


@pytest.mark.parametrize("release_state", ["missing_table", "missing_row", "mismatched"])
def test_scheduled_writer_fails_closed_when_release_record_is_not_current(
    tmp_path, monkeypatch, release_state
):
    background_calls = 0

    def fake_background(conn, **kwargs):
        nonlocal background_calls
        background_calls += 1
        return {"status": "succeeded", "result_summary": {}}

    monkeypatch.setattr(scheduled, "run_background_sync", fake_background)
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, release_state)
    before = _database_snapshot(db)

    error = None
    try:
        scheduled.run_scheduled_daily_sync(
            str(db),
            lock_dir=str(tmp_path),
            as_of_date="2026-07-11",
        )
    except release_gate.StaleReleaseError as exc:
        error = exc

    after = _database_snapshot(db)
    assert (error is not None, background_calls, after) == (True, 0, before)
    assert "release record" in str(error).lower()


def test_scheduled_writer_allows_current_release_record(tmp_path, monkeypatch):
    background_calls = 0

    def fake_background(conn, **kwargs):
        nonlocal background_calls
        background_calls += 1
        return {"status": "succeeded", "result_summary": {}}

    monkeypatch.setattr(scheduled, "run_background_sync", fake_background)
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    release_before = _release_version(db)

    result = scheduled.run_scheduled_daily_sync(
        str(db),
        lock_dir=str(tmp_path),
        as_of_date="2026-07-11",
    )

    assert (result["status"], background_calls) == ("completed", 1)
    assert _release_version(db) == release_before


@pytest.mark.parametrize(
    ("release_state", "stored_version", "expected_status", "expected_db_version"),
    [
        ("current", None, "current", build_info.VERSION),
        ("missing_row", "0.0.0", "stale", "0.0.0"),
        ("missing_row", "malformed", "stale", "malformed"),
        ("missing_row", None, "missing_row", None),
        ("missing_table", None, "missing_table", None),
    ],
)
def test_guarded_read_reports_immutable_release_status(
    tmp_path,
    release_state,
    stored_version,
    expected_status,
    expected_db_version,
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, release_state)
    if stored_version is not None:
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO finance_release (id, version) VALUES (1, ?)",
            (stored_version,),
        )
        conn.commit()
        conn.close()

    statuses = []
    for _ in range(2):
        with release_gate.guarded_read(str(db)) as (_, status):
            statuses.append(status)

    status = statuses[0]
    assert type(status) is release_gate.ReleaseStatus
    assert (
        status.status,
        status.warning,
        status.db_version,
        status.runtime_version,
    ) == (
        expected_status,
        None if expected_status == "current" else statuses[1].warning,
        expected_db_version,
        build_info.VERSION,
    )
    if expected_status != "current":
        assert status.warning
    with pytest.raises(AttributeError):
        status.status = "changed"


@pytest.mark.parametrize(
    "schema_version", [LATEST_SCHEMA_VERSION - 1, LATEST_SCHEMA_VERSION + 1]
)
def test_guarded_read_rejects_incompatible_schema_before_body(
    tmp_path, schema_version
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {schema_version}")
    conn.commit()
    conn.close()
    before = _database_snapshot(db)
    body_entered = False

    with pytest.raises(release_gate.IncompatibleSchemaError):
        with release_gate.guarded_read(str(db)):
            body_entered = True

    assert body_entered is False
    assert _database_snapshot(db) == before


def test_guarded_read_yields_read_only_row_connection_without_transaction(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    before = _database_snapshot(db)

    with release_gate.guarded_read(str(db)) as (conn, _):
        assert conn.row_factory is sqlite3.Row
        assert conn.in_transaction is False
        assert conn.execute(
            "SELECT version FROM finance_release WHERE id = 1"
        ).fetchone()["version"] == build_info.VERSION
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE finance_release SET version = '0.0.0' WHERE id = 1")

    assert _database_snapshot(db) == before


def test_guarded_read_does_not_create_missing_database(tmp_path):
    db = tmp_path / "missing.sqlite"
    guarded_read = release_gate.guarded_read

    with pytest.raises(release_gate.StaleReleaseError):
        with guarded_read(str(db)):
            pass

    assert not db.exists()


@pytest.mark.parametrize("raise_from_body", [False, True])
def test_guarded_read_closes_connection_and_propagates_body_error(
    tmp_path, raise_from_body
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    guarded_conn = None

    def read_body():
        nonlocal guarded_conn
        with release_gate.guarded_read(str(db)) as (conn, _):
            guarded_conn = conn
            if raise_from_body:
                raise RuntimeError("stop read")

    if raise_from_body:
        with pytest.raises(RuntimeError, match="stop read"):
            read_body()
    else:
        read_body()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        guarded_conn.execute("SELECT 1")


def test_stale_writer_returns_structured_reload_required_over_stdio(tmp_path):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    stored_version = "9.9.9"
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE finance_release SET version = ? WHERE id = 1",
        (stored_version,),
    )
    conn.commit()
    conn.close()
    before = _full_database_snapshot(db)

    missing_db = tmp_path / "missing.sqlite"
    malformed_db = tmp_path / "malformed.sqlite"
    _prepare_database(malformed_db, "current")
    conn = sqlite3.connect(malformed_db)
    conn.execute(
        "UPDATE finance_release SET version = 'not-semver' WHERE id = 1"
    )
    conn.commit()
    conn.close()
    malformed_before = _full_database_snapshot(malformed_db)

    async def call_tools():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "financial_agent.server"],
            cwd=Path(__file__).parents[1],
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                stale_result = await session.call_tool(
                    "set_goal",
                    {
                        "name": "Transport probe",
                        "target_amount": 1,
                        "db_path": str(db),
                    },
                )
                missing_result = await session.call_tool(
                    "set_goal",
                    {
                        "name": "Missing database probe",
                        "target_amount": 1,
                        "db_path": str(missing_db),
                    },
                )
                malformed_result = await session.call_tool(
                    "set_goal",
                    {
                        "name": "Malformed release probe",
                        "target_amount": 1,
                        "db_path": str(malformed_db),
                    },
                )
                unrelated_result = await session.call_tool("unknown_tool", {})
                return (
                    stale_result,
                    missing_result,
                    malformed_result,
                    unrelated_result,
                )

    result, missing_result, malformed_result, unrelated_result = anyio.run(call_tools)
    expected = {
        "status": "reload_required",
        "reload_required": True,
        "write_applied": False,
        "runtime_version": build_info.VERSION,
        "stored_version": stored_version,
        "message": (
            f"Release record does not match running version {build_info.VERSION}. "
            "Reload the finance server and retry."
        ),
    }

    assert result.isError is True
    assert result.structuredContent == expected
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == expected
    assert missing_result.isError is True
    assert missing_result.structuredContent is None
    assert "release record could not be read" in missing_result.content[0].text.lower()
    assert malformed_result.isError is True
    assert malformed_result.structuredContent is None
    assert "invalid release record version" in malformed_result.content[0].text.lower()
    assert unrelated_result.isError is True
    assert unrelated_result.structuredContent is None
    assert "unknown tool" in unrelated_result.content[0].text.lower()
    assert not missing_db.exists()
    assert _full_database_snapshot(malformed_db) == malformed_before
    assert _full_database_snapshot(db) == before


def test_in_transaction_release_recheck_returns_new_stored_version_without_write(
    tmp_path, monkeypatch
):
    db = tmp_path / "finance.sqlite"
    _prepare_database(db, "current")
    goals_before = _database_snapshot(db)["goals"]
    stored_version = "9999.0.0"
    real_precheck = release_gate.require_current_release
    helper_calls = []

    def advance_after_precheck(db_path):
        real_precheck(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE finance_release SET version = ? WHERE id = 1",
            (stored_version,),
        )
        conn.commit()
        conn.close()

    def helper_spy(*args, **kwargs):
        helper_calls.append((args, kwargs))
        return {"status": "unexpected"}

    monkeypatch.setattr(release_gate, "require_current_release", advance_after_precheck)
    monkeypatch.setattr(server, "set_goal_for_db", helper_spy)

    async def call_tool():
        return await server.mcp.call_tool(
            "set_goal",
            {
                "name": "In-lock probe",
                "target_amount": 1,
                "db_path": str(db),
            },
        )

    result = anyio.run(call_tool)

    assert result.isError is True
    assert result.structuredContent == {
        "status": "reload_required",
        "reload_required": True,
        "write_applied": False,
        "runtime_version": build_info.VERSION,
        "stored_version": stored_version,
        "message": (
            f"Release record does not match running version {build_info.VERSION}. "
            "Reload the finance server and retry."
        ),
    }
    assert helper_calls == []
    assert _database_snapshot(db)["goals"] == goals_before
    assert _release_version(db) == stored_version
