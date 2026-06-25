# Financial Agent MCP Handoff

Last updated: 2026-06-21

## Why This Exists

This file is the tactical session handoff. `BUILD_PLAN.md` is the product and
architecture plan. `README.md` is setup and command reference. This handoff
answers: what is true right now, where to resume, and what decisions should not
be lost.

## Current Location

Implementation root:

```text
/Users/owner/dev/financial-agent-mcp
```

Old interview-prep pointer:

```text
/Users/owner/dev/interview-prep/implementations/financial-agent/README.md
```

Interview-prep now keeps only learning state and system-design practice status.
The current pickup there is back to system design, especially the in-progress
Rate Limiter drill.

## Core Boundary

The source finance system remains live at:

```text
/Users/owner/dev/areas/finances
```

The source finance database is:

```text
/Users/owner/dev/areas/finances/data/transactions.db
```

Do not mutate that database from this project. This implementation works against
the copied database:

```text
data/transactions.source-copy.sqlite
```

The current copied database has app-owned finance tables initialized and seeded.
The last verification counted 49 obligation instances.

## Product Target

V1 should replace the reliable Claude Code-assisted finance workflow the user
actually uses today:

- sync finances into the database
- report balances and source freshness
- query and explain cash flow
- detect drift and recurring candidates
- create or update Todoist review tasks when user input is needed

V1 should not blindly port every command from `/Users/owner/dev/areas/finances`.
Bring forward only the parts that support the real workflow above.

## Current Implementation

Package:

```text
financial-agent
```

Entry point:

```text
financial-agent-mcp
```

Implemented MCP/server tools:

- `get_finance_status`
- `list_income_sources`
- `apply_income_source`
- `generate_income_instances`
- `import_calendar_facts`
- `list_calendar_facts`
- `apply_obligation_instances`
- `list_obligations`
- `list_obligation_review_candidates`
- `list_statement_input_estimates`
- `scan_charge_onboarding_candidates`
- `list_charge_onboarding_queue`
- `get_next_charge_onboarding_candidate`
- `record_charge_onboarding_decision`
- `preview_charge_onboarding_apply`
- `apply_charge_onboarding_candidate`
- `aggregate_statement_inputs`
- `list_statement_cycles`
- `recompute_statement_estimates`
- `reconcile_obligation_instances`
- `list_matched_obligation_instances`
- `list_unmatched_obligation_instances`
- `detect_drift`
- `list_drift_findings`
- `preview_todoist_review_batch`
- `enqueue_todoist_review_batch`
- `execute_action_outbox`
- `list_action_outbox`
- `run_background_sync`
- `get_background_run`
- `list_background_runs`
- `write_finance_memory`
- `search_finance_memory`
- `list_finance_memories`
- `delete_finance_memory`
- `import_todoist_obligations`
- `list_todoist_sync_records`
- `resolve_todoist_dedup_conflict`
- `apply_obligation_migration`
- `evaluate_guardrails`
- `list_guardrail_findings`
- `apply_guardrail_rules`
- `sync_simplefin`
- `sync_todoist`
- `run_live_validation`
- `get_daily_digest`
- `compare_to_legacy`
- `list_reconciliation_review_items`
- `confirm_reconciliation_match`
- `unconfirm_reconciliation_match`
- `verify_grounding`
- `summarize_spending`

Semantic memory (M4) and a stdlib local inspector UI (M6, `financial-agent-ui`)
are also implemented. Cutover slices G-J are built (Todoist one-off import,
obligation migration, guardrails in status, scheduled daily runner -
`financial-agent-daily`). Phase 1 live ingest is built: `sync_simplefin` and
`sync_todoist` pull live data (stdlib urllib, credentials read from the finances
`.env` at runtime), wired as opt-in steps of `run_background_sync`. Live
validation + daily digest (N/O/P) and the parallel-run parity report +
reconciliation close-out + staged Claude Code assets (R/S/T) are also built. See
`CUTOVER_PLAN.md`. Remaining cutover work is operational and Owner's to trigger
(enable live Todoist write-back, install the `claude-integration/` assets, run
the parallel period, retire legacy). 52 MCP tools total.

