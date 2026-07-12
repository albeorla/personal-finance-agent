"""Tests for card-spend paste-import (design #4).

The Apple Card has no live transaction feed, so monthly charges are pasted in.
This module turns a pasted CSV / statement into real ``transactions`` rows under
``source='apple_card_paste'``, deduped by a deterministic synthetic id,
fuzzy-matched to the right account. The default is dry-run (parse + preview, no
writes); an apply run also records a ``card_import_runs`` row and promotes the
statement total. The latest run drives the digest ``apple_card_stale`` signal.
"""

import datetime as dt
import hashlib
import json
import sqlite3

from financial_agent import card_import as ci
from financial_agent.card_import import (
    apple_card_paste_freshness,
    assign_synthetic_ids,
    detect_format,
    import_card_statement_for_db,
    import_checking_activity_for_db,
    parse_apple_csv,
    parse_apple_statement,
    parse_text,
    synthetic_txn_id,
)
from financial_agent.config import ensure_source_tables
from financial_agent.cashflow import build_cash_flow_projections
from financial_agent.digest import _apple_card_stale
from financial_agent.obligations import apply_obligation_instances
from financial_agent.reconciliation import (
    confirm_reconciliation_match,
    list_matched_obligation_instances,
    reconcile_obligation_instances,
)
from financial_agent.schema import ensure_app_schema
from financial_agent.status import _latest_balances
from financial_agent.verification import run_verification

# An Apple Card CSV export: purchases are positive; Payment / Daily Cash rows are
# inflows that must not count as cycle spend. Two Whole Foods rows on the same day
# at the same amount exercise the ordinal disambiguation.
APPLE_CSV = """Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)
06/05/2026,06/06/2026,APPLE.COM/BILL,Apple Services,Subscriptions,Purchase,9.99
06/10/2026,06/11/2026,WHOLEFDS GRN,Whole Foods Market,Grocery,Purchase,124.55
06/10/2026,06/11/2026,WHOLEFDS GRN,Whole Foods Market,Grocery,Purchase,124.55
06/15/2026,06/16/2026,DAILY CASH REWARD,Apple,Rewards,Daily Cash,3.21
06/18/2026,06/19/2026,ACME PAYMENT THANK YOU,ACME,Payment,Payment,500.00
"""

# The pasted-statement variant of the same cycle: dated money lines plus a
# closing date and a New Balance line the parser recovers on its own.
APPLE_STATEMENT = """Apple Card Statement
Statement closing date 06/21/2026
Transactions
06/05/2026  APPLE.COM/BILL  $9.99
06/10/2026  WHOLE FOODS MARKET  $124.55
06/18/2026  ACME PAYMENT THANK YOU  $-500.00
New Balance $1,234.56
"""

APPLE_ACCOUNT_ID = "ACT-applecard"
APPLE_STATEMENT_OBLIGATION_ID = "apple_card_statement_payment"


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_source_tables(conn)
    ensure_app_schema(conn)
    return conn


def _seed_accounts(conn):
    """An Apple Card and an unrelated checking account, so the fuzzy match has to
    pick the right one (and a Citi/Amex paste cannot silently land on Apple)."""
    conn.executemany(
        "INSERT INTO accounts (id, name, org, kind, currency, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, 'USD', 'x', 'x')",
        [
            (APPLE_ACCOUNT_ID, "Apple Card", "Goldman Sachs Bank", "credit_card"),
            ("ACT-chk", "PREMIER PLUS CKG (4321)", "Chase Bank", "checking"),
        ],
    )


