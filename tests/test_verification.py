"""Tests for the deterministic verification phase.

These prove the consistency checks themselves: each one fires exactly when the
rows it guards stop tying together, the clean model passes, and the phase is
wired into persistence, the background pipeline, and the daily digest. No model
is consulted, so every assertion is deterministic for a given database.
"""

import sqlite3

from financial_agent.background import get_background_run, run_background_sync
from financial_agent.digest import build_daily_digest
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent.verification import (
    list_verification_findings,
    run_verification,
)

_CHECKS = [
    "projection_identity",
    "duplicate_instances",
    "statement_identity",
    "instance_sign_sanity",
    "coverage_horizon",
    "cross_obligation_overlap",
]


def _clean_db(path):
    """A small normal model: a checking account with a balance snapshot plus one
    well-formed projectable obligation instance. Every check can run and pass."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY, account_id TEXT, balance REAL,
            available REAL, recorded_at TEXT, source TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,
            amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);
        """
    )
    conn.execute(
        "INSERT INTO accounts (id,name,org,kind,currency) VALUES "
        "('chk','PREMIER PLUS CKG (4321)','Chase','checking','USD')"
    )
    conn.execute(
        "INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) "
        "VALUES ('chk',9000.0,9000.0,'2026-06-20T00:00:00+00:00','simplefin')"
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    apply_obligation_instances(
        conn,
        obligation={"id": "rent", "name": "Rent check", "kind": "housing", "status": "active", "source": "seed"},
        instances=[{"id": "rent:2026-07-03", "due_date": "2026-07-03", "amount": -3000.0, "source": "seed"}],
    )
    conn.commit()
    return conn


def _insert_obligation(conn, oid, name):
    conn.execute(
        "INSERT OR IGNORE INTO obligations (id,name,kind,status,source,created_at,updated_at) "
        "VALUES (?,?,'bill','active','seed','t','t')",
        (oid, name),
    )


def _insert_instance(conn, *, iid, obligation_id, due_date, amount, direction="outflow", status="expected"):
    """Raw insert into obligation_instances. Bypasses apply_obligation_instances,
    which would abs() the amount and derive direction -- needed to plant a negative
    stored amount or a deliberate duplicate."""
    conn.execute(
        "INSERT INTO obligation_instances (id,obligation_id,due_date,amount,direction,status,source,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,'seed','t','t')",
        (iid, obligation_id, due_date, amount, direction, status),
    )


def _insert_cycle(conn, *, cid, input_count, input_sum):
    conn.execute(
        "INSERT INTO statement_cycles (id,target_obligation_id,cycle_close_date,input_count,input_sum,"
        "created_at,updated_at) VALUES (?,?,?,?,?,'t','t')",
        (cid, "rent", "2026-06-30", input_count, input_sum),
    )


def _insert_cycle_input(conn, *, cid, iid, amount):
    conn.execute(
        "INSERT INTO statement_cycle_inputs (statement_cycle_id,obligation_instance_id,input_amount,"
        "created_at) VALUES (?,?,?,'t')",
        (cid, iid, amount),
    )


# --- clean model -----------------------------------------------------------


def test_clean_model_passes_all_checks(tmp_path):
    conn = _clean_db(tmp_path / "clean.sqlite")
    result = run_verification(conn, as_of_date="2026-06-20")

    assert result["ok"] is True
    assert result["checks_run"] == _CHECKS
    assert result["checks_total"] == 6
    assert result["findings_total"] == 0
    assert result["by_severity"] == {}
    assert result["findings"] == []


# --- duplicate_instances ---------------------------------------------------


def test_duplicate_instances_flags_one_error(tmp_path):
    conn = _clean_db(tmp_path / "dup.sqlite")
    _insert_obligation(conn, "card", "Apple Card")
    _insert_instance(conn, iid="card:a", obligation_id="card", due_date="2026-07-15", amount=100.0)
    _insert_instance(conn, iid="card:b", obligation_id="card", due_date="2026-07-15", amount=100.0)
    conn.commit()

    result = run_verification(conn, as_of_date="2026-06-20", persist=False)

    dups = [f for f in result["findings"] if f["check_id"] == "duplicate_instances"]
    assert len(dups) == 1
    assert dups[0]["severity"] == "error"
    assert dups[0]["evidence"]["obligation_id"] == "card"
    assert dups[0]["evidence"]["due_date"] == "2026-07-15"
    assert dups[0]["evidence"]["count"] == 2
    assert result["ok"] is False


# --- statement_identity ----------------------------------------------------


def test_statement_identity_flags_mismatch_and_passes_balanced(tmp_path):
    conn = _clean_db(tmp_path / "stmt.sqlite")
    # Cycle that does NOT tie out: stored sum 300, inputs add to 100.
    _insert_cycle(conn, cid="cyc-bad", input_count=1, input_sum=300.0)
    _insert_cycle_input(conn, cid="cyc-bad", iid="rent:2026-07-03", amount=100.0)
    # Cycle that ties out exactly.
    _insert_cycle(conn, cid="cyc-ok", input_count=1, input_sum=100.0)
    _insert_cycle_input(conn, cid="cyc-ok", iid="rent:2026-07-03", amount=100.0)
    conn.commit()

    result = run_verification(conn, as_of_date="2026-06-20", persist=False)

    stmt = [f for f in result["findings"] if f["check_id"] == "statement_identity"]
    assert len(stmt) == 1
    assert stmt[0]["severity"] == "error"
    assert stmt[0]["evidence"]["statement_cycle_id"] == "cyc-bad"
    assert stmt[0]["evidence"]["stored_sum"] == 300.0
    assert stmt[0]["evidence"]["actual_sum"] == 100.0


# --- instance_sign_sanity --------------------------------------------------


def test_instance_sign_sanity_flags_negative_stored_amount(tmp_path):
    conn = _clean_db(tmp_path / "sign.sqlite")
    _insert_obligation(conn, "elec", "Electric bill")
    # Raw negative stored amount on a projectable instance -- apply_obligation_instances
    # would have abs()'d this away, so it must be inserted directly.
    _insert_instance(conn, iid="elec:2026-07-09", obligation_id="elec", due_date="2026-07-09", amount=-120.0)
    conn.commit()

    result = run_verification(conn, as_of_date="2026-06-20", persist=False)

    sign = [f for f in result["findings"] if f["check_id"] == "instance_sign_sanity"]
    assert len(sign) == 1
    assert sign[0]["severity"] == "error"
    assert sign[0]["evidence"]["instance_id"] == "elec:2026-07-09"
    assert sign[0]["evidence"]["amount"] == -120.0


# --- coverage_horizon ------------------------------------------------------


def test_coverage_horizon_flags_recurring_that_runs_out(tmp_path):
    conn = _clean_db(tmp_path / "cov.sqlite")
    # A monthly bill whose only future instance is next month runs out well before
    # the 90-day horizon (2026-06-20 + 90d = 2026-09-18).
    conn.execute(
        "INSERT INTO obligations (id,name,kind,cadence,status,source,created_at,updated_at) "
        "VALUES ('gym','Gym','fitness','monthly','active','seed','t','t')"
    )
    _insert_instance(conn, iid="gym:2026-07-01", obligation_id="gym", due_date="2026-07-01", amount=3000.0)
    conn.commit()

    cov = [f for f in run_verification(conn, as_of_date="2026-06-20", persist=False)["findings"]
           if f["check_id"] == "coverage_horizon"]
    assert len(cov) == 1 and cov[0]["severity"] == "warn"
    assert cov[0]["evidence"]["obligation_id"] == "gym"

    # Extend it past the horizon -> no longer flagged.
    _insert_instance(conn, iid="gym:2026-10-01", obligation_id="gym", due_date="2026-10-01", amount=3000.0)
    conn.commit()
    assert not [f for f in run_verification(conn, as_of_date="2026-06-20", persist=False)["findings"]
                if f["check_id"] == "coverage_horizon"]


def test_coverage_horizon_ignores_obligation_with_active_until(tmp_path):
    # A bill that ENDS before the horizon (active_until set) ran out on purpose -
    # it must not be flagged as "runs out early".
    conn = _clean_db(tmp_path / "cov.sqlite")
    conn.execute(
        "INSERT INTO obligations (id,name,kind,cadence,status,source,active_until,created_at,updated_at) "
        "VALUES ('gym','Gym','fitness','monthly','active','seed','2026-07-31','t','t')"
    )
    _insert_instance(conn, iid="gym:2026-07-01", obligation_id="gym", due_date="2026-07-01", amount=3000.0)
    conn.commit()
    assert not [f for f in run_verification(conn, as_of_date="2026-06-20", persist=False)["findings"]
                if f["check_id"] == "coverage_horizon"]


# --- persistence -----------------------------------------------------------


def test_persist_true_writes_findings_then_listable(tmp_path):
    conn = _clean_db(tmp_path / "persist.sqlite")
    _insert_obligation(conn, "card", "Apple Card")
    _insert_instance(conn, iid="card:a", obligation_id="card", due_date="2026-07-15", amount=100.0)
    _insert_instance(conn, iid="card:b", obligation_id="card", due_date="2026-07-15", amount=100.0)
    conn.commit()

    run_verification(conn, as_of_date="2026-06-20", run_id="run_xyz", persist=True)

    rows = list_verification_findings(conn)
    assert len(rows) == 1
    assert rows[0]["check_id"] == "duplicate_instances"
    assert rows[0]["run_id"] == "run_xyz"
    assert rows[0]["status"] == "open"
    assert rows[0]["as_of_date"] == "2026-06-20"
    assert rows[0]["evidence"]["due_date"] == "2026-07-15"

    # Filter by check_id narrows to the same row; an unrelated check is empty.
    assert len(list_verification_findings(conn, check_id="duplicate_instances")) == 1
    assert list_verification_findings(conn, check_id="statement_identity") == []


def test_persist_false_writes_nothing(tmp_path):
    conn = _clean_db(tmp_path / "nopersist.sqlite")
    _insert_obligation(conn, "card", "Apple Card")
    _insert_instance(conn, iid="card:a", obligation_id="card", due_date="2026-07-15", amount=100.0)
    _insert_instance(conn, iid="card:b", obligation_id="card", due_date="2026-07-15", amount=100.0)
    conn.commit()

    result = run_verification(conn, as_of_date="2026-06-20", persist=False)
    assert result["findings_total"] == 1
    assert list_verification_findings(conn, status=None) == []


# --- cross_obligation_overlap ----------------------------------------------


def test_cross_obligation_overlap_flags_likely_duplicate(tmp_path):
    conn = _clean_db(tmp_path / "ov.sqlite")
    # Two active 'auto' obligations with instances in the same month at comparable
    # amounts -> likely the same real bill (old lease replaced by new). Review flag.
    for oid, name in [("oldcar", "Old car lease"), ("newcar", "New car lease")]:
        conn.execute(
            "INSERT INTO obligations (id,name,kind,status,source,created_at,updated_at) "
            "VALUES (?,?,'auto','active','seed','t','t')",
            (oid, name),
        )
    _insert_instance(conn, iid="oldcar:2026-08-08", obligation_id="oldcar", due_date="2026-08-08", amount=700.0)
    _insert_instance(conn, iid="newcar:2026-08-10", obligation_id="newcar", due_date="2026-08-10", amount=600.0)
    conn.commit()

    ov = [f for f in run_verification(conn, as_of_date="2026-06-20", persist=False)["findings"]
          if f["check_id"] == "cross_obligation_overlap"]
    assert len(ov) == 1 and ov[0]["severity"] == "warn"
    assert ov[0]["evidence"]["shared_months"] == ["2026-08"]

    # A different-kind bill in the same month does NOT pair with them.
    conn.execute(
        "INSERT INTO obligations (id,name,kind,status,source,created_at,updated_at) "
        "VALUES ('netflix','Netflix','subscription','active','seed','t','t')"
    )
    _insert_instance(conn, iid="netflix:2026-08-15", obligation_id="netflix", due_date="2026-08-15", amount=15.0)
    conn.commit()
    ov2 = [f for f in run_verification(conn, as_of_date="2026-06-20", persist=False)["findings"]
           if f["check_id"] == "cross_obligation_overlap"]
    assert len(ov2) == 1  # still just the two car leases


def test_cross_obligation_overlap_ignores_unrelated_same_kind_bills(tmp_path):
    # Same kind, same month, but dissimilar names AND amounts more than 15%
    # apart: unrelated subscriptions (the Claude-vs-Optimum noise), no finding.
    conn = _clean_db(tmp_path / "ov2.sqlite")
    for oid, name in [("claude", "Claude subscription"), ("optimum", "Optimum internet")]:
        conn.execute(
            "INSERT INTO obligations (id,name,kind,status,source,created_at,updated_at) "
            "VALUES (?,?,'subscription','active','seed','t','t')",
            (oid, name),
        )
    _insert_instance(conn, iid="claude:2026-08-05", obligation_id="claude", due_date="2026-08-05", amount=200.0)
    _insert_instance(conn, iid="optimum:2026-08-12", obligation_id="optimum", due_date="2026-08-12", amount=120.0)
    conn.commit()

    assert not [f for f in run_verification(conn, as_of_date="2026-06-20", persist=False)["findings"]
                if f["check_id"] == "cross_obligation_overlap"]


def test_cross_obligation_overlap_ignores_unrelated_similar_amounts(tmp_path):
    conn = _clean_db(tmp_path / "ov3.sqlite")
    for oid, name in [("github", "GitHub subscription"), ("nyt", "New York Times subscription")]:
        conn.execute(
            "INSERT INTO obligations (id,name,kind,status,source,created_at,updated_at) "
            "VALUES (?,?,'subscription','active','seed','t','t')",
            (oid, name),
        )
    _insert_instance(conn, iid="github:2026-08-05", obligation_id="github", due_date="2026-08-05", amount=100.0)
    _insert_instance(conn, iid="nyt:2026-08-12", obligation_id="nyt", due_date="2026-08-12", amount=105.0)
    conn.commit()

    assert not [f for f in run_verification(conn, as_of_date="2026-06-20", persist=False)["findings"]
                if f["check_id"] == "cross_obligation_overlap"]


def test_cross_obligation_overlap_keeps_exact_name_even_when_amount_changes(tmp_path):
    conn = _clean_db(tmp_path / "ov4.sqlite")
    for oid, name in [("gym_old", "Gym membership"), ("gym_new", "gym membership")]:
        conn.execute(
            "INSERT INTO obligations (id,name,kind,status,source,created_at,updated_at) "
            "VALUES (?,?,'subscription','active','seed','t','t')",
            (oid, name),
        )
    _insert_instance(conn, iid="gym_old:2026-08-05", obligation_id="gym_old", due_date="2026-08-05", amount=50.0)
    _insert_instance(conn, iid="gym_new:2026-08-12", obligation_id="gym_new", due_date="2026-08-12", amount=80.0)
    conn.commit()

    findings = [f for f in run_verification(conn, as_of_date="2026-06-20", persist=False)["findings"]
                if f["check_id"] == "cross_obligation_overlap"]
    assert len(findings) == 1


# --- acknowledge / baseline --------------------------------------------------


def test_acknowledged_findings_stop_counting_as_new(tmp_path):
    from financial_agent.verification import acknowledge_verification_findings

    conn = _clean_db(tmp_path / "ack.sqlite")
    # Plant a warn finding: two similarly-named same-kind bills in one month.
    for oid, name in [("oldcar", "Old car lease"), ("newcar", "New car lease")]:
        conn.execute(
            "INSERT INTO obligations (id,name,kind,status,source,created_at,updated_at) "
            "VALUES (?,?,'auto','active','seed','t','t')",
            (oid, name),
        )
    _insert_instance(conn, iid="oldcar:2026-08-08", obligation_id="oldcar", due_date="2026-08-08", amount=700.0)
    _insert_instance(conn, iid="newcar:2026-08-10", obligation_id="newcar", due_date="2026-08-10", amount=600.0)
    conn.commit()

    first = run_verification(conn, as_of_date="2026-06-20", persist=True)
    assert first["ok"] is False and first["new_total"] == 1

    assert acknowledge_verification_findings(conn) == {"acknowledged": 1}

    # Still failing, but acknowledged: ok flips true, counts split new vs acked.
    second = run_verification(conn, as_of_date="2026-06-21", persist=True)
    assert second["ok"] is True
    assert second["findings_total"] == 1
    assert second["new_total"] == 0
    assert second["acknowledged_total"] == 1
    assert second["by_check"] == {"cross_obligation_overlap": 1}
    assert second["findings"][0]["acknowledged"] is True
    # No duplicate open row was inserted; the acknowledged row survives.
    assert list_verification_findings(conn, status="open") == []
    assert len(list_verification_findings(conn, status="acknowledged")) == 1

    # Fixing the underlying overlap still resolves the acknowledged row.
    conn.execute("DELETE FROM obligation_instances WHERE id = 'oldcar:2026-08-08'")
    conn.commit()
    run_verification(conn, as_of_date="2026-06-22", persist=True)
    assert list_verification_findings(conn, status="acknowledged") == []
    assert len(list_verification_findings(conn, status="resolved")) == 1


def test_blanket_acknowledge_skips_errors_but_explicit_id_works(tmp_path):
    from financial_agent.verification import acknowledge_verification_findings

    conn = _clean_db(tmp_path / "ackerr.sqlite")
    _insert_obligation(conn, "card", "Apple Card")
    _insert_instance(conn, iid="card:a", obligation_id="card", due_date="2026-07-15", amount=100.0)
    _insert_instance(conn, iid="card:b", obligation_id="card", due_date="2026-07-15", amount=100.0)
    conn.commit()
    run_verification(conn, as_of_date="2026-06-20", persist=True)

    # Blanket acknowledge must not silence the error-severity finding.
    assert acknowledge_verification_findings(conn) == {"acknowledged": 0}
    open_rows = list_verification_findings(conn, status="open")
    assert len(open_rows) == 1

    # An explicit id acknowledges it.
    assert acknowledge_verification_findings(conn, finding_ids=[open_rows[0]["id"]]) == {
        "acknowledged": 1
    }
    assert list_verification_findings(conn, status="open") == []


# --- pipeline integration --------------------------------------------------


def test_background_sync_runs_verify_between_suppress_and_surface(tmp_path):
    conn = _clean_db(tmp_path / "bg.sqlite")
    result = run_background_sync(conn, as_of_date="2026-06-30")

    verify = result["result_summary"]["verify"]
    assert set(verify) == {
        "ok", "checks_total", "findings_total", "new_total",
        "acknowledged_total", "by_severity", "by_check",
    }
    assert verify["ok"] is True
    assert verify["checks_total"] == 6
    assert verify["new_total"] == 0
    assert verify["acknowledged_total"] == 0

    events = [e["event_type"] for e in get_background_run(conn, result["run_id"])["events"]]
    assert "verify" in events
    assert events.index("suppress_contradicted_estimates") < events.index("verify") < events.index("surface_due_items")


def test_background_sync_persists_findings_tagged_with_run_id(tmp_path):
    conn = _clean_db(tmp_path / "bgp.sqlite")
    _insert_obligation(conn, "card", "Apple Card")
    _insert_instance(conn, iid="card:a", obligation_id="card", due_date="2026-07-15", amount=100.0)
    _insert_instance(conn, iid="card:b", obligation_id="card", due_date="2026-07-15", amount=100.0)
    conn.commit()

    result = run_background_sync(conn, as_of_date="2026-06-30")

    assert result["result_summary"]["verify"]["findings_total"] >= 1
    rows = list_verification_findings(conn, check_id="duplicate_instances")
    assert len(rows) == 1
    assert rows[0]["run_id"] == result["run_id"]


# --- digest integration ----------------------------------------------------


def test_digest_includes_verification_block(tmp_path):
    db = str(tmp_path / "digest.sqlite")
    conn = _clean_db(db)
    conn.execute(
        "CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, "
        "accounts_seen INT, transactions_inserted INT, transactions_updated INT, error TEXT)"
    )
    conn.execute(
        "INSERT INTO sync_runs (started_at,finished_at,mode,accounts_seen,transactions_inserted,"
        "transactions_updated,error) VALUES ('2026-06-20T09:58:00+00:00','2026-06-20T10:00:00+00:00','i',1,0,0,NULL)"
    )
    conn.commit()
    conn.close()

    digest = build_daily_digest(db, as_of_date="2026-06-20")

    block = digest["verification"]
    assert set(block) == {"ok", "checks_total", "findings_total", "by_severity", "findings"}
    assert block["ok"] is True
    assert block["checks_total"] == 6
    assert block["findings_total"] == 0

    # The digest read is non-persisting: nothing was written.
    check = sqlite3.connect(db)
    check.row_factory = sqlite3.Row
    assert list_verification_findings(check, status=None) == []


# --- reconcile (resolve-on-fix, no duplicate-on-rerun) ----------------------


def test_persisting_runs_reconcile_instead_of_appending(tmp_path):
    conn = _clean_db(tmp_path / "reconcile.sqlite")
    _insert_obligation(conn, "card", "Apple Card")
    _insert_instance(conn, iid="card:a", obligation_id="card", due_date="2026-07-15", amount=100.0)
    _insert_instance(conn, iid="card:b", obligation_id="card", due_date="2026-07-15", amount=100.0)
    conn.commit()

    # First run opens the finding.
    run_verification(conn, as_of_date="2026-06-20", run_id="run_1", persist=True)
    assert len(list_verification_findings(conn, status="open")) == 1

    # Re-running the SAME still-broken model must not append a duplicate row.
    run_verification(conn, as_of_date="2026-06-21", run_id="run_2", persist=True)
    open_rows = list_verification_findings(conn, status="open")
    assert len(open_rows) == 1
    assert open_rows[0]["run_id"] == "run_1"  # the original open row is kept

    # Fix the duplicate, re-run: the stale finding flips to 'resolved'.
    conn.execute("DELETE FROM obligation_instances WHERE id = 'card:b'")
    conn.commit()
    run_verification(conn, as_of_date="2026-06-22", run_id="run_3", persist=True)
    assert list_verification_findings(conn, status="open") == []
    assert len(list_verification_findings(conn, status="resolved")) == 1
