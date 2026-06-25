# Cutover Plan: replace ~/dev/areas/finances tooling

Last updated: 2026-06-21

Goal: retire the legacy finance ritual in `~/dev/areas/finances` and run on this
MCP server reading **live** data. Today the server is import-ready and feature-
complete for V1 logic (35 tools, 105 tests), but it reads a static copied
snapshot and has no live ingest, so it is a parallel-run candidate, not yet a
same-day swap.

Legend: [NOW] buildable today, no credentials. [YOU] blocked on a token or your
decision.

## Phase 1 - Live data foundation  [BUILT 2026-06-21]

Credentials live in `~/dev/areas/finances/.env` (`SIMPLEFIN_ACCESS_URL`,
`TODOIST_API_TOKEN`) and `obligations.yaml` (`todoist_project_id`); a stdlib
config loader reads them at runtime (never logged/committed).

- [DONE] **SimpleFIN sync ingest** (`sync_simplefin`, MCP tool): pulls accounts,
  balances, and transactions via stdlib urllib (basic-auth + explicit User-Agent
  so the bridge does not 403), normalizes exactly like the legacy
  `finance/simplefin.py`, and upserts into `accounts` / `balance_snapshots` /
  `transactions` / `sync_runs` (epoch -> ISO, idempotent by id). Default 90-day
  lookback (SimpleFIN's cap). Live smoke: 9 accounts, 736 transactions.
- [DONE] **Todoist read sync** (`sync_todoist`, MCP tool): pulls tasks + sections
  read-only via urllib, normalizes exactly like `finance/todoist.py` (the
  cashflow fields slice G reads), upserts and marks missing tasks deleted. Live
  smoke: 6 sections, 20 tasks, 7 cashflow candidates.
- [DONE] Both are wired as opt-in, config-gated first steps of
  `run_background_sync` (option `sync`), and `financial-agent-daily` enables them.
- Still ahead: incremental SimpleFIN (sync from last-posted) is a refinement; the
  full daily flow replacing `just sync` now works end to end against live data.

## Phase 2 - Todoist as a bidirectional, agent-orchestrated one-off input

The local DB stays canonical; Todoist is the *origin* for one-off obligations
(recurring stay model-driven) AND a surface the agent flags back. The loop:
Claude Code reads tasks -> imports one-offs -> flags each task in Todoist with its
linked obligation id. Our server owns the durable, idempotent contract; the agent
(or a future configured sender) performs the actual Todoist API call, so no raw
credentials need to live in the server yet.

Read / import (INPUT):
- [NOW] **`import_todoist_obligations(tasks, options)`.** Accept task rows (from
  the `todoist_tasks` snapshot now, or a live Todoist read later) and
  create/update a canonical **one-off** obligation + single dated instance per
  `cashflow_candidate` task, using the already-parsed `amount_value` /
  `signed_amount` / `amount_direction` / `due_date`. Keyed by Todoist task id
  (idempotent re-import).
- [NOW] **`todoist_sync_records` table.** Map `external_task_id <-> obligation_
  instance_id` with `content_hash`, `last_observed_state`, `sync_status`. Handle
  lifecycle: task edited -> update the instance; `checked`/`completed` -> record
  intent but DO NOT auto-mark paid (bank evidence rule); `is_deleted` -> cancel
  the instance or flag needs_review.
- [NOW] **Dedup guard.** A Todoist one-off must not duplicate an already-modeled
  recurring obligation (match by name/amount/date window before creating).

Write-back / flag (OUTPUT):
- [NOW] **Outbox flag action `todoist_flag_task`.** Reuse the existing
  `action_outbox` + `preview` pattern: enqueue an idempotent "flag this task as
  linked/reviewed" action (e.g. add label `obligation:<id>` or a linking comment)
  keyed by task id. Previewable, never duplicated.
- [YOU/AGENT] **Execution.** The agent reads pending flag actions and performs the
  Todoist write through its Todoist tool, then marks the outbox item succeeded;
  OR a configured server-side sender does it later. Either path requires a Todoist
  capability that does not exist in this session yet (no Todoist MCP is connected;
  only Gmail/Calendar/Linear are). So a prerequisite is: add a Todoist MCP to the
  finances-area Claude Code config, or give the server a guarded Todoist API tool.
- [NOW] **Finance skill / instructions.** A procedural layer telling the agent the
  read -> import -> flag loop and when to run it (so it is not ad hoc).

Recommended default: agent-driven first (server stays credential-light, the
outbox keeps writes previewable + idempotent); add a server-side Todoist sender
only if you want the scheduled background runner to flag tasks without a chat
session.

## Phase 3 - Obligation migration + guardrails

- [NOW] **Full obligation bootstrap.** One-time importer that brings the complete
  current obligation set into canonical rows from `obligations.yaml` +
  `cash-flow.md` + Todoist one-offs, deduped. Today only a curated subset is
  seeded.
- [DONE 2026-06-24] **`obligations.yaml` deprecated as a source of truth.**
  Cash-flow projections read only the `obligation_instances` table; the YAML is no
  longer authoritative for cash events. It is kept (not deleted) for the one-time
  migration bootstrap and a `todoist_project_id` fallback in `config.py`. A
  `_deprecation` note sits at the top of the file; see the README "Deprecations &
  Migrations" section. The legacy `just cashflow` scripts are left in place.
- [NOW] **Explicit guardrails** (carried from the legacy ritual): cash-floor
  check, $200 drift threshold, window-age check, and debt-avalanche payoff
  ordering, surfaced as status warnings.

## Phase 4 - Validate on live data  [BUILT 2026-06-21]

- [DONE] **Validation harness** (`run_live_validation`, MCP tool): copies the
  snapshot to a throwaway working DB, pulls live SimpleFIN + Todoist into the
  copy, runs onboarding scan + reconciliation + drift + guardrails on the real
  data, and reports counts plus integrity checks (accounts present, working XXXX
  account, no orphaned statement targets, normalized amounts). Never mutates the
  source. First live run: the pipeline ran clean, all integrity checks passed,
  and the Amex Platinum->Gold rename did NOT break account-class or statement
  targeting (those key on the org, not the card name).
- [DONE] **Daily digest** (`get_daily_digest`, MCP tool): the human-readable
  just-daily / cash-flow.md replacement (working cash, 7/14/30/60d projection,
  upcoming obligations, drift/review, guardrail status, provenance) with a
  markdown render for parallel-run diffing.
- Still [YOU]: tuning amount tolerance / grace periods is only worth doing once
  obligations go past-due against live transactions (today the seed is mostly
  forward forecasts, so reconciliation has little to match).

## Phase 5 - Proactive + packaging

- [DONE] **Scheduled runner.** `run_background_sync` is wrapped in a daily job
  (`financial-agent-daily`) with a file lock + event log; it pulls live data first.
- [DONE - STAGED] **Install assets drafted.** `claude-integration/` holds the MCP
  registration snippet, the finance skill, the "financial claims must use the
  finance tools" instruction block, and an `INSTALL.md`. Ready to copy in.
- [YOU] **Install into the finances area.** Run the steps in
  `claude-integration/INSTALL.md` (register the server, drop in the skill, add the
  rule block). Deliberate, not autonomous - it writes to the legacy workspace.

## Phase 6 - Parallel-run, parity, cutover

- [DONE - TOOLING] **Parity engine.** `compare_to_legacy` diffs a fresh legacy
  `cash-flow.md` against the new digest (missing / extra / changed obligations +
  working-cash delta, with severities). `get_daily_digest` renders the new side.
- [DONE - TOOLING] **Close-out.** `confirm_reconciliation_match` /
  `unconfirm_reconciliation_match` / `list_reconciliation_review_items` close the
  "mark it paid" step of the ritual.
- [YOU] Run the new system alongside the legacy ritual ~1-2 weeks; use
  `compare_to_legacy` until parity holds; fix any discrepancies it surfaces.
- [YOU] After parity holds, retire the legacy commands. Explicit and last.

## Deferred / optional

- [DONE - GATED] **Live Todoist write sender** (slice U). Built and idempotent,
  behind `TODOIST_WRITE_ENABLED` (default OFF). The code is mock-tested and never
  sends until you set the flag. [YOU] To turn it on: set `TODOIST_WRITE_ENABLED=1`
  in the finances `.env`, then `execute_action_outbox` will create/update the
  review task. Enabling + the first real send is your explicit step.
- [DONE] **M5 grounding/verification harness** (slice V, `verify_grounding`):
  traces every headline figure to a source row and flags ungrounded numbers.
- [DONE] **Spending analytics** (slice W, `summarize_spending`): read-only outflow
  reports by category/merchant/month (not a cutover requirement; useful extra).
- [YOU] **LangGraph re-implementation.** Learning-only, needs the `langgraph`
  dependency; no product value for cutover. Skip unless wanted for interview prep.

## What I can start immediately (no credentials)

Phase 2 (Todoist one-off input + sync records), Phase 3 (full obligation
migration + guardrails), and Phase 5's scheduled runner are all unblocked. The
live SimpleFIN/Todoist syncs (Phase 1) and the actual cutover (Phase 6) need your
tokens and go-ahead.

## Execution: hardened slices G-J (drive these with the /loop)

**STATUS 2026-06-21: G, H, I, J are all built, reviewed, and committed.** The G
adversarial review tightened the dedup matcher (fix applied to G and H); a final
cross-cutting review of G-J was also run. Full suite: 142 tests. The system now
imports Todoist one-offs, migrates the legacy obligation set, enforces guardrails
in status, and has a scheduled daily runner. What remains is all blocked on
tokens/decisions (see "Stop boundary" below and Phases 1, 4, 6).

Build order **G -> H -> I -> J**. Each slice: tests first -> implement ->
`uv run --extra dev python -m pytest -q` green -> ultracode adversarial review workflow ->
fix confirmed findings -> commit -> mark the slice done here. Recommended /loop
ceiling: **8 iterations** (baseline ~7; the loop stops when G-J are built,
reviewed, committed, and docs updated - the number is a safety ceiling, not a
target).

### G. Todoist one-off importer (est ~2-3 cycles)
- Tables: `todoist_sync_records` (external_task_id PK, obligation_instance_id,
  content_hash, last_observed_state_json, sync_status, is_deleted_in_source,
  checked_in_source, completed_at_in_source, timestamps, error_notes);
  `todoist_import_log` (run summary counts).
- Functions/tools: `import_todoist_obligations(conn, tasks, options)`,
  `enqueue_todoist_flag_task(...)`, `list_todoist_sync_records(...)`,
  `handle_todoist_task_lifecycle_change(...)`,
  `resolve_todoist_dedup_conflict(...)`.
- Logic: read `cashflow_candidate=1` tasks; require amount + due_date (else skip
  to needs_review); make a canonical one-off obligation `todoist_oneoff_{id}` +
  single instance (amount=abs(signed_amount), direction from sign); idempotent by
  task id via sync_records; lifecycle: checked/completed -> set `review_after`,
  status stays expected (NOT paid); is_deleted -> instance `canceled`; dedup vs
  existing recurring obligations (same normalized-name + month + amount bucket
  within +/-7d) -> `needs_review_dedup_conflict`, do not create; enqueue a
  dry-run `todoist_flag_task` action in `action_outbox` keyed `todoist_flag:{id}`.
- Tests: amount sign normalization, null amount/date skipped, dedup conflict on
  Partner pay, idempotent re-import, checked != paid, is_deleted -> canceled,
  flag-action idempotency, import_log counts.

### H. Full obligation migration (est ~2-3 cycles)
- Table: `obligation_migration_log` (source_type, source_id, decision, dedup_key,
  result, timestamps).
- Functions/tools: `parse_obligations_yaml(path)`, `parse_cashflow_json(path)`
  (read `~/dev/areas/finances/obligations.yaml` + `cash-flow.md` read-only),
  `apply_obligation_bulk_migration(conn, source_type, ..., dry_run)`,
  `validate_dedup_key(...)`.
- Logic: parse all rows; dedup key = (normalize(name), year-month,
  round(amount,-2)); merge multi-instance recurring (e.g. Partner pay, rent) into
  one obligation with its instances; ambiguous/uncertain rows (e.g. student-loan
  forbearance) -> instance `status='needs_review'` with `review_after`; never
  create master obligations spanning multiple cards; dry-run preview before
  write; full migration_log audit.
- Tests: parse completeness, Partner/rent/Volvo/oil dedup-merge, student-loan ->
  needs_review, dry-run writes nothing, re-run idempotent.

### I. Guardrails into status (est ~1-2 cycles)
- Tables: `guardrail_rules` (seeded, immutable) + `guardrail_evaluations`
  (append-only).
- Values from legacy docs: cash floor **$2500**, drift threshold **$200**,
  window-age **24h**, debt-avalanche order by APR.
- Functions/tools: `apply_guardrail_rules` (idempotent seed),
  `evaluate_guardrails(conn, as_of_date, persist)`, `list_guardrail_findings`;
  wire into `get_finance_status` (add `guardrail_findings`, merge into warnings,
  dedup vs drift).
- Tests: cash-floor breach by window, drift-sum > $200, stale window-age,
  avalanche order, status integration, all-pass -> empty.

### J. Scheduled daily-runner skeleton (est ~1 cycle)
- `run_scheduled_daily_sync(db_path, lock_dir, dry_run)`: stdlib file lock
  (`fcntl.flock`, non-blocking -> skip if held), call `run_background_sync`,
  record in `background_runs` (trigger_type='scheduled'), release on exit; console
  entry for cron. Tests: lock acquire/release, skip-if-held, run recorded,
  idempotent per day.

### Stop boundary (do NOT cross in the loop)
Live SimpleFIN API sync; live Todoist read/write API; executing outbox sends;
install/cutover into `~/dev/areas/finances`; mutating the legacy area; adding any
dependency (no LangGraph); auto-marking instances paid on a checked task;
per-user guardrail config. Each needs Owner's tokens/decisions - stop and report
instead.
