# Claude Code Handoff: Financial Agent MCP Build

Last updated: 2026-06-21

## Objective

Continue building `/Users/owner/dev/financial-agent-mcp`, a local MCP server
for grounded personal-finance workflows.

The immediate implementation target is the charge-onboarding workflow:

```text
transactions -> discovered candidates -> review queue -> accepted policy -> canonical obligations/instances -> cash-flow/reconciliation
```

Do not build a generic finance chatbot. Build deterministic MCP tools and local
data structures that Claude Code can use as the harness.

## Required Reading

Read these before editing:

1. `AGENTS.md`
2. `README.md`
3. `BUILD_PLAN.md`
4. `HANDOFF.md`
5. This file

## Hard Safety Constraints

- Use only the copied database: `data/transactions.source-copy.sqlite`.
- Do not mutate `/Users/owner/dev/areas/finances/data/transactions.db`.
- Do not treat Todoist as the source of financial truth. Todoist is only a
  review/action surface.
- Do not let unresolved onboarding candidates affect cash-flow projection.
- Do not auto-apply discovered candidates into canonical obligations in the
  first slice.
- Do not add external write actions unless they are explicitly configured,
  previewable, idempotent, and tested.

## Current State

Implemented package:

```text
financial-agent
```

MCP entry point:

```text
financial-agent-mcp
```

Current tools:

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

Current app-owned database tables:

- `obligations`
- `obligation_instances`
- `income_sources`
- `income_schedule_versions`
- `calendar_facts`

Current tests:

```bash
uv run --extra dev pytest -q
```

Last known passing result:

```text
20 passed
```

## Already Built

The copied DB contains seeded obligations and instances for:

- Owner / IntelliBridge income
- Partner / Town of Greenwich income
- rent
- Amex Personal Loan autopay
- Apple Card minimum payments
- Apple Card paydown sweeps
- Eversource electric estimates
- Amex statement payments
- New York Times subscription
- Plex via Venmo
- Anthem reimbursement estimates
- Gault card-spend estimates

Important corrections already encoded:

- Cash Magnet is paid off, at zero, and not being used. Do not seed it.
- Gault is not a direct checking outflow. It is a `card_statement_input` that
  targets `amex_statement_payment`.
- Eversource is a direct checking utility outflow with a seasonal estimator.
- Gault card-spend inputs are excluded from cash-flow projection, so checking is
  affected only through the Amex statement-payment layer.

Current estimator-related fields on `obligation_instances`:

- `amount_status`
- `amount_source`
- `amount_observed_at`
- `statement_close_date`
- `review_after`
- `estimation_method`
- `estimation_inputs_json`
- `cash_flow_treatment`
- `statement_target_obligation_id`

## Current Product Requirement

The user does not want to manually prompt "onboard Gault" or "onboard
Eversource."

Required behavior:

1. A background/discovery process scans transaction history.
2. The system groups related transactions into charge-pattern candidates.
3. Candidates go into a durable review queue.
4. Claude Code or a future UI walks the user through that queue one candidate at
   a time.
5. The queue is exhausted when all current candidates are accepted, edited,
   rejected, deferred, merged, split, or marked needs-more-evidence.
6. Only accepted/applied candidates become canonical obligations and dated
   instances.

## Candidate State Model

Use this state machine unless a better reason emerges:

```text
discovered -> proposed -> in_review -> accepted -> applied
```

Alternate states:

```text
rejected
deferred
needs_more_evidence
merged
split
```

Candidates are not cash-flow truth. Canonical obligations and instances are
cash-flow truth after apply.

## Recommended Next Vertical Slice

Build the candidate queue/proposal layer only.

Do not build statement-cycle aggregation yet.
Do not build Todoist writes yet.
Do not build UI yet.

### Step 1: Schema

Add a table similar to:

```text
charge_onboarding_candidates
```

Suggested fields:

- `id`
- `merchant_key`
- `display_name`
- `status`
- `candidate_type`
- `cash_flow_treatment`
- `proposed_schedule_policy_json`
- `proposed_amount_policy_json`
- `proposed_cash_impact_policy_json`
- `proposed_review_policy_json`
- `confidence`
- `evidence_count`
- `evidence_transaction_ids_json`
- `evidence_summary_json`
- `missing_evidence_json`
- `notes`
- `created_at`
- `updated_at`
- `reviewed_at`
- `applied_at`

