"""Pasted checking rows must never coexist with their bank-feed twins.

Regression for the 2026-08 double count: a pasted Chase CSV wrote rows the
SimpleFIN feed also delivered (353 pairs in the live DB), inflating spending
totals and confusing reconciliation. Three guards: the importer skips rows the
feed already has, the feed sync absorbs paste copies when it lands the real row,
and a one-time cleanup pairs and removes the historical twins while re-pointing
every reference at the feed id.
"""

import json
import sqlite3

from financial_agent.card_import import import_checking_activity_for_db
from financial_agent.config import ensure_source_tables
from financial_agent.paste_dedup import dedupe_pasted_transactions_for_db
from financial_agent.schema import ensure_app_schema
from financial_agent.sync_simplefin import _upsert_transaction

CHK = "ACT-chk"
CSV = """Date,Description,Amount
06/02/2026,AQUARION WATER,-32.46
06/03/2026,CITY OF SPRINGFIELD PAYROLL,1850.00
"""


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_source_tables(conn)
    ensure_app_schema(conn)
    conn.execute(
        "INSERT INTO accounts (id, name, org, kind, currency, first_seen_at, last_seen_at) "
        "VALUES (?, 'TOTAL CHECKING (1234)', 'Chase', 'checking', 'USD', 'x', 'x')",
        (CHK,),
    )
    return conn


def _insert(conn, txn_id, source, posted, amount, payee, description=None):
    conn.execute(
        "INSERT INTO transactions (id, account_id, posted, transacted_at, amount, payee, description, "
        "pending, source, first_seen_at, last_seen_at, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'x', 'x', 'x')",
        (txn_id, CHK, posted, posted, amount, payee, payee if description is None else description, source),
    )


def _ids(conn):
    return {r["id"]: r["source"] for r in conn.execute("SELECT id, source FROM transactions")}


