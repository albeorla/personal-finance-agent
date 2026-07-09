"""Tests for the background runner + telemetry (slice F)."""

import sqlite3
from datetime import datetime, timedelta

import financial_agent.background as background
from financial_agent.background import (
    get_background_run,
    get_job_health,
    list_background_runs,
    run_background_sync,
)
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema


def _insert_run(conn, *, run_id, status, hours_ago, run_type="daily_sync"):
    """Insert a completed background run whose finished_at is N hours before now."""

    finished = (datetime.now().astimezone() - timedelta(hours=hours_ago)).isoformat()
    started = (datetime.now().astimezone() - timedelta(hours=hours_ago + 0.1)).isoformat()
    conn.execute(
        "INSERT INTO background_runs (id,trace_id,run_type,trigger_type,status,as_of_date,started_at,finished_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, f"trace_{run_id}", run_type, "manual", status, "2026-06-30", started, finished, started),
    )
    conn.commit()


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,
            amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);
        """
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('ACT-chk','PREMIER PLUS CKG (4321)','Chase','','USD')")
    conn.executemany(
        "INSERT INTO transactions (id,account_id,posted,amount,payee,description,pending,source) VALUES (?,?,?,?,?,?,0,'simplefin')",
        [
            ("n1", "ACT-chk", "2026-04-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
            ("n2", "ACT-chk", "2026-05-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
            ("n3", "ACT-chk", "2026-06-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
        ],
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    apply_obligation_instances(
        conn,
        obligation={"id": "rent", "name": "Rent check", "kind": "housing", "status": "active", "source": "seed"},
        instances=[{"id": "rent:2026-06-01", "due_date": "2026-06-01", "amount": -3000.0, "source": "seed"}],
    )
    conn.commit()
    return conn


_EXPECTED_SEQUENCE = [
    "run_started", "scan_charge_candidates", "reconcile", "detect_drift",
    "suppress_dormant_estimates", "suppress_contradicted_estimates",
    "verify",
    "surface_due_items",
    "reconcile_todoist_completions",
    "run_finished",
]


def test_run_background_sync_records_run_and_ordered_events(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    result = run_background_sync(conn, as_of_date="2026-06-30")

    assert result["status"] == "succeeded"
    assert result["errors"] == 0
    assert result["run_id"].startswith("run_")
    assert result["trace_id"].startswith("trace_")
    assert result["duration_ms"] >= 0
    # Every pipeline step ran and produced a summary. Contradiction suppression
    # is active by default (enforce mode) and appears in the standard pipeline.
    assert set(result["result_summary"]) == {
        "scan_charge_candidates", "reconcile", "detect_drift",
        "suppress_dormant_estimates", "suppress_contradicted_estimates",
        "verify",
        "surface_due_items",
        "reconcile_todoist_completions",
    }
    assert result["result_summary"]["suppress_contradicted_estimates"]["mode"] == "enforce"
    assert result["result_summary"]["scan_charge_candidates"]["created"] == 1
    # With Todoist write-back gated off (the default), surfacing makes no live
    # call and reports awaiting-integration.
    assert result["result_summary"]["surface_due_items"]["status"] == "awaiting-integration"

    run = get_background_run(conn, result["run_id"])
    assert [e["event_type"] for e in run["events"]] == _EXPECTED_SEQUENCE
    assert all(e["status"] in ("ok", "succeeded") for e in run["events"])


def test_event_sequence_is_deterministic_across_runs(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    first = run_background_sync(conn, as_of_date="2026-06-30")
    second = run_background_sync(conn, as_of_date="2026-06-30")

    assert first["run_id"] != second["run_id"]  # each run is its own record
    seq1 = [e["event_type"] for e in get_background_run(conn, first["run_id"])["events"]]
    seq2 = [e["event_type"] for e in get_background_run(conn, second["run_id"])["events"]]
    assert seq1 == seq2 == _EXPECTED_SEQUENCE
    # Idempotent pipeline: the second scan creates nothing new.
    assert second["result_summary"]["scan_charge_candidates"]["created"] == 0


def _add_whole_foods(conn):
    """Add a card account with regular-but-variable grocery spend.

    This is the classifier's PARK case: monthly cadence with a swinging ticket.
    Under the active (enforce) default the scan pulls it out of the active walk.
    """

    conn.execute(
        "INSERT INTO accounts (id,name,org,kind,currency) VALUES "
        "('ACT-amex','Platinum Card (4328)','American Express','','USD')"
    )
    conn.executemany(
        "INSERT INTO transactions (id,account_id,posted,amount,payee,description,pending,source) "
        "VALUES (?,?,?,?,?,?,0,'simplefin')",
        [
            ("wf-1", "ACT-amex", "2026-01-05T08:00:00", -12.00, "Whole Foods", "WHOLE FOODS"),
            ("wf-2", "ACT-amex", "2026-02-05T08:00:00", -45.00, "Whole Foods", "WHOLE FOODS"),
            ("wf-3", "ACT-amex", "2026-03-05T08:00:00", -130.00, "Whole Foods", "WHOLE FOODS"),
            ("wf-4", "ACT-amex", "2026-04-05T08:00:00", -277.00, "Whole Foods", "WHOLE FOODS"),
        ],
    )
    conn.commit()


def test_scan_auto_triages_in_enforce_mode_by_default(tmp_path):
    """The daily pipeline turns the classifier on: a PARK candidate is pulled out
    of the active walk with no options passed."""
    conn = _db(tmp_path / "b.sqlite")
    _add_whole_foods(conn)

    run_background_sync(conn, as_of_date="2026-06-30")

    status = conn.execute(
        "SELECT status FROM charge_onboarding_candidates WHERE merchant_key='whole_foods'"
    ).fetchone()["status"]
    assert status == "parked"


def test_safe_option_still_disables_active_steps(tmp_path):
    """Escape hatch: shadow scan + contradiction disabled returns the pipeline to
    its inert posture -- the off switch still works, only the default changed."""
    conn = _db(tmp_path / "b.sqlite")
    _add_whole_foods(conn)

    result = run_background_sync(
        conn,
        as_of_date="2026-06-30",
        options={
            "scan": {"auto_triage": {"mode": "shadow"}},
            "contradiction": {"enabled": False},
        },
    )

    # Contradiction step is skipped entirely when explicitly disabled.
    assert "suppress_contradicted_estimates" not in result["result_summary"]
    run = get_background_run(conn, result["run_id"])
    assert "suppress_contradicted_estimates" not in [e["event_type"] for e in run["events"]]

    # Shadow scan stamps a disposition but does not move the candidate.
    status = conn.execute(
        "SELECT status FROM charge_onboarding_candidates WHERE merchant_key='whole_foods'"
    ).fetchone()["status"]
    assert status == "proposed"


def test_list_background_runs(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    run_background_sync(conn, as_of_date="2026-06-30")
    run_background_sync(conn, as_of_date="2026-07-01")
    runs = list_background_runs(conn, run_type="daily_sync")
    assert len(runs) == 2
    assert all(r["status"] == "succeeded" for r in runs)


def test_step_error_yields_partial_success_and_run_continues(tmp_path, monkeypatch):
    conn = _db(tmp_path / "b.sqlite")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated reconcile failure")

    monkeypatch.setattr(background, "reconcile_obligation_instances", _boom)
    result = run_background_sync(conn, as_of_date="2026-06-30")

    assert result["status"] == "partial_success"
    assert result["errors"] == 1
    assert "error" in result["result_summary"]["reconcile"]

    run = get_background_run(conn, result["run_id"])
    # The run still completed every step despite the failure.
    assert [e["event_type"] for e in run["events"]] == _EXPECTED_SEQUENCE
    reconcile_event = next(e for e in run["events"] if e["event_type"] == "reconcile")
    assert reconcile_event["status"] == "error"
    # Later steps still ran.
    assert "detect_drift" in result["result_summary"]


def test_get_background_run_unknown_returns_none(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    assert get_background_run(conn, "run_does_not_exist") is None


_EXPECTED_WITH_SYNC = [
    "run_started", "sync_simplefin", "scan_charge_candidates",
    "reconcile", "detect_drift", "suppress_dormant_estimates",
    "suppress_contradicted_estimates", "verify", "surface_due_items",
    "reconcile_todoist_completions",
    "run_finished",
]


def test_sync_steps_run_when_enabled_and_configured(tmp_path, monkeypatch):
    conn = _db(tmp_path / "b.sqlite")
    monkeypatch.setattr(background, "get_finance_config", lambda **k: {"has_simplefin": True})
    monkeypatch.setattr(background, "sync_simplefin", lambda *a, **k: {"accounts": 9, "inserted": 700, "updated": 0, "error": None})

    result = run_background_sync(conn, as_of_date="2026-06-30", options={"sync": True})
    assert result["status"] == "succeeded"
    assert result["result_summary"]["sync_simplefin"]["accounts"] == 9
    run = get_background_run(conn, result["run_id"])
    assert [e["event_type"] for e in run["events"]] == _EXPECTED_WITH_SYNC


def test_sync_warnings_promote_run_to_succeeded_with_warnings(tmp_path, monkeypatch):
    """A completed sync that recorded feed problems must not report a clean ok,
    so get_job_health surfaces the warning state instead of hiding it."""
    conn = _db(tmp_path / "b.sqlite")
    monkeypatch.setattr(background, "get_finance_config", lambda **k: {"has_simplefin": True})
    monkeypatch.setattr(
        background, "sync_simplefin",
        lambda *a, **k: {"accounts": 9, "inserted": 0, "updated": 0, "error": "Chase timeout",
                         "warnings": ["Chase timeout"], "notes": []},
    )

    result = run_background_sync(conn, as_of_date="2026-06-30", options={"sync": True})
    assert result["status"] == "succeeded_with_warnings"
    assert result["result_summary"]["sync_simplefin"]["warnings"] == ["Chase timeout"]
    run = get_background_run(conn, result["run_id"])
    sync_event = next(e for e in run["events"] if e["event_type"] == "sync_simplefin")
    assert sync_event["status"] == "succeeded_with_warnings"

    # Still a heartbeat (the run completed and ingested), but the status shows
    # the feed problem instead of a clean "succeeded".
    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["healthy"] is True
    assert health["last_run_status"] == "succeeded_with_warnings"


def test_sync_expected_notes_do_not_warn(tmp_path, monkeypatch):
    """The permanent Apple Card balance-only note is informational: it must not
    flip the run out of a clean succeeded status."""
    conn = _db(tmp_path / "b.sqlite")
    monkeypatch.setattr(background, "get_finance_config", lambda **k: {"has_simplefin": True})
    monkeypatch.setattr(
        background, "sync_simplefin",
        lambda *a, **k: {"accounts": 9, "inserted": 0, "updated": 0, "error": None, "warnings": [],
                         "notes": ["Apple Card ... (expected: balance-only connection, no transaction feed)"]},
    )

    result = run_background_sync(conn, as_of_date="2026-06-30", options={"sync": True})
    assert result["status"] == "succeeded"
    assert "notes" in result["result_summary"]["sync_simplefin"]


def test_sync_steps_skipped_when_not_configured(tmp_path, monkeypatch):
    conn = _db(tmp_path / "b.sqlite")
    monkeypatch.setattr(background, "get_finance_config", lambda **k: {"has_simplefin": False})
    result = run_background_sync(conn, as_of_date="2026-06-30", options={"sync": True})
    # The SimpleFIN step still appears in the timeline but no network call is made.
    assert "skipped" in result["result_summary"]["sync_simplefin"]


def test_run_background_sync_suppresses_dormant_estimate(tmp_path):
    """Dormancy suppression runs as part of the pipeline and is recorded."""
    import json as _json

    from financial_agent.obligations import apply_obligation_instances as _apply

    conn = _db(tmp_path / "bg_dormant.sqlite")
    # Source account that has gone dormant: zero balance, no recent transactions.
    conn.execute(
        "INSERT INTO accounts (id,name,org,kind,currency) VALUES "
        "('chase_amazon','Chase Amazon','Chase','credit_card','USD')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS balance_snapshots ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, balance REAL, "
        "available REAL, recorded_at TEXT, source TEXT)"
    )
    conn.execute(
        "INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) "
        "VALUES ('chase_amazon',0.0,0.0,'2026-06-23T00:00:00+00:00','simplefin')"
    )
    conn.execute(
        """
        INSERT INTO charge_onboarding_candidates (
            id, merchant_key, display_name, direction, status, candidate_type,
            cash_flow_treatment, proposed_cash_impact_policy_json, evidence_count,
            existing_obligation_id, created_at, updated_at
        ) VALUES ('cand_chase','chase','Chase Card Payment','outflow','applied',
                  'card_statement_input','direct_checking', ?, 3,
                  'onboarded_chase','2026-01-01T00:00:00','2026-01-01T00:00:00')
        """,
        (_json.dumps({"evidence_account_ids": ["chase_amazon"]}, sort_keys=True),),
    )
    _apply(
        conn,
        obligation={
            "id": "onboarded_chase", "name": "Chase card payment estimate",
            "kind": "bill", "cadence": "monthly", "status": "active",
            "source": "charge_onboarding:cand_chase",
        },
        instances=[{
            "id": "onboarded_chase:2026-07-10", "due_date": "2026-07-10",
            "amount": -1162.0, "direction": "outflow", "status": "expected",
            "source": "charge_onboarding:cand_chase",
            "amount_status": "estimated", "estimation_method": "average",
        }],
    )
    conn.commit()

    result = run_background_sync(conn, as_of_date="2026-06-24")

    summary = result["result_summary"]["suppress_dormant_estimates"]
    assert summary["suppressed_count"] == 1
    assert conn.execute(
        "SELECT status FROM obligations WHERE id='onboarded_chase'"
    ).fetchone()["status"] == "dormant_suppressed"

    run = get_background_run(conn, result["run_id"])
    event = next(e for e in run["events"] if e["event_type"] == "suppress_dormant_estimates")
    assert event["status"] == "ok"
    assert event["event_data"]["suppressed_count"] == 1


# --- job health / heartbeat -------------------------------------------------


def test_get_job_health_fresh_run(tmp_path):
    """A successful daily run just completed: the job is healthy, not stale."""
    conn = _db(tmp_path / "h.sqlite")
    result = run_background_sync(conn, as_of_date="2026-06-30")

    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["healthy"] is True
    assert health["is_stale"] is False
    assert health["last_run_id"] == result["run_id"]
    assert health["last_run_status"] == "succeeded"
    assert health["hours_since_last_run"] is not None
    assert health["hours_since_last_run"] < 1


def test_get_job_health_stale_run(tmp_path):
    """The last successful run finished 30h ago: stale and unhealthy."""
    conn = _db(tmp_path / "h.sqlite")
    _insert_run(conn, run_id="run_old", status="succeeded", hours_ago=30)

    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["healthy"] is False
    assert health["is_stale"] is True
    assert health["last_run_id"] == "run_old"
    assert health["hours_since_last_run"] >= 26


def test_get_job_health_no_runs(tmp_path):
    """No daily run on record: unhealthy, stale, with no last run."""
    conn = _db(tmp_path / "h.sqlite")
    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["healthy"] is False
    assert health["is_stale"] is True
    assert health["last_run_id"] is None
    assert health["hours_since_last_run"] is None


def test_partial_success_run_counts_as_heartbeat(tmp_path, monkeypatch):
    """A run that completes with partial_success still saw the data, so a same-day
    partial run keeps the job healthy."""
    conn = _db(tmp_path / "h.sqlite")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated reconcile failure")

    monkeypatch.setattr(background, "reconcile_obligation_instances", _boom)
    result = run_background_sync(conn, as_of_date="2026-06-30")
    assert result["status"] == "partial_success"

    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["healthy"] is True
    assert health["is_stale"] is False
    assert health["last_run_status"] == "partial_success"


def test_get_job_health_old_partial_success_is_stale(tmp_path):
    """If only an OLD partial_success run exists, the job is still stale."""
    conn = _db(tmp_path / "h.sqlite")
    _insert_run(conn, run_id="run_partial_old", status="partial_success", hours_ago=30)
    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["is_stale"] is True
    assert health["last_run_status"] == "partial_success"


def test_stale_threshold_boundary(tmp_path):
    """26h exactly is not stale; just over the threshold is."""
    conn = _db(tmp_path / "h.sqlite")
    _insert_run(conn, run_id="run_at_threshold", status="succeeded", hours_ago=26)
    at = get_job_health(conn, as_of_date="2026-06-30")
    assert at["is_stale"] is False

    conn.execute("DELETE FROM background_runs")
    _insert_run(conn, run_id="run_over", status="succeeded", hours_ago=26.05)
    over = get_job_health(conn, as_of_date="2026-06-30")
    assert over["is_stale"] is True


def test_in_progress_run_does_not_count_as_heartbeat(tmp_path):
    """An in-progress run (finished_at NULL) is not a completed heartbeat."""
    conn = _db(tmp_path / "h.sqlite")
    conn.execute(
        "INSERT INTO background_runs (id,trace_id,run_type,trigger_type,status,as_of_date,started_at,created_at) "
        "VALUES ('run_inflight','t','daily_sync','manual','in_progress','2026-06-30',?,?)",
        (datetime.now().astimezone().isoformat(), datetime.now().astimezone().isoformat()),
    )
    conn.commit()
    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["is_stale"] is True
    assert health["last_run_id"] is None


# --- task-linked charge close-out in the unattended run (IMP-20260708-7) ----


def _seed_open_followup_emission(conn, followup_id, task_id):
    """Surface a due follow-up to Todoist (ledger only, no network) so the run
    has an OPEN emission linked to a followup:<id> to close out."""

    from financial_agent.todoist_outbox import surface_to_todoist

    key = f"followup:{followup_id}"
    surface_to_todoist(
        conn,
        [{"surface_key": key, "content": "Match the $30.30 NYT charge when it posts"}],
        "2026-06-30",
        write_enabled=True,
        token="tok",
        project_id="proj",
        send_func=lambda token, path, body, **kw: {"id": task_id},
    )
    return key


def test_unattended_run_closes_out_task_confirmed_charge_and_is_idempotent(tmp_path):
    """The daily background run reads task completions back itself: a charge the
    user confirmed by checking its Todoist task is closed out (emission resolved,
    linked follow-up resolved) without an interactive session, it NEVER marks the
    charge paid (no transaction match written), and a replay is a clean no-op."""

    from financial_agent.follow_ups import capture_followup, list_due_followups

    conn = _db(tmp_path / "b.sqlite")
    fup = capture_followup(conn, "Match the $30.30 NYT charge when it posts", "2026-06-30")
    key = _seed_open_followup_emission(conn, fup["id"], "T1")
    assert list_due_followups(conn, as_of_date="2026-06-30")  # due before the run

    # Todoist reports the task checked off (the user confirmed the charge). The
    # gate is forced on and the reader injected, so no live network call is made.
    reads: list[str] = []

    def read(token, task_id):
        reads.append(task_id)
        return {"id": task_id, "checked": True}

    opts = {"reconcile_completions": {"write_enabled": True, "token": "tok", "read_func": read}}

    first = run_background_sync(conn, as_of_date="2026-06-30", options=opts)
    step = first["result_summary"]["reconcile_todoist_completions"]
    assert step["resolved"] == 1 and step["followups_resolved"] == 1 and step["failed"] == 0
    assert reads == ["T1"]  # the open emission was checked exactly once

    # Close-out took effect: emission completed, follow-up resolved (no re-nag).
    assert conn.execute(
        "SELECT status FROM todoist_emissions WHERE surface_key = ?", (key,)
    ).fetchone()["status"] == "completed"
    assert not list_due_followups(conn, as_of_date="2026-06-30")

    # Evidence invariant: the close-out marks NOTHING paid. No obligation instance
    # gained a transaction match from reading the task back.
    matched = conn.execute(
        "SELECT COUNT(*) FROM obligation_instances WHERE matched_transaction_id IS NOT NULL"
    ).fetchone()[0]
    assert matched == 0

    # Idempotent replay: the emission is already closed, so the second run finds
    # no open row to check (no read, nothing re-resolved) and does not error.
    reads.clear()
    second = run_background_sync(conn, as_of_date="2026-06-30", options=opts)
    assert second["status"] == "succeeded"
    step2 = second["result_summary"]["reconcile_todoist_completions"]
    assert step2["checked"] == 0 and step2["resolved"] == 0 and step2["failed"] == 0
    assert reads == []
