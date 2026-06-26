# Finance Agent Diagrams

Durable Mermaid diagrams for the local finance MCP server. These diagrams describe the intended architecture and state model without depending on local SQLite data, credentials, or temporary rendered artifacts.

## System Architecture

```mermaid
flowchart LR
    Claude["Claude / MCP client"] <-->|"tool calls"| Server["Finance MCP Server<br/>v0.2.0, 71 tools"]

    SimpleFIN["SimpleFIN<br/>balances + transactions"] -->|"read-only sync"| Server
    Portals["Bank/card portals<br/>manual balances and one-off facts"] -->|"manual inputs"| Server

    Server <-->|"source rows and projections"| SQLite[("SQLite finance DB")]
    Server <-->|"write/search memory"| Memory[("finance_memory<br/>decisions, corrections, facts")]

    Server -->|"surface due items<br/>deduped by emissions ledger"| Todoist["Todoist Finance project"]
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

## Daily Finance Loop

```mermaid
flowchart LR
    subgraph Ingest["1. INGEST"]
        Sync["sync_simplefin<br/>pull SimpleFIN balances + transactions"]
        Manual["set_manual_balance<br/>record current portal balance when a feed is stale"]
        CardPaste["import_card_statement<br/>paste a card statement when a card has no live feed (dry-run by default)"]
        TodoistRead["reconcile_todoist_completions<br/>read completion state for emitted tasks"]
    end

    subgraph Model["2. MODEL"]
        Discover["scan_charge_onboarding_candidates<br/>discover recurring charge candidates"]
        Reconcile["reconcile_obligation_instances<br/>match expected payments to transactions"]
        Drift["detect_drift<br/>flag stale estimates, missing payments, amount changes"]
        Guardrails["evaluate_guardrails<br/>cash floor, drift threshold, debt order"]
        Projection["get_finance_status / get_daily_digest<br/>project cash flow from obligation_instances"]
        Verify["run_verification<br/>prove source rows tie together, persist findings"]
    end

    subgraph Surface["3. SURFACE"]
        Queue["get_surface_queue<br/>collect what needs attention"]
        Digest["get_daily_digest<br/>status color, working cash, upcoming obligations"]
        Push["surface_due_items_to_todoist<br/>upsert tasks through todoist_emissions"]
    end

    Ingest --> Model --> Surface
    Surface -. "daily schedule / next read-back" .-> Ingest
```

## Daily Run Sequence

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Agent as Claude agent
    participant MCP as Finance MCP Server
    participant SF as SimpleFIN
    participant TD as Todoist
    participant DB as SQLite

    User->>Agent: Start daily finance loop
    Agent->>MCP: run_background_sync(sync=true, as_of_date)
    MCP->>SF: Pull accounts, balances, transactions
    SF-->>MCP: Feed payload and warnings
    MCP->>DB: Upsert accounts, balance_snapshots, transactions
    MCP->>DB: Scan charge candidates
    MCP->>DB: Reconcile obligation_instances to transactions
    MCP->>DB: Detect drift and evaluate guardrails
    MCP->>DB: Verify row-tie identities and persist findings
    MCP-->>Agent: Run summary and trace_id

    Agent->>MCP: get_daily_digest(as_of_date)
    MCP->>DB: Read balances, obligation_instances, guardrails, queue
    MCP-->>Agent: Status color, working cash, upcoming obligations

    Agent->>MCP: surface_due_items_to_todoist(as_of_date)
    MCP->>DB: Build surface queue and check todoist_emissions
    alt TODOIST_WRITE_ENABLED is truthy
        MCP->>TD: Create or update Finance project tasks
        TD-->>MCP: Task ids and active-task state
        MCP->>DB: Persist emission status and content_hash
    else write gate is off
        MCP->>DB: Record dry-run outbox state with no external side effect
    end

    Agent-->>User: Present grounded digest and action list
```

## Entity Relationship Diagram

This is the durable data model the MCP server reads and writes. It includes the
source tables created for synced account data plus the app-owned tables created
by `ensure_app_schema`. Some relationships are logical rather than enforced
SQLite foreign keys because several links are stored as stable ids or JSON
evidence.

