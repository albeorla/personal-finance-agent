# Agent Build Project — From-Scratch Harness

Created 2026-06-19. Purpose: close the "never built an agent from scratch" gap by building a small, simplified-Claude-Code-style agent. This project produces lived answers to the 5 AI/agent practitioner questions in `INTERVIEW.md`. Build from scratch first; LangGraph is a later second pass for framework vocabulary only.

The domain is fixed so it maps to question 1: **an agent that provides natural language access to financial data.**

Implementation location: `/Users/owner/dev/financial-agent-mcp`. This was moved out of `/Users/owner/dev/interview-prep/implementations/financial-agent` on 2026-06-21 so interview prep can stay focused on system-design practice.

## Dogfood Target

Build this around the user's existing local finance ritual in `/Users/owner/dev/areas/finances`, not a generic finance chatbot.

Current workflow patterns to preserve:

- Scheduled daily ritual: `jobs/finance-daily` runs `just daily WINDOWS="7,14,30"` at 08:10 local time with a lock directory and append-only event log.
- Command surface: `just sync`, `just balances`, `just recent`, `just search`, `just reconcile`, `just drift`, `just cashflow`, `just review`, `just board-review`, and `just daily`.
- Current source-of-truth split in the legacy finances project: SimpleFIN and Todoist snapshots in SQLite for current state/evidence, Todoist for forecasted obligations, `cash-flow.md` for the active verified near-term window, and durable docs for rules. This is legacy behavior to replace, not the target canonical model.
- Guardrails: cash floor, drift threshold, window-age check, recurring-disagreement check, and debt-avalanche ordering.
- Existing agent pattern: local `finance` router dispatches to ritual modes such as `daily`, `drift`, `refresh`, `weekly`, and `monthly`.

The first product design should be a local finance ritual assistant with a UI: it should explain the current status, run or inspect ritual steps, show which source each conclusion came from, surface decisions needed, and keep tool calls plus telemetry inspectable.

The first access point should preserve the current command-line workflow, but the chat harness should be Claude Code. The implementation should expose the finance system through a local MCP server that Claude Code can call, rather than rebuilding a separate chat interface before the domain tools are useful.

Harness direction:

- Primary interactive harness: Claude Code with a local finance MCP server exposing tools such as `query_transactions`, `summarize_transactions`, `render_result`, and later `create_review_task`.
- Product CLI: out of scope. Do not design a separate CLI chat product.
- Developer/test commands: acceptable only if needed to run the MCP server locally, inspect telemetry, seed copied data, or test deterministic tools. These are not the user-facing application.
- Future standalone chat/UI: out of scope for the first implementation. Revisit only after the MCP tool contracts and grounding behavior are stable.
- Background worker: distinct from the chat harness. It runs scheduled refresh, reconciliation, source-health checks, and review-task creation without requiring an active chat session.

LLM integration direction:

- Use Claude Code as the model-facing harness so the user can rely on the Claude account/subscription workflow already used in Claude Code.
- Do not build the first implementation around direct Anthropic API-key billing.
- The project should own deterministic finance tools, source access, telemetry, grounding checks, renderers, and guarded actions. Claude Code owns the LLM chat loop for the first implementation.
- To make Claude Code behave like a purpose-built finance harness, combine project instructions, a finance skill, and MCP tool design: update the local Claude Code instructions in `/Users/owner/dev/areas/finances` to explain when and how to use the finance MCP server, add a reusable finance skill for the step-by-step operating procedure, add clear "must use finance tools for financial claims" rules, use high-signal tool names/descriptions, return structured tool outputs with provenance, and expose at least one higher-level finance-answering tool for common end-to-end flows.

Claude Code control layers:

- Local project instructions: baseline rule layer for the finances workspace. They tell Claude Code that financial claims require finance MCP tool evidence and define the default safety posture.
- Finance skill: reusable procedural layer. It should define how to handle common finance workflows such as asking a money question, reviewing source health, interpreting reconciliation results, deciding when Todoist review is needed, and formatting the final answer with evidence.
- Finance MCP server: executable capability layer. It owns source-of-truth reads, deterministic calculations, provenance, telemetry, render payloads, and guarded Todoist actions.
- Background runner: proactive execution layer. It runs scheduled refresh/reconciliation/Todoist review generation without requiring an active Claude Code chat session.

Skills should guide Claude Code's behavior, not replace the MCP server. A skill can say "call `analyze_finance_question` first for financial questions," but the MCP tool is what actually queries data, computes totals, records telemetry, and performs guarded writes.

Todoist and proactive action layer:

- Todoist is an action/sync surface, not only a renderer. The system should be able to create or update Finance review tasks proactively when reconciliation finds missing guidance, stale sources, or candidate matches.
- Proactive Todoist updates cannot depend on an active Claude Code chat session. They belong to a background runner that uses the same domain services and action code as the MCP tools.
- The background runner should be deterministic-first, not deterministic-only. It can refresh data, compute source health, reconcile obligation instances, and generate obvious review candidates from rules. It may use an LLM for ambiguous interpretation, such as merchant normalization, category suggestions, recurring-pattern explanation, or human-readable review wording.
- LLM-assisted background decisions should produce suggestions with evidence and confidence, not silently finalize financially meaningful state changes. Low-confidence or consequential ambiguity should become `needs_review`.
- Todoist writes should be guarded by configuration, support dry-run mode, and be idempotent. Use stable keys such as review date, obligation instance id, candidate id, and action type so reruns update the same task/subtask instead of creating duplicates.
- Store Todoist sync state locally: external task id, linked obligation instance or review batch, last pushed content hash, last observed Todoist state, sync status, and errors.
- Represent outgoing writes through an action/outbox table or equivalent durable queue: `pending -> in_progress -> succeeded -> failed/retryable -> needs_review`. This gives recovery, auditability, retries, and a clear place to inspect what the system tried to do.
- MCP tools can expose Todoist actions for interactive use, such as previewing a review batch or approving creation, but the scheduled worker is what makes the system proactive.

