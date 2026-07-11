from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .calendar_facts import (
    import_calendar_facts as import_calendar_facts_for_db,
    list_calendar_facts as list_calendar_facts_for_db,
)
from .income import (
    apply_income_source as apply_income_source_config,
    generate_income_instances as generate_income_instances_for_db,
    list_income_sources as list_income_sources_for_db,
)
from .obligations import (
    apply_obligation_instances as apply_obligation_instances_for_db,
    deactivate_obligation as deactivate_obligation_for_db,
    delete_obligation_instance as delete_obligation_instance_for_db,
    set_obligation_end as set_obligation_end_for_db,
    list_obligation_review_candidates as list_obligation_review_candidates_for_db,
    list_obligations as list_obligations_for_db,
    list_statement_input_estimates as list_statement_input_estimates_for_db,
    suppress_contradicted_estimates as suppress_contradicted_estimates_for_db,
)
from .parity import compare_to_legacy as compare_to_legacy_for_db
from .parity import render_parity_markdown as render_parity_markdown_for_db
from .onboarding import (
    apply_charge_onboarding_candidate as apply_charge_onboarding_candidate_for_db,
    get_next_charge_onboarding_candidate as get_next_charge_onboarding_candidate_for_db,
    list_charge_onboarding_queue as list_charge_onboarding_queue_for_db,
    preview_charge_onboarding_apply as preview_charge_onboarding_apply_for_db,
    record_charge_onboarding_decision as record_charge_onboarding_decision_for_db,
    scan_charge_onboarding_candidates as scan_charge_onboarding_candidates_for_db,
)
from . import build_info
from .adversarial import run_adversarial_review as run_adversarial_review_for_db
from .background import (
    get_background_run as get_background_run_for_db,
    get_job_health as get_job_health_for_db,
    list_background_runs as list_background_runs_for_db,
    run_background_sync as run_background_sync_for_db,
)
from .digest import build_daily_digest as build_daily_digest_for_db
from .digest import render_digest_markdown as render_digest_markdown_for_db
from .digest import summarize_daily_digest as summarize_daily_digest_for_db
from .verification import (
    acknowledge_verification_findings as acknowledge_verification_findings_for_db,
    list_verification_findings as list_verification_findings_for_db,
    run_verification as run_verification_for_db,
)
from .analytics import list_transactions as list_transactions_for_db
from .analytics import render_spending_markdown as render_spending_markdown_for_db
from .backfill import backfill_recurring_instances as backfill_recurring_instances_for_db
from .onboarding import auto_model_high_confidence_recurring as auto_model_high_confidence_recurring_for_db
from .analytics import summarize_spending as summarize_spending_for_db
from .grounding import verify_grounding as verify_grounding_for_db
from .drift import detect_drift as detect_drift_for_db
from .drift import list_drift_findings as list_drift_findings_for_db
from .guardrails import (
    apply_guardrail_rules as apply_guardrail_rules_for_db,
    evaluate_guardrails as evaluate_guardrails_for_db,
    list_guardrail_findings as list_guardrail_findings_for_db,
)
from .memory import (
    delete_memory as delete_memory_for_db,
    list_memories as list_memories_for_db,
    search_memory as search_memory_for_db,
    write_memory as write_memory_for_db,
)
from .manual_balance import set_manual_balance as set_manual_balance_for_db
from .card_import import import_card_statement_for_db, import_checking_activity_for_db
from .migration import apply_obligation_migration as apply_obligation_migration_for_db
from .sync_simplefin import sync_simplefin as sync_simplefin_for_db
from .validate import run_live_validation as run_live_validation_for_db
from .reconciliation import (
    confirm_reconciliation_match as confirm_reconciliation_match_for_db,
    list_matched_obligation_instances as list_matched_obligation_instances_for_db,
    list_reconciliation_review_items as list_reconciliation_review_items_for_db,
    list_unmatched_obligation_instances as list_unmatched_obligation_instances_for_db,
    reconcile_obligation_instances as reconcile_obligation_instances_for_db,
    unconfirm_reconciliation_match as unconfirm_reconciliation_match_for_db,
)
from .statements import (
    aggregate_statement_inputs as aggregate_statement_inputs_for_db,
    get_statement_status as get_statement_status_for_db,
    list_statement_cycles as list_statement_cycles_for_db,
    recompute_statement_estimates as recompute_statement_estimates_for_db,
    set_statement_actual as set_statement_actual_for_db,
)
from .todoist_outbox import (
    complete_todoist_task as complete_todoist_task_impl,
    create_todoist_task as create_todoist_task_impl,
    delete_todoist_task as delete_todoist_task_impl,
    execute_action_outbox as execute_action_outbox_for_db,
    list_action_outbox as list_action_outbox_for_db,
    reconcile_emission as reconcile_emission_for_db,
    reconcile_todoist_completions as reconcile_todoist_completions_for_db,
    list_today_tasks_all_projects_for_db,
    list_todoist_project_for_db,
    reconcile_todoist_project_for_db,
    reopen_todoist_task as reopen_todoist_task_impl,
    request_emission_retire_prefix,
    surface_to_todoist as surface_to_todoist_for_db,
    update_todoist_task as update_todoist_task_impl,
)
from .release_gate import guarded_read, guarded_write, require_current_release
from .status import default_db_path
from .status import get_finance_status as build_finance_status
from .debts import (
    list_debts as list_debts_for_db,
    set_debt_terms as set_debt_terms_for_db,
)
from .goals import (
    list_goals as list_goals_for_db,
    set_goal as set_goal_for_db,
    set_goal_override as set_goal_override_for_db,
)
from .follow_ups import (
    capture_followup as capture_followup_for_db,
    list_due_followups as list_due_followups_for_db,
    resolve_followup as resolve_followup_for_db,
    update_followup as update_followup_for_db,
)
from .surface_queue import (
    build_surface_items as build_surface_items_for_db,
    build_surface_retire_keys as build_surface_retire_keys_for_db,
    build_sync_failed_item as build_sync_failed_item_for_db,
    get_surface_queue as get_surface_queue_for_db,
)


mcp = FastMCP("financial-agent")


def _list_result(items, total: int | None = None, more: int | None = None) -> dict:
    """Wrap a list tool's result as one structured block ({items, count}).

    Returning a bare list makes FastMCP explode it into one content block per
    item (e.g. 115 blocks for the onboarding queue); a dict returns a single
    clean block and adds a count.

    Pass ``total`` (the count before any limit/offset) and ``more`` (rows left
    beyond this page) when the collection was capped, so the caller gets an
    explicit pointer instead of silently seeing a truncated list as if it were
    the whole set.
    """

    items = list(items)
    result = {"items": items, "count": len(items)}
    if total is not None:
        result["total_items"] = total
    if more:
        result["more"] = more
    return result


def _resolve_as_of(as_of_date: str | None) -> str:
    """Default a missing as_of_date to today's ISO date.

    Tools accept as_of_date=None so callers (especially post-compaction, when the
    schema fell out of context) never fail just for omitting today's date.
    """

    import datetime as _dt

    return as_of_date or _dt.date.today().isoformat()


def _has_synced_sources(conn) -> bool:
    """True when the DB has SimpleFIN source tables (a sync has run).

    The daily digest reads balances/freshness/drift from the source tables; an
    app-only DB (obligations seeded, never synced) has none of them, so digest
    enrichment must be skipped rather than crashed on such a DB.
    """
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='balance_snapshots'"
        ).fetchone()
    )


