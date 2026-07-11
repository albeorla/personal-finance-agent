"""Release gate coverage for supported database writers."""

import ast
import sqlite3
from pathlib import Path

import pytest

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
        },
        {
            "text": CHECKING_CSV,
            "account_query": "Apple Card",
            "as_of_date": "2026-07-11",
            "balance": 2500,
            "dry_run": False,
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


def _direct_sqlite_transaction_calls(function_names, transaction_methods):
    tree = ast.parse(Path(server.__file__).read_text())
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
