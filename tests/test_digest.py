"""Tests for the daily digest (slice P). Composition over grounded status; no network."""

import sqlite3

from financial_agent.digest import build_daily_digest, render_digest_markdown
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema


def _status_db(path, *, available, obligations=()):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL, recorded_at TEXT, source TEXT);
        CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, accounts_seen INT, transactions_inserted INT, transactions_updated INT, error TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT, amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);
        """
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('chk','PREMIER PLUS CKG (4321)','Chase','checking','USD')")
    conn.execute("INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) VALUES ('chk',?,?,'2026-06-20T00:00:00+00:00','simplefin')", (available, available))
    conn.execute("INSERT INTO sync_runs (started_at,finished_at,mode,accounts_seen,transactions_inserted,transactions_updated,error) VALUES ('2026-06-20T09:58:00+00:00','2026-06-20T10:00:00+00:00','i',1,0,0,NULL)")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    for oid, name, kind, instances in obligations:
        apply_obligation_instances(conn, obligation={"id": oid, "name": name, "kind": kind, "status": "active", "source": "seed"}, instances=instances)
    conn.commit()
    conn.close()
    return str(path)


def test_build_daily_digest_composes_grounded_status(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=[
        ("rent", "Rent check", "housing", [{"id": "rent:2026-07-03", "due_date": "2026-07-03", "amount": -3000.0, "source": "seed"}]),
    ])
    digest = build_daily_digest(db, as_of_date="2026-06-20")

    assert digest["balances"]["working_cash"] == 9000.0
    assert digest["balances"]["net_across_accounts"] == 9000.0
    assert digest["balances"]["liquid_available"] == 9000.0
    assert "4321" in digest["balances"]["working_account"]
    assert [c["window_days"] for c in digest["cash_flow"]] == [7, 14, 30, 60]
    # Rent appears as an upcoming obligation in the 60-day window.
    assert any(o["obligation_name"] == "Rent check" and o["amount"] == 3000.0 for o in digest["upcoming_obligations"])
    assert "balances" in digest["provenance"] and "cash_flow" in digest["provenance"]
    assert digest["status_color"] in {"GREEN", "YELLOW", "RED"}


def test_account_label_appends_org_when_name_uninformative():
    from financial_agent.digest import _account_label
    assert _account_label({"name": "Owner", "org": "Apple Card (Updated Monthly)"}) == "Owner [Apple Card (Updated Monthly)]"
    assert _account_label({"name": "American Express Gold Card (5000)", "org": "American Express"}) == "American Express Gold Card (5000)"


def test_liquid_available_excludes_card_negative_available(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=5000.0)  # checking balance=available=5000
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('card','Apple Card','Apple','','USD')")
    conn.execute("INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) VALUES ('card',-5000,-8000,'2026-06-20T00:00:00+00:00','simplefin')")
    conn.commit()
    conn.close()
    dg = build_daily_digest(db, as_of_date="2026-06-20")
    assert dg["balances"]["liquid_available"] == 5000.0  # the card's -8000 available is NOT summed in


def test_needs_review_match_shows_in_matches_to_confirm(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=[
        ("ap", "Apple paydown", "card_paydown", [{"id": "ap:2026-06-10", "due_date": "2026-06-10", "amount": -300.0, "source": "seed"}]),
    ])
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO transaction_obligation_matches (obligation_instance_id,transaction_id,match_type,match_score,created_at,updated_at) "
                 "VALUES ('ap:2026-06-10','TRN-x','needs_review',0.65,'x','x')")
    conn.commit()
    conn.close()
    dg = build_daily_digest(db, as_of_date="2026-06-20")
    # A needs_review match is queued for confirmation (and not shown as "cleared").
    assert any(m["obligation_instance_id"] == "ap:2026-06-10" for m in dg["matches_to_confirm"])
    assert not any(c.get("transaction_id") == "TRN-x" for c in dg["recently_cleared"])
    # the removed "Possibly Overdue / may still owe" section is gone (it was dangerous)
    assert "Possibly Overdue" not in render_digest_markdown(dg)


def test_estimated_amount_marked_in_render(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=[
        ("amex", "Amex statement", "credit_card_statement", [{"id": "amex:2026-07-16", "due_date": "2026-07-16", "amount": -5400.0, "source": "seed"}]),
    ])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE obligation_instances SET amount_status='estimated' WHERE id='amex:2026-07-16'")
    conn.commit()
    conn.close()
    assert "(est)" in render_digest_markdown(build_daily_digest(db, as_of_date="2026-06-20"))


def test_status_render_uses_cash_runway_label(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0)
    assert "Cash runway (modeled bills only): GREEN" in render_digest_markdown(build_daily_digest(db, as_of_date="2026-06-20"))


def test_recurring_summary_ranks_by_impact_and_reports_remainder():
    from financial_agent.digest import _recurring_summary

    cands = [{"evidence": {"merchant": f"m{i}", "estimated_monthly_impact": v}} for i, v in enumerate([5, 100, 50, 3, 80])]
    s = _recurring_summary(cands, max_recurring=2)
    assert s["recurring_total"] == 5
    assert [c["evidence"]["estimated_monthly_impact"] for c in s["recurring_candidates"]] == [100, 80]  # top by impact
    assert s["recurring_more_count"] == 3
    assert s["recurring_more_monthly"] == 58.0  # 5 + 50 + 3 hidden


def test_net_worth_includes_card_debt_not_just_available(tmp_path):
    # A card with a real -4000 balance but available 0 must drag net negative;
    # the digest must NOT report a positive "net" from summing `available`.
    db = _status_db(tmp_path / "d.sqlite", available=3000.0)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('card','Amex Gold (5000)','American Express','credit card','USD')")
    conn.execute("INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) VALUES ('card',-4000.0,0.0,'2026-06-20T00:00:00+00:00','simplefin')")
    conn.commit()
    conn.close()

    digest = build_daily_digest(db, as_of_date="2026-06-20")
    assert digest["balances"]["net_across_accounts"] == -1000.0   # 3000 cash - 4000 card debt
    assert digest["balances"]["liquid_available"] == 3000.0
    md = render_digest_markdown(digest)
    assert "Net across all accounts (incl. card debt): $-1,000.00" in md
    assert "Amex Gold (5000)" in md and "$-4,000.00" in md  # the card shows its real debt, not $0.00


def test_status_color_red_when_below_cash_floor(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=1000.0)  # below $2500 floor -> high cash_floor guardrail
    digest = build_daily_digest(db, as_of_date="2026-06-20")
    assert any(g["rule_type"] == "cash_floor" for g in digest["guardrails"])
    assert digest["status_color"] == "RED"
    # the color now carries a human reason naming the cause (a cash danger here)
    assert digest["status_reason"]
    assert "drift to reconcile" not in digest["status_reason"]  # not mislabeled as a chore


def test_status_reason_present_and_db_file_in_provenance(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0)
    digest = build_daily_digest(db, as_of_date="2026-06-20")
    assert digest["status_reason"]  # always populated alongside the color
    assert digest["provenance"]["db_file"] == str(db)  # numbers traceable to a file
    assert "enabled" in digest["adversarial_review"]  # gate state visible


def test_balance_freshness_stamp_renders(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0)
    digest = build_daily_digest(db, as_of_date="2026-06-20")
    # each account carries its snapshot recorded_at (structured)...
    assert all("recorded_at" in a for a in digest["balances"]["accounts"])
    # ...and the markdown stamps how old each balance is, so a stale feed is visible
    assert "as of" in render_digest_markdown(digest)


def test_status_color_green_when_healthy(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0)
    assert build_daily_digest(db, as_of_date="2026-06-20")["status_color"] == "GREEN"


def test_render_markdown_has_sections_and_numbers(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=[
        ("rent", "Rent check", "housing", [{"id": "rent:2026-07-03", "due_date": "2026-07-03", "amount": -3000.0, "source": "seed"}]),
    ])
    md = render_digest_markdown(build_daily_digest(db, as_of_date="2026-06-20"))
    assert "# Finance Daily Digest - 2026-06-20" in md
    assert "## Balances" in md and "## Cash-Flow Projection" in md and "## Upcoming Obligations" in md
    assert "9,000.00" in md and "Rent check" in md
    assert "Provenance:" in md


def test_upcoming_obligations_match_longest_projection(tmp_path):
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=[
        ("rent", "Rent check", "housing", [{"id": "rent:2026-07-03", "due_date": "2026-07-03", "amount": -3000.0, "source": "seed"}]),
        ("nyt", "New York Times", "subscription", [{"id": "nyt:2026-07-23", "due_date": "2026-07-23", "amount": -30.30, "source": "seed"}]),
    ])
    digest = build_daily_digest(db, as_of_date="2026-06-20")
    due = [o["due_date"] for o in digest["upcoming_obligations"]]
    assert due == ["2026-07-03", "2026-07-23"]  # both inside the 60-day window, in date order


def test_auto_match_is_cleared_not_awaiting_confirmation(tmp_path):
    # An auto (high-confidence) match shows as cleared and must NOT also appear in
    # "Matches to Confirm" - the two sections are mutually exclusive (H2).
    db = _status_db(tmp_path / "d.sqlite", available=9000.0, obligations=[
        ("rent", "Rent check", "housing", [{"id": "rent:2026-06-10", "due_date": "2026-06-10", "amount": -3000.0, "source": "seed"}]),
    ])
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO transaction_obligation_matches (obligation_instance_id,transaction_id,match_type,match_score,created_at,updated_at) "
                 "VALUES ('rent:2026-06-10','TRN-r','auto',0.95,'x','x')")
    conn.commit()
    conn.close()
    dg = build_daily_digest(db, as_of_date="2026-06-20")
    cleared = {c.get("transaction_id") for c in dg["recently_cleared"]}
    confirm = {m.get("transaction_id") for m in dg["matches_to_confirm"]}
    assert "TRN-r" in cleared
    assert "TRN-r" not in confirm
    assert not (cleared & confirm)  # no payment double-listed
