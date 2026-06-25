# Finance instructions block

Paste this into the `CLAUDE.md` (or `AGENTS.md`) of the finances workspace
(`~/dev/areas/finances`) when you install the finance MCP server. It is the
baseline rule layer that makes Claude Code treat the finance tools as the source
of truth.

---

## Finance: tools are the source of truth

This workspace has a local finance MCP server (`financial-agent`). For ANY
financial claim — a balance, an amount owed, a due date, a cash-flow projection,
whether a bill was paid — you MUST call a finance MCP tool and base the answer on
its result in the same turn. Never state a dollar figure, date, or balance from
memory, from a file you read earlier, or by estimation.

- Start money questions with `get_daily_digest` or `get_finance_status`. Lead
  with working cash (the operating checking account), not the debt-mixed total.
- Follow the `finance` skill for the step-by-step ritual (refresh, status,
  reconcile + confirm, triage discovered charges, parity check).
- Cash-flow truth lives in `obligation_instances`. Discovered recurring charges
  are proposals in the onboarding queue until applied; they do not affect
  projections until then.
- Marking an obligation paid requires an explicit user confirm plus a recorded
  transaction match (`confirm_reconciliation_match`). Never auto-pay.
- Writing to Todoist is dry-run only unless the user has explicitly enabled live
  integration. Do not execute the action outbox otherwise.
- If you do not have a tool result for a figure, say so and call the tool. Show
  provenance and any `needs_review` / estimate flags the tools return.

The legacy `just`-based ritual is being retired in favor of these tools; prefer
the MCP server over the old commands.