def _seed_statement_instance(conn, close_date="2026-06-21", due_date="2026-07-16"):
    """An estimated Apple Card statement-payment instance for the cycle, eligible
    for promotion by a confirmed statement total."""
    apply_obligation_instances(
        conn,
        obligation={
            "id": APPLE_STATEMENT_OBLIGATION_ID,
            "name": "Apple Card statement payment",
            "kind": "credit_card_statement",
            "status": "active",
            "source": "seed",
        },
        instances=[
            {
                "id": f"{APPLE_STATEMENT_OBLIGATION_ID}:{due_date}",
                "due_date": due_date,
                "amount": -900.0,
                "source": "seed",
                "amount_status": "estimated",
                "amount_source": "manual_projection",
                "statement_close_date": close_date,
            }
        ],
    )


# --- format detection / parsing (pure) -------------------------------------


def test_detect_format_classifies_csv_statement_and_unknown():
    assert detect_format(APPLE_CSV) == "apple_csv"
    assert detect_format(APPLE_STATEMENT) == "apple_statement"
    assert detect_format("") == "unknown"
    assert detect_format("just some random pasted prose with no money") == "unknown"


def test_parse_apple_csv_signs_outflows_and_flags_inflows():
    parsed = parse_apple_csv(APPLE_CSV)
    txns = parsed["txns"]
    assert len(txns) == 5
    apple = txns[0]
    assert apple["transacted_date"] == "2026-06-05"
    assert apple["amount"] == -9.99 and apple["inflow"] is False  # purchase -> outflow
    daily_cash = next(t for t in txns if t["type"] == "daily cash")
    payment = next(t for t in txns if t["type"] == "payment")
    assert daily_cash["amount"] == 3.21 and daily_cash["inflow"] is True
    assert payment["amount"] == 500.00 and payment["inflow"] is True


def test_parse_apple_statement_recovers_total_and_close_date():
    parsed = parse_apple_statement(APPLE_STATEMENT)
    assert parsed["statement_total"] == 1234.56
    assert parsed["statement_close_date"] == "2026-06-21"
    spend = [t for t in parsed["txns"] if not t["inflow"]]
    inflow = [t for t in parsed["txns"] if t["inflow"]]
    assert {t["amount"] for t in spend} == {-9.99, -124.55}  # bare lines -> outflow
    assert inflow and inflow[0]["amount"] == 500.00  # negative source line -> inflow


def test_parse_text_dispatches_and_unknown_is_inert():
    assert parse_text(APPLE_CSV)["format"] == "apple_csv"
    assert parse_text(APPLE_STATEMENT)["format"] == "apple_statement"
    blank = parse_text("nonsense")
    assert blank["format"] == "unknown" and blank["txns"] == []


def test_synthetic_id_is_deterministic_and_ordinal_disambiguates():
    a = synthetic_txn_id(APPLE_ACCOUNT_ID, "2026-06-10", -124.55, "Whole Foods", 0)
    a_again = synthetic_txn_id(APPLE_ACCOUNT_ID, "2026-06-10", -124.55, "Whole Foods", 0)
    ordinal1 = synthetic_txn_id(APPLE_ACCOUNT_ID, "2026-06-10", -124.55, "Whole Foods", 1)
    assert a == a_again and a.startswith("applecard:")  # deterministic
    assert a != ordinal1  # genuine same-day/same-amount duplicate gets a distinct id


def test_assign_ids_distinguishes_genuine_same_day_duplicates():
    parsed = parse_apple_csv(APPLE_CSV)
    stamped = assign_synthetic_ids(APPLE_ACCOUNT_ID, parsed["txns"])
    ids = [t["id"] for t in stamped]
    assert len(ids) == len(set(ids))  # the two identical Whole Foods rows stay distinct
    # Re-stamping the same parse reproduces the same ids exactly (idempotent).
    again = assign_synthetic_ids(APPLE_ACCOUNT_ID, parse_apple_csv(APPLE_CSV)["txns"])
    assert [t["id"] for t in again] == ids


# --- account fuzzy-match ----------------------------------------------------


