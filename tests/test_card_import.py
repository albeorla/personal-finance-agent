"""Tests for card-spend paste-import (design #4).

The Apple Card has no live transaction feed, so monthly charges are pasted in.
This module turns a pasted CSV / statement into real ``transactions`` rows under
``source='apple_card_paste'``, deduped by a deterministic synthetic id,
fuzzy-matched to the right account. The default is dry-run (parse + preview, no
writes); an apply run also records a ``card_import_runs`` row and promotes the
statement total. The latest run drives the digest ``apple_card_stale`` signal.
"""

import datetime as dt
import sqlite3

from financial_agent import card_import as ci
from financial_agent.card_import import (
    apple_card_paste_freshness,
    assign_synthetic_ids,
    detect_format,
    import_card_statement_for_db,
    parse_apple_csv,
    parse_apple_statement,
    parse_text,
    synthetic_txn_id,
)
from financial_agent.config import ensure_source_tables
from financial_agent.digest import _apple_card_stale
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema

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