@mcp.tool()
def set_goal(
    name: str,
    target_amount: float,
    deadline: str | None = None,
    source_account: str | None = None,
    note: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Create or update a savings goal with a target amount and optional deadline.

    Re-running with the same name and source_account updates the existing goal
    instead of creating a duplicate. deadline and source_account are optional; a
    goal with no deadline is treated as open-ended.
    """

    resolved_db_path = db_path or str(default_db_path())
    require_current_release(resolved_db_path)
    with guarded_write(resolved_db_path) as conn:
        result = set_goal_for_db(
            conn,
            name=name,
            target_amount=target_amount,
            deadline=deadline,
            source_account=source_account,
            note=note,
        )
    return result


@mcp.tool()
def list_goals(as_of_date: str | None = None, db_path: str | None = None) -> dict:
    """List active savings goals with progress vs target and an on-track assessment.

    Each goal reports current_progress (a manual override if set, else the
    source account's live balance, else summed matured inflows), progress_pct, a
    required_monthly_rate to hit the deadline, and a status of on_track /
    behind / due_soon / completed / no_deadline.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_goals_for_db(conn, as_of_date))
    finally:
        conn.close()


@mcp.tool()
def set_goal_override(
    goal_id: str,
    override_amount: float | None = None,
    db_path: str | None = None,
) -> dict:
    """Set or clear a manual progress override for a goal.

    Pass override_amount to force the goal's current_progress to that value
    (must be >= 0). Pass null/None to clear the override so the goal reverts to
    its live source-account balance (or matured inflows). Returns the updated
    goal with its recomputed progress.
    """

    resolved_db_path = db_path or str(default_db_path())
    with guarded_write(resolved_db_path) as conn:
        result = set_goal_override_for_db(
            conn,
            goal_id=goal_id,
            override_amount=override_amount,
        )
    return result


@mcp.tool()
def set_debt_terms(
    id: str,
    name: str,
    apr: float,
    account_query: str | None = None,
    account_id: str | None = None,
    balance_source: str = "account",
    balance_override: float | None = None,
    min_payment: float | None = None,
    is_revolving: bool = True,
    autopay: bool = False,
    note: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Create or update a debt's terms (APR, linked account, revolving flag).

    Re-running with the same id updates the existing debt instead of creating a
    duplicate. Pass account_query to resolve a synced account by name or org (an
    explicit account_id wins). Use balance_source='manual' with balance_override
    for debts with no synced account (e.g. a federal student loan). Set
    is_revolving=False for cards paid in full each month so they are excluded
    from the avalanche target order even when their APR is high.
    """

    resolved_db_path = db_path or str(default_db_path())
    with guarded_write(resolved_db_path) as conn:
        result = set_debt_terms_for_db(
            conn,
            id=id,
            name=name,
            apr=apr,
            account_query=account_query,
            account_id=account_id,
            balance_source=balance_source,
            balance_override=balance_override,
            min_payment=min_payment,
            is_revolving=is_revolving,
            autopay=autopay,
            note=note,
        )
    return result


@mcp.tool()
def list_debts(as_of_date: str | None = None, db_path: str | None = None) -> dict:
    """List debts with live balances, monthly interest, and total revolving interest.

    Each debt reports its resolved current_balance (the linked account's latest
    balance snapshot when balance_source='account', else balance_override), the
    modeled monthly_interest (abs(balance) * apr/100 / 12), its is_revolving and
    autopay flags, and min_payment. The result also carries a
    total_monthly_interest summed across the revolving debts only.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list_debts_for_db(conn, as_of_date)
    finally:
        conn.close()


@mcp.tool()
def capture_followup(
    text: str,
    surface_when: str,
    priority: str | None = None,
    linked_obligation_id: str | None = None,
    source: str = "manual",
    db_path: str | None = None,
) -> dict:
    """Capture a dated follow-up reminder in the local store (no Todoist push).

    surface_when is an ISO date; the daily job surfaces the follow-up on or after
    that date via list_due_followups. priority is high / normal / low (optional).
    Re-capturing identical text, date, priority, and source updates the existing
    follow-up instead of creating a duplicate. This writes to the DB only.
    """

    resolved_db_path = db_path or str(default_db_path())
    with guarded_write(resolved_db_path) as conn:
        result = capture_followup_for_db(
            conn,
            text=text,
            surface_when=surface_when,
            priority=priority,
            linked_obligation_id=linked_obligation_id,
            source=source,
        )
    return result


@mcp.tool()
def list_due_followups(as_of_date: str | None = None, db_path: str | None = None) -> dict:
    """List pending follow-ups due on or before as_of_date (the surfacing queue).

    Returns only pending follow-ups whose surface_when is on or before as_of_date,
    ordered by surface_when, then priority (high first). This is what the daily
    job reads to decide what to surface.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_due_followups_for_db(conn, as_of_date))
    finally:
        conn.close()


@mcp.tool()
def resolve_followup(followup_id: str, db_path: str | None = None) -> dict:
    """Mark a follow-up resolved so it stops surfacing. Idempotent."""

    resolved_db_path = db_path or str(default_db_path())
    with guarded_write(resolved_db_path) as conn:
        result = resolve_followup_for_db(conn, followup_id=followup_id)
    return result


@mcp.tool()
def update_followup(
    followup_id: str,
    text: str | None = None,
    surface_when: str | None = None,
    priority: str | None = None,
    linked_obligation_id: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Edit a dated follow-up reminder in place by id - reschedule (surface_when),
    reword (text), re-prioritize (priority), or relink (linked_obligation_id).
    Only the fields you pass change. Use this instead of re-capturing, which would
    create a new row because the capture id is derived from content.
    """

    resolved_db_path = db_path or str(default_db_path())
    with guarded_write(resolved_db_path) as conn:
        result = update_followup_for_db(
            conn,
            followup_id,
            text=text,
            surface_when=surface_when,
            priority=priority,
            linked_obligation_id=linked_obligation_id,
        )
    return result


@mcp.tool()
def get_surface_queue(
    as_of_date: str | None = None,
    limit: int = 30,
    suppress_balance_guardrails: bool = False,
    db_path: str | None = None,
) -> dict:
    """One read for the daily surfacing job: everything worth pushing today.

    Aggregates match confirmations, goals behind/due-soon, estimated amounts
    ready to refresh, stale balance-only snapshots (e.g. Apple Card), trough
    breach risk from the daily digest, and guardrail trips (cash floor / drift /
    window age) into a single prioritized list. Each item carries a type, a
    human-readable message, and a suggested Todoist due date. Read-only: it sends
    nothing and writes nothing. Returns total_items (before the limit) plus the
    top items capped at limit (default 30).

    Set ``suppress_balance_guardrails`` when the day's sync FAILED: it drops the
    balance-derived guardrail trips (cash floor / drift) that would be false on
    stale balances, while still surfacing non-balance items (due follow-ups,
    manual obligations by date, the data-freshness guardrail).

    The queue also drops cash-floor / drift items automatically when the daily
    digest says the working account's own balance date is stale, and surfaces a
    confirm-live-balance item instead.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        digest = (
            build_daily_digest_for_db(db_path=resolved_db_path, as_of_date=as_of_date)
            if _has_synced_sources(conn)
            else {}
        )
        balances = digest.get("balances") or {}
        return get_surface_queue_for_db(
            conn,
            as_of_date=as_of_date,
            limit=limit,
            suppress_balance_guardrails=suppress_balance_guardrails,
            trough_sensitivity=digest.get("trough_sensitivity"),
            working_account_balance_stale={
                "stale": balances.get("working_account_balance_date_stale"),
                "account_name": balances.get("working_account"),
                "balance_age_days": balances.get("working_account_balance_age_days"),
                "balance_date": balances.get("working_account_balance_date"),
            },
        )
    finally:
        conn.close()


@mcp.tool()
def get_finance_status(
    windows: list[int] | None = None,
    db_path: str | None = None,
    working_account_id: str | None = None,
    start_date: str | None = None,
    compact: bool = False,
) -> dict:
    """Return read-only finance status: balances, source freshness, trace ids, and stable V1 slots.

    Set compact=True to drop the per-day cash-flow event arrays (replaced by an
    events_count per window) while keeping all balance and projection summary
    stats. Use it when the full response is too large for the model context.
    """

    return build_finance_status(
        db_path=db_path,
        windows=windows,
        working_account_id=working_account_id,
        start_date=start_date,
        compact=compact,
    )


@mcp.tool()
def set_manual_balance(
    account_query: str,
    balance: float,
    as_of_date: str | None = None,
    note: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Manually correct a stale account balance for balance-only/slow feeds.

    Some feeds refresh slowly (e.g. the Apple Card portal shows "Updated
    Monthly"), so the latest synced balance can lag reality. This records a fresh
    balance snapshot (source='manual', stamped noon UTC on as_of_date) for the
    account matching account_query, so get_finance_status and get_daily_digest
    reflect it immediately.

    account_query is fuzzy-matched against account name/org. A trailing 4-digit
    mask (e.g. "PREMIER PLUS CKG (4321)") is matched exactly against account
    name/id first, so a masked query resolves unambiguously. An ambiguous match
    returns a candidate list and writes nothing; no match returns not_found. A
    later sync that records a newer snapshot supersedes the manual correction.
    """
    as_of_date = _resolve_as_of(as_of_date)

    resolved_db_path = db_path or str(default_db_path())
    with guarded_write(resolved_db_path) as conn:
        result = set_manual_balance_for_db(
            conn,
            account_query=account_query,
            balance=balance,
            as_of_date=as_of_date,
            note=note,
        )
        if result.get("status") != "ok":
            conn.rollback()
    return result


@mcp.tool()
def import_card_statement(
    text: str,
    account_query: str = "Apple Card",
    as_of_date: str | None = None,
    statement_close_date: str | None = None,
    statement_total: float | None = None,
    dry_run: bool = True,
    db_path: str | None = None,
) -> dict:
    """Import a pasted Apple Card statement (CSV or statement text) into the DB.

    The Apple Card has no live transaction feed, so card spend never reaches the
    projection. Paste a monthly download here: it parses into real transaction
    rows (source='apple_card_paste'), dedups against prior pastes via a
    deterministic synthetic id, fuzzy-matches the account, and feeds both the
    onboarding scanner and the statement-estimate rollup. When the paste carries a
    statement total, the Apple Card statement instance is promoted to that
    observed total (a protected amount the rollup never overwrites) and a sticky
    manual balance is recorded.

    Default dry_run=True: parse + preview only. Re-run with dry_run=false to write.
    as_of_date defaults to today. An ambiguous account match writes nothing and
    returns candidates; a wrong-card paste is blocked by the match floor.
    """

    import datetime as _dt

    resolved_db_path = db_path or str(default_db_path())
    resolved_as_of = as_of_date or _dt.date.today().isoformat()
    if dry_run:
        with guarded_read(resolved_db_path) as (conn, release_status):
            result = import_card_statement_for_db(
                conn,
                text=text,
                account_query=account_query,
                as_of_date=resolved_as_of,
                statement_close_date=statement_close_date,
                statement_total=statement_total,
                dry_run=True,
            )
        return {**result, "release_warning": release_status.warning}

    with guarded_write(resolved_db_path) as conn:
        result = import_card_statement_for_db(
            conn,
            text=text,
            account_query=account_query,
            as_of_date=resolved_as_of,
            statement_close_date=statement_close_date,
            statement_total=statement_total,
            dry_run=False,
        )
        if result.get("status") != "ok":
            conn.rollback()
    return result


@mcp.tool()
def import_checking_activity(
    text: str,
    account_query: str = "checking",
    as_of_date: str | None = None,
    balance: float | None = None,
    dry_run: bool = True,
    db_path: str | None = None,
) -> dict:
    """Import pasted checking-account activity (CSV) into the DB.

    The operating checking account is manual-sourced, so its activity is pasted in
    as a CSV. This parses date/description/amount rows, fuzzy-matches the account,
    stamps deterministic synthetic ids so a re-paste is idempotent, and upserts the
    new rows as real transactions (source='checking_paste'). When ``balance`` is
    given, a sticky manual balance snapshot is recorded for the same account, in
    the same db transaction as the rows.

    Default dry_run=True: parse + preview only. Re-run with dry_run=false to write.
    as_of_date defaults to today. An ambiguous account match writes nothing and
    returns candidates.
    """

    import datetime as _dt
    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    resolved_as_of = as_of_date or _dt.date.today().isoformat()
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = import_checking_activity_for_db(
            conn,
            text=text,
            account_query=account_query,
            as_of_date=resolved_as_of,
            balance=balance,
            dry_run=dry_run,
        )
        if not dry_run and result.get("status") == "ok":
            conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def list_income_sources(db_path: str | None = None) -> dict:
    """List configured income sources, schedule versions, review dates, and generated horizons."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_income_sources_for_db(conn))
    finally:
        conn.close()


@mcp.tool()
def apply_income_source(source: dict, db_path: str | None = None) -> dict:
    """Create or update an income source and schedule version after user confirmation."""

    resolved_db_path = db_path or str(default_db_path())
    with guarded_write(resolved_db_path) as conn:
        result = apply_income_source_config(conn, source)
    return result


@mcp.tool()
def import_calendar_facts(facts: list[dict], db_path: str | None = None) -> dict:
    """Import normalized calendar facts into local storage.

    This tool expects source-specific calendar import to happen before the call.
    Facts should include fact_type, date, source, and optional external_id,
    calendar_id, related entity, title, confidence, notes, and payload.
    """

    resolved_db_path = db_path or str(default_db_path())
    with guarded_write(resolved_db_path) as conn:
        result = import_calendar_facts_for_db(conn, facts)
    return result


@mcp.tool()
def list_calendar_facts(
    fact_type: str | None = None,
    start_date: str | None = None,
    through_date: str | None = None,
    status: str | None = "active",
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
    db_path: str | None = None,
) -> dict:
    """List local normalized calendar facts with optional filters."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_calendar_facts_for_db(
            conn,
            fact_type=fact_type,
            start_date=start_date,
            through_date=through_date,
            status=status,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
        ))
    finally:
        conn.close()