## Implementation and Deployment Target

The implementation in this repo should stay generic enough to be tested and explained as an AI/agent engineering project, but the finished product is intended to replace the current workflow in `/Users/owner/dev/areas/finances`.

Replacement should be staged, not a hard cutover:

- Build against copied data first, using `/Users/owner/dev/financial-agent-mcp/data/transactions.source-copy.sqlite`.
- Run in parallel with the current finance ritual until the new system can explain cash flow, obligations, source freshness, and review items at least as well as the existing commands.
- Add write actions, such as Todoist review task creation, only behind explicit configuration.
- Deploy the usable tool as a UI plus background maintenance runner once the agent loop, grounding checks, telemetry, and obligation model are reliable.
- Cut over only when the new tool can replace the current daily command flow for practical use: refresh data, inspect cash flow, reconcile obligations, surface review work, and explain every financial conclusion from source evidence.

Until cutover, `/Users/owner/dev/areas/finances` remains the live operational system. This project should not mutate the source finance database directly.

V1 parity scope:

- Do not clone every existing command in `/Users/owner/dev/areas/finances`. Many commands are legacy/supporting surfaces, not the actual daily workflow.
- The goal is to publish this implementation as a real reusable MCP server that can be pulled into `/Users/owner/dev/areas/finances`. After the finances area uses the MCP server reliably, a separate agent session should be able to clear out the old local tooling and replace it with this server integration.
- V1 should replace the reliable Claude Code-assisted workflow the user actually uses:
  - Sync finances into the local database.
  - Show current balances and source freshness.
  - Query and explain cash flow.
  - Detect drift between expected cash-flow assumptions, current balances/transactions, local obligation records, and Todoist reflection state.
  - Detect recurring obligations or recurring-transaction candidates that should affect the forecast or Todoist review.
  - Update Todoist when the system needs review tasks or obligation/task changes.
- Existing commands such as recent, search, reconcile, and board-review are implementation inventory. Bring them forward only when they directly support sync, balances, cash-flow answers, drift/recurring detection, or Todoist updates.
- Drift and recurring detection should be scoped into V1, but redesigned as reliable system outputs. Today they are mostly handled by manual Claude Code reasoning over commands and files; V1 should turn them into explicit evidence-backed results.
- Do not enshrine Todoist as the V1 canonical obligation source. V1 may import or compare against current Todoist tasks for migration/parity, but the target cash-flow model should be driven by local obligation records plus observed bank evidence. Todoist should become the reflected task/review surface.
- Do not blindly port `finance.cashflow` from the legacy project. First verify which parts are actually used and deterministic. Bring forward only logic that supports the real workflow: balances, source freshness, deterministic cash-flow projection, drift/recurring detection, and Todoist review/update actions.
- V1 success means Claude Code plus the finance MCP server can handle the core ritual without manual command choreography: refresh data, report balances, answer cash-flow questions from source evidence, surface source-health/drift/recurring warnings, and create/update Todoist review items through guarded/idempotent actions.
- Packaging success means the MCP server can be installed or referenced from the finances area with clear setup instructions, without relying on hidden state in this interview-prep workspace.

Obligation bootstrap/refinement lane:

- Build local obligation truth organically from the sources that exist today, but do not treat any one of them as automatically canonical.
- Candidate sources:
  - `obligations.yaml`: structured carry-forward items with dates, amounts, labels, and source notes. Strongest first import source because it is machine-readable, but it contains stale/past items and duplicate history that need filtering.
  - `cash-flow.md`: human-maintained active-window narrative and rules. Useful for context, policies, and visible assumptions; parse cautiously because it can be stale and mixes tables, prose, decisions, and historical log.
  - Todoist snapshot tables: current reflected/planning tasks. Useful for migration comparison and action/reflection state; not the target source of truth.
  - `recurring_charges`: detector output from transaction history. Useful for recurring candidates and drift signals; confidence should affect whether a candidate is imported, shown as needs-review, or left informational.
  - `.remember/now.md` in the finances area: current session continuity and corrections. Useful as review context, not a durable canonical model.
- Import flow: pull candidates from all sources, normalize into candidate obligation/instance shapes, deduplicate by date/amount/name/source evidence, flag conflicts, and require confirmation or explicit policy before promoting to canonical local obligations.
- Initial safe behavior: write imported items as candidates or `needs_review` unless they come from a trusted structured source and are unambiguous. Todoist writes remain preview/guarded.
- Pay-source modeling decision:
  - Owner / IntelliBridge is a semi-monthly income schedule on the nominal 10th and 25th, rolled back to the previous business day for weekends/holidays. Payroll lands in personal checking `1793`; working-cash projections should model the transfer into joint checking `XXXX`, because that is when the operating account receives the cash. This is a working rule derived from observed transactions and should be verified against the annual pay calendar when available.
  - Partner / Town of Greenwich is a biweekly Friday payroll schedule, rolled back to the previous business day for holidays. It deposits directly into joint checking `XXXX`. This is easier to model from an anchored biweekly rule and should still be compared against observed deposits.