```mermaid
erDiagram
    ACCOUNTS ||--o{ BALANCE_SNAPSHOTS : has
    ACCOUNTS ||--o{ TRANSACTIONS : posts
    ACCOUNTS ||--o{ DEBTS : can_back
    ACCOUNTS ||--o{ CARD_IMPORT_RUNS : imports

    OBLIGATIONS ||--o{ OBLIGATION_INSTANCES : schedules
    INCOME_SOURCES ||--o{ INCOME_SCHEDULE_VERSIONS : versions
    INCOME_SOURCES ||--o{ OBLIGATION_INSTANCES : generates
    INCOME_SCHEDULE_VERSIONS ||--o{ OBLIGATION_INSTANCES : generated_by
    CALENDAR_FACTS }o--o{ INCOME_SOURCES : may_adjust_pay_dates

    TRANSACTIONS ||--o{ TRANSACTION_OBLIGATION_MATCHES : observed_match
    OBLIGATION_INSTANCES ||--o| TRANSACTION_OBLIGATION_MATCHES : match_evidence
    OBLIGATION_INSTANCES ||--o| UNMATCHED_OBLIGATIONS : unmatched_state
    OBLIGATIONS ||--o{ DRIFT_FINDINGS : can_have
    OBLIGATION_INSTANCES ||--o{ DRIFT_FINDINGS : can_have

    CHARGE_ONBOARDING_CANDIDATES }o--o| OBLIGATIONS : may_promote_to
    CHARGE_ONBOARDING_CANDIDATES }o--o{ TRANSACTIONS : evidence_json

    OBLIGATIONS ||--o{ STATEMENT_CYCLES : target_statement
    OBLIGATION_INSTANCES ||--o| STATEMENT_CYCLES : statement_instance
    STATEMENT_CYCLES ||--o{ STATEMENT_CYCLE_INPUTS : rolls_up
    OBLIGATION_INSTANCES ||--o{ STATEMENT_CYCLE_INPUTS : input_charge

    GOALS ||--o| TODOIST_EMISSIONS : may_surface
    FOLLOW_UPS ||--o| TODOIST_EMISSIONS : may_surface
    OBLIGATIONS ||--o{ FOLLOW_UPS : may_link
    GUARDRAIL_RULES ||--o{ GUARDRAIL_EVALUATIONS : evaluated_as
    BACKGROUND_RUNS ||--o{ OPERATION_EVENTS : emits

    ACCOUNTS {
        string id PK
        string name
        string org
        string kind
        string currency
        string first_seen_at
        string last_seen_at
    }

    BALANCE_SNAPSHOTS {
        int id PK
        string account_id FK
        float balance
        float available
        string recorded_at
        string source
        string manual_note
    }

    TRANSACTIONS {
        string id PK
        string account_id FK
        string posted
        string transacted_at
        float amount
        string payee
        string source
    }

    SYNC_RUNS {
        int id PK
        string started_at
        string finished_at
        string mode
        int transactions_inserted
        int transactions_updated
        string error
    }

    OBLIGATIONS {
        string id PK
        string name
        string kind
        string cadence
        string status
        string source
        bool autopay
        bool amount_discretionary
    }

    OBLIGATION_INSTANCES {
        string id PK
        string obligation_id FK
        string due_date
        float amount
        string direction
        string status
        string amount_status
        string cash_flow_treatment
        string statement_target_obligation_id
    }

    INCOME_SOURCES {
        string id PK
        string person
        string employer
        string status
        float default_amount
        string active_from
        string active_until
    }

    INCOME_SCHEDULE_VERSIONS {
        string id PK
        string income_source_id FK
        string schedule_type
        string rule_json
        string valid_from
        string status
    }

    CALENDAR_FACTS {
        string id PK
        string fact_type
        string fact_date
        string related_entity_type
        string related_entity_id
        string status
    }

    CHARGE_ONBOARDING_CANDIDATES {
        string id PK
        string merchant_key
        string display_name
        string status
        string candidate_type
        string cash_flow_treatment
        float priority_score
        string existing_obligation_id
    }

    STATEMENT_CYCLES {
        string id PK
        string target_obligation_id FK
        string statement_instance_id FK
        string cycle_close_date
        float input_sum
        int input_count
    }

    STATEMENT_CYCLE_INPUTS {
        string statement_cycle_id PK
        string obligation_instance_id PK
        float input_amount
        string due_date
    }

    CARD_IMPORT_RUNS {
        string id PK
        string account_id FK
        string imported_at
        string statement_close_date
        int txn_count
        float total_spend
    }

    TRANSACTION_OBLIGATION_MATCHES {
        string obligation_instance_id PK
        string transaction_id FK
        string match_type
        float match_score
        string as_of_date
    }

    UNMATCHED_OBLIGATIONS {
        string obligation_instance_id PK
        string obligation_id
        string due_date
        int age_days
        bool past_grace
        string status
    }

    DRIFT_FINDINGS {
        string id PK
        string finding_type
        string severity
        string obligation_id
        string obligation_instance_id
        string status
        string resolved_at
    }

    ACTION_OUTBOX {
        string id PK
        string idempotency_key
        string action_type
        string status
        bool dry_run
        string external_task_id
    }

    TODOIST_EMISSIONS {
        string surface_key PK
        string todoist_task_id
        string status
        string content_hash
        string last_seen
        string retire_requested_at
    }

    BACKGROUND_RUNS {
        string id PK
        string trace_id
        string run_type
        string status
        string as_of_date
        string started_at
        string finished_at
    }

    OPERATION_EVENTS {
        int id PK
        string run_id FK
        int event_seq
        string event_type
        string status
        string event_time
    }

    GUARDRAIL_RULES {
        string id PK
        string rule_type
        float threshold_value
        string severity_default
    }

    GUARDRAIL_EVALUATIONS {
        string id PK
        string rule_type
        string evaluation_date
        bool passed
        string finding_json
    }

    GOALS {
        string id PK
        string name
        float target_amount
        string deadline
        string source_account
        string status
    }

    DEBTS {
        string id PK
        string account_id FK
        string name
        float apr
        string balance_source
        bool is_revolving
    }

    FOLLOW_UPS {
        string id PK
        string text
        string surface_when
        string priority
        string status
        string linked_obligation_id
    }

    MEMORY_RECORDS {
        string id PK
        string kind
        string text
        string metadata_json
        int token_count
        string source
    }

    OBLIGATION_MIGRATION_LOG {
        string id PK
        string run_timestamp
        string source_type
        bool dry_run
        int created_obligations
        int created_instances
    }
```