@mcp.tool()
def apply_obligation_instances(
    obligation: dict,
    instances: list[dict],
    db_path: str | None = None,
) -> dict:
    """Create or update an obligation and its dated instances.

    obligation keys: id (req), name (req), kind (req), source (req), autopay
    (REQUIRED here: true = the bill auto-pays itself; false = you pay it manually,
    so it surfaces as a due reminder); cadence (e.g. 'monthly'), status (default
    'active'), active_until (ISO date the bill stops projecting), amount_discretionary
    (default False - True when the modeled amount is only a floor, e.g. a card minimum).

    Each instances item: due_date (req), amount (req; negative => outflow when
    direction is omitted, stored as magnitude with the sign carried by direction),
    source (req); optional id (defaults to '<obligation_id>:<due_date>',
    auto-suffixed ':1'/':2' for multiple instances sharing a date so they never
    overwrite each other), direction ('inflow'/'outflow'), status (default
    'expected'), confidence, notes, amount_status, amount_source,
    amount_observed_at, statement_close_date, review_after, estimation_method,
    estimation_inputs (dict), cash_flow_treatment, statement_target_obligation_id.

    Returns {obligation_id, created, updated, instance_ids} so the caller can tell
    new inserts from re-applied upserts (never a silent no-op).
    """

    import sqlite3

    # Require an explicit autopay decision when a bill is created conversationally.
    # The backing function defaults autopay=True (quiet) for the auto-detectors, but
    # a hand-added bill with no decision would then silently never surface as a
    # reminder. Forcing the choice here closes that trap at the one user-facing path.
    if "autopay" not in obligation:
        raise ValueError(
            "obligation must set 'autopay': true (the bill auto-pays itself) or "
            "false (you pay it manually, so it surfaces as a due reminder). This is "
            "required so a manual bill can never silently fail to surface."
        )

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = apply_obligation_instances_for_db(
            conn,
            obligation=obligation,
            instances=instances,
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def delete_obligation_instance(
    instance_id: str,
    db_path: str | None = None,
) -> dict:
    """Soft-delete a single obligation instance by id.

    Marks the instance ``deleted`` so it drops out of cash-flow projections,
    obligation listings, reconciliation, and drift, while preserving the row and
    its history. Re-apply the instance with an explicit id to revive it.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = delete_obligation_instance_for_db(conn, instance_id)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def set_obligation_end(
    obligation_id: str,
    active_until: str | None,
    db_path: str | None = None,
) -> dict:
    """Set (or clear) the date a recurring bill stops projecting.

    A bill with a known end - a lease, a loan payoff, a subscription being
    cancelled - otherwise keeps filling the runway forever. Pass active_until
    (YYYY-MM-DD) to hard-stop its instances from the cash-flow projection on and
    after that date; pass null to clear it (open-ended again). Reversible: no
    instances are deleted, they are just excluded past the end date.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = set_obligation_end_for_db(conn, obligation_id, active_until)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def deactivate_obligation(
    obligation_id: str,
    db_path: str | None = None,
) -> dict:
    """Retire a whole obligation (status -> inactive) so all its instances drop out
    of the projection, listings, reconciliation, and drift. Rows are preserved for
    audit; idempotent. Returns projectable_instances_removed so you can see how many
    upcoming bills this pulls from the runway before relying on it.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = deactivate_obligation_for_db(conn, obligation_id)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def suppress_contradicted_estimates(
    as_of_date: str | None = None,
    mode: str = "report",
    options: dict | None = None,
    db_path: str | None = None,
) -> dict:
    """Lower or suppress an averaged estimate the account's real burn contradicts.

    The "shrunk but not dead" companion to dormancy suppression: an averaged
    auto-estimate (e.g. a card payment) that keeps projecting at full size while
    the account's actual merchant burn has collapsed. Compares modeled monthly
    outflow against observed burn over a lookback window; on a sustained
    contradiction it either rewrites the instance amount down to the observed
    figure (keeping the obligation active and projectable) or, when burn is near
    zero, routes it to dormant. Both paths are reversible and emit a low-severity
    drift finding.

    mode='report' (default) emits findings but mutates nothing -- the observe
    posture for the first live run. mode='enforce' applies the resolution. Tunable
    thresholds (contradiction_ratio, flat_balance_ratio, modeled_floor,
    contradiction_cycles, contradiction_lookback_days, near_zero_monthly) go in
    options.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    opts = {**(options or {}), "mode": mode}
    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = suppress_contradicted_estimates_for_db(
            conn, as_of_date=as_of_date, options=opts
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def list_obligations(
    obligation_id: str | None = None,
    name_contains: str | None = None,
    kind: str | None = None,
    status: str | None = "active",
    include_instances: bool = True,
    instances_start: str | None = None,
    instances_through: str | None = None,
    compact: bool = False,
    full: bool = False,
    limit: int | None = None,
    offset: int | None = None,
    db_path: str | None = None,
) -> dict:
    """List local canonical obligations and optionally their dated instances.

    Pass obligation_id to fetch just one obligation (any status), or
    name_contains to substring-match on name/id, instead of dumping the whole
    roster. instances_start/instances_through (YYYY-MM-DD) window the instances
    by due date - prefer a window whenever include_instances is true. Instance
    rows are compact by default (due date, amount, status, confidence); set
    full=true for estimation provenance and notes. Set compact=True to replace
    each obligation's instances array with an instance_count. Use limit/offset
    to page the roster; when a limit truncates it, the result carries
    total_items and a "more" count instead of silently dropping the rest.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = list_obligations_for_db(
            conn,
            obligation_id=obligation_id,
            name_contains=name_contains,
            kind=kind,
            status=status,
            include_instances=include_instances,
            instances_start=instances_start,
            instances_through=instances_through,
            instances_summary=not full,
            compact=compact,
        )
        total = len(rows)
        # ponytail: page in Python after the DB read - the obligation roster is
        # tens of rows, so slicing here is fine; push LIMIT/OFFSET into the SQL
        # only if the roster ever grows large enough to matter.
        start = offset or 0
        paged = rows[start:]
        if limit is not None:
            paged = paged[:limit]
        more = total - (start + len(paged))
        return _list_result(paged, total=total, more=more)
    finally:
        conn.close()


@mcp.tool()
def list_obligation_review_candidates(
    as_of_date: str | None = None,
    db_path: str | None = None,
) -> dict:
    """List obligation instances that need review, such as estimated amounts due for refresh."""
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_obligation_review_candidates_for_db(
            conn,
            as_of_date=as_of_date,
        ))
    finally:
        conn.close()


@mcp.tool()
def list_statement_input_estimates(
    target_obligation_id: str | None = None,
    start_date: str | None = None,
    through_date: str | None = None,
    status: str | None = "expected",
    limit: int | None = None,
    offset: int | None = None,
    full: bool = False,
    db_path: str | None = None,
) -> dict:
    """List card-spend inputs that feed future statement estimates without
    directly reducing checking. Rows are compact by default (instance id,
    obligation, due date, amount, status, confidence); set full=true for
    estimation provenance and notes. Use limit/offset to page.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_statement_input_estimates_for_db(
            conn,
            target_obligation_id=target_obligation_id,
            start_date=start_date,
            through_date=through_date,
            status=status,
            limit=limit,
            offset=offset,
            summary=not full,
        ))
    finally:
        conn.close()


@mcp.tool()
def generate_income_instances(
    start_date: str,
    through_date: str,
    db_path: str | None = None,
    extra_closure_dates: list[str] | None = None,
) -> dict:
    """Generate dated income obligation instances from configured income schedules.

    extra_closure_dates accepts YYYY-MM-DD dates imported from a payroll or
    calendar source for one-off local closures.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = generate_income_instances_for_db(
            conn,
            start_date=start_date,
            through_date=through_date,
            extra_closure_dates=extra_closure_dates,
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def scan_charge_onboarding_candidates(
    options: dict | None = None,
    db_path: str | None = None,
) -> dict:
    """Scan transaction history and discover reviewable charge-pattern candidates.

    This is the background-discovery entry point: it groups related transactions
    by merchant and account, proposes schedule/amount/cash-impact/review
    policies, and stores durable candidates in a review queue. It is idempotent
    and never writes canonical obligations or moves cash flow. Options support
    min_evidence, include_inflows, and link_existing_obligations.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = scan_charge_onboarding_candidates_for_db(conn, options=options)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def list_charge_onboarding_queue(
    status: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    include_resolved: bool = False,
    full: bool = False,
    db_path: str | None = None,
) -> dict:
    """List charge-onboarding candidates ordered by estimated monthly cash impact.

    By default returns only the active queue (candidates still awaiting a
    decision) as compact rows (id, merchant, amount, cadence, confidence,
    status). Set full=true for evidence transactions, policies, and notes -
    prefer full=true only with a small limit. Use limit/offset to page. Pass
    status to filter exactly, or include_resolved=True to see decided/paused
    candidates such as deferred or rejected ones.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_charge_onboarding_queue_for_db(
            conn,
            status=status,
            limit=limit,
            offset=offset,
            include_resolved=include_resolved,
            summary=not full,
        ))
    finally:
        conn.close()


@mcp.tool()
def get_next_charge_onboarding_candidate(db_path: str | None = None) -> dict | None:
    """Return the single highest-priority unresolved charge-onboarding candidate."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return get_next_charge_onboarding_candidate_for_db(conn)
    finally:
        conn.close()


def _record_onboarding_decision(conn, candidate_id: str, decision: dict | str) -> dict:
    """Record one decision + the surfaced-reminder cleanup hook. Caller commits.

    Auto-cleanup (spec section 2): a rejected/deferred candidate flags its linked
    obligation's surfaced due-date reminders for removal on the next live surface
    run; an accepted/reset one clears any earlier tombstone so a re-surfaced item
    is not immediately retired again.
    """
    result = record_charge_onboarding_decision_for_db(conn, candidate_id, decision)
    existing_obligation_id = result.get("existing_obligation_id")
    if existing_obligation_id:
        status = result.get("status")
        prefix = f"obligation-due:{existing_obligation_id}:"
        if status in ("rejected", "deferred"):
            request_emission_retire_prefix(conn, prefix)
        elif status in ("proposed", "accepted"):
            conn.execute(
                "UPDATE todoist_emissions SET retire_requested_at = NULL "
                "WHERE surface_key LIKE ? || '%' AND status = 'open'",
                (prefix,),
            )
    return result


@mcp.tool()
def record_charge_onboarding_decision(
    candidate_id: str,
    decision: dict | str,
    db_path: str | None = None,
) -> dict:
    """Record a review decision against a charge-onboarding candidate.

    Supported decisions: defer, reject, park, needs_more_evidence, in_review,
    accept, reset. The accept decision only marks a candidate ready; the
    canonical write happens in apply_charge_onboarding_candidate, so apply is
    rejected here. Restructuring (merge/split/edit) is also rejected.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = _record_onboarding_decision(conn, candidate_id, decision)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def record_charge_onboarding_decisions(
    decisions: list[dict],
    db_path: str | None = None,
) -> dict:
    """Record review decisions for MANY charge-onboarding candidates in one call.

    Each item is {"candidate_id": str, "decision": <str|dict>} where decision is an
    action string ("defer"/"reject"/...) or {"action": ...}, same as the single
    record_charge_onboarding_decision. This clears the one-at-a-time grind on a big
    queue. Items are applied independently in one transaction; a bad item is
    reported as an error and does NOT abort the rest. Returns
    {total, applied, failed, results:[{candidate_id, ok, status|error}]}.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    results: list[dict] = []
    applied = failed = 0
    try:
        for item in decisions or []:
            cid = (item or {}).get("candidate_id")
            dec = (item or {}).get("decision")
            if not cid:
                failed += 1
                results.append({"candidate_id": cid, "ok": False, "error": "missing candidate_id"})
                continue
            try:
                res = _record_onboarding_decision(conn, cid, dec)
                applied += 1
                results.append({"candidate_id": cid, "ok": True, "status": res.get("status")})
            except Exception as exc:  # noqa: BLE001 - record per item, never abort the batch
                failed += 1
                results.append({"candidate_id": cid, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]})
        conn.commit()
        return {"total": len(decisions or []), "applied": applied, "failed": failed, "results": results}
    finally:
        conn.close()