- Calendar-source modeling decision:
  - The scheduler should not depend directly on a live Google Calendar MCP session. The MCP server owns deterministic date calculation; calendar import is a separate source-ingestion concern.
  - Business-day calculation currently supports weekends, observed fixed-date US holidays, common US floating holidays, and explicit extra closure dates.
  - Google Calendar, Google Workspace CLI, payroll calendars, or manually curated annual pay calendars should feed explicit closure/pay dates into the finance system as source evidence. The scheduler then uses those dates deterministically.
  - If a calendar or schedule rule changes an already-generated income date, stale expected generated instances should be marked `canceled` so cash-flow projection does not double-count old and new dates.
  - Calendar facts are now local source evidence. `business_closure` facts affect business-day adjustment. `income_pay_date` facts can drive `calendar_dates` income schedules when an exact payroll calendar is available.
  - Do not build a live Google Calendar fetcher before the core obligation model is useful. Build fixed outflows first, then add a guarded source adapter that reads Google Calendar or Google Workspace CLI events, filters by configured calendars and title patterns, normalizes them into `calendar_facts`, and records provenance such as calendar id, event id, title, date, confidence, and imported time.
  - Example importer rule: a configured payroll calendar event whose title contains `Pay Date` becomes an `income_pay_date` fact linked to an `income_source`; an event whose title contains `Office closed` becomes a `business_closure` fact. The importer should only write facts, not cash-flow instances.
- Fixed-outflow modeling decision:
  - Start with exact dated instances before general recurring generation. This avoids pretending that ambiguous legacy forecast patterns are canonical.
  - Rent is the first fixed outflow because it is large, recurring, and more stable than variable card-statement estimates.
  - The first rent seed uses exact known dates from `/Users/owner/dev/areas/finances/obligations.yaml`: 2026-07-03 and 2026-08-04. A future pass can promote rent to a recurring rule after reviewing the due-date pattern.
  - The Amex Personal Loan autopay is the second fixed outflow because the amount and cadence were resolved by portal evidence: `500.84` due on the 27th.
  - Apple Card minimums and sweeps are exact dated plan items, not a recurring rule. Seed them as plan instances with confidence attached because several amounts are intentionally recomputed from live card balance at check-in.
  - Eversource electric estimates are exact dated utility outflow instances with structured estimator metadata. Current V1 policy uses the copied transaction-history average `$115.87` and applies a `1.5` summer multiplier, producing `$173.80` for June, July, and August 2026. They are included in projection with low confidence and should be replaced after actual bills are available.
  - Amex statement payments are baseline cash-flow obligations, not optional scenarios, because the payment is monthly, mandatory, on-time sensitive, and materially moves checking. The uncertainty belongs on the monthly instance amount lifecycle: estimated before close, statement-known after close, then paid after matching the checking transaction. Legacy source labels say Platinum, but the user reports the product moved to Gold, so the durable obligation should stay product-neutral as `Amex statement payment`.
  - Amex Cash Magnet should not be seeded from the legacy August estimate. The user confirmed on 2026-06-21 that Cash Magnet has been paid off, is at zero, and is not being used.
  - Small direct subscriptions can be batched when their blast radius is low. New York Times is seeded as a medium-confidence direct checking subscription because copied transactions show recurring `XXXX` payments; Plex via Venmo is seeded as low confidence because the legacy source is explicit but copied transaction matching is weak.
  - Anthem reimbursement estimates are modeled as low-confidence inflows. They belong in projection because they materially affect checking, but they need later reconciliation because transaction names appear as generic remote online deposits rather than clean Anthem rows.
  - Gault is seeded as card statement input, not as a direct checking outflow. Current V1 policy expects low summer/fall charges and larger winter heating-season charges based on copied Amex transaction history. These rows use `cash_flow_treatment = card_statement_input` and target `amex_statement_payment`, so they can feed statement estimates without double-counting checking.

Charge onboarding design:

- The MCP server should onboard new charges through an evidence-backed workflow, not by letting the chat model write raw obligation rows directly.
- Onboarding should normally start from background discovery, not from the user prompting for a specific merchant. The background runner scans transactions, groups charge patterns, and maintains a review queue until the discovered scope is exhausted.
- The conversational experience should process that queue one candidate at a time: show evidence, proposed policies, confidence, and the specific decision needed; then accept, edit, reject, defer, merge, split, or mark needs-more-evidence.
- The workflow should gather matching transactions, normalize merchant identity, classify the cash-flow impact, infer cadence or seasonality where possible, select policy templates, and return a reviewable proposal.
- A proposal should include durable obligation fields, schedule policy, amount policy, cash-impact policy, review policy, confidence, evidence count, and missing evidence.
- Applying a proposal should be a separate guarded action. Creating a candidate and accepting it are different state transitions.
- Queue state should be durable: `discovered -> proposed -> in_review -> accepted -> applied`, with alternate terminal or pause states `rejected`, `deferred`, `needs_more_evidence`, `merged`, and `split`.
- For Gault-like charges, the selected policy is seasonal card spend with `cash_flow_treatment = card_statement_input`.
- For Eversource-like charges, the selected policy is direct checking outflow with a seasonal amount estimator.

