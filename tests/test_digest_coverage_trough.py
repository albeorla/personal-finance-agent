"""Tests for digest coverage census (#3) and trough-sensitivity band + floor-breach
status gate (#7). Pure composition over a seeded SQLite db; no network, no writes.

Style mirrors tests/test_digest.py: a hand-built accounts/balance/sync skeleton,
then `apply_obligation_instances` for the roster, then `build_daily_digest`.
"""

import sqlite3

from financial_agent.digest import build_daily_digest, render_digest_markdown
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema


def _seed_db(path, *, available, obligations=()):
    """Seed a one-checking-account db plus an obligation roster.

    Each obligation is (id, name, kind, autopay, instances). Instances are full
    dicts so a test can set confidence / amount_status / estimation_inputs to
    exercise the estimated-driver path.
    """

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
    conn.execute("INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source,balance_date) VALUES ('chk',?,?,'2026-06-20T00:00:00+00:00','simplefin','2026-06-20')", (available, available))
    conn.execute("INSERT INTO sync_runs (started_at,finished_at,mode,accounts_seen,transactions_inserted,transactions_updated,error) VALUES ('2026-06-20T09:58:00+00:00','2026-06-20T10:00:00+00:00','i',1,0,0,NULL)")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    for oid, name, kind, autopay, instances in obligations:
        apply_obligation_instances(
            conn,
            obligation={"id": oid, "name": name, "kind": kind, "status": "active", "source": "seed", "autopay": autopay},
            instances=instances,
        )
    conn.commit()
    conn.close()
    return str(path)


# --- #3 coverage census ------------------------------------------------------


# rent (manual, in window), netflix (autopay, in window), gym (manual, out of window).
_COVERAGE_ROSTER = [
    ("rent", "Rent check", "housing", False,
     [{"id": "rent:2026-07-03", "due_date": "2026-07-03", "amount": -3000.0, "source": "seed"}]),
    ("netflix", "Netflix", "subscription", True,
     [{"id": "netflix:2026-07-10", "due_date": "2026-07-10", "amount": -20.0, "source": "seed"}]),
    ("gym", "Gym", "subscription", False,
     [{"id": "gym:2026-12-01", "due_date": "2026-12-01", "amount": -50.0, "source": "seed"}]),
]


def test_coverage_counts_add_up_and_match_seeded_roster(tmp_path):
    db = _seed_db(tmp_path / "d.sqlite", available=9000.0, obligations=_COVERAGE_ROSTER)
    cov = build_daily_digest(db, as_of_date="2026-06-20")["coverage"]

    # Roster census: 3 modeled, 1 autopay (netflix), 2 needing the user (rent, gym).
    assert cov["modeled_obligations"] == 3
    assert cov["autopay_silent"] == 1
    assert cov["manual_attention"] == 2
    # The split is exhaustive: autopay + manual == modeled.
    assert cov["autopay_silent"] + cov["manual_attention"] == cov["modeled_obligations"]

    # In-window (<= longest/60d window from 2026-06-20): rent + netflix; gym (Dec) is out.
    assert cov["in_window_obligations"] == 2
    assert cov["in_window_autopay"] == 1   # netflix
    assert cov["in_window_manual"] == 1    # rent
    assert cov["in_window_autopay"] + cov["in_window_manual"] == cov["in_window_obligations"]


def test_coverage_block_renders(tmp_path):
    db = _seed_db(tmp_path / "d.sqlite", available=9000.0, obligations=_COVERAGE_ROSTER)
    md = render_digest_markdown(build_daily_digest(db, as_of_date="2026-06-20"), verbose=True)

    assert "## Coverage" in md
    # Lead line restates the roster census in human terms.
    assert "Modeled: 3 obligations (1 autopay/silent, 2 need you)." in md
    # In-window line restates the per-window split.
    assert "This window: 2 hit checking; 1 autopay (no action), 1 manual." in md
    # Provenance is wired for the new block.
    assert "coverage <-" in md


# --- #7 trough sensitivity band ----------------------------------------------