@mcp.tool()
def preview_charge_onboarding_apply(
    candidate_id: str,
    start_date: str | None = None,
    through_date: str | None = None,
    horizon_days: int = 180,
    obligation_id: str | None = None,
    amount_override: float | None = None,
    cadence_override: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Preview the canonical obligation and dated instances that applying would create.

    Read-only and writes nothing. Use this to show a reviewer exactly what would
    land in the cash-flow model (obligation, instances, schedule summary, and
    warnings) before committing to apply. Pass amount_override and/or
    cadence_override (e.g. 'monthly') to preview a corrected figure when the
    detector misread the amount or cadence.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return preview_charge_onboarding_apply_for_db(
            conn,
            candidate_id,
            start_date=start_date,
            through_date=through_date,
            horizon_days=horizon_days,
            obligation_id=obligation_id,
            amount_override=amount_override,
            cadence_override=cadence_override,
        )
    finally:
        conn.close()


@mcp.tool()
def apply_charge_onboarding_candidate(
    candidate_id: str,
    start_date: str | None = None,
    through_date: str | None = None,
    horizon_days: int = 180,
    obligation_id: str | None = None,
    require_accepted: bool = True,
    amount_override: float | None = None,
    cadence_override: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Promote an accepted candidate into a canonical obligation plus dated instances.

    This is the guarded write that turns a reviewed candidate into cash-flow
    truth. By default the candidate must already be accepted (record an accept
    decision first). Writing is idempotent: re-applying the same window updates
    instances in place instead of duplicating them. Pass amount_override and/or
    cadence_override to correct a detector misread in this one call instead of
    rejecting and re-modeling.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = apply_charge_onboarding_candidate_for_db(
            conn,
            candidate_id,
            start_date=start_date,
            through_date=through_date,
            horizon_days=horizon_days,
            obligation_id=obligation_id,
            require_accepted=require_accepted,
            amount_override=amount_override,
            cadence_override=cadence_override,
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def aggregate_statement_inputs(
    target_obligation_id: str,
    db_path: str | None = None,
) -> dict:
    """Group card-statement-input charges into the statement cycle that pays them.

    Deterministic and idempotent. Builds statement cycles from the target
    obligation's statement instances (those with a statement close date) and
    assigns each card input to its cycle, reporting any inputs that fall past the
    last known statement close as unrolled.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = aggregate_statement_inputs_for_db(conn, target_obligation_id=target_obligation_id)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def list_statement_cycles(
    target_obligation_id: str,
    db_path: str | None = None,
) -> dict:
    """List statement cycles for a card obligation with their aggregated card-input evidence."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_statement_cycles_for_db(conn, target_obligation_id=target_obligation_id))
    finally:
        conn.close()


@mcp.tool()
def get_statement_status(
    obligation_id: str,
    as_of_date: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Show the latest closed statement and current open-cycle pace for a card obligation.

    Refreshes the card-input rollup first, then reports the most recent closed
    statement, the open cycle's spend so far, modeled amount, variance, and
    whether spend is ahead of or behind the modeled pace.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = get_statement_status_for_db(
            conn, obligation_id=obligation_id, as_of_date=as_of_date
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def recompute_statement_estimates(
    target_obligation_id: str,
    baseline: float | None = None,
    db_path: str | None = None,
) -> dict:
    """Fill unconfirmed statement estimates from the card-input rollup, guarded.

    Only statement instances whose amount is an unconfirmed projection are
    recomputed, as baseline (expected non-modeled card spend) plus the rolled-up
    modeled card inputs for that cycle. Portal/observed amounts are never
    overwritten. With no baseline the existing estimate is preserved (not lowered
    to inputs-only). Idempotent.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = recompute_statement_estimates_for_db(
            conn, target_obligation_id=target_obligation_id, baseline=baseline
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def set_statement_actual(
    obligation_id: str,
    amount: float,
    cycle_close_date: str | None = None,
    due_date: str | None = None,
    source: str = "portal_statement_amount",
    note: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Record an observed statement balance on the matching statement instance.

    The direct-entry path for a portal-read statement amount (e.g. the monthly
    Apple Card balance, which has no transaction feed). Pick the instance by
    cycle_close_date (YYYY-MM-DD statement close) or due_date; the amount is
    written as confirmed with provenance and is never overwritten by the rollup
    estimator. Errors list the known cycles when nothing matches.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = set_statement_actual_for_db(
            conn,
            obligation_id=obligation_id,
            amount=amount,
            cycle_close_date=cycle_close_date,
            due_date=due_date,
            source=source,
            note=note,
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def reconcile_obligation_instances(
    as_of_date: str | None = None,
    options: dict | None = None,
    db_path: str | None = None,
) -> dict:
    """Match expected obligation instances against observed transactions.

    Deterministic and idempotent. Records the best transaction match per instance
    as review evidence (amount + date + merchant scoring). Conservative by
    default: matches are not silently marked paid (set options.auto_mark_paid to
    opt in), unmatched past-grace instances become needs_review not overdue (set
    options.flag_unmatched_needs_review to opt in), and card-statement-input
    instances are skipped (they settle via the statement, not a checking match).
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = reconcile_obligation_instances_for_db(conn, as_of_date=as_of_date, options=options)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def list_matched_obligation_instances(
    match_type: str | None = None,
    db_path: str | None = None,
) -> dict:
    """List obligation instances matched to a transaction, with score and evidence."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_matched_obligation_instances_for_db(conn, match_type=match_type))
    finally:
        conn.close()


@mcp.tool()
def list_unmatched_obligation_instances(
    past_grace_only: bool = False,
    db_path: str | None = None,
) -> dict:
    """List expected obligation instances with no matching transaction (drift inputs)."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_unmatched_obligation_instances_for_db(conn, past_grace_only=past_grace_only))
    finally:
        conn.close()


@mcp.tool()
def detect_drift(
    as_of_date: str | None = None,
    options: dict | None = None,
    persist: bool = True,
    db_path: str | None = None,
) -> dict:
    """Detect evidence-backed drift findings, ordered by severity.

    Finds missing expected obligations (past due, no matching transaction),
    stale estimates (review date passed), amount changes (charge happened but
    differs materially), and unexpected recurring charges (discovered by
    onboarding but not yet modeled). Deterministic and idempotent. When persist
    is true, findings are upserted and disappeared ones marked resolved.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = detect_drift_for_db(conn, as_of_date=as_of_date, options=options, persist=persist)
        if persist:
            conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def list_drift_findings(
    status: str | None = "active",
    finding_type: str | None = None,
    db_path: str | None = None,
) -> dict:
    """List stored drift findings, filtered by status (default active) and type."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_drift_findings_for_db(conn, status=status, finding_type=finding_type))
    finally:
        conn.close()


@mcp.tool()
def execute_action_outbox(
    options: dict | None = None,
    db_path: str | None = None,
) -> dict:
    """Process outbox items. Live Todoist sending is gated OFF by default.

    Dry-run items are always simulated. Pending items send to Todoist ONLY when
    TODOIST_WRITE_ENABLED is set in the finances .env AND a token + project id are
    configured; otherwise they are marked awaiting-integration (no external call).
    When enabled, sending is idempotent - one task per outbox key, updated in
    place on rerun rather than duplicated.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = execute_action_outbox_for_db(conn, options=options)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def create_todoist_task(
    content: str,
    due_string: str | None = None,
    due_date: str | None = None,
    description: str | None = None,
    priority: int | None = None,
    project_id: str | None = None,
) -> dict:
    """Create a free-form Todoist task (e.g. a one-off reminder) in the Finance project.

    Live Todoist write-back is gated OFF by default. The task is created ONLY when
    TODOIST_WRITE_ENABLED is set in the finances .env AND a token + project id are
    configured; otherwise this makes no external call and returns
    {"status": "awaiting-integration", "sent": false, "reason": ...}.

    This is a direct create, not an idempotent outbox row: a one-off reminder has no
    stable key, so calling twice creates two tasks. Use the review-batch path for
    work that must dedupe.

    Args:
        content: Task title (required).
        due_string: Natural-language due date (e.g. "today", "Jul 28").
        due_date: ISO yyyy-mm-dd due date; wins over due_string when both are given.
        description: Optional task body.
        priority: Todoist priority 1-4.
        project_id: Target project; defaults to the configured finance project.

    Returns on success: {"status": "created", "sent": true, "task_id", "url", "content"}.
    """

    return create_todoist_task_impl(
        content,
        due_string=due_string,
        due_date=due_date,
        description=description,
        priority=priority,
        project_id=project_id,
    )


@mcp.tool()
def update_todoist_task(
    task_id: str,
    content: str | None = None,
    due_string: str | None = None,
    due_date: str | None = None,
    description: str | None = None,
    priority: int | None = None,
    project_id: str | None = None,
) -> dict:
    """Update an existing Todoist task in place, editing only the provided fields.

    Live Todoist write-back is gated OFF by default. The task is updated ONLY when
    TODOIST_WRITE_ENABLED is set in the finances .env AND a token is configured;
    otherwise this makes no external call and returns
    {"status": "awaiting-integration", "sent": false, "reason": ...}.

    Any argument left unset is omitted from the request, so a partial update never
    clears an untouched field. The task is addressed by id, so no project id is
    needed unless you pass `project_id` to move the task to another project.

    Args:
        task_id: Id of the task to update (required).
        content: New task title.
        due_string: Natural-language due date (e.g. "today", "Jul 28").
        due_date: ISO yyyy-mm-dd due date; wins over due_string when both are given.
        description: New task body (pass "" to clear it).
        priority: Todoist priority 1-4.
        project_id: Move the task to this project.

    Returns on success: {"status": "updated", "sent": true, "task_id", "url"}.
    """

    return update_todoist_task_impl(
        task_id,
        content=content,
        due_string=due_string,
        due_date=due_date,
        description=description,
        priority=priority,
        project_id=project_id,
    )


@mcp.tool()
def complete_todoist_task(task_id: str) -> dict:
    """Complete (close) an existing Todoist task by id.

    Live Todoist write-back is gated OFF by default. The task is closed ONLY when
    TODOIST_WRITE_ENABLED is set in the finances .env AND a token is configured;
    otherwise this makes no external call and returns
    {"status": "awaiting-integration", "sent": false, "reason": ...}.

    Args:
        task_id: Id of the task to complete (required).

    Returns on success: {"status": "completed", "sent": true, "task_id"}.
    """

    return complete_todoist_task_impl(task_id)


@mcp.tool()
def reopen_todoist_task(task_id: str) -> dict:
    """Reopen (un-complete) an existing Todoist task by id.

    Live Todoist write-back is gated OFF by default. The task is reopened ONLY when
    TODOIST_WRITE_ENABLED is set in the finances .env AND a token is configured;
    otherwise this makes no external call and returns
    {"status": "awaiting-integration", "sent": false, "reason": ...}.

    Args:
        task_id: Id of the task to reopen (required).

    Returns on success: {"status": "reopened", "sent": true, "task_id"}.
    """

    return reopen_todoist_task_impl(task_id)


@mcp.tool()
def delete_todoist_task(task_id: str) -> dict:
    """Delete an existing Todoist task by id.

    Live Todoist write-back is gated OFF by default. The task is deleted ONLY when
    TODOIST_WRITE_ENABLED is set in the finances .env AND a token is configured;
    otherwise this makes no external call and returns
    {"status": "awaiting-integration", "sent": false, "reason": ...}. A task that
    is already gone (404) is treated as success, so the delete is idempotent.

    Args:
        task_id: Id of the task to delete (required).

    Returns on success: {"status": "deleted", "sent": true, "task_id"}.
    """

    return delete_todoist_task_impl(task_id)


@mcp.tool()
def surface_due_items_to_todoist(
    as_of_date: str | None = None,
    db_path: str | None = None,
    sync_failed: bool = False,
) -> dict:
    """Push today's due items to Todoist with automatic de-duplication.

    Builds the day's surfaceable items (due follow-ups, goals behind pace,
    estimated amounts past review, stale balance-only snapshots) and UPSERTS each
    against the todoist_emissions ledger keyed by a stable surface_key. The same
    item maps to the same Todoist task across days and re-runs: a new item is
    created (with a [fa:<key>] marker and the fa-auto label), an unchanged item is
    skipped, a changed item updates the same task in place, and a task the user
    completed or deleted is treated as resolved and never recreated.

    ``sync_failed`` (default off): set it when the day's run_background_sync FAILED.
    Balances are then stale, so the daily routine drops cash-floor / drift items
    from the read queue (suppress_balance_guardrails); this tool builds items
    itself, so it prepends ONE "Data sync failed - balances stale" item
    (surface_key data-sync-failed:<as_of_date>, highest priority) here instead of
    the caller passing an items array. The emissions ledger dedupes it on a
    same-day re-run like any other item.

    Live Todoist write-back is gated OFF by default. With the gate closed (no
    TODOIST_WRITE_ENABLED, token, or project) this makes no external call, leaves
    the ledger untouched, and returns status "awaiting-integration". Returns a
    summary with created / updated / skipped / resolved / failed counts.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Headline enrichment needs synced balances; on an app-only DB (obligations
        # seeded, no sync yet) there is no balance/health read, so surface the bare
        # due items instead of crashing on the digest build.
        if _has_synced_sources(conn):
            digest = build_daily_digest_for_db(db_path=resolved_db_path, as_of_date=as_of_date)
            headline = summarize_daily_digest_for_db(digest).get("headline")
        else:
            headline = None
        items = build_surface_items_for_db(conn, as_of_date=as_of_date, headline=headline)
        retire_keys = build_surface_retire_keys_for_db(conn, as_of_date=as_of_date)
        if sync_failed:
            # Prepend the stale-data flag so it leads the push; the ledger dedupes
            # it on a same-day re-run.
            items = [build_sync_failed_item_for_db(as_of_date), *items]
        result = surface_to_todoist_for_db(conn, items, as_of_date, retire_keys=retire_keys)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def reconcile_todoist_emission(
    surface_key: str,
    todoist_task_id: str,
    content_hash: str,
    db_path: str | None = None,
) -> dict:
    """Adopt an existing Todoist task that carries a [fa:<key>] marker.

    Call this when a task for a surface_key already exists in Todoist (created by
    hand or by a prior install) but has no ledger row yet. Inserting the ledger
    row makes future surface_due_items_to_todoist runs skip the task instead of
    creating a duplicate. Writes only to the local ledger; no external call.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = reconcile_emission_for_db(conn, surface_key, todoist_task_id, content_hash)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def reconcile_todoist_completions(
    as_of_date: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Map user-completed/deleted Todoist tasks back to the emissions ledger.

    Closes the re-nag gap: when the user checks off or deletes a surfaced task,
    nothing else records that, so the next surface run recreates it. This checks
    each open emission against its live Todoist task (GET /tasks/<id>): a 404 means
    the task was completed or deleted; a returned task that is checked/completed
    means the user closed it. Either way the emission is marked resolved (so
    surface_due_items_to_todoist will not recreate it) and any follow-up linked by
    a followup:<id> surface_key is resolved too.

    Gated by TODOIST_WRITE_ENABLED like the other Todoist calls. With the gate off
    (no token/flag) this makes no external call and no-ops. The Todoist read is
    read-only; the only writes are to the local ledger / follow-ups. Returns
    checked / resolved / followups_resolved / still_open / failed counts.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = reconcile_todoist_completions_for_db(conn, as_of_date=as_of_date)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def reconcile_todoist_project(
    as_of_date: str | None = None,
    apply: bool = False,
    db_path: str | None = None,
) -> dict:
    """LIST the whole Finance project, classify every task, and clean up drift.

    Closes the root gap that lets the Finance project drift (tasks were created
    but never retired, and there was no server-side LIST). This pages through every
    task in the project and classifies each as
    managed, stale_applied (a legacy "Onboard charge:" task), duplicate,
    fa_auto_orphan, or kept. A task is deletable ONLY if it matches one of three
    explicit rules - a fa-auto-labelled task we lost track of, a legacy onboarding
    task whose candidate is already decided, or a duplicate copy of a managed/
    fa-auto survivor. Everything else (ritual reminders, hand-made tasks) is kept
    and never deleted.

    ``apply`` defaults to false (dry-run): the report shows what WOULD be deleted
    with no external call. Apply (real deletes + ledger retire) requires live
    write-back (TODOIST_WRITE_ENABLED + token + project); with the gate off it
    returns the report with applied=false and reason "awaiting-integration" and
    mutates nothing. A truncated or failed LIST forces report-only (zero deletes,
    zero ledger resolutions) because duplicate and ledger-orphan inference are
    unsound on a partial view. Idempotent: a second run over a cleaned project
    finds nothing to delete.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = reconcile_todoist_project_for_db(conn, as_of_date=as_of_date, apply=apply)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def list_todoist_project(
    as_of_date: str | None = None,
    db_path: str | None = None,
) -> dict:
    """READ-ONLY LIST + classify of the whole Finance project (no delete, no apply).

    The read-only sibling of ``reconcile_todoist_project``: it pages through every
    task in the Finance project and classifies each as managed, stale_applied,
    duplicate, fa_auto_orphan, or kept, returning the SAME report shape. It has NO
    delete capability and NO apply path - ``applied`` is always false and every
    ``actions`` count is zero - so it can never mutate Todoist or the local ledger.
    Tasks a cleanup WOULD remove still show as ``would_delete`` for visibility, but
    nothing is deleted. Each task entry carries ``content``, ``due_date``,
    ``labels``, and ``description``, so there is no need to hit the raw Todoist
    API for those fields.

    Use this for board reads under the finance-scoped read-only permission profile;
    use the delete-capable ``reconcile_todoist_project`` (kept prompting) when you
    actually intend to clean up. A truncated or failed LIST is reported via the
    ``truncated`` / ``status`` fields exactly as the reconcile path reports it.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list_todoist_project_for_db(conn, as_of_date=as_of_date)
    finally:
        conn.close()


@mcp.tool()
def list_today_tasks_all_projects(
    as_of_date: str | None = None,
    db_path: str | None = None,
) -> dict:
    """READ-ONLY: Todoist tasks due today or overdue across ALL projects.

    Companion to ``list_todoist_project`` (which reads only the Finance project):
    this catches finance-relevant tasks filed under Personal or other projects,
    which the Finance-only board read cannot see. Each task carries ``content``,
    ``project_id``, ``due_date``, ``labels``, and an ``is_finance_project`` flag.
    Relevance is left to you to judge from the content, so a finance task filed
    elsewhere is never re-hidden by an over-eager keyword filter. No writes.
    """

    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list_today_tasks_all_projects_for_db(conn, as_of_date=as_of_date)
    finally:
        conn.close()


@mcp.tool()
def list_action_outbox(
    status: str | None = None,
    db_path: str | None = None,
) -> dict:
    """List durable action-outbox items (intended external writes) and their status."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_action_outbox_for_db(conn, status=status))
    finally:
        conn.close()


@mcp.tool()
def run_background_sync(
    as_of_date: str | None = None,
    options: dict | None = None,
    run_type: str = "daily_sync",
    trigger_type: str = "manual",
    db_path: str | None = None,
) -> dict:
    """Run the finance pipeline as one auditable background run.

    Orchestrates: scan charge candidates, reconcile transactions, detect drift,
    suppress dormant estimates, and surface the day's due items to Todoist
    (de-duped, gated off by default). Records a run record plus an ordered
    operation-event log. A failing step is logged and the run continues
    (partial_success). Returns the run id, trace id, status, and step summaries.

    NOTE: the surface_due_items step here is DELIBERATELY gated off (its summary
    shows awaiting-integration / created:0) unless options carries a "surface"
    dict, e.g. options={"surface": {"write_enabled": true}} to resolve the
    Todoist gate from the finances .env. For the live daily push, prefer calling
    surface_due_items_to_todoist directly.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = run_background_sync_for_db(
            conn, as_of_date=as_of_date, options=options, run_type=run_type, trigger_type=trigger_type
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def get_background_run(run_id: str, db_path: str | None = None) -> dict | None:
    """Return a background run record plus its ordered operation-event log."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return get_background_run_for_db(conn, run_id)
    finally:
        conn.close()


@mcp.tool()
def list_background_runs(
    run_type: str | None = None,
    status: str | None = None,
    limit: int = 20,
    db_path: str | None = None,
) -> dict:
    """List recent background runs with their status and timing."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_background_runs_for_db(conn, run_type=run_type, status=status, limit=limit))
    finally:
        conn.close()


@mcp.tool()
def get_version() -> dict:
    """Report the version and git commit of the code this server is RUNNING.

    The MCP server is long-running: code merged to main only takes effect after a
    restart, so a live session can keep serving stale logic. These values are
    captured once at process startup, so they describe the running process - use
    them to confirm which code is actually live. ``running_commit`` is "unknown"
    when the server runs from a non-git checkout. Read-only.
    """

    return {
        "version": build_info.VERSION,
        "running_commit": build_info.RUNNING_COMMIT,
        "running_dirty": build_info.RUNNING_DIRTY,
        "started_at": build_info.STARTED_AT,
    }


@mcp.tool()
def get_job_health(
    as_of_date: str | None = None,
    stale_threshold_hours: int = 26,
    db_path: str | None = None,
) -> dict:
    """Report whether the daily sync job is alive based on its last completed run.

    A silently-stopped scheduler is invisible - nothing fails, the data just ages.
    This turns the absence of a recent successful daily run into a visible signal:
    when the last completed run is older than ``stale_threshold_hours`` (default 26h)
    the job is flagged stale. ``as_of_date`` defaults to today, so the daily
    health-check can call this with no arguments. Read-only.
    """

    import datetime as _dt
    import sqlite3

    resolved_as_of = as_of_date or _dt.date.today().isoformat()
    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return get_job_health_for_db(
            conn, as_of_date=resolved_as_of, stale_threshold_hours=stale_threshold_hours
        )
    finally:
        conn.close()


@mcp.tool()
def run_verification(
    as_of_date: str | None = None,
    persist: bool = True,
    db_path: str | None = None,
) -> dict:
    """Run the deterministic consistency checks over the local finance model.

    The grounding gate proves each headline number traces to a source row; this
    proves the rows tie together. Checks (all pure code, no model, so a finding
    is a real broken identity, not a guess): the projection's ending balance
    equals its own signed events; no obligation has two projectable instances on
    one due date; each statement cycle's rollup matches its input rows; and
    projectable amounts are non-negative (direction carries the sign). Returns a
    summary plus each finding. With persist=True (default) findings are written
    to verification_findings for later listing; pass persist=False for a
    read-only check. This runs automatically inside run_background_sync; call it
    directly to re-check after a correction.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = run_verification_for_db(conn, as_of_date=as_of_date, persist=persist)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def list_verification_findings(
    status: str | None = "open",
    check_id: str | None = None,
    source: str | None = None,
    limit: int = 100,
    db_path: str | None = None,
) -> dict:
    """List persisted verification findings (consistency / review flags), newest first.

    Defaults to open findings; pass status=null for every status, or a check_id
    to filter to one check (projection_identity / duplicate_instances /
    statement_identity / instance_sign_sanity, or an 'adversarial:<area>' check).
    Filter by source to separate the two producers: 'deterministic' for the
    pure-code identity checks, 'adversarial' for the spawned-reviewer's advisory
    flags. This is the read to confirm a correction cleared a finding after
    re-running run_verification.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(
            list_verification_findings_for_db(
                conn, status=status, check_id=check_id, source=source, limit=limit
            )
        )
    finally:
        conn.close()


@mcp.tool()
def acknowledge_verification_findings(
    finding_ids: list[str] | None = None,
    check_id: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Acknowledge open verification findings so future runs report only NEW ones.

    Acknowledged findings keep their row and stay listable
    (list_verification_findings status='acknowledged') but stop flipping the
    verify summary's ok and stop counting as new in run_verification /
    run_background_sync; when the underlying identity is fixed they still resolve
    automatically. Pass finding_ids to acknowledge specific findings (any
    severity, an explicit per-id decision). Without ids this blanket-acknowledges
    open warn findings (optionally one check_id) and deliberately skips
    error-severity findings - errors need explicit ids so they cannot be silenced
    in bulk.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = acknowledge_verification_findings_for_db(
            conn, finding_ids=finding_ids, check_id=check_id
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def run_adversarial_review(
    as_of_date: str | None = None,
    persist: bool = True,
    model: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Spawn an independent Claude reviewer to sanity-check the riskiest model rows.

    Advisory, attention-routing only - the findings are NOT verdicts. This spawns
    the Claude Code CLI ('claude -p') as a read-only subprocess on the user's
    Claude subscription (OAuth; no Anthropic API key is used) and asks it to try
    to refute the highest-leverage parts of the forecast: the estimated,
    low-confidence outflows on the projected low point; the large estimated
    obligations that move the projection; and the freshly-classified
    recurring-charge candidates and their evidence.

    Fail-open: if the claude CLI is missing, errors, times out, or returns
    unparseable output, this returns available=False and writes nothing. With
    persist=True (default) the reviewer's flags are reconciled into
    verification_findings tagged source='adversarial' (resolve-on-clear, scoped
    so it never touches the deterministic checks) and surface in the daily digest.
    This runs automatically inside run_background_sync only when
    FINANCE_AGENT_ADVERSARIAL is enabled; call it directly to review on demand.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = run_adversarial_review_for_db(
            conn, as_of_date=as_of_date, persist=persist, model=model
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def write_finance_memory(
    text: str | None = None,
    metadata: dict | None = None,
    kind: str = "note",
    source: str | None = None,
    db_path: str | None = None,
    content: str | None = None,
) -> dict:
    """Store a finance memory (a correction, decision, or fact to recall later).

    Idempotent by (kind, source, text). Use kind to scope memories, e.g.
    'decision', 'correction', 'fact'. 'content' is accepted as an alias for
    'text' (a common first-call mistake); one of the two is required.
    """

    text = text or content
    if not text:
        raise ValueError("write_finance_memory requires 'text' (or its alias 'content')")

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = write_memory_for_db(conn, text=text, metadata=metadata, kind=kind, source=source)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def search_finance_memory(
    query: str,
    k: int = 5,
    min_score: float = 0.05,
    max_tokens: int = 1500,
    kind: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Retrieve the most relevant finance memories for a query, under a context policy.

    Records are scored by similarity, then filtered by min_score, capped at k,
    and bounded by a max_tokens budget. The result reports how many records each
    limit dropped so the amount of memory entering context is explicit.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return search_memory_for_db(
            conn, query=query, k=k, min_score=min_score, max_tokens=max_tokens, kind=kind
        )
    finally:
        conn.close()


@mcp.tool()
def list_finance_memories(
    kind: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> dict:
    """List stored finance memories, optionally filtered by kind."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_memories_for_db(conn, kind=kind, limit=limit))
    finally:
        conn.close()


@mcp.tool()
def delete_finance_memory(memory_id: str, db_path: str | None = None) -> dict:
    """Delete a finance memory by id (e.g. when a correction is no longer true)."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = delete_memory_for_db(conn, memory_id=memory_id)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def apply_obligation_migration(
    path: str,
    source: str = "obligations_yaml",
    dry_run: bool = True,
    options: dict | None = None,
    db_path: str | None = None,
) -> dict:
    """Migrate the complete obligation set from a legacy source into canonical rows.

    source is 'obligations_yaml' (trusted, machine-readable) or 'cashflow_md'
    (stale narrative, imported as needs_review). Instance-level dedup skips
    anything already modeled; ambiguous rows become needs_review. dry_run (the
    default) computes the full plan and writes nothing. Reads the legacy file
    read-only.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = apply_obligation_migration_for_db(
            conn, source=source, path=path, dry_run=dry_run, options=options
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def evaluate_guardrails(
    as_of_date: str | None = None,
    persist: bool = False,
    db_path: str | None = None,
) -> dict:
    """Evaluate operating guardrails (cash floor, drift threshold, window age, debt avalanche).

    Returns findings ordered by severity. Reads balances and drift from the DB.
    When persist is true, records the evaluation (pass/fail per rule).
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = evaluate_guardrails_for_db(conn, as_of_date=as_of_date, persist=persist)
        if persist:
            conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def list_guardrail_findings(
    evaluation_date: str | None = None,
    rule_type: str | None = None,
    db_path: str | None = None,
) -> dict:
    """List recorded guardrail evaluations, optionally filtered by date or rule."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _list_result(list_guardrail_findings_for_db(conn, evaluation_date=evaluation_date, rule_type=rule_type))
    finally:
        conn.close()


@mcp.tool()
def apply_guardrail_rules(db_path: str | None = None) -> dict:
    """Idempotently seed the default guardrail rules into the database."""

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = apply_guardrail_rules_for_db(conn)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def sync_simplefin(
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int = 45,
    incremental: bool = False,
    db_path: str | None = None,
) -> dict:
    """Pull live accounts, balances, and transactions from SimpleFIN into the DB.

    Reads SIMPLEFIN_ACCESS_URL from the finances .env at runtime (never returned).
    Read-only against SimpleFIN; idempotent upsert by transaction id. When
    start_date is omitted, incremental=true resumes from the last synced
    transactions (cheap for a daily run); otherwise it pulls lookback_days,
    capped at SimpleFIN's recommended 45-day window (pass an explicit start_date
    for a deliberate backfill). Returns counts plus 'warnings' (actionable feed
    problems) and 'notes' (expected balance-only connections like Apple Card).
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = sync_simplefin_for_db(
            conn, start_date=start_date, end_date=end_date, lookback_days=lookback_days, incremental=incremental
        )
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def run_live_validation(
    as_of_date: str | None = None,
    sync: bool = True,
    keep_work_db: bool = False,
    db_path: str | None = None,
) -> dict:
    """Validate the pipeline on live data without touching the committed snapshot.

    Copies the database to a throwaway working DB, optionally pulls live SimpleFIN
    into the copy, then runs the read pipeline (onboarding scan, reconciliation,
    drift, guardrails) and returns a report with integrity checks (e.g. no
    orphaned statement targets after a card rename). The source database is never
    mutated.
    """
    as_of_date = _resolve_as_of(as_of_date)

    resolved_db_path = db_path or str(default_db_path())
    return run_live_validation_for_db(
        source_db_path=resolved_db_path, as_of_date=as_of_date, sync=sync, keep_work_db=keep_work_db
    )


@mcp.tool()
def get_daily_digest(
    as_of_date: str | None = None,
    windows: list[int] | None = None,
    verbose: bool = False,
    db_path: str | None = None,
) -> dict:
    """The daily finance summary (replaces `just daily`). Default is a compact
    summary: working cash + balance one-liners, source freshness, next-14d
    obligations, projection window endpoints with trough bands, guardrail/
    drift/match alerts, and queue counts. Set verbose=true for the full
    payload (all events, finding bodies, recurring candidate detail) plus a
    cash-flow.md-style narrative under the 'markdown' key. Read-only.
    """

    resolved_db_path = db_path or str(default_db_path())
    digest = build_daily_digest_for_db(
        db_path=resolved_db_path,
        as_of_date=as_of_date,
        windows=tuple(windows) if windows else (7, 14, 30, 60),
    )
    if verbose:
        digest["markdown"] = render_digest_markdown_for_db(digest, verbose=True)
        return digest
    return summarize_daily_digest_for_db(digest)


@mcp.tool()
def list_reconciliation_review_items(as_of_date: str | None = None, db_path: str | None = None) -> dict:
    """List recorded transaction matches whose obligation instance still awaits
    confirmation (the day's close-out queue). Read-only.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {"items": list_reconciliation_review_items_for_db(conn, as_of_date=as_of_date)}
    finally:
        conn.close()


@mcp.tool()
def confirm_reconciliation_match(
    instance_id: str, transaction_id: str | None = None, db_path: str | None = None
) -> dict:
    """Mark a reviewed obligation instance paid using its recorded transaction
    match. Guarded: requires a recorded match (run reconcile first); never
    auto-pays. Reversible with unconfirm_reconciliation_match.

    When there is no recorded match the error explains why (no candidate
    transactions in the date window vs amount outside tolerance, with the
    nearest candidates listed). Pass transaction_id to force-match a specific
    transaction to the instance; it is recorded as a normal confirmed match.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = confirm_reconciliation_match_for_db(conn, instance_id, transaction_id)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def unconfirm_reconciliation_match(instance_id: str, db_path: str | None = None) -> dict:
    """Reverse a confirmation: return the obligation instance to 'expected' and
    clear the matched-transaction evidence.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = unconfirm_reconciliation_match_for_db(conn, instance_id)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def compare_to_legacy(
    legacy_cashflow_md_path: str,
    as_of_date: str | None = None,
    base_year: int = 2026,
    render_markdown: bool = True,
    db_path: str | None = None,
) -> dict:
    """Parallel-run parity: diff a legacy cash-flow.md against the new daily digest.

    Reads the legacy markdown the caller points at (supply a fresh one from your
    own daily ritual); never runs the legacy ritual or writes any legacy file.
    Reports matched / missing-in-new / extra-in-new / amount-or-date-changed
    obligations with a severity each, plus the working-cash delta. Set
    render_markdown for a short parity summary under the 'markdown' key.
    """

    resolved_db_path = db_path or str(default_db_path())
    report = compare_to_legacy_for_db(
        legacy_cashflow_md_path=legacy_cashflow_md_path,
        db_path=resolved_db_path,
        as_of_date=as_of_date,
        base_year=base_year,
    )
    if render_markdown:
        report["markdown"] = render_parity_markdown_for_db(report)
    return report


@mcp.tool()
def summarize_spending(
    start_date: str,
    end_date: str,
    group_by: str = "category",
    exclude_transfers: bool = True,
    render_markdown: bool = True,
    db_path: str | None = None,
) -> dict:
    """Summarize outflow spending over a date range, grouped by category,
    merchant, or month: totals, counts, top buckets, a month-over-month trend,
    and the transaction ids behind each bucket. Transfers and income are excluded
    by default. Read-only. Set render_markdown for a summary under 'markdown'.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        report = summarize_spending_for_db(
            conn, start_date=start_date, end_date=end_date, group_by=group_by, exclude_transfers=exclude_transfers
        )
    finally:
        conn.close()
    if render_markdown:
        report["markdown"] = render_spending_markdown_for_db(report)
    return report


@mcp.tool()
def list_transactions(
    start_date: str | None = None,
    end_date: str | None = None,
    query: str | None = None,
    min_amount: float | None = None,
    account_id: str | None = None,
    include_pending: bool = True,
    limit: int = 50,
    db_path: str | None = None,
) -> dict:
    """List individual transactions, newest first, so you can quote an EXACT
    charge amount (for reconciliation or "what was that $X charge?") instead of
    only the aggregates from summarize_spending. Read-only.

    Optional filters: start_date / end_date (YYYY-MM-DD, inclusive, on posted
    date), query (case-insensitive substring over payee + description),
    min_amount (absolute-value floor), account_id, include_pending. limit is
    capped at 500; the 'truncated' flag signals more rows matched.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list_transactions_for_db(
            conn,
            start_date=start_date,
            end_date=end_date,
            query=query,
            min_amount=min_amount,
            account_id=account_id,
            include_pending=include_pending,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def verify_grounding(payload: dict, as_of_date: str | None = None, db_path: str | None = None) -> dict:
    """Check that every headline dollar figure in a finance payload (a
    get_finance_status or get_daily_digest result) traces to a source: working
    cash to the latest operating-account balance snapshot, each upcoming
    obligation to its instance row, each projection endpoint recomputable from
    those. Returns a grounding report flagging any number it could not trace.
    Read-only - use it to verify a finance answer before relying on it.
    """

    resolved_db_path = db_path or str(default_db_path())
    return verify_grounding_for_db(payload, resolved_db_path, as_of_date=as_of_date)


@mcp.tool()
def backfill_recurring_instances(as_of_date: str | None = None, lookback_days: int = 90, db_path: str | None = None) -> dict:
    """Materialize past-due instances for active recurring obligations over a
    trailing window and reconcile them against posted transactions, so the digest
    can answer "did rent / Amex / Apple clear this cycle?". Past instances do not
    enter the cash-flow projection (forward-only); no payment is fabricated -
    matches come from the normal reconciliation matcher.
    """
    as_of_date = _resolve_as_of(as_of_date)

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = backfill_recurring_instances_for_db(conn, as_of_date=as_of_date, lookback_days=lookback_days)
        conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool()
def auto_model_high_confidence_recurring(as_of_date: str | None = None, db_path: str | None = None) -> dict:
    """Apply HIGH-confidence, well-evidenced direct-checking recurring candidates
    (e.g. a car payment) as proper obligations so they enter the cash-flow
    projection and the runway becomes accurate. Conservative: only confidence=high
    direct-checking with >=3 occurrences; everything else stays in the review queue.
    """

    import sqlite3

    resolved_db_path = db_path or str(default_db_path())
    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = auto_model_high_confidence_recurring_for_db(conn, as_of_date=as_of_date)
        conn.commit()
        return result
    finally:
        conn.close()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