## Product Scope

The product should feel like a general personal finance assistant: the user can ask questions about their money, but the first durable workflows are cash flow and obligations.

Primary user flows:

- Cash-flow view: show projected cash position over configurable windows such as 7, 14, 30, and 60 days. Treat 90 days as useful later only if confidence is visible.
- Upcoming obligations: show bills, transfers, one-offs, and recurring obligations that affect the cash-flow window.
- Drift review: compare expected obligations against observed transactions and Todoist state, then surface missing, stale, duplicate, or unexpectedly changed items.
- Natural-language investigation: answer targeted questions about transactions, obligations, cash-flow changes, and why the projection moved.
- Data-health alerts: detect when a connected account or feed is stale, unauthenticated, or incomplete, such as Apple Card authentication failing, and tell the user what needs attention.
- Spending reports: summarize spending by merchant, account, period, and best-effort category. Use Tally-style merchant rules/enrichment where available, while showing confidence because merchants such as Amazon, CVS, and grocery stores can obscure the true category.
- Proactive background maintenance: run scheduled refreshes, pull new transactions, reconcile obligations, update source-health state, and create Finance Todoist review tasks when user input is needed.

Answering constraint:

- The assistant should accept generic personal-finance questions, but every financial answer must be grounded in tool calls against the current source of truth or an explicitly named derived calculation. If the tools cannot provide the evidence, the assistant should say what is missing rather than inventing or estimating numbers.
- Deterministic financial operations should live in tools, not in the model response. The model can interpret intent and choose tools, but filtering, summing, grouping, cash-flow calculation, and reconciliation should be performed by deterministic code.
- Tool inputs and outputs should be purpose-built contracts for the system, not leaked SQL details. A transaction-query tool should return structured data that can be consumed by later tools, final-answer generation, telemetry, and UI rendering.
- Renderer and action tools are valid system tools when they turn grounded structured results into a user-facing surface or downstream action, such as an inline view, UI panel, Todoist review task, or email digest. They must sit downstream of evidence and calculation tools; renderers should not invent financial facts.

Implementation rule:

- Build the smallest production-shaped vertical slice, not a disposable toy. A first implementation can be narrow, but its contracts should point toward the deployed finance assistant: structured source-of-truth reads, deterministic calculations, provenance, renderable outputs, telemetry, and guarded write/action tools.

Source-of-truth direction:

- The application database should own obligations.
- Todoist should become a reflection/action surface for obligation instances, not the canonical source of truth.
- Recurring obligations should remain active at the obligation level even when a specific Todoist task is completed or removed.
- One-off obligations should be representable without pretending they recur.
- The system should track synchronization state between database obligation instances and Todoist tasks, including completed, removed, stale, or recreated tasks.
- An obligation instance should be considered financially done only when matched to bank evidence where possible. Todoist completion is useful user intent/task state, but it is weaker evidence than a matching transaction.
- Keep financial reconciliation status separate from Todoist task state. `task_completed` should not be the primary obligation-instance status because completing a task is not proof that money moved.
- For the first version, use `needs_review` as the conservative fallback when an obligation is due and no matching bank transaction is found. Do not automatically call it overdue until the matching rules, source-health rules, and grace periods are reliable.
- During reconciliation, a new SimpleFIN transaction should match against existing expected obligation instances first. Todoist tasks should not be used as matching authority. If no expected instance matches, the system can create a `needs_review` candidate for a new one-off obligation or a new recurring obligation pattern.
- Candidate promotion rule: if an unmatched transaction has no similar history, treat it as a one-off candidate or unmatched finding. If similar transactions repeat across later periods, promote it to a recurring obligation candidate for review. Do not automatically create a canonical recurring obligation until the user confirms it or the confidence rule is strong enough.
- Similar transaction signals should include normalized merchant/name, payee/description tokens, amount or amount range, account, direction, date/cadence pattern, and Tally-style merchant resolution or category enrichment. Similarity should produce a confidence score and evidence, not an opaque yes/no.
- Matching should require user review before finalizing. Even high-confidence matches should create a review item, not silently mark an obligation instance paid.
- Review batching: create one Finance Todoist review task for the current day, with review candidates as subtasks or checklist items. Completing that review task means the user reviewed the batch; it is not itself bank evidence.
- Each review item should ask for the specific guidance the system needs to proceed, such as approve/reject match, classify as one-off, mark recurring, choose category, update amount range, or reconnect a source. The user can supply that guidance on the Todoist task or subtask.
- Recurring obligations should learn over time. Each accepted match should update that obligation's expected amount profile, such as typical amount, minimum, maximum, tolerance, variance, and confidence. This creates dialed-in ranges per recurring obligation instead of one global matching rule.

External systems:

- SimpleFIN is the transaction and balance ingestion source. It feeds local SQLite with current balances, balance snapshots, transactions, and source freshness signals.
- Tally is the merchant/category enrichment workflow for spending analysis. It uses transaction exports, `merchants.rules`, `views.rules`, `tally discover`, and `tally up` to produce repeatable summaries and unknown-merchant reports.
- Tally output should be treated as enriched classification evidence, not raw bank truth. Store category, subcategory, rule source, confidence, and unknown/unclassified state separately from the original transaction.
- Tally is a planned component for spending-report features. It should not block the first cash-flow or obligation reconciliation path, but the architecture should leave a clean enrichment/reporting lane for it.