## Object Creation And Update Flow

This diagram answers "when are the durable objects created or updated?" Read-only
tools such as `get_finance_status`, `get_daily_digest`, and list tools are
intentionally omitted unless they call a helper that writes state.

```mermaid
flowchart TB
    Start([Tool call or scheduled daily run])
    Schema["Schema bootstrap<br/>creates missing tables and columns<br/>(ensure_source_tables, ensure_app_schema)"]

    subgraph SourceIngest["1. Source ingest and manual facts"]
        Sync["Live feed sync<br/>creates/updates accounts and transactions<br/>creates balance_snapshots and sync_runs<br/>(sync_simplefin)"]
        ManualBalance["Manual balance correction<br/>deletes same-day manual snapshot<br/>creates balance_snapshots source=manual<br/>(set_manual_balance)"]
        CardPaste["Card statement paste<br/>creates paste transactions and card_import_runs<br/>can create manual balance snapshot<br/>can update statement obligation instance amount<br/>(import_card_statement)"]
        Calendar["Calendar fact import<br/>creates/updates calendar_facts<br/>(import_calendar_facts)"]
    end

    subgraph Modeling["2. Canonical model writes"]
        Obligations["Obligation write<br/>creates/updates obligations and obligation_instances<br/>marks instances deleted/canceled when explicitly removed or regenerated<br/>(apply_obligation_instances, delete_obligation_instance, backfill)"]
        Income["Income setup<br/>creates/updates income_sources and income_schedule_versions<br/>also creates an income obligation<br/>(apply_income_source)"]
        IncomeInstances["Income generation<br/>creates/updates income obligation_instances<br/>cancels obsolete generated expected instances<br/>(generate_income_instances)"]
        Statements["Statement rollup<br/>creates/updates statement_cycles<br/>deletes/rebuilds statement_cycle_inputs<br/>updates unprotected statement estimates<br/>(aggregate_statement_inputs, recompute_statement_estimates)"]
        GoalsDebtsFollowups["User-modeled facts<br/>creates/updates goals, debts, follow_ups, memory_records<br/>(set_goal, set_debt_terms, capture_followup, write_finance_memory)"]
    end

    subgraph CandidateReview["3. Discovery, review, and promotion"]
        Scan["Recurring-charge scan<br/>creates candidates or refreshes evidence<br/>can auto-park or auto-reject new noise<br/>(scan_charge_onboarding_candidates)"]
        Decide["Human or agent decision<br/>updates candidate status, decision_json, reviewed_at<br/>(record_charge_onboarding_decision)"]
        Apply["Accepted candidate apply<br/>creates/updates obligation and instances<br/>updates candidate to applied and stamps applied_at<br/>(apply_charge_onboarding_candidate)"]
    end

    subgraph ReconcileAndRisk["4. Reconciliation, drift, and guardrails"]
        Reconcile["Transaction reconciliation<br/>creates/updates transaction_obligation_matches<br/>creates/updates unmatched_obligations<br/>can update instance status to needs_review or paid<br/>(reconcile_obligation_instances, confirm_reconciliation_match)"]
        Drift["Drift detection<br/>creates/updates active drift_findings<br/>marks disappeared findings resolved<br/>(detect_drift persist=true)"]
        Guardrails["Guardrail evaluation<br/>creates guardrail_rules if missing<br/>optionally inserts guardrail_evaluations<br/>(evaluate_guardrails persist=true)"]
        Verify["Row-tie verification<br/>runs four deterministic checks that prove source rows agree<br/>inserts verification_findings when persist=true<br/>(run_verification)"]
    end

    subgraph SurfaceAndTelemetry["5. Surfacing and run telemetry"]
        Queue["Surface item build<br/>mostly read-only<br/>can mark onboarding digest emission for retirement when the queue empties<br/>(build_surface_items)"]
        Todoist["Todoist surfacing<br/>creates/updates todoist_emissions<br/>marks open, completed, deleted_by_user, or retired<br/>(surface_due_items_to_todoist, reconcile_todoist_completions)"]
        Outbox["One-off Todoist outbox<br/>updates action_outbox send state<br/>status moves pending/dry_run to simulated, succeeded, failed, or no_integration_configured<br/>(execute_action_outbox)"]
        Background["Background wrapper<br/>creates background_runs<br/>creates operation_events per step<br/>updates run status and summary at finish<br/>(run_background_sync)"]
    end

    Start --> Schema
    Schema --> Sync
    Schema --> ManualBalance
    Schema --> CardPaste
    Schema --> Calendar
    Schema --> Obligations
    Schema --> Income
    Schema --> GoalsDebtsFollowups
    Schema --> Scan
    Schema --> Reconcile
    Schema --> Guardrails
    Schema --> Background

    Background -. wraps daily steps .-> Sync
    Background -. wraps daily steps .-> Scan
    Background -. wraps daily steps .-> Reconcile
    Background -. wraps daily steps .-> Drift
    Background -. wraps daily steps .-> Verify
    Background -. wraps daily steps .-> Queue
    Background -. wraps daily steps .-> Todoist

    Sync --> Scan
    ManualBalance --> Reconcile
    CardPaste --> Scan
    CardPaste --> Statements
    Calendar --> IncomeInstances
    Income --> IncomeInstances
    IncomeInstances --> Reconcile
    Obligations --> Statements
    Obligations --> Reconcile
    Scan --> Decide --> Apply --> Obligations
    Reconcile --> Drift --> Guardrails
    Reconcile --> Queue
    Drift --> Queue
    Guardrails --> Verify --> Queue
    GoalsDebtsFollowups --> Queue
    Queue --> Todoist

    classDef ingest fill:#e7eff7,stroke:#1f4e79,color:#17202a
    classDef model fill:#e8f2ec,stroke:#3d7b65,color:#17202a
    classDef review fill:#f5ecdd,stroke:#b87922,color:#17202a
    classDef risk fill:#f4e6ea,stroke:#aa4a5d,color:#17202a
    classDef surface fill:#edf0f2,stroke:#2f3b47,color:#17202a
    class Sync,ManualBalance,CardPaste,Calendar ingest
    class Obligations,Income,IncomeInstances,Statements,GoalsDebtsFollowups model
    class Scan,Decide,Apply review
    class Reconcile,Drift,Guardrails,Verify risk
    class Queue,Todoist,Outbox,Background,Schema surface
```

