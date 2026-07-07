import sqlite3
from datetime import UTC, datetime

from financial_agent.schema import ensure_app_schema
from financial_agent.status import WORKING_BALANCE_STALE_DAYS, get_finance_status


def _build_status_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            org TEXT,
            kind TEXT,
            currency TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE balance_snapshots (
            id INTEGER PRIMARY KEY,
            account_id TEXT NOT NULL,
            balance REAL NOT NULL,
            available REAL NOT NULL,
            recorded_at TEXT NOT NULL,
            source TEXT NOT NULL
        );

        CREATE TABLE sync_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            mode TEXT NOT NULL,
            accounts_seen INTEGER NOT NULL,
            transactions_inserted INTEGER NOT NULL,
            transactions_updated INTEGER NOT NULL,
            error TEXT
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO accounts (
            id, name, org, kind, currency, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("checking-1", "Checking 4321", "Chase", "checking", "USD", "2026-06-01T00:00:00+00:00", "2026-06-20T10:00:00+00:00"),
            ("savings-1", "Savings 4323", "Chase", "savings", "USD", "2026-06-01T00:00:00+00:00", "2026-06-20T10:00:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO balance_snapshots (
            account_id, balance, available, recorded_at, source
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("checking-1", 1000.0, 950.0, "2026-06-20T10:00:00+00:00", "simplefin"),
            ("checking-1", 900.0, 900.0, "2026-06-19T10:00:00+00:00", "simplefin"),
            ("savings-1", 500.0, 500.0, "2026-06-20T09:30:00+00:00", "simplefin"),
        ],
    )
    conn.execute(
        """
        INSERT INTO sync_runs (
            started_at, finished_at, mode, accounts_seen,
            transactions_inserted, transactions_updated, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-06-20T09:58:00+00:00", "2026-06-20T10:00:00+00:00", "incremental", 2, 4, 1, None),
    )
    conn.commit()
    conn.close()


def _seed_many_instances(db_path, count=40):
    """Seed enough dated instances that every window holds many events.

    Adds `count` instances inside a 7-day window (so the 7/14/30-day windows
    each carry all of them) to exercise compact mode on a large `events` array.
    """
    conn = sqlite3.connect(db_path)
    ensure_app_schema(conn)
    conn.execute(
        """
        INSERT INTO obligations (
            id, name, kind, cadence, status, source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "misc",
            "Misc bills",
            "bill",
            "monthly",
            "active",
            "test",
            "2026-06-20T00:00:00+00:00",
            "2026-06-20T00:00:00+00:00",
        ),
    )
    rows = []
    for i in range(count):
        # Spread across days 21-25 (within the 7-day window) so each window
        # captures all the events.
        day = 21 + (i % 5)
        rows.append(
            (
                f"misc-{i}",
                "misc",
                f"2026-06-{day:02d}",
                10.0 + i,
                "outflow",
                "expected",
                "test",
                "high",
                f"Misc charge {i} with a descriptive note to add some bytes.",
                "2026-06-20T00:00:00+00:00",
                "2026-06-20T00:00:00+00:00",
            )
        )
    conn.executemany(
        """
        INSERT INTO obligation_instances (
            id, obligation_id, due_date, amount, direction, status,
            source, confidence, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def test_get_finance_status_compact_mode_reduces_size(tmp_path):
    import json

    db_path = tmp_path / "transactions.sqlite"
    _build_status_db(db_path)
    _seed_many_instances(db_path)

    common = dict(
        windows=[7, 14, 30],
        working_account_id="checking-1",
        start_date="2026-06-20",
        now=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
    )
    full = get_finance_status(db_path=db_path, **common)
    compact = get_finance_status(db_path=db_path, compact=True, **common)

    full_size = len(json.dumps(full))
    compact_size = len(json.dumps(compact))

    assert full_size > 50_000
    assert compact_size < 30_000
    assert compact_size < full_size * 0.3

    metadata_keys = {
        "window_days",
        "starting_balance",
        "ending_balance",
        "lowest_balance",
        "lowest_balance_date",
        "provenance",
    }
    assert len(compact["cash_flow_projections"]) == 3
    for full_proj, compact_proj in zip(
        full["cash_flow_projections"], compact["cash_flow_projections"], strict=True
    ):
        assert "events" not in compact_proj
        assert compact_proj["events_count"] == len(full_proj["events"])
        assert compact_proj["events_count"] >= 40
        for key in metadata_keys:
            assert compact_proj[key] == full_proj[key]

    # Non-projection sections are untouched by compact mode. balances carries a
    # freshly minted result_id per call, so compare it without that field.
    full_balances = {k: v for k, v in full["balances"].items() if k != "result_id"}
    compact_balances = {k: v for k, v in compact["balances"].items() if k != "result_id"}
    assert compact_balances == full_balances
    for section in ("source_freshness", "drift_warnings", "guardrail_findings"):
        assert compact[section] == full[section]


def test_get_finance_status_default_mode_unchanged(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_status_db(db_path)
    _seed_many_instances(db_path)

    common = dict(
        windows=[7, 14, 30],
        working_account_id="checking-1",
        start_date="2026-06-20",
        now=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
    )
    explicit_default = get_finance_status(db_path=db_path, compact=False, **common)
    implicit_default = get_finance_status(db_path=db_path, **common)

    # trace_id / result_refs are freshly minted per call; everything else
    # (including the full events arrays) must match between explicit-default and
    # implicit-default, proving compact=False changes nothing.
    volatile = {"trace_id", "result_refs"}
    explicit_stable = {k: v for k, v in explicit_default.items() if k not in volatile}
    implicit_stable = {k: v for k, v in implicit_default.items() if k not in volatile}
    # result_id under balances also derives from result_refs.
    for shape in (explicit_stable, implicit_stable):
        shape["balances"] = {
            k: v for k, v in shape["balances"].items() if k != "result_id"
        }
    assert explicit_stable == implicit_stable
    for proj in explicit_default["cash_flow_projections"]:
        assert "events" in proj
        assert "events_count" not in proj


def test_compact_parameters_flow_through_server(tmp_path):
    import pytest

    pytest.importorskip("mcp", reason="MCP server deps not installed")
    from financial_agent import server

    db_path = tmp_path / "transactions.sqlite"
    _build_status_db(db_path)
    _seed_many_instances(db_path)

    # get_finance_status server wrapper honors compact.
    full_status = server.get_finance_status(
        db_path=str(db_path),
        windows=[7, 14, 30],
        working_account_id="checking-1",
        start_date="2026-06-20",
    )
    compact_status = server.get_finance_status(
        db_path=str(db_path),
        windows=[7, 14, 30],
        working_account_id="checking-1",
        start_date="2026-06-20",
        compact=True,
    )
    assert all("events" in p for p in full_status["cash_flow_projections"])
    assert all("events" not in p for p in compact_status["cash_flow_projections"])
    assert all("events_count" in p for p in compact_status["cash_flow_projections"])

    # list_obligations server wrapper honors compact.
    full_obs = server.list_obligations(
        db_path=str(db_path), kind="bill", include_instances=True
    )
    compact_obs = server.list_obligations(
        db_path=str(db_path), kind="bill", include_instances=True, compact=True
    )
    assert all("instances" in ob for ob in full_obs["items"])
    assert all("instances" not in ob for ob in compact_obs["items"])
    assert all("instance_count" in ob for ob in compact_obs["items"])


def test_get_finance_status_returns_balances_freshness_and_trace(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_status_db(db_path)

    result = get_finance_status(
        db_path=db_path,
        windows=[7, 14, 30],
        now=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
    )

    assert result["schema_version"] == "finance_status.v1"
    assert result["trace_id"].startswith("trace_")
    assert result["result_refs"][0].startswith("result_")
    assert result["requested_windows_days"] == [7, 14, 30]

    assert result["balances"]["total_available"] == 1450.0
    assert result["balances"]["total_balance"] == 1500.0
    assert [account["account_id"] for account in result["balances"]["accounts"]] == [
        "checking-1",
        "savings-1",
    ]

    assert result["source_freshness"]["simplefin"]["status"] == "fresh"
    assert result["source_freshness"]["simplefin"]["age_hours"] == 2.0
    # Todoist is output-only now: source freshness reports SimpleFIN only.
    assert "todoist" not in result["source_freshness"]

    assert result["cash_flow_projections"] == []
    assert result["drift_warnings"] == []
    assert result["recurring_candidates"] == []
    assert result["todoist_review_candidates"] == []
    assert "local obligation schema is not initialized" in result["warnings"]


def test_get_finance_status_projects_cash_flow_from_local_obligations(tmp_path):
    db_path = tmp_path / "transactions.sqlite"
    _build_status_db(db_path)

    conn = sqlite3.connect(db_path)
    ensure_app_schema(conn)
    conn.execute(
        """
        INSERT INTO obligations (
            id, name, kind, cadence, status, source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "rent",
            "Rent",
            "bill",
            "monthly",
            "active",
            "test",
            "2026-06-20T00:00:00+00:00",
            "2026-06-20T00:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO obligations (
            id, name, kind, cadence, status, source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "paycheck",
            "Paycheck",
            "income",
            "semi_monthly",
            "active",
            "test",
            "2026-06-20T00:00:00+00:00",
            "2026-06-20T00:00:00+00:00",
        ),
    )
    conn.executemany(
        """
        INSERT INTO obligation_instances (
            id, obligation_id, due_date, amount, direction, status,
            source, confidence, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "rent-2026-06-21",
                "rent",
                "2026-06-21",
                300.0,
                "outflow",
                "expected",
                "test",
                "high",
                None,
                "2026-06-20T00:00:00+00:00",
                "2026-06-20T00:00:00+00:00",
            ),
            (
                "paycheck-2026-06-25",
                "paycheck",
                "2026-06-25",
                800.0,
                "inflow",
                "expected",
                "test",
                "high",
                None,
                "2026-06-20T00:00:00+00:00",
                "2026-06-20T00:00:00+00:00",
            ),
            (
                "insurance-2026-07-01",
                "rent",
                "2026-07-01",
                50.0,
                "outflow",
                "expected",
                "test",
                "medium",
                None,
                "2026-06-20T00:00:00+00:00",
                "2026-06-20T00:00:00+00:00",
            ),
        ],
    )
    conn.commit()
    conn.close()

    result = get_finance_status(
        db_path=db_path,
        windows=[7, 14],
        working_account_id="checking-1",
        start_date="2026-06-20",
        now=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
    )

    assert "cash-flow projection is not implemented yet" not in result["warnings"]
    assert len(result["cash_flow_projections"]) == 2

    seven_day = result["cash_flow_projections"][0]
    assert seven_day["window_days"] == 7
    assert seven_day["start_date"] == "2026-06-20"
    assert seven_day["end_date_exclusive"] == "2026-06-27"
    assert seven_day["starting_balance"] == 950.0
    assert seven_day["ending_balance"] == 1450.0
    assert seven_day["lowest_balance"] == 650.0
    assert seven_day["lowest_balance_date"] == "2026-06-21"
    assert [event["instance_id"] for event in seven_day["events"]] == [
        "rent-2026-06-21",
        "paycheck-2026-06-25",
    ]

    fourteen_day = result["cash_flow_projections"][1]
    assert fourteen_day["window_days"] == 14
    assert fourteen_day["ending_balance"] == 1400.0
    assert [event["instance_id"] for event in fourteen_day["events"]] == [
        "rent-2026-06-21",
        "paycheck-2026-06-25",
        "insurance-2026-07-01",
    ]


def test_working_account_uses_tighter_stale_threshold_than_other_accounts(tmp_path):
    """The working (checking) account is stale at WORKING_BALANCE_STALE_DAYS (1
    day), tighter than the general BALANCE_DATE_STALE_DAYS (3 days) every other
    account still uses - so a balance-only feed (e.g. a card updated monthly)
    is not spuriously flagged on the same 2-day-old snapshot."""
    db_path = tmp_path / "transactions.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL, recorded_at TEXT, source TEXT);
        CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, accounts_seen INT, transactions_inserted INT, transactions_updated INT, error TEXT);
        """
    )
    conn.execute("INSERT INTO accounts VALUES ('chk','PREMIER PLUS CKG (4321)','Chase','checking','USD')")
    conn.execute("INSERT INTO accounts VALUES ('card','Rewards Card (9999)','Big Bank','','USD')")
    # Both balances are 2 days old as of 2026-06-20 - over the working account's
    # 1-day bar, under every other account's 3-day bar.
    conn.execute(
        "INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) "
        "VALUES ('chk',1000,1000,'2026-06-18T00:00:00+00:00','simplefin')"
    )
    conn.execute(
        "INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) "
        "VALUES ('card',-200,-200,'2026-06-18T00:00:00+00:00','simplefin')"
    )
    conn.execute(
        "INSERT INTO sync_runs (started_at,finished_at,mode,accounts_seen,transactions_inserted,transactions_updated,error) "
        "VALUES ('2026-06-20T09:58:00+00:00','2026-06-20T10:00:00+00:00','incremental',2,0,0,NULL)"
    )
    conn.commit()
    conn.close()
    ensure_app_schema(sqlite3.connect(db_path))

    result = get_finance_status(
        db_path=str(db_path),
        working_account_id="chk",
        start_date="2026-06-20",
        now=datetime(2026, 6, 20, tzinfo=UTC),
    )

    assert WORKING_BALANCE_STALE_DAYS == 1
    card = next(a for a in result["balances"]["accounts"] if a["account_id"] == "card")
    assert card["balance_age_days"] == 2
    assert card["balance_date_stale"] is False  # under the general 3-day bar

    working = result["cash_flow_projections"][0]["working_account"]
    assert working["balance_age_days"] == 2
    assert working["balance_date_stale"] is True  # over the working-account 1-day bar
