# Claude Code Start Here

This is the standalone finance MCP server project.

Before implementing, read these files in order:

1. `AGENTS.md`
2. `README.md`
3. `BUILD_PLAN.md`
4. `HANDOFF.md`
5. `CLAUDE_CODE_HANDOFF.md`

Current implementation target:

Build the charge-onboarding candidate queue and proposal workflow. Do not skip
straight to applying obligations into cash flow. The first slice should discover
candidate charge patterns from copied transactions, store reviewable candidates,
and expose MCP tools to list and walk the queue.

Safety boundary:

- Work only against `data/transactions.source-copy.sqlite`.
- Do not mutate `/Users/owner/dev/areas/finances/data/transactions.db`.
- Keep candidates separate from canonical obligations until accepted/applied.
- Keep Todoist out of source-of-truth projection logic.

Verification:

```bash
uv run --extra dev pytest -q
```