def test_import_skips_rows_the_feed_already_delivered(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _insert(conn, "TRN-feed-aq", "simplefin", "2026-06-02T08:00:00", -32.46, "Aquarion Water Company")

    applied = import_checking_activity_for_db(
        conn, text=CSV, account_query="TOTAL CHECKING", as_of_date="2026-06-08", dry_run=False
    )

    assert applied["status"] == "ok"
    assert applied["new"] == 1 and applied["feed_duplicate"] == 1 and applied["duplicate"] == 0
    assert any("bank feed" in w for w in applied["warnings"])
    sources = _ids(conn)
    assert sources["TRN-feed-aq"] == "simplefin"
    assert list(sources.values()).count("checking_paste") == 1  # only the payroll row


def test_feed_row_arriving_after_a_paste_absorbs_the_paste_copy(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _insert(conn, "checking:paste-1", "checking_paste", "2026-06-02T00:00:00", -32.46, "AQUARION WATER")
    conn.execute(
        "INSERT INTO transaction_obligation_matches (obligation_instance_id, transaction_id, match_type, "
        "match_score, created_at, updated_at) VALUES ('aquarion:2026-06-02', 'checking:paste-1', 'auto', 1.0, 'x', 'x')"
    )

    feed_txn = {
        "id": "TRN-feed-aq",
        "posted": 1780401600,  # 2026-06-02 12:00:00 UTC
        "transacted_at": 1780401600,
        "amount": -32.46,
        "payee": "Aquarion Water Company",
        "description": "AQUARIONWATER UTILITYPMT",
        "pending": 0,
        "fetched_at": "2026-06-05T08:00:00",
    }
    assert _upsert_transaction(conn, CHK, feed_txn, "simplefin") == "inserted"

    assert _ids(conn) == {"TRN-feed-aq": "simplefin"}
    match = conn.execute("SELECT transaction_id FROM transaction_obligation_matches").fetchone()
    assert match["transaction_id"] == "TRN-feed-aq"


def test_cleanup_pairs_one_to_one_repoints_references_and_deletes_paste_rows(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    # Two feed rows and two paste copies on the same day/amount (a real pair each),
    # plus one paste row with no twin, plus one feed row with no twin.
    _insert(conn, "TRN-f1", "simplefin", "2026-06-02T08:00:00", -50.00, "Shell")
    _insert(conn, "TRN-f2", "simplefin", "2026-06-02T09:00:00", -50.00, "Shell")
    _insert(conn, "checking:p1", "checking_paste", "2026-06-02T00:00:00", -50.00, "SHELL OIL")
    _insert(conn, "checking:p2", "checking_paste", "2026-06-02T00:00:00", -50.00, "SHELL OIL")
    _insert(conn, "checking:p3", "checking_paste", "2026-06-04T00:00:00", -12.00, "COFFEE")
    _insert(conn, "TRN-f3", "simplefin", "2026-06-05T08:00:00", -99.00, "Grocer")
    conn.execute(
        "UPDATE obligation_instances SET matched_transaction_id = 'checking:p1' WHERE 0"
    )  # column exists; nothing to point yet
    conn.execute(
        "INSERT INTO drift_findings (id, finding_type, severity, related_transaction_ids_json, status, as_of_date, "
        "created_at, updated_at) VALUES ('d1', 'deposit_arrived', 'low', ?, 'active', '2026-06-05', 'x', 'x')",
        (json.dumps(["checking:p2", "other"]),),
    )

    preview = dedupe_pasted_transactions_for_db(conn, dry_run=True)
    assert preview["dry_run"] is True and preview["pairs"] == 2 and preview["deleted"] == 0
    assert preview["duplicate_amount_total"] == 100.0
    assert len(_ids(conn)) == 6  # nothing written

    applied = dedupe_pasted_transactions_for_db(conn, dry_run=False)
    assert applied["deleted"] == 2
    assert _ids(conn) == {
        "TRN-f1": "simplefin",
        "TRN-f2": "simplefin",
        "checking:p3": "checking_paste",
        "TRN-f3": "simplefin",
    }
    drift = json.loads(conn.execute("SELECT related_transaction_ids_json FROM drift_findings WHERE id = 'd1'").fetchone()[0])
    assert "checking:p2" not in drift and "other" in drift and len(drift) == 2

    # Idempotent: a second run finds nothing.
    assert dedupe_pasted_transactions_for_db(conn, dry_run=False)["pairs"] == 0


# --- cross-date twins: the paste lane dates a card purchase by its transaction
# date, the feed by its posting date (live 2026-07..09: five newspaper and trash-pickup rows
# 2-4 days apart survived the exact-date cleanup). ---------------------------


def _live_shaped_rows(conn):
    # Newspaper: two paste copies of one charge (two pastes formatted the payee
    # differently) against one feed row three days later.
    _insert(conn, "checking:news-a", "checking_paste", "2026-07-10T00:00:00", -30.30, "POS DEBIT DAILYPLANET 555-010-0199 NY")
    _insert(conn, "checking:news-b", "checking_paste", "2026-07-10T00:00:00", -30.30, "POS DEBIT                DAILYPLANET*  555-010-0199 NY  0000")
    _insert(conn, "TRN-news", "simplefin", "2026-07-13T08:00:00", -30.30, "Daily Planet", "DAILYPLANET* 555-010-0199 NY 07/10")
    # Trash pickup: paste 8/01, feed 8/03.
    _insert(conn, "checking:trash", "checking_paste", "2026-08-01T00:00:00", -48.00, "POS DEBIT ACME WASTE HAULING 5550100123 CT")
    _insert(conn, "TRN-trash", "simplefin", "2026-08-03T08:00:00", -48.00, "Acme Waste Hauling", "ACME WASTE HAULING 555-0100 CT 07/31")
    # Not twins: same amount a few days apart, but only boilerplate or a payment rail in common.
    _insert(conn, "checking:atm", "checking_paste", "2026-08-31T00:00:00", -200.00, "ATM WITHDRAWAL 000714 08/31")
    _insert(conn, "TRN-atm", "simplefin", "2026-09-02T08:00:00", -200.00, "ATM Withdrawal", "ATM WITHDRAWAL 000801 09/02")
    _insert(conn, "checking:venmo", "checking_paste", "2026-08-25T00:00:00", -20.00, "VENMO PAYMENT 1044")
    _insert(conn, "TRN-venmo", "simplefin", "2026-08-28T08:00:00", -20.00, "Transfer to Venmo", "VENMO PAYMENT 1992")


def test_cleanup_removes_cross_date_twins_and_second_paste_copies(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _live_shaped_rows(conn)
    conn.execute(
        "INSERT INTO transaction_obligation_matches (obligation_instance_id, transaction_id, match_type, "
        "match_score, created_at, updated_at) VALUES ('news:2026-07-10', 'checking:news-b', 'auto', 1.0, 'x', 'x')"
    )

    preview = dedupe_pasted_transactions_for_db(conn, dry_run=True)
    assert preview["pairs"] == 3
    assert preview["duplicate_amount_total"] == 108.60

    dedupe_pasted_transactions_for_db(conn, dry_run=False)
    remaining = _ids(conn)
    assert not {"checking:news-a", "checking:news-b", "checking:trash"} & set(remaining)
    assert remaining["checking:atm"] == "checking_paste"
    assert remaining["checking:venmo"] == "checking_paste"
    match = conn.execute("SELECT transaction_id FROM transaction_obligation_matches").fetchone()
    assert match["transaction_id"] == "TRN-news"
    assert dedupe_pasted_transactions_for_db(conn, dry_run=False)["pairs"] == 0


def test_import_skips_a_row_the_feed_delivered_under_a_later_posting_date(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _insert(conn, "TRN-news", "simplefin", "2026-07-13T08:00:00", -30.30, "Daily Planet", "DAILYPLANET* 555-010-0199 NY 07/10")
    _insert(conn, "TRN-atm", "simplefin", "2026-07-13T08:00:00", -200.00, "ATM Withdrawal", "ATM WITHDRAWAL 000801")
    csv = """Date,Description,Amount
07/10/2026,POS DEBIT DAILYPLANET 555-010-0199 NY,-30.30
07/10/2026,ATM WITHDRAWAL 000714,-200.00
"""

    applied = import_checking_activity_for_db(
        conn, text=csv, account_query="TOTAL CHECKING", as_of_date="2026-07-14", dry_run=False
    )

    assert applied["feed_duplicate"] == 1 and applied["new"] == 1
    pasted = [r["payee"] for r in conn.execute("SELECT payee FROM transactions WHERE source = 'checking_paste'")]
    assert len(pasted) == 1 and "ATM" in pasted[0]


def test_feed_row_landing_days_later_absorbs_the_paste_copies(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _insert(conn, "checking:news-a", "checking_paste", "2026-07-10T00:00:00", -30.30, "POS DEBIT DAILYPLANET 555-010-0199 NY")
    _insert(conn, "checking:news-b", "checking_paste", "2026-07-10T00:00:00", -30.30, "POS DEBIT                DAILYPLANET*  555-010-0199 NY")
    _insert(conn, "checking:atm", "checking_paste", "2026-07-11T00:00:00", -30.30, "ATM WITHDRAWAL 000714")

    feed_txn = {
        "id": "TRN-news",
        "posted": 1783929600,  # 2026-07-13 08:00:00 UTC
        "transacted_at": 1783929600,
        "amount": -30.30,
        "payee": "Daily Planet",
        "description": "DAILYPLANET* 555-010-0199 NY 07/10",
        "pending": 0,
        "fetched_at": "2026-07-13T08:00:00",
    }
    assert _upsert_transaction(conn, CHK, feed_txn, "simplefin") == "inserted"

    assert _ids(conn) == {"TRN-news": "simplefin", "checking:atm": "checking_paste"}
