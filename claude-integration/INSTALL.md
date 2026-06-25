# Installing the finance harness into the finances workspace

These assets are **staged here, not installed**. They wire Claude Code in
`~/dev/areas/finances` to use the local finance MCP server as the source of
truth for the daily ritual. Install them deliberately when you are ready to cut
over — nothing here touches the legacy workspace until you copy it.

## What's in this directory

- `finance-skill/SKILL.md` — the operating procedure skill (refresh, status,
  reconcile + confirm, triage, parity).
- `finance-instructions.md` — the baseline rule block ("financial claims must use
  the finance MCP tools") to paste into the workspace `CLAUDE.md` / `AGENTS.md`.
- `mcp-registration.json` — the `.mcp.json` entry that registers the server.

## Install steps (run from the finances workspace, when ready)

1. **Register the server.** Either:
   - `claude mcp add financial-agent -- uv run --directory /Users/owner/dev/financial-agent-mcp financial-agent-mcp`, or
   - merge the `financial-agent` entry from `mcp-registration.json` into
     `~/dev/areas/finances/.mcp.json`.
2. **Install the skill.** Copy `finance-skill/` to
   `~/dev/areas/finances/.claude/skills/finance/` (so the file lands at
   `.claude/skills/finance/SKILL.md`).
3. **Add the rule block.** Append the contents of `finance-instructions.md` to
   the workspace `CLAUDE.md` (or `AGENTS.md`).
4. **Confirm credentials.** The server reads `SIMPLEFIN_ACCESS_URL` and
   `TODOIST_API_TOKEN` from `~/dev/areas/finances/.env` at runtime (never logged
   or committed).
5. **Smoke test.** In a Claude Code session in the workspace, ask "what's my
   working cash and what's due this week" and confirm it calls `get_daily_digest`
   / `get_finance_status` rather than guessing.

## Before relying on it (parallel-run)

Run the new digest alongside your existing `just daily` for a week or two and use
`compare_to_legacy` (point it at a freshly regenerated `cash-flow.md`) to confirm
the two systems agree. Retire the legacy ritual only once parity holds.

## Not done automatically (your call)

- Enabling live Todoist write-back (the outbox stays dry-run until you turn it on).
- Any of the install steps above — this directory only stages them.
