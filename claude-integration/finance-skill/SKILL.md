---
name: finance
description: Operating procedure for the personal-finance ritual. Use whenever the user asks about balances, cash flow, obligations, bills, what is due, source freshness, reconciliation, drift, or the daily/weekly finance review. All financial claims must come from the finance MCP tools, never from memory or estimation.
---

# Finance Ritual

This skill drives the local finance MCP server (`financial-agent`). It owns the
source-of-truth reads, deterministic projections, provenance, and guarded
actions. Your job is to call the right tools in the right order and report what
they return — never to guess a number.

## Hard rule

Every financial claim (a balance, an amount due, a date, a projection) MUST be
backed by a finance MCP tool result in the same turn. If you do not have a tool
result for it, say so and call the tool. Do not estimate, recall, or extrapolate
dollar figures.

## Daily ritual (the common path)

1. **Refresh** live data: `run_background_sync` with `options={"sync": true}`
   (pulls SimpleFIN, then scans/reconciles/detects drift and surfaces due items
   in one audited run). For a quick manual refresh use `sync_simplefin`
   (`incremental=true`).
2. **Read the digest**: `get_daily_digest` (working cash, 7/14/30/60d projection,
   upcoming obligations, drift, matches to confirm, guardrail status). This is
   the morning summary. Lead your answer with its `status_color` and working cash.
3. **Close out matches**: `list_reconciliation_review_items` shows matches
   awaiting confirmation. For each one the user confirms, call
   `confirm_reconciliation_match(instance_id)`. Use
   `unconfirm_reconciliation_match` to reverse. Never confirm without the user.
4. **Triage discovered charges**: `list_charge_onboarding_queue` /
   `get_next_charge_onboarding_candidate`; record a decision with
   `record_charge_onboarding_decision`; preview then apply with
   `preview_charge_onboarding_apply` and `apply_charge_onboarding_candidate`.

## Answering a money question

- Balances / "how much do I have": `get_finance_status` or `get_daily_digest`;
  report working cash (operating account), not the debt-mixed total.
- "What's due / what's coming": the digest's upcoming obligations, or
  `list_obligations` for the full set.
- "Did X get paid": `reconcile_obligation_instances` then
  `list_matched_obligation_instances` / `list_unmatched_obligation_instances`.
- Drift / "is anything off": `detect_drift` and `list_drift_findings`.
- Guardrails (cash floor, drift threshold, window age, debt order):
  `evaluate_guardrails` / `list_guardrail_findings`.

## Parallel-run / parity check

While migrating off the legacy `just daily` ritual: `compare_to_legacy` diffs a
fresh legacy `cash-flow.md` against the new digest and reports where they
disagree (missing / extra / changed obligations + working-cash delta). Use it to
decide when the new system is trustworthy enough to rely on alone.

## Todoist (output only)

- Todoist is **output-only**: the server pushes reminders out and reads back
  completions of tasks it pushed; it is not a source of obligations.
- `surface_due_items_to_todoist` pushes the day's due items idempotently via the
  emissions ledger; `reconcile_todoist_completions` absorbs user completions of
  those tasks; `create_todoist_task` makes a one-off reminder.
- All writing is **gated off** unless the user has explicitly enabled it.
  `execute_action_outbox` only sends when live Todoist integration is turned on.
  Do not enable or send without explicit user instruction.

## Provenance and uncertainty

Tool outputs carry the source and, where relevant, a confidence and a
`needs_review` state. Surface those. When an amount is an estimate (e.g. a
statement payment before the statement closes), say it is an estimate and show
the basis. When something needs the user's input, say so plainly rather than
inventing a value.