## Candidate Lifecycle

```mermaid
stateDiagram-v2
    [*] --> proposed: scanner creates candidate
    proposed --> proposed: scanner refreshes evidence
    proposed --> parked: auto-triage park
    proposed --> rejected: auto-triage or reject
    proposed --> in_review: start review
    proposed --> deferred: defer
    proposed --> needs_more_evidence: needs more evidence
    in_review --> accepted: accept
    accepted --> applied: apply candidate
    applied --> [*]: creates obligation + instances
    deferred --> proposed: reset
    needs_more_evidence --> proposed: reset
    parked --> proposed: reset
    rejected --> proposed: reset
```

## Obligation Instance Lifecycle

```mermaid
stateDiagram-v2
    [*] --> expected: instance created
    expected --> expected: match evidence recorded
    expected --> needs_review: weak match or missing past grace
    needs_review --> paid: user confirms recorded match
    expected --> paid: auto_mark_paid option
    expected --> canceled: schedule regeneration or backfill rollback
    expected --> deleted: explicit delete tool
    needs_review --> expected: unconfirm or rewrite expected state
    paid --> expected: unconfirm
    canceled --> [*]
    deleted --> [*]
    paid --> [*]
```

## Todoist Emission Lifecycle

```mermaid
stateDiagram-v2
    [*] --> open: surface_to_todoist creates or adopts task
    open --> open: content changed, update same task
    open --> completed: read back completion
    open --> deleted_by_user: task disappears
    open --> retired: retire request or project cleanup
    retired --> open: recurring need resurfaces
    completed --> [*]: permanent suppression
    deleted_by_user --> [*]: permanent suppression
```

