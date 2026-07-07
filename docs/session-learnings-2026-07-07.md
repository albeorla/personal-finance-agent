# Session learnings backlog (2026-07-07)

Mined from a daily-ritual work session, then each item verified against the
current source before landing here. Anchors are `file:line` at time of writing.
Ordered for an implementation pass: real bugs first, then modeling gaps, then the
context-leanness contract, then already-shipped items.

## Tier 1 - correctness bugs (do first)

### 1. Statement recompute can wipe a good estimate to inputs-only
- **Symptom:** moving a bill from checking to a card did not reliably reach the
  open card statement estimate; a recompute briefly overwrote a confirmed
  estimate with an inputs-only sum.
- **Where:** `statements.py:49` (aggregation) and `statements.py:310` (recompute).
  Aggregation only counts a converted row when it has
  `cash_flow_treatment='card_statement_input'` + a matching
  `statement_target_obligation_id` + a projectable status; unbound converted rows
  are invisible. Recompute defaults `baseline=0.0` and writes `baseline + input_sum`,
  so an unconfirmed estimate is clobbered before the warning returns.
- **Fix:** report or reject unbound card inputs; make recompute require an explicit
  baseline or preserve the existing estimate when none is supplied.
- **Impact: high. Effort: med.**

### 2. One payment covering multiple obligation instances cannot be matched
- **Symptom:** a single bank debit covered two modeled instances; the matcher is
  strictly one-transaction-to-one-instance, so a phantom future outflow survived
  and the workaround was to retire the whole obligation.
- **Where:** `reconciliation.py:89` - `claimed` transaction set, `_scored_candidates`
  compares one txn amount to one instance, `_record_match` stores one instance per
  match.
- **Fix:** add a pre-pass that tries same-direction, nearby-date instance groups
  whose summed amount equals one transaction, then records the same
  `transaction_id` on each with group evidence. Never auto-merge the obligations
  themselves; this is a match, not a merge.
- **Impact: high. Effort: med.**

### 3. Past-due unpaid instances are not carried into the runway as due-now
- **Symptom:** an unpaid item dated before the projection start could stop showing
  as owed; a still-uncleared check sat before `start_date` and was not in the
  forward window.
- **Where:** `cashflow.py:187` drops pre-start instances; `_roll_forward_to_start`
  only nets items between snapshot date and start date; `_count_past_due_unreconciled`
  returns only a count. Partially handled (the digest can warn), but the owed
  dollars are not carried as due-now events.
- **Fix:** return the omitted instance ids + amounts and insert them at `start_date`
  as due-now events (or a starting adjustment), carefully avoiding double-count
  against the roll-forward gap.
- **Impact: high. Effort: med.**

## Tier 2 - modeling + tooling gaps

### 4. No atomic checking-activity CSV importer
- **Symptom:** the operating checking account is manual-sourced; keeping it fresh
  means pasting a bank activity export and hand-setting the balance after shell-
  parsing rows. The card importer handles card statements only.
- **Where:** `server.py:568`, `card_import.py:285` (card flow has a generic CSV
  parser, but no checking path).
- **Fix:** one small `import_checking_activity` that writes checking transactions
  and the balance snapshot in a single DB transaction - `csv.DictReader`, existing
  account matching, deterministic transaction ids, optional balance snapshot.
- **Impact: high. Effort: med.**

### 5. Discretionary paydowns are modeled like fixed obligations
- **Symptom:** optional debt sweeps behave as fixed outflows, so they must be
  hand-reduced every time to protect the cash floor.
- **Where:** `obligations.py:101` stores `amount_discretionary` but
  `surface_queue.py:350` only uses it to word the reminder; `cashflow.py:205` still
  subtracts the full amount; `guardrails.py:138` only warns after the floor breaks.
- **Fix:** split a required-minimum payment from the optional sweep; cap only the
  optional part to available headroom above the cash floor in the projection.
- **Impact: high. Effort: med.**

### 6. No arrival detector for non-payroll deposits
- **Symptom:** reimbursement-type deposits are too unreliable to project as
  scheduled income but matter when they land; the durable rule became "flag
  non-payroll deposits" rather than model flat future inflows.
- **Where:** `income.py:135` (scheduled income), `drift.py:72` / `drift.py:291`
  (general drift + recurring inflow candidates) - no one-off arrival detector.
- **Fix:** small positive-transaction detector for recent deposits not matched to
  scheduled payroll/income; surface as an observed "deposit arrived" event, not a
  projected income stream.
- **Impact: med. Effort: med.**

### 7. No transfer primitive (own-account movements modeled as income rewrites)
- **Symptom:** moving money between the user's own accounts is modeled by
  rewriting income instances, which is brittle and low-confidence.
- **Where:** `schema.py:29` has no transfer entity (only obligations, instances,
  income, debts, candidates); `income.py:26` makes one-sided inflow instances;
  `onboarding.py:593` can label `internal_transfer` but the auto path skips
  transfer-like candidates and manual apply still writes a one-sided instance.
- **Fix:** smallest transfer-schedule model (`from_account_id`, `to_account_id`,
  amount, cadence/date); cash flow includes only the signed effect on the selected
  working account.
- **Impact: high. Effort: high.** Biggest item; scope carefully or defer.

## Cross-cutting - response-shaping contract (context leanness)

Every read tool must be lean by default without hiding anything critical. The base
mechanism already exists (see "already shipped" B4 below); this is enforcement +
a consistent contract, not a rebuild.

- **Compact by default, full behind an explicit `verbose`/`include` param.**
- **A fixed critical-field set always surfaces regardless of verbosity:** guardrail
  / cash-floor status, `needs_review`, estimate / low-confidence flags, staleness
  warnings, drift over threshold. These are never truncated away.
- **Large collections paginate or cap** with an explicit "N more, call with X"
  pointer - never a silent dump. Smallest concrete add today: `limit`/`offset` on
  `list_obligations`.
- **Audit task:** walk every read tool in `server.py` and confirm it meets the
  contract; add the missing caps.
- **Impact: med. Effort: low-med.**

## Already shipped this session (verify only, don't rebuild)

- **Working-balance freshness gating.** `status.py:218`, `cashflow.py:23`,
  `digest.py:1036`, `surface_queue.py:112`, `server.py:444` - tighter stale
  threshold on checking, `working_account_balance_date_stale` exposed, status
  capped at YELLOW before RED floor alarms, `confirm-live-balance` surfaced. Action:
  add a regression test; optionally a "fresh-but-manual balance is lower
  confidence" flag if that becomes policy.
- **Compact/paginated reads (base).** `get_daily_digest` compact-by-default with
  `verbose=true`; `list_transactions` has filters + capped `limit`; `list_obligations`
  has filters, date windows, `include_instances`, `compact`. Remaining work is the
  contract audit above.
- **Human-readable Todoist titles.** `surface_queue.py:341`, `todoist_outbox.py:595`
  - write path already renders actions like "Pay <bill> $N"; only the read-only
  queue `message` is still data-like. Polish only if that computed surface is the
  one that matters.
