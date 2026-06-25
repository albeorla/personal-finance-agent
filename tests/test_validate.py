"""Tests for the live-data validation harness (slice O). No network."""

import sqlite3

from financial_agent import validate as val
from financial_agent.config import ensure_source_tables
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent.validate import build_validation_report, run_live_validation


def _seed(path, *, orphan_target=False):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_source_tables(conn)
    ensure_app_schema(conn)
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency,first_seen_at,last_seen_at) "
                 "VALUES ('chk','PREMIER PLUS CKG (XXXX)','Chase','','USD','x','x')")
    conn.execute("INSERT INTO transactions (id,account_id,posted,amount,payee,source,first_seen_at,last_seen_at,fetched_at) "
                 "VALUES ('t1','chk','2026-06-10T08:00:00',-115.87,'Eversource Energy','simplefin','x','x','x')")
    apply_obligation_instances(
        conn,
        obligation={"id": "amex_statement_payment", "name": "Amex statement payment", "kind": "credit_card_statement",
                    "status": "active", "source": "seed"},
        instances=[{"id": "amex_statement_payment:2026-07-16", "due_date": "2026-07-16", "amount": -5400.0, "source": "seed"}],
    )
    target = "does_not_exist" if orphan_target else "amex_statement_payment"
    apply_obligation_instances(
        conn,
        obligation={"id": "gault", "name": "Gault Energy", "kind": "card_spend_input", "status": "active", "source": "seed"},
        instances=[{"id": "gault:2026-07-01", "due_date": "2026-07-01", "amount": -500.0, "source": "seed",
                    "cash_flow_treatment": "card_statement_input", "statement_target_obligation_id": target}],
    )
    conn.commit()
    conn.close()
    return str(path)


def test_build_validation_report_passes_on_clean_db(tmp_path):
    db = _seed(tmp_path / "v.sqlite")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    report = build_validation_report(conn, as_of_date="2026-06-21")
    conn.close()

    assert report["all_checks_passed"] is True
    names = {c["name"] for c in report["checks"]}
    assert {"accounts_present", "working_account_present", "no_orphan_statement_targets", "instance_amounts_normalized"} <= names
    assert "reconcile" in report and "drift_by_type" in report


def test_orphan_statement_target_fails_the_check(tmp_path):
    db = _seed(tmp_path / "v.sqlite", orphan_target=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    report = build_validation_report(conn, as_of_date="2026-06-21")
    conn.close()

    orphan_check = next(c for c in report["checks"] if c["name"] == "no_orphan_statement_targets")
    assert orphan_check["passed"] is False
    assert "does_not_exist" in orphan_check["evidence"]["orphans"]
    assert report["all_checks_passed"] is False


def test_run_live_validation_does_not_mutate_source(tmp_path):
    db = _seed(tmp_path / "v.sqlite")
    before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM charge_onboarding_candidates").fetchone()[0]

    result = run_live_validation(source_db_path=db, as_of_date="2026-06-21", sync=False)

    # The scan ran on the working copy, not the source.
    after = sqlite3.connect(db).execute("SELECT COUNT(*) FROM charge_onboarding_candidates").fetchone()[0]
    assert before == after == 0
    assert result["report"]["all_checks_passed"] is True
    assert result["work_db_path"] is None  # cleaned up


def test_run_live_validation_cleans_up_its_temp_dir(tmp_path, monkeypatch):
    db = _seed(tmp_path / "v.sqlite")
    made = tmp_path / "workdir"
    made.mkdir()
    monkeypatch.setattr(val.tempfile, "mkdtemp", lambda **k: str(made))
    run_live_validation(source_db_path=db, as_of_date="2026-06-21", sync=False)
    assert not made.exists()  # the temp dir it created is removed, not just the file


def test_run_live_validation_with_monkeypatched_sync(tmp_path, monkeypatch):
    db = _seed(tmp_path / "v.sqlite")
    monkeypatch.setattr(val, "get_finance_config", lambda **k: {"has_simplefin": True})
    monkeypatch.setattr(val, "sync_simplefin", lambda *a, **k: {"accounts": 9, "inserted": 5, "updated": 700, "error": None})

    result = run_live_validation(source_db_path=db, as_of_date="2026-06-21", sync=True)
    assert result["synced"]["simplefin"]["accounts"] == 9
    # Todoist is output-only now, so the validation harness pulls SimpleFIN only.
    assert "todoist" not in result["synced"]
    # Secret-free: the sync summaries carry only counts.
    assert "access_url" not in str(result["synced"]) and "token" not in str(result["synced"])