def test_apple_query_matches_the_apple_card_account_not_checking(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    result = import_card_statement_for_db(conn, text=APPLE_CSV, as_of_date="2026-06-25")
    assert result["account"]["account_id"] == APPLE_ACCOUNT_ID
    assert result["account"]["account_name"] == "Apple Card"


# --- dry-run default makes no writes ---------------------------------------


def test_dry_run_is_default_and_writes_nothing(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    result = import_card_statement_for_db(conn, text=APPLE_CSV, as_of_date="2026-06-25")
    assert result["dry_run"] is True and result["status"] == "preview"
    assert result["new"] == 5 and result["card_import_run_id"] is None
    # 3 purchases sum to a negative cycle spend; payment + daily cash excluded.
    assert result["cycle_spend_total"] == -259.09
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM card_import_runs").fetchone()[0] == 0


# --- apply run writes transactions + run row + rollup ----------------------


def test_apply_writes_transactions_and_a_card_import_run(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    result = import_card_statement_for_db(
        conn, text=APPLE_CSV, as_of_date="2026-06-25", dry_run=False
    )
    assert result["status"] == "ok" and result["new"] == 5
    assert result["card_import_run_id"] is not None
    txns = conn.execute(
        "SELECT source, payee, amount FROM transactions WHERE account_id = ? ORDER BY amount",
        (APPLE_ACCOUNT_ID,),
    ).fetchall()
    assert len(txns) == 5
    assert all(t["source"] == "apple_card_paste" for t in txns)
    assert all(t["payee"] for t in txns)  # non-empty payee so the onboarding scanner picks them up
    run = conn.execute(
        "SELECT account_id, txn_count, total_spend, source_format FROM card_import_runs"
    ).fetchone()
    assert run["account_id"] == APPLE_ACCOUNT_ID
    assert run["txn_count"] == 5 and run["source_format"] == "apple_csv"
    assert run["total_spend"] == -259.09  # signed cycle spend


def test_apply_statement_promotes_total_onto_the_statement_instance(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    _seed_statement_instance(conn, close_date="2026-06-21", due_date="2026-07-16")
    result = import_card_statement_for_db(
        conn, text=APPLE_STATEMENT, as_of_date="2026-06-25", dry_run=False
    )
    promo = result["promotion"]
    assert promo["action"] == "promote" and promo["applied"] is True
    inst = conn.execute(
        "SELECT amount, amount_status, amount_source FROM obligation_instances "
        "WHERE id = ?",
        (f"{APPLE_STATEMENT_OBLIGATION_ID}:2026-07-16",),
    ).fetchone()
    assert inst["amount"] == 1234.56  # observed total, not the -900 estimate
    assert inst["amount_status"] == "observed"
    assert inst["amount_source"] == "statement_amount"  # protected; rollup never clobbers it
    # A sticky manual balance snapshot is recorded for the liability (negative).
    snap = conn.execute(
        "SELECT balance, source FROM balance_snapshots WHERE account_id = ? AND source = 'manual'",
        (APPLE_ACCOUNT_ID,),
    ).fetchone()
    assert snap["balance"] == -1234.56


# --- deterministic dedup ----------------------------------------------------


def test_reimporting_the_same_paste_is_a_no_op(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    first = import_card_statement_for_db(
        conn, text=APPLE_CSV, as_of_date="2026-06-25", dry_run=False
    )
    assert first["new"] == 5 and first["duplicate"] == 0
    second = import_card_statement_for_db(
        conn, text=APPLE_CSV, as_of_date="2026-06-26", dry_run=False
    )
    assert second["new"] == 0 and second["duplicate"] == 5  # re-paste reproduces ids
    # No duplicate transaction rows; still 5 total.
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id = ?", (APPLE_ACCOUNT_ID,)
    ).fetchone()[0] == 5


def test_dry_run_preview_after_apply_reports_all_duplicate(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    import_card_statement_for_db(conn, text=APPLE_CSV, as_of_date="2026-06-25", dry_run=False)
    preview = import_card_statement_for_db(conn, text=APPLE_CSV, as_of_date="2026-06-26")
    assert preview["dry_run"] is True
    assert preview["new"] == 0 and preview["duplicate"] == 5


# --- checking activity paste (manual-sourced operating account) -------------


# A checking-activity CSV: a payroll deposit (inflow) and two same-amount coffee
# withdrawals on different days (distinct synthetic ids by date).
CHECKING_CSV = """Date,Description,Amount
06/05/2026,PAYROLL DEPOSIT,2500.00
06/06/2026,CORNER STORE,-42.10
06/07/2026,CORNER STORE,-42.10
"""


def test_confirmed_chase_import_flows_through_reconciliation_and_projection(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    due_date = "2099-07-11"
    instance_id = f"synthetic_orchard_rent:{due_date}"
    chase_csv = (
        "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
        "DEBIT,07/11/2099,SYNTHETIC ORCHARD RENT,-25.00,ACH_DEBIT,4975.00,\n"
    )
    conn.execute(
        "INSERT INTO balance_snapshots "
        "(account_id, balance, available, recorded_at, source, balance_date) "
        "VALUES ('ACT-chk', 5000.0, 5000.0, '2099-07-11T00:00:00+00:00', "
        "'simplefin', ?)",
        (due_date,),
    )
    apply_obligation_instances(
        conn,
        obligation={
            "id": "synthetic_orchard_rent",
            "name": "Synthetic Orchard Rent",
            "kind": "housing",
            "status": "active",
            "source": "seed",
        },
        instances=[
            {
                "id": instance_id,
                "due_date": due_date,
                "amount": -25.0,
                "source": "seed",
                "cash_flow_treatment": "direct_checking",
            }
        ],
    )

    preview = import_checking_activity_for_db(
        conn,
        text=chase_csv,
        account_query="PREMIER PLUS CKG",
        as_of_date=due_date,
    )
    applied = import_checking_activity_for_db(
        conn,
        text=chase_csv,
        account_query="PREMIER PLUS CKG",
        as_of_date=due_date,
        dry_run=False,
        confirmed_source_hash=preview["source_hash"],
    )
    assert applied["status"] == "ok" and applied["new"] == 1
    imported = conn.execute(
        "SELECT id, source, payee, amount FROM transactions "
        "WHERE account_id = 'ACT-chk' AND source = 'checking_paste'"
    ).fetchone()
    assert imported is not None
    assert imported["payee"] == "SYNTHETIC ORCHARD RENT"
    assert imported["amount"] == -25.0

    reconciliation = reconcile_obligation_instances(conn, as_of_date=due_date)
    assert reconciliation["matched_auto"] == 1
    matches = list_matched_obligation_instances(conn)
    assert len(matches) == 1
    assert matches[0]["obligation_instance_id"] == instance_id
    assert matches[0]["transaction_id"] == imported["id"]

    confirmed = confirm_reconciliation_match(conn, instance_id)
    assert confirmed["matched_transaction_id"] == imported["id"]
    projections, _ = build_cash_flow_projections(
        conn,
        accounts=_latest_balances(conn, as_of=dt.date.fromisoformat(due_date)),
        windows=[30],
        start_date=dt.date.fromisoformat(due_date),
        working_account_id="ACT-chk",
    )
    assert instance_id not in {event["instance_id"] for event in projections[0]["events"]}

    verification = run_verification(conn, as_of_date=due_date, persist=False)
    assert not [
        finding
        for finding in verification["findings"]
        if finding["check_id"] == "projection_identity"
    ]


def test_native_chase_activity_requires_matching_preview_and_is_idempotent(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    descriptions = (
        "FAKE ALPHA CREDIT",
        "FAKE BETA DEBIT",
        "FAKE INVALID DATE",
    )
    chase_csv = (
        "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
        f"CREDIT,07/10/2099,{descriptions[0]},125.00,ACH_CREDIT,900.00,\n"
        f"DEBIT,07/11/2099,{descriptions[1]},-25.00,DEBIT_CARD,875.00,\n"
        f"DEBIT,not-a-date,{descriptions[2]},-5.00,DEBIT_CARD,870.00,\n"
    )
    source_hash = hashlib.sha256(chase_csv.encode("utf-8")).hexdigest()

    preview = import_checking_activity_for_db(
        conn,
        text=chase_csv,
        account_query="PREMIER PLUS CKG",
        as_of_date="2099-07-12",
    )
    assert preview.get("format") == "chase_activity"
    assert preview["status"] == "preview" and preview["dry_run"] is True
    assert {
        key: preview[key]
        for key in (
            "total_rows",
            "parsed_rows",
            "new",
            "duplicate",
            "skipped_rows",
            "row_error_count",
        )
    } == {
        "total_rows": 3,
        "parsed_rows": 2,
        "new": 2,
        "duplicate": 0,
        "skipped_rows": 1,
        "row_error_count": 1,
    }
    assert preview["row_errors"] == [{"row_number": 4, "error_code": "invalid_date"}]
    assert preview["source_hash"] == source_hash
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM checking_import_runs").fetchone()[0] == 0

    serialized_preview = json.dumps(preview, sort_keys=True)
    assert "4321" not in serialized_preview
    assert chase_csv not in serialized_preview
    assert all(description not in serialized_preview for description in descriptions)

    unconfirmed = import_checking_activity_for_db(
        conn,
        text=chase_csv,
        account_query="PREMIER PLUS CKG",
        as_of_date="2099-07-12",
        dry_run=False,
    )
    assert unconfirmed["status"] not in {"ok", "preview"}
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM checking_import_runs").fetchone()[0] == 0

    wrong_hash = import_checking_activity_for_db(
        conn,
        text=chase_csv,
        account_query="PREMIER PLUS CKG",
        as_of_date="2099-07-12",
        dry_run=False,
        confirmed_source_hash="0" * 64,
    )
    assert wrong_hash["status"] not in {"ok", "preview"}
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM checking_import_runs").fetchone()[0] == 0

    applied = import_checking_activity_for_db(
        conn,
        text=chase_csv,
        account_query="PREMIER PLUS CKG",
        as_of_date="2099-07-12",
        dry_run=False,
        confirmed_source_hash=preview["source_hash"],
    )
    assert applied["status"] == "ok"
    assert applied["new"] == 2 and applied["duplicate"] == 0
    transaction_rows = conn.execute(
        "SELECT source FROM transactions WHERE account_id = 'ACT-chk'"
    ).fetchall()
    assert len(transaction_rows) == 2
    assert all(row["source"] == "checking_paste" for row in transaction_rows)
    receipts = conn.execute("SELECT * FROM checking_import_runs").fetchall()
    assert len(receipts) == 1
    receipt = dict(receipts[0])
    assert {
        key: receipt[key]
        for key in (
            "source_hash",
            "total_rows",
            "parsed_rows",
            "new_count",
            "duplicate_count",
            "skipped_rows",
            "row_error_count",
        )
    } == {
        "source_hash": source_hash,
        "total_rows": 3,
        "parsed_rows": 2,
        "new_count": 2,
        "duplicate_count": 0,
        "skipped_rows": 1,
        "row_error_count": 1,
    }

    retry = import_checking_activity_for_db(
        conn,
        text=chase_csv,
        account_query="PREMIER PLUS CKG",
        as_of_date="2099-07-13",
        dry_run=False,
        confirmed_source_hash=preview["source_hash"],
    )
    assert retry["new"] == 0 and retry["duplicate"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id = 'ACT-chk'"
    ).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM checking_import_runs").fetchone()[0] == 1


def test_checking_import_writes_txns_and_balance_and_is_idempotent(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    # Dry run (default) parses but writes nothing.
    preview = import_checking_activity_for_db(
        conn, text=CHECKING_CSV, account_query="PREMIER PLUS CKG", as_of_date="2026-06-08"
    )
    assert preview["dry_run"] is True and preview["new"] == 3
    assert preview["account"]["account_id"] == "ACT-chk"  # not the Apple Card
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    # Apply writes 3 checking txns under the checking source + a manual balance.
    applied = import_checking_activity_for_db(
        conn,
        text=CHECKING_CSV,
        account_query="PREMIER PLUS CKG",
        as_of_date="2026-06-08",
        balance=3200.00,
        dry_run=False,
    )
    assert applied["status"] == "ok" and applied["new"] == 3
    rows = conn.execute(
        "SELECT source, amount FROM transactions WHERE account_id = 'ACT-chk' ORDER BY amount"
    ).fetchall()
    assert len(rows) == 3 and all(r["source"] == "checking_paste" for r in rows)
    snap = conn.execute(
        "SELECT balance FROM balance_snapshots WHERE account_id = 'ACT-chk' AND source = 'manual'"
    ).fetchone()
    assert snap["balance"] == 3200.00

    # Re-paste reproduces the same synthetic ids -> nothing new, no duplicate rows.
    again = import_checking_activity_for_db(
        conn,
        text=CHECKING_CSV,
        account_query="PREMIER PLUS CKG",
        as_of_date="2026-06-09",
        dry_run=False,
    )
    assert again["new"] == 0 and again["duplicate"] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id = 'ACT-chk'"
    ).fetchone()[0] == 3


# A bank export with separate Debit/Credit columns (no signed Amount column).
# A Debit is money out (rent, an outflow), a Credit is money in (a deposit).
DEBIT_CREDIT_CSV = """Date,Description,Debit,Credit
06/01/2026,RENT,3000.00,
06/02/2026,PAYCHECK,,2500.00
"""


def test_debit_credit_columns_sign_outflows_negative_and_inflows_positive():
    # Regression: parse_generic_csv used to lump a "Debit" column into the
    # signed-amount candidates, so Debit=3000 (an outflow) imported as +3000
    # inflow - a $6,000 sign error - and the Credit row was dropped entirely.
    parsed = ci.parse_generic_csv(DEBIT_CREDIT_CSV)
    by_merchant = {t["merchant"]: t for t in parsed["txns"]}
    assert len(parsed["txns"]) == 2  # neither row is silently skipped
    assert by_merchant["RENT"]["amount"] == -3000.00
    assert by_merchant["RENT"]["inflow"] is False
    assert by_merchant["PAYCHECK"]["amount"] == 2500.00
    assert by_merchant["PAYCHECK"]["inflow"] is True


def test_debit_credit_ambiguous_row_is_skipped_not_silently_netted():
    # A row with BOTH Debit and Credit non-zero is contradictory. It must be
    # skipped (and counted), never silently netted to credit - debit, which
    # would invent a sign. A 0.00 in the unused column is NOT ambiguous.
    csv_text = (
        "Date,Description,Debit,Credit\n"
        "06/01/2026,AMBIGUOUS,20.00,50.00\n"   # both populated -> skip loudly
        "06/02/2026,CLEAN OUTFLOW,42.00,0.00\n"  # zero credit -> still a clean -42
    )
    parsed = ci.parse_generic_csv(csv_text)
    by_merchant = {t["merchant"]: t for t in parsed["txns"]}
    assert "AMBIGUOUS" not in by_merchant  # not netted into a +30 phantom
    assert parsed["skipped"] == 1
    assert by_merchant["CLEAN OUTFLOW"]["amount"] == -42.00


def test_signed_amount_column_still_wins_and_is_unchanged():
    # The real Chase export uses a single signed Amount column; must not regress.
    parsed = ci.parse_generic_csv(CHECKING_CSV)
    by_merchant = {t["merchant"]: t for t in parsed["txns"]}
    assert by_merchant["PAYROLL DEPOSIT"]["amount"] == 2500.00
    assert by_merchant["PAYROLL DEPOSIT"]["inflow"] is True
    assert by_merchant["CORNER STORE"]["amount"] == -42.10
    assert by_merchant["CORNER STORE"]["inflow"] is False


def test_checking_import_end_to_end_with_debit_credit_columns(tmp_path):
    # End-to-end through the checking importer (which calls parse_generic_csv
    # directly): the outflow lands as a negative transaction, not a positive one.
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    applied = import_checking_activity_for_db(
        conn,
        text=DEBIT_CREDIT_CSV,
        account_query="PREMIER PLUS CKG",
        as_of_date="2026-06-03",
        dry_run=False,
    )
    assert applied["status"] == "ok" and applied["new"] == 2
    rows = dict(
        conn.execute(
            "SELECT payee, amount FROM transactions WHERE account_id = 'ACT-chk'"
        ).fetchall()
    )
    assert rows["RENT"] == -3000.00
    assert rows["PAYCHECK"] == 2500.00


# --- digest board-health: apple_card_stale flips ---------------------------


def test_freshness_unknown_before_any_paste(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    fresh = apple_card_paste_freshness(conn)
    assert fresh["status"] == "unknown"
    assert _apple_card_stale(conn) is False  # nothing imported -> not "stale"


def test_freshness_fresh_after_a_paste_this_cycle(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    # Paste covering a statement that closed two days ago -> well within a cycle.
    close = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    text = (
        "Apple Card Statement\n"
        f"Statement closing date {dt.date.fromisoformat(close).strftime('%m/%d/%Y')}\n"
        "06/05/2026  APPLE.COM/BILL  $9.99\n"
        "New Balance $9.99\n"
    )
    import_card_statement_for_db(conn, text=text, as_of_date=dt.date.today().isoformat(), dry_run=False)
    assert apple_card_paste_freshness(conn)["status"] == "fresh"
    assert _apple_card_stale(conn) is False


def test_freshness_stale_when_last_covered_cycle_is_old(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    # A paste whose covered close is well past one cycle (CYCLE_STALE_DAYS=35).
    old_close = (dt.date.today() - dt.timedelta(days=ci.CYCLE_STALE_DAYS + 10)).isoformat()
    conn.execute(
        "INSERT INTO card_import_runs (id, account_id, imported_at, statement_close_date, "
        "txn_count, total_spend, source_format, error) VALUES (?, ?, ?, ?, 1, -9.99, 'apple_csv', NULL)",
        ("run-old", APPLE_ACCOUNT_ID, dt.datetime.now().isoformat(timespec="seconds"), old_close),
    )
    fresh = apple_card_paste_freshness(conn)
    assert fresh["status"] == "stale"
    assert fresh["age_days"] > ci.CYCLE_STALE_DAYS
    assert _apple_card_stale(conn) is True


def test_board_health_apple_card_stale_uses_the_paste_signal(tmp_path):
    conn = _db(tmp_path / "s.sqlite")
    _seed_accounts(conn)
    # Before any paste: not stale. After an old paste: stale. The digest proxy
    # tracks the paste cycle, not a balance-snapshot age.
    assert _apple_card_stale(conn) is False
    old_close = (dt.date.today() - dt.timedelta(days=ci.CYCLE_STALE_DAYS + 5)).isoformat()
    conn.execute(
        "INSERT INTO card_import_runs (id, account_id, imported_at, statement_close_date, "
        "txn_count, total_spend, source_format, error) VALUES (?, ?, ?, ?, 1, -1.0, 'apple_csv', NULL)",
        ("run-x", APPLE_ACCOUNT_ID, dt.datetime.now().isoformat(timespec="seconds"), old_close),
    )
    assert _apple_card_stale(conn) is True