## Source Of Truth Precedence

```mermaid
flowchart TB
    Live["Tier 1: Live MCP tools + SQLite<br/>balances, transactions, obligations, dated instances"]
    Detector["Tier 2: Recurring-charge detector<br/>auto-detected baseline; candidates do not project until applied"]
    Manual["Tier 3: Manual carry-forward facts<br/>manual balances, payroll, reimbursements, one-offs, follow-ups"]
    Todoist["Tier 4: Todoist<br/>output and notification surface; completions read back"]
    Scratch["Never authoritative: scratch notes and analysis"]

    Live --> Detector --> Manual --> Todoist --> Scratch

    classDef top fill:#e8f2ec,stroke:#3d7b65,color:#17202a
    classDef mid fill:#e7eff7,stroke:#1f4e79,color:#17202a
    classDef low fill:#f5ecdd,stroke:#b87922,color:#17202a
    classDef output fill:#f4e6ea,stroke:#aa4a5d,color:#17202a
    classDef none fill:#edf0f2,stroke:#2f3b47,color:#17202a
    class Live top
    class Detector mid
    class Manual low
    class Todoist output
    class Scratch none
```

## User / Agent / System Swimlane

```mermaid
flowchart LR
    subgraph User
        U1["sees unknown recurring charge"]
        U2["accepts, defers, or rejects model"]
        U3["checks off due reminder"]
    end

    subgraph Agent
        A1["explains evidence and proposed model"]
        A2["calls decision or preview tool"]
        A3["presents daily digest"]
    end

    subgraph MCP["Finance MCP Server"]
        M1["scan_charge_onboarding_candidates"]
        M2["record_charge_onboarding_decision"]
        M3["apply_charge_onboarding_candidate"]
        M4["surface_due_items_to_todoist"]
        M5["reconcile_todoist_completions"]
    end

    subgraph Systems
        S1[("SQLite candidate queue")]
        S2[("SQLite obligations + instances")]
        S3["Todoist task"]
        S4[("todoist_emissions ledger")]
    end

    M1 --> S1 --> A1 --> U1 --> U2 --> A2 --> M2
    M2 -->|accept| M3 --> S2 --> M4 --> S3 --> U3 --> M5 --> S4 --> A3
    M2 -->|defer or reject| S1
```

## Cash-Flow Template

This is a structural view only. Live amounts should come from `get_finance_status` or `get_daily_digest`; do not hard-code private balances into docs.

```mermaid
flowchart LR
    Income["income transfers"] --> Cash["Working cash<br/>checking projection"]
    Secondary["secondary pay"] --> Cash
    Reimbursements["reimbursements<br/>estimated or observed"] -.-> Cash

    Cash --> Housing["Housing<br/>rent"]
    Cash --> Debt["Debt<br/>card statements, loan autopays, paydown sweeps"]
    Cash --> Bills["Recurring bills<br/>utilities, subscriptions, garbage"]
    Cash --> Auto["Auto<br/>lease"]

    Cash -. "guardrail: $2,500 cash floor" .-> Floor["lowest projected balance marker"]

    classDef inflow fill:#e7eff7,stroke:#1f4e79,color:#17202a
    classDef cash fill:#e8f2ec,stroke:#3d7b65,color:#17202a
    classDef outflow fill:#f4e6ea,stroke:#aa4a5d,color:#17202a
    classDef guardrail fill:#f5ecdd,stroke:#b87922,color:#17202a
    class Income,Secondary,Reimbursements inflow
    class Cash cash
    class Housing,Debt,Bills,Auto outflow
    class Floor guardrail
```
