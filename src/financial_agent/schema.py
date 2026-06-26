from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

# Latest schema version. Kept in lockstep with the max version in _MIGRATIONS;
# a fresh DB ends at this version after ensure_app_schema runs. When adding a
# migration, append it to _MIGRATIONS (never reorder/renumber) and bump this.
LATEST_SCHEMA_VERSION = 2


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """Baseline migration: the full create-if-missing + ALTER schema body.

    This is the original ensure_app_schema body, kept idempotent (CREATE ...
    IF NOT EXISTS plus _ensure_column guards) so existing local DBs at
    user_version 0 converge cleanly without data loss.
    """

    conn.executescript(
        """
        -- obligations.status values: 'active' (projects), 'inactive', and
        -- 'dormant_suppressed' (auto-deactivated by
        -- obligations.suppress_dormant_avg_estimates when the source account goes
        -- dormant; excluded from projection, fully reversible by setting status
        -- back to 'active'). No DB-level enum so reversal stays a simple UPDATE.
        CREATE TABLE IF NOT EXISTS obligations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            cadence TEXT,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS obligation_instances (
            id TEXT PRIMARY KEY,
            obligation_id TEXT NOT NULL,
            due_date TEXT NOT NULL,
            amount REAL NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('inflow', 'outflow')),
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence TEXT,
            notes TEXT,
            amount_status TEXT,
            amount_source TEXT,
            amount_observed_at TEXT,
            statement_close_date TEXT,
            review_after TEXT,
            estimation_method TEXT,
            estimation_inputs_json TEXT,
            cash_flow_treatment TEXT,
            statement_target_obligation_id TEXT,
            generated_from_income_source_id TEXT,
            generated_from_schedule_version_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (obligation_id) REFERENCES obligations(id)
        );

        CREATE TABLE IF NOT EXISTS income_sources (
            id TEXT PRIMARY KEY,
            person TEXT NOT NULL,
            employer TEXT NOT NULL,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL,
            default_amount REAL NOT NULL,
            deposit_account_id TEXT,
            working_account_id TEXT,
            source TEXT NOT NULL,
            confidence TEXT NOT NULL,
            active_from TEXT NOT NULL,
            active_until TEXT,
            review_by TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS income_schedule_versions (
            id TEXT PRIMARY KEY,
            income_source_id TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            rule_json TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            confidence TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            review_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (income_source_id) REFERENCES income_sources(id)
        );

        CREATE TABLE IF NOT EXISTS calendar_facts (
            id TEXT PRIMARY KEY,
            fact_type TEXT NOT NULL,
            fact_date TEXT NOT NULL,
            source TEXT NOT NULL,
            external_id TEXT,
            calendar_id TEXT,
            related_entity_type TEXT,
            related_entity_id TEXT,
            title TEXT,
            confidence TEXT NOT NULL,
            status TEXT NOT NULL,
            notes TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS charge_onboarding_candidates (
            id TEXT PRIMARY KEY,
            merchant_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            account_class TEXT,
            direction TEXT NOT NULL CHECK (direction IN ('inflow', 'outflow')),
            status TEXT NOT NULL,
            candidate_type TEXT,
            cash_flow_treatment TEXT,
            proposed_schedule_policy_json TEXT,
            proposed_amount_policy_json TEXT,
            proposed_cash_impact_policy_json TEXT,
            proposed_review_policy_json TEXT,
            confidence TEXT,
            priority_score REAL,
            evidence_count INTEGER NOT NULL,
            evidence_transaction_ids_json TEXT,
            evidence_summary_json TEXT,
            missing_evidence_json TEXT,
            notes TEXT,
            decision_json TEXT,
            existing_obligation_id TEXT,
            first_evidence_date TEXT,
            last_evidence_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            reviewed_at TEXT,
            applied_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_charge_onboarding_candidates_status
            ON charge_onboarding_candidates(status);

        CREATE INDEX IF NOT EXISTS idx_charge_onboarding_candidates_merchant
            ON charge_onboarding_candidates(merchant_key);

        CREATE TABLE IF NOT EXISTS statement_cycles (
            id TEXT PRIMARY KEY,
            target_obligation_id TEXT NOT NULL,
            statement_instance_id TEXT,
            cycle_open_date TEXT,
            cycle_close_date TEXT NOT NULL,
            due_date TEXT,
            input_count INTEGER NOT NULL DEFAULT 0,
            input_sum REAL NOT NULL DEFAULT 0,
            confidence TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS statement_cycle_inputs (
            statement_cycle_id TEXT NOT NULL,
            obligation_instance_id TEXT NOT NULL,
            input_amount REAL NOT NULL,
            input_confidence TEXT,
            due_date TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (statement_cycle_id, obligation_instance_id)
        );

        CREATE INDEX IF NOT EXISTS idx_statement_cycles_target
            ON statement_cycles(target_obligation_id, cycle_close_date);

        -- Card-spend paste-import runs (design #4). One row per real (non-dry-run)
        -- import of a pasted Apple Card CSV / statement. The latest row per
        -- account drives the Apple-Card paste-freshness signal (measured against
        -- the statement cycle, not the SimpleFIN sync clock). total_spend is the
        -- signed (negative) cycle spend; statement_close_date is the cycle the
        -- paste covered; error records a failed import without losing the trail.
        CREATE TABLE IF NOT EXISTS card_import_runs (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            statement_close_date TEXT,
            txn_count INTEGER NOT NULL DEFAULT 0,
            total_spend REAL NOT NULL DEFAULT 0,
            source_format TEXT,
            error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_card_import_runs_account
            ON card_import_runs(account_id, imported_at);

        CREATE TABLE IF NOT EXISTS transaction_obligation_matches (
            obligation_instance_id TEXT PRIMARY KEY,
            transaction_id TEXT NOT NULL,
            match_type TEXT NOT NULL,
            match_score REAL NOT NULL,
            amount_score REAL,
            date_score REAL,
            merchant_score REAL,
            amount_delta REAL,
            date_delta_days INTEGER,
            as_of_date TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS unmatched_obligations (
            obligation_instance_id TEXT PRIMARY KEY,
            obligation_id TEXT,
            due_date TEXT,
            as_of_date TEXT,
            age_days INTEGER,
            grace_period_days INTEGER,
            past_grace INTEGER,
            status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_txn_obligation_matches_txn
            ON transaction_obligation_matches(transaction_id);

        CREATE TABLE IF NOT EXISTS drift_findings (
            id TEXT PRIMARY KEY,
            finding_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            obligation_id TEXT,
            obligation_instance_id TEXT,
            related_transaction_ids_json TEXT,
            cash_flow_impact REAL,
            confidence TEXT,
            evidence_json TEXT,
            recommended_action TEXT,
            status TEXT NOT NULL,
            as_of_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_drift_findings_status
            ON drift_findings(status, finding_type);

        CREATE TABLE IF NOT EXISTS action_outbox (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            action_type TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            payload_json TEXT,
            payload_hash TEXT,
            dry_run INTEGER NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            item_count INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_action_outbox_status
            ON action_outbox(status, action_type);

        -- Todoist surfacing ledger (idempotent daily push). One row per
        -- surfaceable item, keyed by a stable surface_key (e.g.
        -- 'followup:<id>', 'goal:<name>:behind'). The ledger is the single
        -- source of truth that prevents re-creating a task on re-run, across
        -- days, or when the user already made the task manually.
        -- status values: 'open', 'completed', 'deleted_by_user', 'retired'.
        -- 'retired' = the surfaced task was auto-removed (project reconcile /
        -- retire drain) but the underlying need may recur; unlike 'completed' and
        -- 'deleted_by_user', a 'retired' row does NOT suppress recreation, so a
        -- recurring surface_key resurfaces normally when it next becomes due.
        -- No DB-level CHECK on status so adding a value stays a code-only change.
        CREATE TABLE IF NOT EXISTS todoist_emissions (
            surface_key TEXT PRIMARY KEY,
            todoist_task_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_todoist_emissions_status
            ON todoist_emissions(status);

        CREATE TABLE IF NOT EXISTS background_runs (
            id TEXT PRIMARY KEY,
            trace_id TEXT,
            run_type TEXT NOT NULL,
            trigger_type TEXT,
            status TEXT NOT NULL,
            as_of_date TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            duration_ms INTEGER,
            result_summary_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS operation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            event_seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT,
            event_data_json TEXT,
            error TEXT,
            event_time TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_operation_events_run
            ON operation_events(run_id, event_seq);

        CREATE INDEX IF NOT EXISTS idx_background_runs_type
            ON background_runs(run_type, started_at);

        CREATE TABLE IF NOT EXISTS memory_records (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            metadata_json TEXT,
            term_frequency_json TEXT NOT NULL,
            token_count INTEGER NOT NULL,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_memory_records_kind
            ON memory_records(kind);

        CREATE TABLE IF NOT EXISTS obligation_migration_log (
            id TEXT PRIMARY KEY,
            run_timestamp TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_path TEXT,
            dry_run INTEGER NOT NULL,
            parsed INTEGER NOT NULL,
            created_obligations INTEGER NOT NULL,
            created_instances INTEGER NOT NULL,
            skipped_already_modeled INTEGER NOT NULL,
            needs_review INTEGER NOT NULL,
            errors_json TEXT
        );

        CREATE TABLE IF NOT EXISTS guardrail_rules (
            id TEXT PRIMARY KEY,
            rule_type TEXT NOT NULL UNIQUE,
            threshold_value REAL,
            threshold_json TEXT,
            severity_default TEXT,
            description TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS guardrail_evaluations (
            id TEXT PRIMARY KEY,
            rule_type TEXT NOT NULL,
            evaluation_date TEXT NOT NULL,
            passed INTEGER NOT NULL,
            finding_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_guardrail_evaluations_date
            ON guardrail_evaluations(evaluation_date, rule_type);

        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            target_amount REAL NOT NULL,
            deadline TEXT,
            source_account TEXT,
            current_progress REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_goals_status
            ON goals(status);

        CREATE INDEX IF NOT EXISTS idx_goals_deadline
            ON goals(deadline);

        -- Structured, queryable debts layer. This replaces the hard-coded
        -- DEBT_AVALANCHE_APR_ORDER constant: the avalanche guardrail and the
        -- interest math read each debt's APR and live balance from here.
        --   account_id      links to a synced accounts.id for a live balance
        --                   (NULL for debts with no synced account, e.g. a
        --                   federal student loan tracked only in a portal).
        --   balance_source  'account' = use the linked account's latest
        --                   balance_snapshot; 'manual' = use balance_override.
        --   balance_override signed manual balance (negative = owed) for debts
        --                   with no synced account.
        --   is_revolving    1 = accrues interest / is a paydown target;
        --                   0 = paid in full each month, excluded from the
        --                   avalanche target order even if its APR is high.
        CREATE TABLE IF NOT EXISTS debts (
            id TEXT PRIMARY KEY,
            account_id TEXT,
            name TEXT NOT NULL,
            apr REAL NOT NULL,
            balance_source TEXT NOT NULL DEFAULT 'account'
                CHECK (balance_source IN ('account', 'manual')),
            balance_override REAL,
            min_payment REAL,
            is_revolving INTEGER NOT NULL DEFAULT 1,
            autopay INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_debts_account
            ON debts(account_id);

        CREATE INDEX IF NOT EXISTS idx_debts_revolving
            ON debts(is_revolving);

        CREATE TABLE IF NOT EXISTS follow_ups (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            surface_when TEXT NOT NULL,
            priority TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            linked_obligation_id TEXT,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_follow_ups_surface_when
            ON follow_ups(surface_when, status);

        CREATE INDEX IF NOT EXISTS idx_follow_ups_linked_obligation
            ON follow_ups(linked_obligation_id);

        CREATE INDEX IF NOT EXISTS idx_obligation_instances_due_date
            ON obligation_instances(due_date);

        CREATE INDEX IF NOT EXISTS idx_obligation_instances_status
            ON obligation_instances(status);

        CREATE INDEX IF NOT EXISTS idx_income_schedule_versions_source
            ON income_schedule_versions(income_source_id);

        CREATE INDEX IF NOT EXISTS idx_calendar_facts_type_date
            ON calendar_facts(fact_type, fact_date);

        CREATE INDEX IF NOT EXISTS idx_calendar_facts_related
            ON calendar_facts(related_entity_type, related_entity_id, fact_type, fact_date);
        """
    )
    _ensure_column(conn, "obligation_instances", "generated_from_income_source_id", "TEXT")
    _ensure_column(conn, "obligation_instances", "generated_from_schedule_version_id", "TEXT")
    _ensure_column(conn, "obligation_instances", "amount_status", "TEXT")
    _ensure_column(conn, "obligation_instances", "amount_source", "TEXT")
    _ensure_column(conn, "obligation_instances", "amount_observed_at", "TEXT")
    _ensure_column(conn, "obligation_instances", "statement_close_date", "TEXT")
    _ensure_column(conn, "obligation_instances", "review_after", "TEXT")
    _ensure_column(conn, "obligation_instances", "estimation_method", "TEXT")
    _ensure_column(conn, "obligation_instances", "estimation_inputs_json", "TEXT")
    _ensure_column(conn, "obligation_instances", "cash_flow_treatment", "TEXT")
    _ensure_column(conn, "obligation_instances", "statement_target_obligation_id", "TEXT")
    _ensure_column(conn, "obligation_instances", "matched_transaction_id", "TEXT")
    _ensure_column(conn, "obligation_instances", "matched_at", "TEXT")
    _ensure_column(conn, "obligation_instances", "match_confidence", "REAL")
    # autopay flag (manual-due surfacing): 1 = the bill pays itself (autopay /
    # auto-debit), so it stays quiet and only drift-detection catches a failure;
    # 0 = a human must take an action (write the rent check, run an Apple Card
    # paydown sweep), so the surface queue reminds a few days before it is due.
    # Defaults existing rows to 1 (quiet) so nothing new surfaces until a bill is
    # explicitly classified as manual.
    _ensure_column(conn, "obligations", "autopay", "INTEGER NOT NULL DEFAULT 1")
    # amount_discretionary flag (manual-due surfacing wording): 1 = the user
    # decides the amount each time and the modeled figure is just a floor (e.g.
    # the Apple Card payment, where the modeled amount is only the minimum); the
    # surface queue then frames it as a decide-amount task rather than a fixed
    # bill. 0 = the modeled amount is the amount to pay. Defaults existing rows to
    # 0 so wording is unchanged until a bill is explicitly marked discretionary.
    _ensure_column(conn, "obligations", "amount_discretionary", "INTEGER NOT NULL DEFAULT 0")
    # Goal progress override (slice: live balances). When set, the goal's
    # current_progress is forced to balance_override_amount instead of being
    # derived from the source account's latest balance snapshot. NULL means
    # "use the live balance"; the set-at timestamp supports staleness/audit.
    _ensure_column(conn, "goals", "balance_override_amount", "REAL")
    _ensure_column(conn, "goals", "balance_override_set_at", "TEXT")
    # Todoist write-back state (slice U): tracks the external task so reruns update
    # the same task instead of creating duplicates.
    _ensure_column(conn, "action_outbox", "external_task_id", "TEXT")
    _ensure_column(conn, "action_outbox", "last_pushed_hash", "TEXT")
    _ensure_column(conn, "action_outbox", "last_observed_state", "TEXT")
    # Tombstone: set when a candidate/obligation decision marks a surfaced task
    # for removal; drained by surface_to_todoist (delete + flip status to
    # 'retired'). Nullable, defaults NULL (no retire pending).
    _ensure_column(conn, "todoist_emissions", "retire_requested_at", "TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_obligation_instances_income_source
            ON obligation_instances(generated_from_income_source_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_obligation_instances_cash_flow_treatment
            ON obligation_instances(cash_flow_treatment)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_obligation_instances_statement_target
            ON obligation_instances(statement_target_obligation_id)
        """
    )


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Add verification_findings: the deterministic self-check ledger.

    The grounding gate proves each headline number traces to a source row; a
    verification run proves the rows tie together (the projection equals its own
    events, no obligation has duplicate dated instances, a statement rollup
    matches its inputs, amounts carry a sane sign). Each failed check writes one
    row here so the engine's own composition errors become legible to the human
    verifier instead of passing silently. Idempotent (CREATE ... IF NOT EXISTS).
    """

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS verification_findings (
            id TEXT PRIMARY KEY,
            run_id TEXT,
            check_id TEXT NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT,
            evidence_json TEXT,
            as_of_date TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_verification_findings_run
            ON verification_findings(run_id);

        CREATE INDEX IF NOT EXISTS idx_verification_findings_status
            ON verification_findings(status, check_id);
        """
    )


# Ordered migration registry: (target version, idempotent apply function).
# Append-only; never reorder or renumber existing entries.
_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migrate_to_v1),
    (2, _migrate_to_v2),
]


def ensure_app_schema(conn: sqlite3.Connection) -> None:
    """Create local app-owned finance tables.

    These tables are separate from legacy Todoist snapshots. They are the target
    source for deterministic projections and later Todoist reflection.

    Applies the ordered migrations, tracked by PRAGMA user_version, running only
    the steps newer than the DB's current version. Idempotent: re-running on an
    up-to-date DB is a no-op.
    """

    current = get_schema_version(conn)
    for version, apply in _MIGRATIONS:
        if version > current:
            apply(conn)
            # PRAGMA cannot be parameterized; version is a trusted int literal
            # from the hardcoded _MIGRATIONS list, never user input.
            conn.execute(f"PRAGMA user_version = {version}")
            current = version


def get_schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def has_app_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'obligation_instances'
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m financial_agent.schema /path/to/transactions.sqlite")
    db_path = Path(sys.argv[1]).expanduser()
    conn = sqlite3.connect(db_path)
    try:
        ensure_app_schema(conn)
        conn.commit()
    finally:
        conn.close()
    print(f"initialized app schema in {db_path}")


if __name__ == "__main__":
    main()