Language rules:

- Prefer user-facing language in code and data models when it is accurate enough.
- Use separate internal terms only when the system needs a stricter distinction than ordinary user language provides.
- Example: `paid` is acceptable as a user-facing and likely internal financial status if it means "matched to bank evidence." If the system needs the implementation detail, use supporting fields such as `matched_transaction_id`, `matched_at`, and `match_confidence` rather than forcing the user to think in terms of `matched`.
- Initial financial statuses for obligation instances: `expected`, `paid`, `partially_paid`, `canceled`, and `needs_review`. Add `overdue` later only when the product has a clear confidence policy for declaring something late rather than review-worthy.

Likely domain objects:

- `Obligation`: the durable commitment, such as rent, card payment, insurance, subscription, reimbursement, or one-off bill.
- `ObligationInstance`: one scheduled occurrence of an obligation, which can be projected into cash flow and mirrored to Todoist.
- `Transaction`: observed financial evidence from the bank feed.
- `CashFlowProjection`: computed view over a time window from balances plus obligation instances.
- `DriftFinding`: mismatch between expected obligations, observed transactions, Todoist state, and cash-flow assumptions.
- `DataSourceHealth`: current status of a financial feed or integration, such as fresh, stale, unauthenticated, partial, or error.
- `SpendingReport`: aggregate view over transactions with category confidence and caveats.
- `TransactionEnrichment`: classification attached to a transaction or merchant, such as category, subcategory, rule source, confidence, and whether it came from Tally/manual/LLM.
- `BackgroundRun`: scheduled or manual maintenance run that records sync, reconciliation, created review tasks, source-health warnings, errors, and telemetry.
- `TodoistSyncRecord`: local mapping between domain objects and Todoist tasks/subtasks, including external ids, sync status, content hash, last observed state, and errors.
- `ActionOutboxItem`: durable record of a planned external write, such as creating or updating a Todoist review task, with idempotency key, status, attempts, and error details.

## How to work this

- Mechanics-first, one milestone at a time. Do not skip ahead.
- After each milestone, answer the linked interview question out loud from what you just built — that is the real test, not the code passing.
- Keep it small. Target a few hundred lines total. No framework, no premature abstraction.
- Suggested stack: Python, a local SQLite file as the "financial data," and a local MCP server for Claude Code integration. Plain functions over classes until a class earns itself.
- Build a small UI on top after the core loop and tool contracts are real enough to test outside Claude Code. The UI should show the user prompt, agent response, tool calls, context snapshot, and telemetry so it helps debug the agent rather than hiding the mechanics.

## M0 — The dataset (build this first, before any agent code)

Use a copied SQLite database, never the source finance database directly.

- Source database: `/Users/owner/dev/areas/finances/data/transactions.db`
- Working copy: `/Users/owner/dev/financial-agent-mcp/data/transactions.source-copy.sqlite`
- Current copied schema includes `accounts` and `transactions`, plus finance/task-support tables.
- Core query tables:
  - `transactions(id, account_id, posted, transacted_at, amount, payee, description, pending, source, first_seen_at, last_seen_at, fetched_at)`
  - `accounts(id, name, org, kind, currency, first_seen_at, last_seen_at)`

This copied database is what the agent queries. Real-ish personal finance data makes "fabricated numbers" (Q5) detectable later, because any number in the answer should trace back to queried rows or a calculation tool.

## Milestones

### M1 — The agent loop or harness boundary (answers Q1: context representation)
Build or integrate the core loop: user message to harness context to tool call to tool result to final answer. If Claude Code is the first harness, document what Claude Code owns versus what this project owns.
- Claude Code owns: chat session, model call, context window, tool-choice loop, and conversational UX.
- This project owns: finance tool definitions, source-of-truth reads, deterministic calculations, renderers/action payloads, telemetry, and grounding evidence.
- Harness control strategy: give Claude Code a project instruction that financial answers must use MCP finance tools, then expose tools whose descriptions make the intended route obvious. For common user questions, prefer a high-level tool that performs the whole grounded flow over forcing Claude Code to manually chain many low-level tools.
- Define the context and tool surface as an explicit, inspectable structure: system/tool instructions, available finance tools, running message history or Claude Code session state, tool results, and later memory. Be able to print or explain it.
- Acceptance: you can hand-draw / write out exactly what is in context on turn N, which pieces are owned by Claude Code, which pieces are owned by the finance tool server, and why. That diagram IS the answer to Q1.

### M2 — Real tools (makes it a financial-data agent)
Add 2-3 tools the model can call:
- `get_finance_status(options)` — structured status primitive for the repeatable finance ritual. It should return balances/source freshness, cash-flow projections for requested windows, drift warnings, recurring candidates, and Todoist review candidates. This is the foundation for daily status, background runs, and predictable regression tests.
- `analyze_finance_question(question, options)` — high-level read-only tool for common questions. It internally queries, summarizes, and returns grounded evidence plus a renderable result so Claude Code does not have to manually chain low-level tools for every finance question.
  - It may call or reuse `get_finance_status` when a natural-language question overlaps the daily ritual, cash-flow status, source freshness, drift, or recurring obligations.
  - Initial output shape should include `answer`, `confidence`, `warnings`, `evidence_summary`, `result_refs`, `trace_id`, and `render_payload`.
  - It should not dump every internal step into the user-facing answer by default. It should persist a trace and return identifiers so Claude Code, a future UI, or a telemetry inspector can drill into the internal steps when needed.
