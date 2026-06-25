"""Tests for the parallel-run parity report (slice R)."""

import sqlite3

from financial_agent.obligations import apply_obligation_instances
from financial_agent.parity import compare_to_legacy, render_parity_markdown
from financial_agent.schema import ensure_app_schema


def _status_db(path, *, available=9000.0, obligations=()):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL, recorded_at TEXT, source TEXT);
        CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, accounts_seen INT, transactions_inserted INT, transactions_updated INT, error TEXT);
        CREATE TABLE todoist_sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, project_id TEXT, sections_seen INT, tasks_seen INT, cashflow_tasks_seen INT, inserted INT, updated INT, missing_marked_deleted INT, error TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT, amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);
        """
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('chk','PREMIER PLUS CKG (XXXX)','Chase','checking','USD')")
    conn.execute("INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) VALUES ('chk',?,?,'2026-06-20T00:00:00+00:00','simplefin')", (available, available))
    conn.execute("INSERT INTO sync_runs (started_at,finished_at,mode,accounts_seen,transactions_inserted,transactions_updated,error) VALUES ('2026-06-20T09:58:00+00:00','2026-06-20T10:00:00+00:00','i',1,0,0,NULL)")
    conn.execute("INSERT INTO todoist_sync_runs (started_at,finished_at,project_id,sections_seen,tasks_seen,cashflow_tasks_seen,inserted,updated,missing_marked_deleted,error) VALUES ('2026-06-19T03:00:00+00:00','2026-06-19T03:05:00+00:00','p',1,1,1,0,0,0,NULL)")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    for oid, name, kind, instances in obligations:
        apply_obligation_instances(conn, obligation={"id": oid, "name": name, "kind": kind, "status": "active", "source": "seed"}, instances=instances)
    conn.commit()
    conn.close()
    return str(path)


def _legacy_md(path, rows, working_line="| Joint Checking (XXXX) | $5,000.00 present / $5,200.00 avail | primary |"):
    body = ["# Finance", "## Obligations Due (window)", "| Due | Obligation | Amount | Auto | Notes |", "|--|--|--|--|--|"]
    body += rows
    body += ["## All Balances", working_line]
    path.write_text("\n".join(body) + "\n")
    return str(path)


_EVERSOURCE = ("eversource", "Eversource electric", "utility",
               [{"id": "eversource:2026-07-27", "due_date": "2026-07-27", "amount": -173.80, "source": "seed"}])


def test_parity_matched_when_identical(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", obligations=[_EVERSOURCE])
    md = _legacy_md(tmp_path / "cf.md", ["| Jul 27 | Eversource electric | $173.80 | AUTO | |"])
    report = compare_to_legacy(legacy_cashflow_md_path=md, db_path=db, as_of_date="2026-06-20")
    assert report["summary"]["matched"] == 1
    assert report["in_parity"] is True
    assert report["discrepancies"] == []


def test_parity_missing_in_new(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", obligations=[_EVERSOURCE])
    md = _legacy_md(tmp_path / "cf.md", [
        "| Jul 27 | Eversource electric | $173.80 | AUTO | |",
        "| Jun 22 | Garbage Santaguida | $48.00 | AUTO | |",  # not modeled in the new system
    ])
    report = compare_to_legacy(legacy_cashflow_md_path=md, db_path=db, as_of_date="2026-06-20")
    miss = [d for d in report["discrepancies"] if d["kind"] == "missing_in_new"]
    assert len(miss) == 1 and miss[0]["severity"] == "medium"  # advisory, not a proven system error
    assert "Garbage" in miss[0]["legacy"]["label"]
    assert report["in_parity"] is False


def test_parity_amount_changed_is_flagged(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", obligations=[
        ("eversource", "Eversource electric", "utility", [{"id": "eversource:2026-07-27", "due_date": "2026-07-27", "amount": -300.0, "source": "seed"}]),
    ])
    md = _legacy_md(tmp_path / "cf.md", ["| Jul 27 | Eversource electric | $173.80 | AUTO | |"])
    report = compare_to_legacy(legacy_cashflow_md_path=md, db_path=db, as_of_date="2026-06-20")
    chg = [d for d in report["discrepancies"] if d["kind"] == "amount_changed"]
    assert len(chg) == 1
    assert chg[0]["severity"] == "medium"  # |300 - 173.80| >= 50
    assert chg[0]["amount_delta"] == 126.2


def test_parity_extra_in_new(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", obligations=[
        _EVERSOURCE,
        ("nyt", "New York Times", "subscription", [{"id": "nyt:2026-07-23", "due_date": "2026-07-23", "amount": -30.30, "source": "seed"}]),
    ])
    md = _legacy_md(tmp_path / "cf.md", ["| Jul 27 | Eversource electric | $173.80 | AUTO | |"])  # no NYT
    report = compare_to_legacy(legacy_cashflow_md_path=md, db_path=db, as_of_date="2026-06-20")
    extra = [d for d in report["discrepancies"] if d["kind"] == "extra_in_new"]
    assert any("New York Times" in e["new"]["name"] for e in extra)


def test_parity_matches_renamed_obligation_by_amount_date(tmp_path):
    # "NYTimes" (legacy) vs "New York Times subscription" (new): no token overlap,
    # but same amount + date + direction -> one matched item, not missing + extra.
    db = _status_db(tmp_path / "d.sqlite", obligations=[
        ("nyt", "New York Times subscription", "subscription", [{"id": "nyt:2026-07-23", "due_date": "2026-07-23", "amount": -30.30, "source": "seed"}]),
    ])
    md = _legacy_md(tmp_path / "cf.md", ["| Jul 23 | NYTimes | $30.30 | AUTO | |"])
    report = compare_to_legacy(legacy_cashflow_md_path=md, db_path=db, as_of_date="2026-06-20")
    assert report["summary"]["matched"] == 1
    assert report["summary"]["missing_in_new"] == 0 and report["summary"]["extra_in_new"] == 0


def test_parity_working_cash_delta(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=[_EVERSOURCE])
    md = _legacy_md(tmp_path / "cf.md", ["| Jul 27 | Eversource electric | $173.80 | AUTO | |"])
    report = compare_to_legacy(legacy_cashflow_md_path=md, db_path=db, as_of_date="2026-06-20")
    wc = report["working_cash"]
    assert wc["legacy_working"] == 5200.0  # the 'avail' figure
    assert wc["new_working_cash"] == 9000.0
    assert wc["delta"] == 3800.0


def test_render_parity_markdown(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", obligations=[_EVERSOURCE])
    md = _legacy_md(tmp_path / "cf.md", [
        "| Jul 27 | Eversource electric | $173.80 | AUTO | |",
        "| Jun 22 | Garbage Santaguida | $48.00 | AUTO | |",
    ])
    out = render_parity_markdown(compare_to_legacy(legacy_cashflow_md_path=md, db_path=db, as_of_date="2026-06-20"))
    assert "# Parallel-Run Parity" in out
    assert "In parity: NO" in out
    assert "MISSING in new" in out and "Garbage" in out
