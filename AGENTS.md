# Financial Agent MCP Instructions

This project is the standalone implementation home for the local finance MCP
server. It was moved out of `/Users/owner/dev/interview-prep` on 2026-06-21
so interview prep can return to system-design practice.

## Start Here

Before changing code or data, read:

1. `README.md` for setup, commands, and current tools.
2. `BUILD_PLAN.md` for product and architecture decisions.
3. `HANDOFF.md` for the current tactical pickup point.

## Safety Rules

- Use the copied SQLite database in `data/transactions.source-copy.sqlite`.
- Do not mutate `/Users/owner/dev/areas/finances/data/transactions.db`.
- Treat `/Users/owner/dev/areas/finances` as the live operational system
  until cutover is explicit.
- Todoist is a reflection/action surface, not the canonical source for cash-flow
  projections.
- Financial claims must be traceable to tool output, explicit calculations, or
  rows in the copied database.
- Add Todoist or external write actions only behind explicit configuration,
  preview/dry-run behavior, and idempotency.

## Collaboration Style

- Build one vertical slice at a time with the user in the loop.
- When a product decision is ambiguous, state the tradeoff and ask before
  encoding it.
- Prefer deterministic finance logic first. Use an LLM only for ambiguous
  interpretation, review wording, or suggestions that preserve evidence and
  confidence.
- Keep this MCP server generic and reusable, but optimize V1 around replacing
  the user's current Claude Code-assisted finance ritual.

## Verification

Run this after code changes:

```bash
uv run --extra dev python -m pytest -q
```

Use `python -m pytest`, not the bare `pytest` console script. The bare script can
resolve to a system Python that lacks the `mcp` server dependency, which silently
SKIPS the two MCP-layer wiring tests (`test_compact_parameters_flow_through_server`
and `test_surface_tool_prepends_sync_failed_item_when_flag_set`). Running pytest as
a module pins the project venv (which has `mcp`) so those tests actually execute —
a green run must show 0 skipped.

Useful smoke check:

```bash
uv run python - <<'PY'
import financial_agent.server as server
print("server_tools_loaded", hasattr(server, "list_obligation_review_candidates"))
PY
```