`get_finance_status` now fills `drift_warnings` (missing/stale/amount-changed)
and `recurring_candidates` (discovered-but-unapplied charges) from a read-only
drift pass. The Todoist layer is preview + a durable action outbox only: nothing
is ever sent to a live Todoist (no integration is configured by design).
`run_background_sync` orchestrates the whole pipeline (scan -> reconcile ->
detect drift -> preview review batch) as one auditable run with an event log;
on the copied database it completes in ~25ms.

Current tests:

```bash
uv run --extra dev pytest -q
```

Last result:

```text
208 passed
```

## Current Data Model

The copied database includes the legacy copied tables plus app-owned tables:

- `obligations`
- `obligation_instances`
- `income_sources`
- `income_schedule_versions`
- `calendar_facts`
- `charge_onboarding_candidates`

`charge_onboarding_candidates` is the durable review queue produced by
background discovery. It is intentionally separate from `obligations`: a
candidate is a reviewable proposal (merchant key, display name, status,
candidate type, cash-flow treatment, proposed schedule/amount/cash-impact/review
policies, confidence, priority, evidence transaction ids, evidence summary, and
missing evidence), not cash-flow truth. Candidates never write
`obligation_instances`, so they cannot affect projections until applied.

`obligation_instances` includes lifecycle fields for uncertain amounts:

- `amount_status`
- `amount_source`
- `amount_observed_at`
- `statement_close_date`
- `review_after`
- `estimation_method`
- `estimation_inputs_json`
- `cash_flow_treatment`
- `statement_target_obligation_id`

This was added for Amex statement payments, where a payment is definitely owed
monthly, but the exact amount may not be known until the statement closes or the
portal is checked.

## Seeded Finance Facts

Income sources:

- Owner / IntelliBridge: semi-monthly 10th/25th, rolled back to previous
  business day, modeled as working-cash transfer into checking `XXXX`.
- Partner / Town of Greenwich: biweekly Friday payroll, rolled back for
  holidays, direct deposit into checking `XXXX`.

Obligations and projected instances:

- `rent_check`
- `amex_personal_loan_autopay`
- `apple_card_minimum_payments`
- `apple_card_paydown_sweeps`
- `eversource_electric_estimates`
- `gault_card_spend_estimates`
- `amex_statement_payment`
- `new_york_times_subscription`
- `plex_venmo_subscription`
- `anthem_reimbursement_estimates`

Explicit correction:

- Cash Magnet is intentionally skipped because the user confirmed it is paid
  off, at zero, and not being used.

Important modeling boundary:

- Gault is seeded as `card_statement_input`, not as a direct checking outflow.
  It targets `amex_statement_payment` and can be listed through
  `list_statement_input_estimates`. Cash-flow projection excludes these rows, so
  checking is affected only by the statement payment layer.

## Current Projection Checkpoint

Last known projection checkpoint after the Eversource/Gault estimator update:

- Start date: `2026-06-21`
- 30 days: ending `$8,008.50`, lowest `$5,440.28` on `2026-06-25`
- 60 days: ending `$8,608.20`, lowest `$5,440.28`
- 90 days: ending `$16,119.20`, lowest `$5,440.28`

Treat these as checkpoint values, not permanent truth. Recompute from the copied
database before relying on them.

## Current Pickup

Done in this slice (charge-onboarding background discovery + review queue):

- `src/financial_agent/onboarding.py` discovers charge-pattern candidates from
  copied transactions and stores them in `charge_onboarding_candidates`.
- Detection (steps 1-5 below) is implemented deterministically: gather evidence
  by merchant/account/date/amount, normalize merchant identity, classify
  cash-flow impact, infer schedule and amount policy, and return a reviewable
  proposal with confidence, evidence, and missing-evidence notes.
- MCP exposes `scan_charge_onboarding_candidates`,
  `list_charge_onboarding_queue`, `get_next_charge_onboarding_candidate`, and
  `record_charge_onboarding_decision`.
- Scanning is idempotent and never regresses a human decision. Candidates do not
  affect cash-flow projection. The decision tool supports
  defer/reject/needs_more_evidence/in_review/reset; accept/apply/merge/split
  raise on purpose (step 6 is the next slice).