- `query_transactions(filters)` — accepts structured filters, runs parameterized SQL against the DB, and returns a structured result object.
  - Initial input shape should support date range, account, payee, description, pending status, amount range, and limit.
  - Initial output shape should include `result_id`, `schema_version`, `rows`, `row_count`, `date_range`, `filters_applied`, `truncated`, `provenance`, and optional summary fields such as `total_amount` when useful.
- `summarize_transactions(rows_or_query_ref, options)` — computes deterministic totals, counts, date ranges, and groupings from queried transaction evidence.
- `render_result(result_ref, surface, options)` — turns grounded structured results into a CLI view, inline view, UI panel payload, Todoist review payload, or email payload without changing the underlying financial facts.
- `get_finance_run_trace(trace_id)` — inspection tool that returns the stored internal steps, tool timings, evidence, warnings, calculation details, and errors for a prior finance run.
- `get_finance_result(result_id)` — inspection tool that returns a stored structured result, such as queried transaction evidence or a summary result, for follow-up analysis or UI rendering.
- `preview_todoist_review_batch(date_or_run_id, options)` — returns the Todoist task/subtask payload that would be created from review candidates, without writing.
- `create_or_update_todoist_review_batch(review_batch_id, options)` — guarded write action that creates or updates the Finance Todoist review task using idempotency keys and records the outcome locally.
- `calculate(expression)` or `sum_amounts(rows)` — so math is done by a tool, not hallucinated by the model. (This choice directly sets up Q5.)
- Acceptance: ask "How much did I spend on groceries in May?" and the agent calls the query tool, gets real rows, and answers from them.
- V1 ritual acceptance: ask for finance status and receive balances/source freshness, 7/14/30 day cash-flow projections, drift warnings, recurring candidates, Todoist review candidates, evidence summaries, and trace/result ids.

### M3 — Per-turn telemetry (answers Q2: telemetry for a turn)
Instrument the loop. For each turn, emit a structured record:
- trace id, timestamp, user input, tool calls (name, args, latency, result refs, error?), deterministic calculation steps, warnings, final response, stop reason, and whether each financial claim is backed by source evidence.
- If Claude Code owns the model call, the finance trace may not include model tokens/latency. In that case, record what this project can observe: MCP tool invocation time, finance operation timing, result sizes, provenance, warnings, and returned trace/result ids.
- Acceptance: one turn produces one telemetry object you can paste as the answer to Q2. You should be able to point at a real field and say why it's there.

### M4 — Semantic memory (answers Q3 + Q4: memory interfaces + context control)
Add semantic memory as a separate module with a clean interface, then wire it into the loop.
- Interface (the part Q3 is really asking): something like `memory.write(text, metadata)` and `memory.search(query, k) -> records`. Back it with embeddings + cosine similarity (can be in-memory/numpy at first; no vector DB needed).
- Integration into the agent graph: on each turn, `memory.search(user_input)` and inject the top-k records into the context assembler from M1.
- Q4 — controlling how many records enter context: pick and defend a policy. Options to reason through: fixed top-k (e.g. k=5), a similarity-score threshold, a token budget cap, or recency+relevance ranking. Acceptance: you can state the number, the mechanism, and the failure mode you're guarding against (context bloat / irrelevant records crowding out the query).

### M5 — Hallucination handling (answers Q5: fabricated numbers)
Now exploit what you built. Force or observe a case where the agent states a number not grounded in tool output.
- Address it operationally, the way the question wants: (1) detect via telemetry — did the answer's numbers trace to a tool result? (2) prevent — require math through the calculate tool, ground answers in retrieved rows, add a verification step. (3) the process answer — reproduce from telemetry, find where grounding broke, add a regression check.
- Acceptance: you can walk from "customer reports fabricated numbers" to a concrete diagnosis path using your own telemetry, then name the fix.

### M6 — Local UI test harness
Build a local UI for exercising the agent after the MCP path is useful.
- First access point: Claude Code chat harness plus local MCP server, because the desired interaction model is Claude Code-like and the current workflow is local/command-driven.
- Later UI surface: prompt input, final answer, tool-call timeline, context snapshot, telemetry JSON, and error display.
- Acceptance: ask a finance question from the UI and inspect which rows/tools/telemetry produced the answer.

## After M5
- Re-implement M1-M4 in LangGraph to learn the framework vocabulary (nodes/edges/state/checkpointing) and to be able to say you've used it. The understanding from the scratch build is what makes this fast.

