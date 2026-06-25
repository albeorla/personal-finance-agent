# Go-Live Readiness Report — financial-agent-mcp (v1.1)

Date: 2026-06-21. Verdict: **GO for daily PARALLEL-RUN use** — run it alongside
the legacy `just daily` ritual for ~1 week, cross-check with `compare_to_legacy`,
then retire the legacy path once they agree. The documented residuals below are
not blockers; they are bounded, honest limitations.

## What this was

Two efforts. (1) A go-live hardening loop that installed the MCP server into a
sandbox copy and ran 9 adversarial QA rounds (shipped as v1.0.0, GO for daily
use). (2) A gap-filling loop (this report) that closed the four biggest gaps,
installed the server into the real workspace for parallel-run, and ran 4 more
adversarial rounds until no HIGH/CRITICAL remained.

## Gaps closed (G1-G4)

- **G1 - "Did it clear?"** A historical backfill materializes the past instances
  each recurring OUTFLOW obligation implies, reconciles them against real
  transactions, and surfaces a "Recently Cleared" digest section. Rent ($3,000)
  and the Amex loan ($500.84) show as cleared against their real payments. It only
  asserts what it can prove: unmatched history is canceled, never guessed.
- **G2 - Complete the projection.** High-confidence direct-checking recurring
  charges that were unmodeled (the $580.84/mo Volvo car payment, a monthly fee)
  are auto-modeled as proper obligations, so the runway reflects them; the 60-day
  projection moved from an optimistic $8,584 to an accurate $7,372. Internal
  transfers are excluded.
- **G3 - Phantom duplicates.** Stale Todoist one-offs that duplicated a proper
  recurring obligation (NYT $28.62, Plex) are canceled.
- **G4 - Live install for PARALLEL-RUN.** Additive install into
  `~/dev/areas/finances`: a new `.mcp.json`, a NEW `finance-mcp` skill, and an
  appended instruction block. The server starts from the live workspace (55 tools,
  grounded). Your legacy `finance` skill, `just daily`, and `data/transactions.db`
  are untouched; Todoist write-back is left OFF.

## Hardening result (4 post-gap adversarial rounds)

The gap work introduced regressions; each round caught real ones and they were
fixed at the root. Severity trajectory: **R1 2 HIGH -> R2 2 CRITICAL -> R3 2 HIGH
-> R4 0 HIGH / 0 CRITICAL.**

- **R1 (HIGH):** the backfill flooded a "Possibly Overdue / may still owe" section
  with ~$16K of Amex bills that had actually cleared, plus inflows. Fix: backfill
  OUTFLOWS only; **removed that dangerous section** (the Drift section honestly
  frames items as "confirm whether these cleared"); drift rendering made
  actionable; runway warning names the real charge.
- **R2 (CRITICAL):** unmatched backfilled history (the loan posts ~9 days off its
  modeled day; the Amex statement is variable vs a flat estimate) became false
  CRITICAL "missing payment" alarms though both cleared. Fix: the backfill
  **cancels unmatched history** - only proven clears are asserted.
- **R3 (HIGH):** the same payments appeared in both "Recently Cleared" and "Matches
  to Confirm"; and the runway showed confident GREEN while its biggest bill (Amex
  statement) is a flat estimate. Fix: only `needs_review` matches await confirmation
  (mutually exclusive sections); removed the false "card spend rides the statement
  payment" reassurance; a material estimated bill now caps the runway at YELLOW
  with a CAUTION line.
- **R4:** no HIGH/CRITICAL (7 medium, 3 low - all documented residuals below).

## Verified state

- Full suite **235 passed**; git tree clean (77 commits this effort).
- `verify_grounding` TRUE on the real digest and status (39 source-traced checks).
- Server launches from the live workspace and serves 55 tools.
- Live Todoist review task is single and idempotent.
- Runway is an honest **YELLOW** (the biggest bill is a variable estimate).

## Documented residuals (bounded; not blockers)

1. **Variable card-statement amount.** The Amex statement payment is inherently
   variable ($4,800-$10,000); modeled as an estimate, it caps the runway at YELLOW
   with a caution. Per-card Apple/Chase statement obligations are not modeled, so
   ~$1,000/mo of non-Amex card spend is not in the projection. A deeper modeling
   improvement.
2. **Historical "did it clear?" is proof-only.** Off-cadence or variable past
   payments that can't be matched are canceled (silent), not asserted - so a bill
   that cleared at an unmatchable amount/date shows in neither Cleared nor missing,
   rather than risk a false alarm.
3. **Drift loose-match can coincidentally suppress** a same-day, near-amount item
   (a $300 paydown vs a $297.54 fee); and an instance due the day before the
   projection start sits in a 1-day blind spot. Bounded; tightening the matcher
   risks breaking exact-amount/no-merchant matches (e.g. rent checks), so left as-is.
4. **Reconciliation does not auto-mark paid** (by design); a cleared instance can
   stay status `expected` while "Recently Cleared" shows its match as evidence.
5. **Spending "Other" ~12%** - a deterministic categorizer misses some local
   merchants (e.g. a Toast-POS restaurant string).
6. **`liquid_available`** counts any account with balance >= 0 as a deposit
   (a paid-off card reads as deposit liquidity); `account.kind` is unpopulated, so
   deposit-vs-debt is inferred by balance sign (grounded).
7. **Recurring "$/mo" for unmodeled charges** is a ranking-derived estimate, not a
   measured monthly figure; **parity** (`compare_to_legacy`) is advisory.

## Remaining steps (yours)

1. Run the new digest alongside `just daily` for ~1 week; cross-check with
   `compare_to_legacy`. Retire the legacy ritual once they agree.
2. Set `TODOIST_WRITE_ENABLED=1` in `~/dev/areas/finances/.env` when you want the
   server to write the daily review task (verified idempotent).
3. Optionally model per-card statement obligations (Apple/Chase) to fully close
   the card-spend projection gap.