- Running the scanner on the copied database discovers 114 candidates, with
  Gault Energy classified as `card_statement_input` + `seasonal_card_spend`
  (targeting `amex_statement_payment`), Eversource as `direct_checking` +
  `seasonal_multiplier`, and New York Times as `direct_checking` + `fixed`.

Apply slice: DONE. `record_charge_onboarding_decision` now supports `accept`
(status -> accepted). `preview_charge_onboarding_apply` is read-only and shows
the obligation plus dated instances that would be created.
`apply_charge_onboarding_candidate` is the guarded write: it requires an
accepted candidate, generates the canonical obligation plus dated instances from
the proposed schedule/amount policy, sets the candidate to `applied`, and is
idempotent. `direct_checking` obligations project into checking,
`card_statement_input` obligations feed statement estimates, and inflows behave
like income. Nothing auto-applies.

Next implementation decision (statement-cycle aggregation, slice B):

1. Use applied `card_statement_input` instances to assign statement input rows to
   specific Amex statement cycles and roll them into future
   `amex_statement_payment` amounts (cycle window = previous close exclusive to
   this close inclusive), keeping a baseline for non-onboarded recurring card
   spend.

Detection reference (now implemented; kept for the apply slice):

1. Gather evidence from copied transactions by merchant, account, date, and amount.
2. Normalize merchant identity.
3. Classify cash-flow impact: direct checking, card statement input,
   reimbursement/inflow, transfer/internal, or review-only.
4. Infer schedule and amount policy: fixed, average, seasonal multiplier,
   seasonal card spend, statement balance, or actual-after-observed.
5. Return a reviewable onboarding proposal with confidence and evidence.
6. Apply the accepted proposal through a guarded tool that writes obligation and
   obligation-instance rows. (NOT YET BUILT - this is the next slice.)

Requirement update:

- The user should not have to prompt "onboard Gault" or another specific charge.
- The background runner should discover charge-pattern candidates and maintain a
  durable review queue.
- The user-facing conversation should walk the queue one candidate at a time
  until all current candidates are accepted, edited, rejected, deferred, merged,
  split, or marked needs-more-evidence.
- Accepted candidates become canonical obligations, policy metadata, and dated
  instances. Non-accepted candidates remain separate from cash-flow truth.

## Calendar Support State

Current support is import-ready, not live-fetching:

- deterministic business calendar handles weekends, observed fixed-date US
  holidays, common US floating holidays, and explicit closure dates
- `import_calendar_facts` stores typed calendar facts
- stored `business_closure` facts affect business-day adjustment
- stored `income_pay_date` facts can drive exact calendar-date schedules

No live Google Calendar fetcher is implemented yet. The intended sequence is to
finish useful obligation modeling first, then add a guarded adapter that reads
Google Calendar or Google Workspace CLI events and normalizes them into
`calendar_facts`.

## Todoist Boundary

Todoist should become an action and review surface, not the source of financial
truth.

The target write model is:

- preview review batches before writing
- create or update review tasks only behind explicit configuration
- use idempotency keys so reruns update the same task instead of duplicating
- store Todoist sync state locally
- represent external writes through an action/outbox table or equivalent durable
  queue

Todoist completion means the review workflow happened. It is not bank evidence
that money moved.

## How To Resume In A New Session

1. Open a session rooted at `/Users/owner/dev/financial-agent-mcp`.
2. Read `AGENTS.md`, `README.md`, `BUILD_PLAN.md`, this file, and
   `CLAUDE_CODE_HANDOFF.md`.
3. Run `uv run --extra dev pytest -q`.
4. Inspect current status with the MCP/status service or direct tests.
5. Run `scan_charge_onboarding_candidates` and walk the queue with
   `get_next_charge_onboarding_candidate` to see current discovery output.
6. Resume at the charge-onboarding apply slice (promoting accepted candidates
   into canonical obligations through a guarded, previewable action).

Resolved in this slice: the first onboarding slice includes review-state updates
(`defer`, `reject`, `needs_more_evidence`, `in_review`, `reset`), not just
scan/list/next. Apply remains intentionally out of scope and raises.

Suggested first user-facing question:

> When applying an accepted candidate, should the apply action immediately
> generate dated obligation instances for the projection horizon, or only create
> the durable obligation plus its policy and defer instance generation to a
> separate scheduled step?