## Status
- M0: working SQLite copy created at `/Users/owner/dev/financial-agent-mcp/data/transactions.source-copy.sqlite`
- M1: done. Claude Code is the harness; this project owns the local finance MCP server (31 tools) and all deterministic finance logic.
- M2: substantially done. `get_finance_status` returns balances, source freshness, deterministic cash-flow projections, drift warnings, recurring candidates, `trace_id`, and `result_refs`. Income, calendar-fact, obligation, charge-onboarding (discover/review/apply), statement-cycle aggregation, reconciliation, drift, Todoist-reflection (preview + outbox), and background-run tools are implemented. The charge-onboarding loop is closed end to end (transactions -> candidates -> review -> applied obligations -> cash flow).
- M3: done. `run_background_sync` records an auditable run plus an ordered operation-event log (per-operation telemetry: operation, result counts, timing, errors, trace id). `get_background_run` / `list_background_runs` inspect it.
- M4 (semantic memory): done. `src/financial_agent/memory.py` provides `write_memory` / `search_memory` with a dependency-free bag-of-words cosine embedding and a context-control policy (similarity threshold -> top-k -> token budget) that reports what each limit dropped. The embedding function is the only swap point for a real model later.
- M5 (hallucination handling): partially addressed structurally. Every financial number traces to obligation/transaction rows or an explicit estimator policy with provenance; estimates carry confidence and `amount_source`. A dedicated grounding/verification harness is still future work.
- M6: done. The background runner (`run_background_sync`) is the proactive layer, and `src/financial_agent/ui.py` is a stdlib-only local inspector (`financial-agent-ui`) showing status, projections, drift, queue, the background-run tool-call timeline, and raw telemetry. No web framework.
- LangGraph pass: NOT started. This is the only remaining BUILD_PLAN item and it needs the `langgraph` dependency (a new dependency, which is a stop condition), so it awaits explicit approval. It is a learning re-implementation, not new product capability.

### Implemented slices (charge-onboarding through V1 pipeline)
- A. Charge-onboarding discovery + review queue + guarded apply (candidate -> canonical obligation + dated instances).
- B. Statement-cycle aggregation (roll `card_statement_input` into statement estimates; never overwrites portal amounts).
- C. Reconciliation (match transactions to obligation instances; conservative, idempotent).
- D. Drift detection wired into status (missing/stale/amount-changed/unexpected-recurring).
- E. Todoist reflection: review-batch preview + durable action outbox (dry-run only; no live writes).
- F. Background runner + telemetry (one auditable run over the whole pipeline).
- G. Todoist as a one-off obligation input (import + sync records + dry-run flag outbox).
- H. Full obligation migration from `obligations.yaml` + `cash-flow.md` (deduped, needs_review fallback).
- I. Operating guardrails wired into status (cash floor $2,500, drift $200, window-age 24h, debt-avalanche).
- J. Scheduled daily-runner skeleton (`financial-agent-daily`, fcntl-locked).
- K. Live SimpleFIN sync (stdlib urllib; accounts/balances/transactions into the copied DB).
- L. Live Todoist read sync (stdlib urllib, read-only; normalized like the legacy importer).
- M. Live sync wired as opt-in, config-gated steps of `run_background_sync`.
- M4 semantic memory; M6 stdlib inspector UI (`financial-agent-ui`).
- N. Incremental SimpleFIN sync (resume from last-posted; daily job does not re-pull 90 days).
- O. Live-data validation harness (run_live_validation) with integrity checks; proves the pipeline correct on real data without touching the snapshot.
- P. Daily digest (get_daily_digest) - the human-readable just-daily / cash-flow.md replacement, with provenance and a markdown render.
- R. Parallel-run parity report (compare_to_legacy): diffs a legacy cash-flow.md against the new digest so cutover rests on a precise disagreement list.
- S. Reconciliation close-out: confirm/unconfirm a reviewed match to mark an instance paid (guarded, never auto); the digest shows matches awaiting confirmation.
- T. Claude Code finance-harness assets staged in claude-integration/ (skill + instructions + MCP registration), for deliberate install into the finances workspace.
- U. Todoist write-back sender, GATED OFF (TODOIST_WRITE_ENABLED, default false): idempotent create/update of the review task; mock-tested, never sends until enabled.
- V. Grounding/verification harness (verify_grounding): traces every headline figure in a status/digest to a source row; flags ungrounded numbers.
- W. Spending analytics (summarize_spending): rules-based categorizer + outflow reports by category/merchant/month with trend + provenance.
- Verification: `uv run --extra dev pytest -q` passes with 208 tests. 52 MCP tools. V1 is feature-complete (the last feature, Todoist write-back, ships gated off); remaining steps are operational and Owner's to trigger (enable write-back, install the assets, run the parallel period, retire legacy) - see CUTOVER_PLAN.md.

Implementation checkpoint 2026-06-20:

