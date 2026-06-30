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

## Schedule the daily run

The server ships a `financial-agent-daily` entry point (runs ingest -> model ->
surface in-process). There is no built-in scheduler — the daily cadence lives at
the OS layer. On macOS, a launchd agent is the durable way; author the plist once
per machine (every field is machine-specific):

```xml
<!-- ~/Library/LaunchAgents/com.finance.daily.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.finance.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/uv</string><string>run</string>
    <string>--directory</string><string>/ABSOLUTE/PATH/TO/financial-agent-mcp</string>
    <string>financial-agent-daily</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>FINANCE_AGENT_DB_PATH</key><string>/ABSOLUTE/PATH/TO/finance-agent.sqlite</string>
    <key>SIMPLEFIN_ACCESS_URL</key><string>...</string>
    <key>TODOIST_API_TOKEN</key><string>...</string>
  </dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/finance-daily.log</string>
  <key>StandardErrorPath</key><string>/tmp/finance-daily.err</string>
</dict></plist>
```

Load and test it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.finance.daily.plist
launchctl kickstart -k gui/$(id -u)/com.finance.daily   # run once now
```

Linux/cron alternative (one line): `0 8 * * * cd /PATH/TO/repo && FINANCE_AGENT_DB_PATH=/PATH/db uv run financial-agent-daily >>/tmp/finance-daily.log 2>&1`

## Not done automatically (your call)

- Enabling live Todoist write-back (the outbox stays dry-run until you turn it on).
- Any of the install steps above — this directory only stages them.
- Scheduling the daily run (the plist/cron above is a template; load it yourself).
