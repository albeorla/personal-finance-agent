"""Tests for drift detection (slice D)."""

import sqlite3
from datetime import UTC, datetime

from financial_agent.drift import detect_drift, list_drift_findings
from financial_agent.obligations import apply_obligation_instances
from financial_agent.onboarding import scan_charge_onboarding_candidates
from financial_agent.schema import ensure_app_schema
from financial_agent.status import get_finance_status


def _db(path, transactions=()):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,
            amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);
        """
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('ACT-chk','PREMIER PLUS CKG (4321)','Chase Bank','','USD')")
    conn.executemany(
        "INSERT INTO transactions (id,account_id,posted,amount,payee,description,pending,source) VALUES (?,?,?,?,?,?,0,'simplefin')",
        transactions,
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    conn.commit()
    return conn


def _ob(conn, oid, name, kind, instances):
    apply_obligation_instances(
        conn,
        obligation={"id": oid, "name": name, "kind": kind, "status": "active", "source": "seed"},
        instances=instances,
    )


def _types(result):
    return {f["finding_type"] for f in result["findings"]}


def test_missing_expected_severity_scales_with_age(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    _ob(conn, "rent", "Rent check", "housing",
        [{"id": "rent:2026-06-01", "due_date": "2026-06-01", "amount": -3000.0, "source": "seed"}])

    recent = detect_drift(conn, as_of_date="2026-06-20", persist=False)  # 19 days old
    miss = [f for f in recent["findings"] if f["finding_type"] == "missing_expected"]
    assert len(miss) == 1
    assert miss[0]["severity"] == "high"

    old = detect_drift(conn, as_of_date="2026-07-15", persist=False)  # 44 days old
    miss = [f for f in old["findings"] if f["finding_type"] == "missing_expected"]
    assert miss[0]["severity"] == "critical"


def test_within_grace_is_not_missing(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    _ob(conn, "rent", "Rent check", "housing",
        [{"id": "rent:2026-06-18", "due_date": "2026-06-18", "amount": -3000.0, "source": "seed"}])
    result = detect_drift(conn, as_of_date="2026-06-20", persist=False)  # 2 days, within grace 7
    assert "missing_expected" not in _types(result)


def test_matched_obligation_is_not_drift(tmp_path):
    conn = _db(
        tmp_path / "d.sqlite",
        transactions=[("t-ever", "ACT-chk", "2026-06-26T08:00:00", -115.87, "Eversource Energy", "EVERSOURCE")],
    )
    _ob(conn, "eversource", "Eversource electric estimates", "utility",
        [{"id": "eversource:2026-06-25", "due_date": "2026-06-25", "amount": -115.87, "source": "seed"}])
    result = detect_drift(conn, as_of_date="2026-07-10", persist=False)
    assert _types(result) == set()  # clean auto-match -> no finding


def test_amount_changed_when_charge_present_but_different(tmp_path):
    conn = _db(
        tmp_path / "d.sqlite",
        transactions=[("t-ever", "ACT-chk", "2026-06-26T08:00:00", -150.00, "Eversource Energy", "EVERSOURCE")],
    )
    _ob(conn, "eversource", "Eversource electric estimates", "utility",
        [{"id": "eversource:2026-06-25", "due_date": "2026-06-25", "amount": -115.87, "source": "seed"}])
    result = detect_drift(conn, as_of_date="2026-07-10", persist=False)
    changed = [f for f in result["findings"] if f["finding_type"] == "amount_changed"]
    assert len(changed) == 1
    assert "missing_expected" not in _types(result)  # mutually exclusive
    assert changed[0]["evidence"]["observed_amount"] == 150.00
    assert changed[0]["evidence"]["expected_amount"] == 115.87


def test_cash_flow_impact_signs_follow_convention(tmp_path):
    # A missing outflow lowers the (would-be) balance, so its impact is negative.
    conn = _db(tmp_path / "d.sqlite")
    _ob(conn, "rent", "Rent check", "housing",
        [{"id": "rent:2026-06-01", "due_date": "2026-06-01", "amount": -3000.0, "source": "seed"}])
    miss = [f for f in detect_drift(conn, as_of_date="2026-06-20", persist=False)["findings"]
            if f["finding_type"] == "missing_expected"][0]
    assert miss["cash_flow_impact"] == -3000.0

    # An outflow that came in higher than expected is extra spend -> negative impact.
    conn2 = _db(
        tmp_path / "d2.sqlite",
        transactions=[("t", "ACT-chk", "2026-06-26T08:00:00", -150.00, "Eversource Energy", "EVERSOURCE")],
    )
    _ob(conn2, "eversource", "Eversource electric estimates", "utility",
        [{"id": "eversource:2026-06-25", "due_date": "2026-06-25", "amount": -115.87, "source": "seed"}])
    changed = [f for f in detect_drift(conn2, as_of_date="2026-07-10", persist=False)["findings"]
               if f["finding_type"] == "amount_changed"][0]
    assert changed["cash_flow_impact"] < 0


def test_stale_estimate_flagged_after_review_date(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    _ob(conn, "amex_statement_payment", "Amex statement payment", "credit_card_statement",
        [{"id": "amex_statement_payment:2026-07-16", "due_date": "2026-07-16", "amount": -5400.0, "source": "seed",
          "amount_status": "estimated", "amount_source": "portal_current_balance_estimate",
          "statement_close_date": "2026-06-21", "review_after": "2026-06-22"}])

    assert "stale_estimate" not in _types(detect_drift(conn, as_of_date="2026-06-21", persist=False))
    result = detect_drift(conn, as_of_date="2026-06-22", persist=False)
    stale = [f for f in result["findings"] if f["finding_type"] == "stale_estimate"]
    assert len(stale) == 1
    assert stale[0]["severity"] == "high"


def test_unexpected_recurring_from_unapplied_candidate(tmp_path):
    conn = _db(
        tmp_path / "d.sqlite",
        transactions=[
            ("n1", "ACT-chk", "2026-04-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
            ("n2", "ACT-chk", "2026-05-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
            ("n3", "ACT-chk", "2026-06-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
        ],
    )
    scan_charge_onboarding_candidates(conn)  # creates an un-applied NYT candidate
    result = detect_drift(conn, as_of_date="2026-06-30", persist=False)
    recurring = [f for f in result["findings"] if f["finding_type"] == "unexpected_recurring"]
    assert len(recurring) == 1
    assert recurring[0]["evidence"]["merchant"] == "New York Times"
    assert recurring[0]["severity"] == "low"


def test_unexpected_recurring_surfaces_when_linked_obligation_is_dead(tmp_path):
    # A recurring charge mis-imported as a Todoist one-off, then canceled, leaves an
    # active obligation with NO projectable instance while the charge keeps posting.
    # It must still surface as unmodeled (else it is invisible everywhere yet drains cash).
    conn = _db(
        tmp_path / "d.sqlite",
        transactions=[
            ("v1", "ACT-chk", "2026-04-07T08:00:00", -580.84, "Volvo Car Fin Auto Finan Web", "VOLVO"),
            ("v2", "ACT-chk", "2026-05-07T08:00:00", -580.84, "Volvo Car Fin Auto Finan Web", "VOLVO"),
            ("v3", "ACT-chk", "2026-06-08T08:00:00", -580.84, "Volvo Car Fin Auto Finan Web", "VOLVO"),
        ],
    )
    scan_charge_onboarding_candidates(conn)
    _ob(conn, "volvo_dead", "Volvo lease", "loan_autopay",
        [{"id": "volvo_dead:2026-05-09", "due_date": "2026-05-09", "amount": -580.84, "source": "todoist"}])
    conn.execute("UPDATE obligation_instances SET status='canceled' WHERE obligation_id='volvo_dead'")
    conn.execute("UPDATE charge_onboarding_candidates SET existing_obligation_id='volvo_dead' WHERE merchant_key LIKE '%volvo%'")
    conn.commit()

    result = detect_drift(conn, as_of_date="2026-06-30", persist=False)
    recurring = [f for f in result["findings"] if f["finding_type"] == "unexpected_recurring"]
    assert any("Volvo" in (f["evidence"].get("merchant") or "") for f in recurring)


def test_detect_drift_persists_and_resolves(tmp_path):
    conn = _db(tmp_path / "d.sqlite")
    _ob(conn, "rent", "Rent check", "housing",
        [{"id": "rent:2026-06-01", "due_date": "2026-06-01", "amount": -3000.0, "source": "seed"}])

    detect_drift(conn, as_of_date="2026-06-20", persist=True)
    active = list_drift_findings(conn, status="active")
    assert len(active) == 1

    # Resolve the obligation (mark paid) and re-detect: the finding flips to resolved.
    conn.execute("UPDATE obligation_instances SET status = 'paid' WHERE id = 'rent:2026-06-01'")
    detect_drift(conn, as_of_date="2026-06-20", persist=True)
    assert list_drift_findings(conn, status="active") == []
    assert len(list_drift_findings(conn, status="resolved")) == 1


def test_status_surfaces_drift_and_recurring(tmp_path):
    conn = _db(
        tmp_path / "d.sqlite",
        transactions=[
            ("n1", "ACT-chk", "2026-04-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
            ("n2", "ACT-chk", "2026-05-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
            ("n3", "ACT-chk", "2026-06-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
        ],
    )
    conn.execute(
        "CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL, recorded_at TEXT, source TEXT)"
    )
    conn.execute("INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) VALUES ('ACT-chk',5000,5000,'2026-06-20T00:00:00+00:00','simplefin')")
    conn.execute("CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, accounts_seen INT, transactions_inserted INT, transactions_updated INT, error TEXT)")
    _ob(conn, "rent", "Rent check", "housing",
        [{"id": "rent:2026-06-01", "due_date": "2026-06-01", "amount": -3000.0, "source": "seed"}])
    scan_charge_onboarding_candidates(conn)
    conn.commit()

    result = get_finance_status(
        db_path=str(tmp_path / "d.sqlite"),
        windows=[30],
        start_date="2026-06-20",
        now=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
    )
    assert any(f["finding_type"] == "missing_expected" for f in result["drift_warnings"])
    assert any(f["finding_type"] == "unexpected_recurring" for f in result["recurring_candidates"])


def test_recurring_monthly_impact_divides_out_type_weight():
    from financial_agent.drift import _recurring_monthly_impact
    # priority_score = amount*monthly_rate*type_weight; monthly $ = score/weight
    row = {"candidate_type": "card_statement_input", "priority_score": 626.79}  # weight 0.9
    assert _recurring_monthly_impact(row) == 696.43
    row2 = {"candidate_type": "direct_checking_outflow", "priority_score": 100.0}  # weight 1.0
    assert _recurring_monthly_impact(row2) == 100.0
