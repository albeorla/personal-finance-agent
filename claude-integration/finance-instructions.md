# Finance instructions block

Paste this into the `CLAUDE.md` (or `AGENTS.md`) of the workspace where the
finance MCP server is installed. It is the baseline rule layer that makes
agents treat the finance tools as the source of truth. The installed copy in
`~/dev/finances-golive/AGENTS.md` ("Finance instructions block") is the live
example of this block in use.

---

## Finance: tools are the source of truth

This workspace has a local finance MCP server (`financial-agent`). For ANY
financial claim (a balance, an amount owed, a due date, a cash-flow projection,
whether a bill was paid) you MUST call a finance MCP tool and base the answer
on its result in the same turn. Never state a dollar figure, date, or balance
from memory, from a file you read earlier, or by estimation. Do not do
arithmetic on tool numbers in prose; take derived figures from a tool field,
or show the two tool numbers and label the delta explicitly as your own
estimate.

- Start money questions with `get_daily_digest` or `get_finance_status`. Lead
  with working cash (the operating checking account), not the debt-mixed total.
- Follow the `finance` skill for the step-by-step ritual (refresh, status,
  reconcile + confirm, triage discovered charges, parity check).
- Cash-flow truth lives in `obligation_instances`. Discovered recurring charges
  are proposals in the onboarding queue until applied; they do not affect
  projections until then.
- Marking an obligation paid requires a recorded transaction match
  (`confirm_reconciliation_match`). Posted transactions ARE the record: when
  the pairing is clearly evidenced (payee, amount, date), match and confirm
  without asking and report it; ask only when ambiguous. Never mark paid
  without transaction evidence.
- Never auto-merge or auto-deactivate similar-looking obligations. Flag
  suspected duplicates via `list_obligation_review_candidates` and require an
  explicit "yes, same bill" confirmation before merging or ending either one.
- Todoist write-back follows `TODOIST_WRITE_ENABLED`: when it is off, the
  action outbox is dry-run; when it is on, task create/update/complete/delete
  calls hit the real board. Distinguish the computed surface queue from what
  is actually on the board when reporting.
- If you do not have a tool result for a figure, say so and call the tool.
  Show provenance and any `needs_review` / estimate flags the tools return.
- Persist finance facts as they are established. When the user states a
  decision, correction, amount-with-source, date, or rule, write it to MCP
  memory (`write_finance_memory`) in the same session; the MCP memory is the
  durable store, the conversation is not.
- When the user corrects an account fact, re-read that account's live state
  with a finance MCP tool (`get_finance_status` or `get_daily_digest`) in the
  same turn before acting on the correction. Do not delete or reschedule a
  reminder, complete a follow-up task, or write a memory from a claimed or
  inferred account fact you have not re-read from a tool this turn. Never
  assume two accounts or feeds are the same one; confirm the specific account
  first.
- If an attached image, infographic, or statement renders as an empty or
  placeholder icon with no visible content, do NOT build income-split or
  payoff math on it. Treat any number read off an infographic or statement
  image as approximate until cross-checked against a tool result, and ask for
  a re-send before analyzing.
