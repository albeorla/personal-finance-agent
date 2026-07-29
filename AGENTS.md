# financial-agent-mcp

Project-specific notes. User-level defaults in `~/.codex/AGENTS.md` apply; this file only adds what is specific to this repo.

## What this is
The finance MCP server (89 tools over a local SQLite database). Development happens here in the private repo; the public copy is published as `personal-finance-agent`. The live install serving daily use is the finances workspace at `~/dev/finances-golive` (its DB, `.env`, and rule block live there, not here). See `README.md` for architecture and `docs/` for design.

## Hard constraints
- Never point dev or test runs at the live database (`~/dev/finances-golive/finance-agent.sqlite`). Tests use fixtures/temp DBs; anything touching `FINANCE_AGENT_DB_PATH` must use a scratch path.
- Cash-flow truth is `obligation_instances`; discovered recurring charges are proposals until applied and must not affect projections.
- No live writes (Todoist, SimpleFIN) and no new dependencies without explicit approval.

## Working here
- Tests: `uv run pytest -q` (about 945 tests). Run before handoff.
- `claude-integration/` holds the staged install assets (skill, instructions block, MCP registration); `tests/test_integration_assets.py` guards their wording, so run it after editing them.
- `hooks/pre-commit` exists; see `hooks/README.md`.