def test_trough_band_picks_estimated_drivers_before_low_point(tmp_path):
    # Timeline (start 9000):
    #   06-25 Rent -800   (confirmed, NOT estimated)      -> 8200
    #   07-01 Apple -3000 (estimated, low)                -> 5200
    #   07-02 Eversource -1000 (estimated, low)           -> 4200  <- trough
    #   07-15 Payroll +5000 (inflow)                      -> 9200
    #   07-20 Water -2000 (estimated, low, AFTER trough)  -> 7200
    # Only the two estimated low-confidence outflows on/before the trough date are
    # drivers. Rent is excluded (not estimated); Water is excluded (after trough).
    db = _seed_db(tmp_path / "d.sqlite", available=9000.0, obligations=[
        ("rent", "Rent check", "housing", True,
         [{"id": "rent:2026-06-25", "due_date": "2026-06-25", "amount": -800.0, "source": "seed"}]),
        ("apple", "Apple Card", "card_paydown", True,
         [{"id": "apple:2026-07-01", "due_date": "2026-07-01", "amount": -3000.0,
           "source": "seed", "confidence": "low", "amount_status": "estimated"}]),
        ("eversource", "Eversource", "utility", True,
         [{"id": "eversource:2026-07-02", "due_date": "2026-07-02", "amount": -1000.0,
           "source": "seed", "confidence": "low", "amount_status": "estimated"}]),
        ("payroll", "Payroll", "income", True,
         [{"id": "payroll:2026-07-15", "due_date": "2026-07-15", "amount": 5000.0, "source": "seed"}]),
        ("water", "Water bill", "utility", True,
         [{"id": "water:2026-07-20", "due_date": "2026-07-20", "amount": -2000.0,
           "source": "seed", "confidence": "low", "amount_status": "estimated"}]),
    ])
    digest = build_daily_digest(db, as_of_date="2026-06-20")
    ts = digest["trough_sensitivity"]

    # The trough is the Eversource date, not the later (above-trough) Water debit.
    assert ts["lowest_balance"] == 4200.0
    assert ts["lowest_balance_date"] == "2026-07-02"

    # Exactly the two estimated drivers before the trough, ranked by downside
    # (Apple 3000*0.20=600 first, Eversource 1000*0.20=200).
    names = [d["obligation_name"] for d in ts["drivers"]]
    assert names == ["Apple Card", "Eversource"]
    assert "Water bill" not in names   # after the trough -> cannot move it
    assert "Rent check" not in names   # confirmed (not estimated) -> not soft

    # Band = lowest -/+ summed downside (600 + 200 = 800).
    assert ts["low_estimate"] == 3400.0
    assert ts["high_estimate"] == 5000.0

    # The per-window band on the longest (60d) window agrees with the headline.
    longest = digest["cash_flow"][-1]
    assert longest["trough_band_drivers"] == 2
    assert longest["trough_low_estimate"] == 3400.0
    assert longest["trough_high_estimate"] == 5000.0

    # Band line renders with both estimated drivers named.
    md = render_digest_markdown(digest, verbose=True)
    assert "Trough sensitivity" in md
    assert "2 estimated bills" in md
    assert "Apple Card" in md and "Eversource" in md


# --- #7 floor-breach status gate ---------------------------------------------


def test_status_yellow_when_estimate_could_breach_cash_floor(tmp_path):
    # Point trough clears the $2,500 floor (2700), but a small estimated bill's
    # downside could push it under -> breach_risk -> YELLOW (not GREEN).
    #   start 9000; Rent -5400 (confirmed) -> 3600; Eversource -900 (estimated) -> 2700.
    #   downside = 900 * 0.35 (no confidence tier) = 315 -> low estimate 2385 < 2500.
    # Eversource is < $1,000 so it does NOT trip the estimated_material YELLOW cap;
    # the floor-breach gate is what flips the color.
    db = _seed_db(tmp_path / "d.sqlite", available=9000.0, obligations=[
        ("rent", "Rent check", "housing", True,
         [{"id": "rent:2026-07-01", "due_date": "2026-07-01", "amount": -5400.0, "source": "seed"}]),
        ("eversource", "Eversource", "utility", True,
         [{"id": "eversource:2026-07-02", "due_date": "2026-07-02", "amount": -900.0,
           "source": "seed", "amount_status": "estimated"}]),
    ])
    digest = build_daily_digest(db, as_of_date="2026-06-20")

    # Point estimate is above the floor (so no cash_floor guardrail fires)...
    longest = digest["cash_flow"][-1]
    assert longest["lowest_balance"] == 2700.0
    assert not any(g["rule_type"] == "cash_floor" for g in digest["guardrails"])
    # ...but the estimated downside crosses the floor.
    assert digest["trough_sensitivity"]["breach_risk"] is True
    assert longest["trough_breach_risk"] is True
    assert not digest["estimated_material"]  # the < $1k bill does not trip that cap
    assert digest["status_color"] == "YELLOW"


def test_status_red_when_estimate_band_crosses_zero(tmp_path):
    # Point trough stays above the floor (2600, no cash_floor guardrail), but the
    # estimated downside pushes the low estimate below zero -> RED.
    #   start 10300; Apple -7700 (estimated) -> 2600. downside 7700*0.35=2695 ->
    #   low estimate 2600-2695 = -95 (< 0).
    db = _seed_db(tmp_path / "d.sqlite", available=10300.0, obligations=[
        ("apple", "Apple Card", "card_paydown", True,
         [{"id": "apple:2026-07-01", "due_date": "2026-07-01", "amount": -7700.0,
           "source": "seed", "amount_status": "estimated"}]),
    ])
    digest = build_daily_digest(db, as_of_date="2026-06-20")

    longest = digest["cash_flow"][-1]
    assert longest["lowest_balance"] == 2600.0          # point estimate clears 0 and the floor
    assert not any(g["rule_type"] == "cash_floor" for g in digest["guardrails"])
    assert longest["trough_low_estimate"] == -95.0      # band crosses zero
    assert digest["trough_sensitivity"]["low_estimate"] == -95.0
    assert digest["status_color"] == "RED"