Keep this table separate from `obligations`.

### Step 2: Domain Module

Add a new module:

```text
src/financial_agent/onboarding.py
```

Suggested functions:

- `scan_charge_onboarding_candidates(conn, options) -> dict`
- `list_charge_onboarding_queue(conn, status=None, limit=...) -> list[dict]`
- `get_next_charge_onboarding_candidate(conn) -> dict | None`
- `record_charge_onboarding_decision(conn, candidate_id, decision) -> dict`

For the first slice, `record_charge_onboarding_decision` can support
`deferred`, `rejected`, and `needs_more_evidence`. Applying canonical
obligations can be a later slice.

### Step 3: MCP Tools

Expose tools in `src/financial_agent/server.py`:

- `scan_charge_onboarding_candidates(...)`
- `list_charge_onboarding_queue(...)`
- `get_next_charge_onboarding_candidate(...)`
- maybe `record_charge_onboarding_decision(...)` if the first slice includes
  review-state updates

### Step 4: Candidate Detection

For V1, keep detection deterministic and simple:

- Group transactions by normalized merchant/payee/description tokens.
- Ignore positive payments/refunds unless the candidate type is inflow.
- Track account/org so card spend can be separated from direct checking.
- Use evidence count, amount spread, cadence, and account kind/name to propose
  a candidate type.

Starting policy mapping:

- Direct checking monthly merchant -> direct checking obligation candidate.
- Amex/card merchant -> card statement input candidate.
- Similar amounts on a predictable monthly cadence -> fixed or average amount
  policy.
- Usage-driven amounts with seasonal hints -> seasonal or needs review.
- Low evidence count -> needs more evidence or low confidence.

Use existing copied transaction examples to drive tests:

- Eversource: direct checking, seasonal utility candidate.
- Gault: card statement input, seasonal card-spend candidate.
- New York Times: monthly direct checking subscription candidate.

### Step 5: Tests First

Add tests before implementation.

Suggested test file:

```text
tests/test_onboarding.py
```

Acceptance tests:

1. Given transaction rows for Gault on an Amex account, scanning creates one
   candidate with:
   - `display_name` like `Gault Energy`
   - `cash_flow_treatment = card_statement_input`
   - proposed amount policy indicating seasonal/card-spend estimate
   - evidence transaction IDs
   - status `proposed` or `discovered`

2. Given Eversource checking transactions, scanning creates one candidate with:
   - `cash_flow_treatment = direct_checking`
   - proposed amount policy indicating average or seasonal multiplier
   - evidence transaction IDs

3. Candidates appear in `list_charge_onboarding_queue`.

4. `get_next_charge_onboarding_candidate` returns the highest-priority
   unresolved candidate.

5. Cash-flow projection does not include candidate rows before they are applied.

6. Running the scanner twice updates or preserves the same candidate instead of
   creating duplicates.

## Definition of Done For This Handoff

The handoff task is complete when:

- The schema can store onboarding candidates.
- The scanner can discover at least Gault and Eversource patterns in tests.
- The queue tools can list and return the next candidate.
- Scanning is idempotent.
- Unapplied candidates do not affect cash flow.
- `uv run --extra dev pytest -q` passes.
- `README.md`, `BUILD_PLAN.md`, and `HANDOFF.md` are updated with the new tools
  and current pickup.

## Stop Conditions

Stop and ask the user before:

- Mutating the source finance database.
- Creating Todoist tasks.
- Applying candidates into canonical obligations automatically.
- Adding direct LLM/API billing.
- Introducing a new framework or dependency.
- Changing the copied DB in a way that cannot be explained from tests and source
  evidence.

## Suggested First Claude Code Prompt

```text
Read AGENTS.md, README.md, BUILD_PLAN.md, HANDOFF.md, and CLAUDE_CODE_HANDOFF.md.
Then implement the first charge-onboarding vertical slice:

1. Add durable onboarding candidate schema.
2. Add deterministic candidate scanning from copied transactions.
3. Add queue listing and next-candidate functions.
4. Expose MCP tools for scan/list/next.
5. Add tests first for Gault card statement input, Eversource direct utility,
   idempotent scanning, and candidates not affecting cash flow.
6. Run uv run --extra dev pytest -q.

Do not mutate /Users/owner/dev/areas/finances/data/transactions.db.
Do not build Todoist writes or auto-apply candidates yet.
```

