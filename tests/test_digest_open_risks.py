"""The close must name what is still open (IMP-20260811-2).

Replay: the close for the day carrying a $7,250 Amex payment due 8-16 and an
unresolved transfer reminder dated 8-28. Both were real, both were invisible in
the close, so the reader had to ask "did you factor in X" twice. These tests pin
the two halves of the fix: every open item is listed with its date and amount,
and the verdict cannot read all-clear while one is open.
"""

import sqlite3

from financial_agent.digest import build_daily_digest, render_digest_markdown, summarize_daily_digest
from financial_agent.follow_ups import capture_followup
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema


def _db(path, *, available=20000.0, obligations=(), cadences=(), followups=()):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL, recorded_at TEXT, source TEXT, balance_date TEXT);
        CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, accounts_seen INT, transactions_inserted INT, transactions_updated INT, error TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT, amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);
        """
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('chk','PREMIER PLUS CKG (4321)','Chase','checking','USD')")
    conn.execute(
        "INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source,balance_date) "
        "VALUES ('chk',?,?,'2026-08-14T00:00:00+00:00','simplefin','2026-08-14')",
        (available, available),
    )
    conn.execute(
        "INSERT INTO sync_runs (started_at,finished_at,mode,accounts_seen,transactions_inserted,transactions_updated,error) "
        "VALUES ('2026-08-14T09:58:00+00:00','2026-08-14T10:00:00+00:00','i',1,0,0,NULL)"
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    for oid, name, kind, instances in obligations:
        apply_obligation_instances(
            conn,
            obligation={"id": oid, "name": name, "kind": kind, "status": "active", "source": "seed"},
            instances=instances,
        )
    for oid, cadence in cadences:
        conn.execute("UPDATE obligations SET cadence = ? WHERE id = ?", (cadence, oid))
    for text, surface_when in followups:
        capture_followup(conn, text, surface_when)
    conn.commit()
    conn.close()
    return str(path)


def _amex_and_transfer_db(path):
    return _db(
        path,
        obligations=[
            (
                "amex",
                "Amex statement payment",
                "credit_card_statement",
                [{"id": "amex:2026-08-16", "due_date": "2026-08-16", "amount": -7250.0, "source": "seed"}],
            )
        ],
        followups=[("Confirm the $4,000 transfer to savings", "2026-08-28")],
    )


def test_close_names_the_large_payment_and_the_unresolved_transfer(tmp_path):
    digest = build_daily_digest(_amex_and_transfer_db(tmp_path / "d.sqlite"), as_of_date="2026-08-14")

    by_label = {r["label"]: r for r in digest["open_risks"]}
    amex = by_label["Amex statement payment"]
    assert (amex["date"], amex["amount"]) == ("2026-08-16", 7250.0)
    assert amex["counted_in_runway"] is True

    transfer = by_label["Confirm the $4,000 transfer to savings"]
    # Dated two weeks out, so the due-today follow-up query never surfaced it.
    assert (transfer["date"], transfer["amount"]) == ("2026-08-28", 4000.0)
    assert transfer["counted_in_runway"] is False
    assert transfer["blocks_all_clear"] is True

    # Both are readable in the SHORT close, with their dates and amounts.
    md = render_digest_markdown(digest)
    assert "## Still open (2)" in md
    assert "2026-08-16  $7,250.00  Amex statement payment" in md
    assert "2026-08-28  $4,000.00  Confirm the $4,000 transfer to savings" in md

    # And in the compact summary a session reads instead of the full digest.
    summary = summarize_daily_digest(digest)
    assert summary["queue_counts"]["open_risks"] == 2
    assert {r["label"] for r in summary["open_risks"]} == set(by_label)


def test_verdict_cannot_read_all_clear_while_something_is_open(tmp_path):
    # $20,000 of cash against a $7,250 payment: the runway clears the floor
    # comfortably, which is exactly when the close used to read GREEN.
    digest = build_daily_digest(_amex_and_transfer_db(tmp_path / "d.sqlite"), as_of_date="2026-08-14")

    assert digest["cash_flow"][-1]["lowest_balance"] > 2500.0
    assert digest["status_color"] != "GREEN"
    assert "2 items still open" in digest["status_reason"]
    assert "Amex statement payment $7,250.00 on 2026-08-16" in digest["status_reason"]
    assert digest["status_color"] in render_digest_markdown(digest)


def test_confirmed_recurring_bill_is_listed_but_keeps_the_all_clear(tmp_path):
    db = _db(
        tmp_path / "d.sqlite",
        obligations=[
            (
                "rent",
                "Rent check",
                "housing",
                [{"id": "rent:2026-09-01", "due_date": "2026-09-01", "amount": -3000.0, "source": "seed"}],
            )
        ],
        cadences=[("rent", "monthly")],
    )
    digest = build_daily_digest(db, as_of_date="2026-08-14")

    rent = next(r for r in digest["open_risks"] if r["label"] == "Rent check")
    assert (rent["date"], rent["amount"]) == ("2026-09-01", 3000.0)
    # Known schedule, confirmed amount, already in the projection: named, not alarming.
    assert rent["blocks_all_clear"] is False
    assert digest["status_color"] == "GREEN"
    assert "[counted] 2026-09-01" in render_digest_markdown(digest)


def test_estimated_and_one_time_payments_hold_the_verdict(tmp_path):
    db = _db(
        tmp_path / "d.sqlite",
        obligations=[
            (
                "tax",
                "Estimated tax payment",
                "tax",
                [{"id": "tax:2026-09-15", "due_date": "2026-09-15", "amount": -5000.0, "source": "seed"}],
            )
        ],
    )
    digest = build_daily_digest(db, as_of_date="2026-08-14")

    tax = next(r for r in digest["open_risks"] if r["label"] == "Estimated tax payment")
    assert tax["blocks_all_clear"] is True
    assert "one-time payment" in tax["why"]
    assert digest["status_color"] == "YELLOW"


def test_quiet_day_still_reports_all_clear(tmp_path):
    digest = build_daily_digest(_db(tmp_path / "d.sqlite"), as_of_date="2026-08-14")

    assert digest["open_risks"] == []
    assert digest["status_color"] == "GREEN"
    assert "## Still open (0)" in render_digest_markdown(digest)


def test_small_payments_stay_out_of_the_block(tmp_path):
    db = _db(
        tmp_path / "d.sqlite",
        obligations=[
            (
                "wifi",
                "Internet",
                "utility",
                [{"id": "wifi:2026-08-20", "due_date": "2026-08-20", "amount": -80.0, "source": "seed"}],
            )
        ],
    )
    digest = build_daily_digest(db, as_of_date="2026-08-14")

    assert digest["open_risks"] == []
    assert digest["status_color"] == "GREEN"