- Package root: `/Users/owner/dev/financial-agent-mcp`
- Entry point: `financial-agent-mcp`
- Core status service: `src/financial_agent/status.py`
- MCP wrapper: `src/financial_agent/server.py`
- Local app-owned schema: `src/financial_agent/schema.py`
- Cash-flow projection: `src/financial_agent/cashflow.py`
- Income-source scheduling: `src/financial_agent/income.py`
- Business-calendar support: `src/financial_agent/calendar.py`
- Calendar-fact storage and query: `src/financial_agent/calendar_facts.py`
- Obligation instance storage: `src/financial_agent/obligations.py`
- Tests: `tests/test_status.py`, `tests/test_income_sources.py`, `tests/test_calendar.py`, `tests/test_calendar_facts.py`, `tests/test_obligations.py`
- Current verification: `uv run --extra dev pytest -q`
- Copied DB app schema: initialized with `obligations`, `obligation_instances`, `income_sources`, `income_schedule_versions`, and `calendar_facts`; no source finances database was mutated.
- Copied DB seed: Owner / IntelliBridge and Partner / Town of Greenwich income sources are configured and generated as local income obligation instances through 2026-12-31.
- Owner / IntelliBridge implementation: semi-monthly 10th/25th schedule, previous-business-day adjustment, modeled as working-cash transfer into joint checking `XXXX`, medium confidence, review by 2026-09-01.
- Partner / Town of Greenwich implementation: biweekly Friday schedule anchored from 2026-06-05, previous-business-day adjustment, direct deposit into joint checking `XXXX`, high confidence, review by 2026-11-30.
- Calendar checkpoint: business-day adjustment now handles weekends, observed fixed-date US holidays, common US floating holidays, and explicit extra closure dates. The copied DB was regenerated so Partner's 2026-07-03 generated instance is `canceled` and the 2026-07-02 observed-holiday-adjusted instance is active.
- Calendar-facts checkpoint: MCP now exposes `import_calendar_facts` and `list_calendar_facts`. Income generation reads stored `business_closure` facts as closure dates and supports `calendar_dates` schedules backed by related `income_pay_date` facts.
- Known limitation: no live Google Calendar fetcher is implemented yet. Calendar support is deterministic and import-ready; the next importer should convert external calendar/payroll events into `calendar_facts`.
- Fixed-outflow checkpoint: MCP now exposes `apply_obligation_instances`, `list_obligations`, `list_obligation_review_candidates`, and `list_statement_input_estimates`. The copied database has `rent_check` with July 3 and August 4, 2026 outflow instances; `amex_personal_loan_autopay` with June 27, July 27, and August 27, 2026 outflow instances; `apple_card_minimum_payments` with June 30 and July 30, 2026 outflow instances; `apple_card_paydown_sweeps` with June 20, July 17, July 24, August 25, and September 11, 2026 outflow instances; `eversource_electric_estimates` with June 25, July 27, and August 27, 2026 low-confidence utility outflow estimates using average-plus-summer-multiplier metadata; `amex_statement_payment` with July 16 and August 16, 2026 lifecycle-aware estimated statement outflows; `gault_card_spend_estimates` with summer/fall/winter card statement inputs; `new_york_times_subscription` with a July 23, 2026 medium-confidence outflow; `plex_venmo_subscription` with an August 1, 2026 low-confidence outflow; and `anthem_reimbursement_estimates` with July 3, July 17, July 31, and August 14, 2026 low-confidence inflows. Cash Magnet is intentionally skipped because it is paid off, at zero, and not being used.
- Review checkpoint: `list_obligation_review_candidates(as_of_date)` detects estimated obligation amounts whose `review_after` date has arrived. On 2026-06-22 it returns the July 16 Amex statement estimate as needing portal refresh after the June 21 statement close.
- Charge-onboarding checkpoint (2026-06-21): background discovery is implemented. `src/financial_agent/onboarding.py` scans copied transactions and stores reviewable candidates in a new `charge_onboarding_candidates` table, kept separate from `obligations`. Detection is deterministic: it groups transactions by `(merchant_key, account_class, direction)` (account class is inferred from name/org because `accounts.kind` is empty in the copy), then proposes schedule, amount, cash-impact, and review policies. Amount policy mirrors the existing estimator vocabulary: `fixed` (New York Times settled at `$30.30`), `seasonal_multiplier` for usage-driven checking utilities (Eversource, base average `$115.87`), `seasonal_card_spend` for usage-driven card charges (Gault Energy on the Amex card, targeting `amex_statement_payment`), `average`, and `needs_review`. Card charges become `card_statement_input`; checking/savings become `direct_checking`; highly variable non-utility card spend becomes `variable_spend`; transfers/debt payments become `internal_transfer`. The review walk is priority-ordered by estimated monthly cash impact, weighted by candidate type so real recurring obligations lead. MCP now exposes `scan_charge_onboarding_candidates`, `list_charge_onboarding_queue`, `get_next_charge_onboarding_candidate`, and `record_charge_onboarding_decision` (defer/reject/needs_more_evidence/in_review/reset; accept/apply/merge/split are deferred to a later guarded slice and raise). Scanning is idempotent and never regresses a human decision; candidates never write `obligation_instances`, so cash-flow projection is unaffected until a candidate is applied. Running the scanner on the copied database discovers 114 candidates from 1,611 transactions.
- Charge-onboarding apply checkpoint (2026-06-21): the apply slice is built, closing the onboarding loop. `record_charge_onboarding_decision` supports `accept`; `preview_charge_onboarding_apply` returns the obligation plus dated instances that would be created (read-only); and `apply_charge_onboarding_candidate` is the guarded write that promotes an accepted candidate into a canonical obligation plus dated instances generated from the proposed schedule/amount policy. Applied `direct_checking` obligations project into the checking forecast, `card_statement_input` obligations feed statement estimates (excluded from checking), and inflows behave like income. Apply is idempotent (deterministic instance ids) and never automatic. MCP now exposes `preview_charge_onboarding_apply` and `apply_charge_onboarding_candidate` (16 tools total).
- Current verification: `uv run --extra dev pytest -q` passes with 52 tests.
- Next implementation target: statement-cycle aggregation (roll applied `card_statement_input` instances into future `amex_statement_payment` amounts by cycle), then reconciliation (match observed transactions to expected instances), drift detection wired into status, a dry-run Todoist reflection/outbox layer, and per-operation telemetry. These complete the V1 ritual-replacement scope.
