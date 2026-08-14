"""No cash verdict without stating how current and how complete the data is.

The replay: a runway answer was given while an account on the roster (a partner's
Chase account) had no balance data at all. The answer read clean and had to be
corrected by hand. A floor verdict now stays unverified in that case, and every
cash answer states the as-of date of the balances it used plus the accounts it
could not see.
"""

import sqlite3
from datetime import UTC, datetime

from financial_agent.config import ensure_source_tables
from financial_agent.guardrails import evaluate_guardrails
from financial_agent.schema import ensure_app_schema
from financial_agent.status import (
    balance_coverage,
    describe_balance_coverage,
    get_finance_status,
)


AS_OF = "2026-07-11"
NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)


def _db(path, *, available=9000.0, with_unlinked_account=True):
    conn = sqlite3.connect(path)
    ensure_source_tables(conn)
    ensure_app_schema(conn)
    conn.execute(
        "INSERT INTO accounts (id, name, org, kind, currency, first_seen_at, last_seen_at) "
        "VALUES ('chk', 'Checking 4321', 'Chase', 'checking', 'USD', ?, ?)",
        (f"{AS_OF}T00:00:00+00:00", f"{AS_OF}T00:00:00+00:00"),
    )
    if with_unlinked_account:
        # On the roster, never returned a balance: invisible to a plain
        # balances query because that joins through balance_snapshots.
        conn.execute(
            "INSERT INTO accounts (id, name, org, kind, currency, first_seen_at, last_seen_at) "
            "VALUES ('chase-joint', 'Chase Joint 9911', 'Chase', 'checking', 'USD', ?, ?)",
            (f"{AS_OF}T00:00:00+00:00", f"{AS_OF}T00:00:00+00:00"),
        )
    conn.execute(
        "INSERT INTO balance_snapshots "
        "(account_id, balance, available, recorded_at, source, balance_date) "
        "VALUES ('chk', ?, ?, ?, 'simplefin', ?)",
        (available, available, f"{AS_OF}T10:00:00+00:00", AS_OF),
    )
    conn.execute(
        "INSERT INTO sync_runs (started_at, finished_at, mode, accounts_seen, "
        "transactions_inserted, transactions_updated, error) "
        "VALUES (?, ?, 'incremental', 1, 0, 0, NULL)",
        (f"{AS_OF}T09:58:00+00:00", f"{AS_OF}T10:00:00+00:00"),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def _cash_floor(result):
    return [f for f in result["findings"] if f["rule_type"] == "cash_floor"]


def test_account_with_no_data_withholds_the_all_clear(tmp_path):
    conn = _db(tmp_path / "finance.sqlite", available=9000.0)  # comfortably above the floor

    findings = _cash_floor(
        evaluate_guardrails(conn, as_of_date=AS_OF, drift_findings=[], now=NOW)
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding["id"] == "guardrail:cash_floor:unverified"
    assert finding["evidence"]["verdict"] == "unverified"
    assert finding["evidence"]["reason"] == "missing_account_data"
    assert [a["account_name"] for a in finding["evidence"]["coverage"]["accounts_missing_data"]] == [
        "Chase Joint 9911"
    ]
    assert "Chase Joint 9911" in finding["message"]
    assert f"as of {AS_OF}" in finding["message"]


def test_complete_fresh_data_still_gives_a_verdict(tmp_path):
    conn = _db(tmp_path / "finance.sqlite", available=9000.0, with_unlinked_account=False)

    assert _cash_floor(evaluate_guardrails(conn, as_of_date=AS_OF, drift_findings=[], now=NOW)) == []


def test_breach_message_states_as_of_date_and_gaps(tmp_path):
    conn = _db(tmp_path / "finance.sqlite", available=1000.0, with_unlinked_account=False)

    findings = _cash_floor(
        evaluate_guardrails(conn, as_of_date=AS_OF, drift_findings=[], now=NOW)
    )

    assert findings
    for finding in findings:
        assert f"as of {AS_OF}" in finding["message"]
        assert finding["evidence"]["coverage"]["complete"] is True


def test_status_states_as_of_and_missing_accounts(tmp_path):
    db_path = tmp_path / "finance.sqlite"
    _db(db_path).close()

    status = get_finance_status(db_path=db_path, start_date=AS_OF, now=NOW)
    coverage = status["balances"]["coverage"]

    assert coverage["balances_as_of"] == AS_OF
    assert coverage["complete"] is False
    assert [a["account_id"] for a in coverage["accounts_missing_data"]] == ["chase-joint"]
    assert any(
        f"as of {AS_OF}" in w and "Chase Joint 9911" in w for w in status["warnings"]
    )


def test_coverage_sentence_names_stale_accounts(tmp_path):
    conn = _db(tmp_path / "finance.sqlite", with_unlinked_account=False)
    conn.execute(
        "INSERT INTO accounts (id, name, org, kind, currency, first_seen_at, last_seen_at) "
        "VALUES ('card', 'Apple Card', 'Apple', 'credit', 'USD', ?, ?)",
        (f"{AS_OF}T00:00:00+00:00", f"{AS_OF}T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO balance_snapshots "
        "(account_id, balance, available, recorded_at, source, balance_date) "
        "VALUES ('card', -400, -400, ?, 'simplefin', '2026-07-01')",
        (f"{AS_OF}T10:00:00+00:00",),
    )
    conn.commit()

    coverage = balance_coverage(conn, as_of=datetime.fromisoformat(AS_OF).date())
    sentence = describe_balance_coverage(coverage)

    assert coverage["balances_as_of"] == "2026-07-01"  # oldest input sets how current the answer is
    assert coverage["complete"] is False
    assert "as of 2026-07-01" in sentence
    assert "Apple Card (2026-07-01)" in sentence


def test_app_only_db_without_accounts_table_reports_no_gaps(tmp_path):
    conn = sqlite3.connect(tmp_path / "app-only.sqlite")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)

    coverage = balance_coverage(conn, as_of=datetime.fromisoformat(AS_OF).date())

    assert coverage["accounts_missing_data"] == []
    assert coverage["accounts_represented"] == 0
