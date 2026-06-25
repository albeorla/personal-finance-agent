# personal-finance-agent

A local [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives an AI agent grounded, evidence-backed tools for managing personal finances.

It is deterministic and it never invents numbers. Every balance, due date, projection endpoint, and cash-flow figure the agent reports is backed by a tool result computed over a local SQLite database — not estimated, not recalled from a prior message. A built-in grounding check (`verify_grounding`) exists specifically to confirm that each headline dollar figure traces back to a source row before the agent is allowed to state it.

The server runs entirely on your machine, talks to your own data sources (a bank-aggregation feed and a task manager), and exposes that data to an MCP client (such as Claude Code) as a catalog of finance tools.

---

## Architecture at a glance

```mermaid
flowchart LR
    Claude["Claude / MCP client"] <-->|"tool calls"| Server["Finance MCP Server<br/>v0.2.0, 62 tools"]

    SimpleFIN["SimpleFIN<br/>balances + transactions"] -->|"sync_simplefin"| Server
    Portals["Bank/card portals<br/>manual balance inputs"] -->|"set_manual_balance"| Server

    Server <-->|"read/write source rows"| SQLite[("SQLite finance DB<br/>balances, transactions, obligations, instances")]
    Server <-->|"decisions, corrections, facts"| Memory[("finance_memory")]

    Server -->|"surface_due_items_to_todoist<br/>deduped by emissions ledger"| Todoist["Todoist Finance project"]
    Todoist -->|"completion read-back"| Server

    classDef client fill:#e7eff7,stroke:#1f4e79,color:#17202a
    classDef server fill:#e8f2ec,stroke:#3d7b65,color:#17202a
    classDef source fill:#f5ecdd,stroke:#b87922,color:#17202a
    classDef output fill:#f4e6ea,stroke:#aa4a5d,color:#17202a
    classDef store fill:#edf0f2,stroke:#2f3b47,color:#17202a
    class Claude client
    class Server server
    class SimpleFIN,Portals source
    class Todoist output
    class SQLite,Memory store
```

```mermaid
flowchart LR
    subgraph Ingest["1. INGEST"]
        Sync["sync_simplefin<br/>pull balances + transactions"]
        Manual["set_manual_balance<br/>correct stale balance-only feeds"]
        ReadBack["reconcile_todoist_completions<br/>absorb task completions"]
    end

    subgraph Model["2. MODEL"]
        Candidates["scan_charge_onboarding_candidates<br/>discover recurring charges"]
        Reconcile["reconcile_obligation_instances<br/>match expected to observed"]
        Projection["get_finance_status / get_daily_digest<br/>project cash flow from obligation_instances"]
        Guardrails["evaluate_guardrails<br/>cash floor, drift, debt order"]
    end

    subgraph Surface["3. SURFACE"]
        Queue["get_surface_queue<br/>prioritize what needs attention"]
        Digest["get_daily_digest<br/>status, working cash, upcoming obligations"]
        TodoistPush["surface_due_items_to_todoist<br/>write-gated, deduped output"]
    end

    Ingest --> Model --> Surface
    Surface -. "next daily run" .-> Ingest
```

More durable Mermaid diagrams live in [docs/diagrams.md](docs/diagrams.md).

---

## What it does: INGEST -> MODEL -> SURFACE

The server implements a single loop. Each stage is deterministic and idempotent, and none of it mutates the original source database.

### 1. INGEST — pull the facts in

- **SimpleFIN sync** (`sync_simplefin`) pulls live accounts, balances, and transactions into the local DB via idempotent upsert (default 90-day window; `incremental` mode resumes from the last synced transactions so a daily run stays cheap). Read-only against SimpleFIN.
- **Manual balance snapshots** (`set_manual_balance`) handle balance-only feeds that refresh slowly — for example a card whose portal shows "Updated Monthly". A manual snapshot is written as an ordinary `balance_snapshots` row (`source='manual'`) and is treated as authoritative for its calendar day, so the agent reads current reality instead of a stale feed value.

### 2. MODEL — turn facts into a forecast

- **Obligations and dated instances** are the cash-flow truth. A durable obligation plus its exact dated `obligation_instances` drive a **deterministic, day-by-day cash-flow projection** over the requested windows. The projection reads only obligation instances — nothing else can move the forecast.
- **Recurring-charge onboarding** discovers candidate recurring charges from transaction history and stages them in a review queue. Candidates are *not* cash-flow truth: they never write obligation instances and cannot change the forecast until a human accepts and applies them.
- **Reconciliation and drift** match expected instances against observed transactions, and flag missing payments, stale estimates, amount changes, and unmodeled recurring charges.
- **Goals** track savings targets and pace; **follow-ups** are dated reminders the daily routine fires on.
- **Guardrails** carry forward operating rules of thumb as explicit, evidence-backed checks (for example a cash floor: the projected lowest balance must not drop below a threshold).

### 3. SURFACE — push what needs attention

- A daily routine collects everything worth acting on today (matches to confirm, goals behind pace, estimates past review, stale balance-only snapshots, guardrail trips) into one prioritized queue (`get_surface_queue` / `get_daily_digest`).
- `surface_due_items_to_todoist` pushes those items to Todoist through an **idempotent emissions ledger**. Each item maps to a stable surface key, so the same item maps to the same task across days and re-runs: a new item is created, an unchanged item is skipped, a changed item updates the existing task *in place*, and a task the user completed or deleted is treated as resolved and never recreated. No duplicates.

---

## Tool catalog

The server registers 62 MCP tools. They group by area as follows. (Names are exact; see `src/financial_agent/server.py` for signatures.)

**Status, projection, and digest**
- `get_finance_status` — balances, source freshness, deterministic cash-flow projection over requested windows, guardrail findings, with `trace_id` and result references.
- `get_daily_digest` — the human-readable morning summary (working cash, multi-window projection, upcoming obligations with running balances, drift/review items, recurring candidates, and a GREEN/YELLOW/RED status), each with provenance.
- `summarize_spending` — outflow spending by category / merchant / month with totals, a month-over-month trend, and the transaction ids behind each bucket (rules-based, no LLM).
- `verify_grounding` — the "is the agent allowed to say this number" gate: confirms each headline figure traces to a source row.

**Obligations and instances**
- `apply_obligation_instances`, `delete_obligation_instance`, `list_obligations`
- `list_obligation_review_candidates` — estimated amounts whose review date has arrived (for example a statement estimate to refresh after close).
- `list_statement_input_estimates` — card-spend estimates that feed statement estimates without directly reducing checking cash flow.

**Income and calendar**
- `list_income_sources`, `apply_income_source`, `generate_income_instances`
- `import_calendar_facts`, `list_calendar_facts` — normalized pay-date and business-closure facts that drive income scheduling.

**Recurring-charge onboarding** (discover -> review -> apply)
- `scan_charge_onboarding_candidates` — deterministic background discovery; proposes candidates, never writes canonical obligations.
- `list_charge_onboarding_queue`, `get_next_charge_onboarding_candidate` — work the queue, prioritized by estimated monthly cash impact.
- `record_charge_onboarding_decision` — `defer` / `reject` / `needs_more_evidence` / `in_review` / `accept` / `reset`.
- `preview_charge_onboarding_apply` — read-only preview of what applying would create.
- `apply_charge_onboarding_candidate` — guarded write that promotes an accepted candidate into a canonical obligation plus instances (idempotent: re-applying a window updates in place).
- `auto_model_high_confidence_recurring`, `backfill_recurring_instances`

**Statement cycles** (for card-statement-payment obligations)
- `aggregate_statement_inputs`, `list_statement_cycles`, `recompute_statement_estimates` — roll card-input charges into the statement cycle that pays them; never overwrites a confirmed/observed amount.

**Reconciliation and drift**
- `reconcile_obligation_instances` — match expected instances to observed transactions (conservative by default; never silently marks paid).
- `list_matched_obligation_instances`, `list_unmatched_obligation_instances`
- `list_reconciliation_review_items`, `confirm_reconciliation_match`, `unconfirm_reconciliation_match` — confirming a match marks an instance paid using its recorded transaction match (guarded — never auto-pays).
- `detect_drift`, `list_drift_findings`

**Guardrails**
- `evaluate_guardrails`, `list_guardrail_findings`, `apply_guardrail_rules`

**Goals**
- `set_goal`, `list_goals`, `set_goal_override`

**Follow-ups and the surface queue**
- `capture_followup`, `list_due_followups`, `resolve_followup`
- `get_surface_queue` — the single read for the daily surfacing job.

**Todoist output and the action outbox** (writes gated OFF by default; Todoist is output-only)
- `surface_due_items_to_todoist` — idempotent push via the emissions ledger.
- `reconcile_todoist_emission`, `reconcile_todoist_completions` — adopt pre-existing tasks; absorb user completions of tasks we pushed.
- `create_todoist_task`, `execute_action_outbox`, `list_action_outbox` — create a one-off reminder and process the durable outbox; nothing is sent externally unless write-back is explicitly enabled.

**Background runner and job health**
- `run_background_sync` — orchestrates the whole pipeline (sync -> scan -> reconcile -> detect drift -> suppress dormant estimates -> surface due items) as one auditable run with an ordered event log; a failing step is recorded and the run continues.
- `get_background_run`, `list_background_runs`, `get_job_health`

**Memory** (corrections, decisions, facts to recall)
- `write_finance_memory`, `search_finance_memory`, `list_finance_memories`, `delete_finance_memory` — a deterministic, dependency-free bag-of-words embedding with a context-control retrieval policy (similarity threshold, then top-k, then a token budget).

**Migration, validation, and parity** (one-time bootstrap and cutover)
- `apply_obligation_migration` — seed a fresh DB from legacy files once (not an ongoing input).
- `run_live_validation` — prove the pipeline on live data against a throwaway copy without touching the committed snapshot.
- `compare_to_legacy` — diff a legacy cash-flow file against the new digest and report differences with a severity each.

---

## Setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/) for dependency and environment management.
- Python >= 3.11 (declared in `pyproject.toml`).
- A [SimpleFIN](https://www.simplefin.org/) access URL for bank balances and transactions (optional; the server runs without it, just with no live ingest).
- A [Todoist](https://todoist.com/) API token if you want task-board sync and surfacing (optional).

### Run the server

```bash
uv run financial-agent-mcp
```

By default the server reads `data/transactions.source-copy.sqlite` (relative to the package). Override the path with the `FINANCE_AGENT_DB_PATH` environment variable.

### Register it as an MCP server

Add an entry to your MCP client's config (for Claude Code, the workspace `.mcp.json`). The server runs over stdio out of this repo via `uv`, so no install/copy of the code is needed:

```json
{
  "mcpServers": {
    "financial-agent": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/personal-finance-agent", "financial-agent-mcp"],
      "env": {
        "FINANCE_AGENT_DB_PATH": "/path/to/your/transactions.sqlite",
        "FINANCE_AGENT_ENV": "/path/to/your/.env"
      }
    }
  }
}
```

- `FINANCE_AGENT_DB_PATH` points the server at your local SQLite database.
- `FINANCE_AGENT_ENV` points at the `.env` holding your credentials (defaults to `~/dev/areas/finances/.env`). Setting it lets a registered server read a sandbox `.env` without touching any other workspace.

### Credentials (`.env`)

Credentials are read from the `.env` file at runtime and are never logged or committed. Create a `.env` with placeholder values like the block below — fill in your own:

```dotenv
# Bank / balance + transaction feed (SimpleFIN)
SIMPLEFIN_ACCESS_URL=https://USERNAME:PASSWORD@bridge.simplefin.org/simplefin

# Todoist task board (optional)
TODOIST_API_TOKEN=your-todoist-api-token
TODOIST_PROJECT_ID=your-finance-project-id   # optional

# Live Todoist write-back is OFF unless this is truthy (1/true/yes/on).
# With it unset, the outbox stays dry-run and makes no external calls.
TODOIST_WRITE_ENABLED=false                  # optional
```

The presence of each credential is surfaced as a safe boolean (`has_simplefin`, `has_todoist`) — the secret values themselves are never returned. If `TODOIST_PROJECT_ID` is unset, the server falls back to a legacy lookup; set it explicitly to drop that fallback.

---

## Running the tests

```bash
uv run --extra dev python -m pytest
```

Run pytest as a module (`python -m pytest`), not the bare `pytest` console script: the bare script can resolve to a system Python that lacks the `mcp` dependency and silently skip the MCP-layer wiring tests. Running as a module pins the project venv, so a green run shows 0 skipped.

---

## Privacy

**This repository contains no personal financial data.** All balances, transactions, obligations, and credentials live in your local SQLite database and your `.env` file, both of which are gitignored (`*.sqlite`, `*.db`, `.env`, and the `data/` directory are all excluded). The server is read-only against the upstream sources and is designed never to mutate the original feed; live ingest pulls into a local copy.

Todoist write-back is OFF by default. Even with credentials present, the action outbox stays dry-run until `TODOIST_WRITE_ENABLED` is explicitly turned on.
