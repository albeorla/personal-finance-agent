# Finance Agent Diagrams

Durable Mermaid diagrams for the local finance MCP server. These diagrams describe the intended architecture and state model without depending on local SQLite data, credentials, or temporary rendered artifacts.

## System Architecture

```mermaid
flowchart LR
    Claude["Claude / MCP client"] <-->|"tool calls"| Server["Finance MCP Server<br/>v0.2.0, 69 tools"]

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

## Database Relationship View

```mermaid
erDiagram
    ACCOUNTS ||--o{ BALANCE_SNAPSHOTS : has
    ACCOUNTS ||--o{ TRANSACTIONS : posts
    OBLIGATIONS ||--o{ OBLIGATION_INSTANCES : schedules
    TRANSACTIONS ||--o{ TRANSACTION_OBLIGATION_MATCHES : observes
    OBLIGATION_INSTANCES ||--o| TRANSACTION_OBLIGATION_MATCHES : matched_by
    OBLIGATION_INSTANCES ||--o| UNMATCHED_OBLIGATIONS : can_flag
    CHARGE_ONBOARDING_CANDIDATES }o--o| OBLIGATIONS : may_create
    FOLLOW_UPS ||--o| TODOIST_EMISSIONS : may_surface

    ACCOUNTS {
        string id PK
        string name
        string org
        string kind
        string currency
    }

    BALANCE_SNAPSHOTS {
        int id PK
        string account_id FK
        float balance
        float available
        string recorded_at
        string source
    }

    TRANSACTIONS {
        string id PK
        string account_id FK
        float amount
        string payee
        string posted
        int pending
    }

    OBLIGATIONS {
        string id PK
        string name
        string kind
        string cadence
        string status
        int autopay
    }

    OBLIGATION_INSTANCES {
        string id PK
        string obligation_id FK
        string due_date
        float amount
        string status
        string confidence
        string cash_flow_treatment
    }

    TRANSACTION_OBLIGATION_MATCHES {
        string obligation_instance_id PK
        string transaction_id FK
        string match_type
        float match_score
        float amount_delta
    }

    UNMATCHED_OBLIGATIONS {
        string obligation_instance_id PK
        string due_date
        int age_days
        int past_grace
        string status
    }

    CHARGE_ONBOARDING_CANDIDATES {
        string id PK
        string merchant_key
        string display_name
        string status
        string cash_flow_treatment
        float priority_score
    }

    TODOIST_EMISSIONS {
        string surface_key PK
        string todoist_task_id
        string status
        string content_hash
        string last_seen
    }

    FOLLOW_UPS {
        string id PK
        string text
        string surface_when
        string status
        string linked_obligation_id
    }
```

## Candidate Lifecycle

```mermaid
stateDiagram-v2
    [*] --> proposed: scanner finds pattern
    proposed --> in_review: user opens queue
    proposed --> deferred: defer
    proposed --> rejected: reject
    in_review --> accepted: accept
    accepted --> applied: apply candidate
    applied --> [*]: creates obligation + instances
    deferred --> proposed: revisit later
```

## Obligation Instance Lifecycle

```mermaid
stateDiagram-v2
    [*] --> expected: instance created
    expected --> matched: transaction found
    matched --> needs_review: conservative match
    needs_review --> confirmed: user confirms
    confirmed --> cleared: mark paid
    expected --> drift: no match past grace
    drift --> expected: edit or resolve
    cleared --> [*]
```

## Todoist Emission Lifecycle

```mermaid
stateDiagram-v2
    [*] --> none
    none --> open: surfaced
    open --> completed: read back completion
    open --> deleted_by_user: task disappears
    open --> retired: cleanup removes stale task
    retired --> open: need recurs
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
