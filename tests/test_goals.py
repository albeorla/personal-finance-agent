import sqlite3
from datetime import date

from financial_agent.config import SOURCE_SCHEMA
from financial_agent.goals import list_goals, set_goal, set_goal_override
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SOURCE_SCHEMA)
    ensure_app_schema(conn)
    return conn


def _seed_balance_snapshot(conn, *, account_id, balance, recorded_at, available=None):
    """Insert a balance snapshot for an account (mirrors a SimpleFIN sync row)."""
    conn.execute(
        """
        INSERT INTO balance_snapshots (account_id, balance, available, recorded_at, source)
        VALUES (?, ?, ?, ?, 'test')
        """,
        (account_id, balance, balance if available is None else available, recorded_at),
    )


def _seed_inflow(conn, *, obligation_id, source, due_date, amount, direction="inflow"):
    """Create a one-instance obligation routed to a source account."""
    conn.execute(
        """
        INSERT INTO obligations (id, name, kind, cadence, status, source, created_at, updated_at)
        VALUES (?, ?, 'income', 'monthly', 'active', ?, '2026-01-01', '2026-01-01')
        ON CONFLICT(id) DO NOTHING
        """,
        (obligation_id, obligation_id, source),
    )
    conn.execute(
        """
        INSERT INTO obligation_instances (
            id, obligation_id, due_date, amount, direction, status, source,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'expected', ?, '2026-01-01', '2026-01-01')
        """,
        (f"{obligation_id}:{due_date}", obligation_id, due_date, amount, direction, source),
    )


def test_set_goal_creates_new_goal_with_all_fields(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = set_goal(
        conn,
        name="Emergency Fund",
        target_amount=5000,
        deadline="2026-12-31",
        source_account="account:savings",
        note="3 months expenses",
    )
    assert result["created"] is True
    assert result["updated"] is False
    assert result["goal_id"] == "goal_emergency_fund_account_savings"

    row = conn.execute("SELECT * FROM goals WHERE id = ?", (result["goal_id"],)).fetchone()
    assert row["name"] == "Emergency Fund"
    assert row["target_amount"] == 5000
    assert row["deadline"] == "2026-12-31"
    assert row["source_account"] == "account:savings"
    assert row["note"] == "3 months expenses"
    assert row["status"] == "active"
    assert row["created_at"] and row["updated_at"]


def test_set_goal_nullable_deadline_and_account(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = set_goal(conn, name="Vacation", target_amount=2000)
    assert result["deadline"] is None
    assert result["source_account"] is None
    assert result["goal_id"] == "goal_vacation_general"


def test_set_goal_idempotent_upsert(tmp_path):
    conn = _db(tmp_path / "t.db")
    first = set_goal(conn, name="Car", target_amount=10000, source_account="account:savings")
    second = set_goal(conn, name="Car", target_amount=12000, source_account="account:savings")
    assert first["created"] is True
    assert second["created"] is False
    assert second["updated"] is True
    assert first["goal_id"] == second["goal_id"]

    rows = conn.execute("SELECT target_amount FROM goals").fetchall()
    assert len(rows) == 1
    assert rows[0]["target_amount"] == 12000


def test_set_goal_rejects_empty_name(tmp_path):
    conn = _db(tmp_path / "t.db")
    for bad in ("", "   "):
        try:
            set_goal(conn, name=bad, target_amount=100)
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_set_goal_rejects_nonpositive_target(tmp_path):
    conn = _db(tmp_path / "t.db")
    for bad in (0, -100):
        try:
            set_goal(conn, name="X", target_amount=bad)
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_set_goal_rejects_malformed_deadline(tmp_path):
    conn = _db(tmp_path / "t.db")
    try:
        set_goal(conn, name="X", target_amount=100, deadline="not-a-date")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_list_goals_empty_returns_zero_progress(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    goals = list_goals(conn, "2026-06-24")
    assert len(goals) == 1
    assert goals[0]["current_progress"] == 0
    assert goals[0]["progress_pct"] == 0


def test_list_goals_sums_matured_inflows(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    _seed_inflow(conn, obligation_id="pay1", source="account:savings",
                 due_date="2026-03-01", amount=100)
    _seed_inflow(conn, obligation_id="pay2", source="account:savings",
                 due_date="2026-04-01", amount=100)
    _seed_inflow(conn, obligation_id="pay3", source="account:savings",
                 due_date="2026-05-01", amount=100)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 300


def test_list_goals_ignores_outflows_and_future(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    _seed_inflow(conn, obligation_id="in", source="account:savings",
                 due_date="2026-03-01", amount=100)
    _seed_inflow(conn, obligation_id="out", source="account:savings",
                 due_date="2026-03-01", amount=500, direction="outflow")
    _seed_inflow(conn, obligation_id="future", source="account:savings",
                 due_date="2026-09-01", amount=999)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 100


def test_list_goals_filters_by_source_account(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    _seed_inflow(conn, obligation_id="sav", source="account:savings",
                 due_date="2026-03-01", amount=100)
    _seed_inflow(conn, obligation_id="chk", source="account:checking",
                 due_date="2026-03-01", amount=400)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 100


def test_list_goals_no_account_sums_all_inflows(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31")
    _seed_inflow(conn, obligation_id="sav", source="account:savings",
                 due_date="2026-03-01", amount=100)
    _seed_inflow(conn, obligation_id="chk", source="account:checking",
                 due_date="2026-03-01", amount=400)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 500


def test_list_goals_on_track_status(tmp_path):
    conn = _db(tmp_path / "t.db")
    # Goal created 2026-01-01, deadline 2026-12-31. As-of 2026-07-01 is ~50%
    # through. Progress 500/1000 = 50% keeps pace -> on_track.
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    conn.execute("UPDATE goals SET created_at = '2026-01-01'")
    _seed_inflow(conn, obligation_id="p", source="account:savings",
                 due_date="2026-06-01", amount=500)
    goals = list_goals(conn, "2026-07-01")
    assert goals[0]["status"] == "on_track"


def test_list_goals_behind_status(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    conn.execute("UPDATE goals SET created_at = '2026-01-01'")
    # ~75% through the year, only $100 saved -> behind.
    _seed_inflow(conn, obligation_id="p", source="account:savings",
                 due_date="2026-06-01", amount=100)
    goals = list_goals(conn, "2026-10-01")
    assert goals[0]["status"] == "behind"


def test_list_goals_due_soon_status(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-07-01",
             source_account="account:savings")
    # as_of within 14 days of deadline -> due_soon regardless of pace.
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["status"] == "due_soon"


def test_list_goals_completed_status(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    _seed_inflow(conn, obligation_id="p", source="account:savings",
                 due_date="2026-03-01", amount=1200)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["status"] == "completed"
    assert goals[0]["progress_pct"] > 1.0
    assert goals[0]["required_monthly_rate"] == 0.0
    assert goals[0]["remaining_amount"] == 0


def test_list_goals_no_deadline_status(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Open", target_amount=1000)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["status"] == "no_deadline"
    assert goals[0]["required_monthly_rate"] is None
    assert goals[0]["months_remaining"] is None


def test_progress_pct_value(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    _seed_inflow(conn, obligation_id="p", source="account:savings",
                 due_date="2026-03-01", amount=250)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["progress_pct"] == 0.25


def test_monthly_rate_formula(tmp_path):
    conn = _db(tmp_path / "t.db")
    # target 1000, progress 200, deadline 2026-12-31, as_of 2026-06-24.
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    _seed_inflow(conn, obligation_id="p", source="account:savings",
                 due_date="2026-03-01", amount=200)
    goals = list_goals(conn, "2026-06-24")
    days_remaining = (date(2026, 12, 31) - date(2026, 6, 24)).days
    expected = round(800 / (days_remaining / 30.44), 2)
    assert goals[0]["required_monthly_rate"] == expected
    assert goals[0]["days_remaining"] == days_remaining


def test_monthly_rate_null_for_no_deadline(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Open", target_amount=1000)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["required_monthly_rate"] is None


def test_goal_name_with_special_chars(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = set_goal(conn, name="Kid's College Fund! (2030)", target_amount=50000)
    assert result["goal_id"] == "goal_kid_s_college_fund_2030_general"


def test_set_and_list_roundtrip_multiple_goals(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Emergency", target_amount=5000, deadline="2026-12-31",
             source_account="account:savings")
    set_goal(conn, name="Vacation", target_amount=3000)
    set_goal(conn, name="Car", target_amount=10000, deadline="2026-07-01",
             source_account="account:savings")
    goals = list_goals(conn, "2026-06-24")
    by_name = {g["name"]: g for g in goals}
    assert set(by_name) == {"Emergency", "Vacation", "Car"}
    assert by_name["Vacation"]["status"] == "no_deadline"
    assert by_name["Car"]["status"] == "due_soon"


# --- live-balance progress -------------------------------------------------


def test_progress_reflects_live_balance(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    _seed_balance_snapshot(conn, account_id="account:savings", balance=750,
                           recorded_at="2026-06-24")
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 750.0


def test_manual_override_takes_precedence_over_balance(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
                      source_account="account:savings")
    _seed_balance_snapshot(conn, account_id="account:savings", balance=500,
                           recorded_at="2026-06-24")
    set_goal_override(conn, result["goal_id"], override_amount=999)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 999.0


def test_override_can_be_cleared_back_to_balance(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
                      source_account="account:savings")
    _seed_balance_snapshot(conn, account_id="account:savings", balance=500,
                           recorded_at="2026-06-24")
    set_goal_override(conn, result["goal_id"], override_amount=999)
    set_goal_override(conn, result["goal_id"], override_amount=None)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 500.0


def test_no_source_account_falls_back_to_inflows(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31")
    # Balance snapshot exists but must be ignored without a source account.
    _seed_balance_snapshot(conn, account_id="account:savings", balance=750,
                           recorded_at="2026-06-24")
    _seed_inflow(conn, obligation_id="p1", source="account:savings",
                 due_date="2026-03-01", amount=100)
    _seed_inflow(conn, obligation_id="p2", source="account:checking",
                 due_date="2026-04-01", amount=200)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 300.0


def test_latest_balance_snapshot_wins(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    _seed_balance_snapshot(conn, account_id="account:savings", balance=100,
                           recorded_at="2026-06-01")
    _seed_balance_snapshot(conn, account_id="account:savings", balance=500,
                           recorded_at="2026-06-24")
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 500.0


def test_future_balance_snapshots_ignored(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    _seed_balance_snapshot(conn, account_id="account:savings", balance=300,
                           recorded_at="2026-06-24")
    _seed_balance_snapshot(conn, account_id="account:savings", balance=999,
                           recorded_at="2026-07-01")
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 300.0


def test_status_uses_live_balance_for_pace(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    conn.execute("UPDATE goals SET created_at = '2026-01-01'")
    # ~50% elapsed at 2026-07-01, live balance 500/1000 = 50% -> on_track.
    _seed_balance_snapshot(conn, account_id="account:savings", balance=500,
                           recorded_at="2026-06-24")
    goals = list_goals(conn, "2026-07-01")
    assert goals[0]["current_progress"] == 500.0
    assert goals[0]["status"] == "on_track"


def test_override_zero_is_valid(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
                      source_account="account:savings")
    _seed_balance_snapshot(conn, account_id="account:savings", balance=500,
                           recorded_at="2026-06-24")
    set_goal_override(conn, result["goal_id"], override_amount=0.0)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 0.0


def test_balance_snapshot_on_as_of_day_in_range(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    # recorded_at carries a same-day timestamp; as_of date must still include it.
    _seed_balance_snapshot(conn, account_id="account:savings", balance=420,
                           recorded_at="2026-06-24T15:30:00")
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 420.0


def test_set_goal_override_returns_updated_goal(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
                      source_account="account:savings")
    updated = set_goal_override(conn, result["goal_id"], override_amount=999)
    assert updated["goal_id"] == result["goal_id"]
    assert updated["current_progress"] == 999.0


def test_set_goal_override_clear_returns_reverted_goal(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
                      source_account="account:savings")
    _seed_balance_snapshot(conn, account_id="account:savings", balance=500,
                           recorded_at="2026-06-24")
    set_goal_override(conn, result["goal_id"], override_amount=999)
    cleared = set_goal_override(conn, result["goal_id"], override_amount=None)
    assert cleared["current_progress"] == 500.0


def test_set_goal_override_rejects_unknown_goal(tmp_path):
    conn = _db(tmp_path / "t.db")
    try:
        set_goal_override(conn, "goal_does_not_exist", override_amount=100)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_set_goal_override_rejects_negative_amount(tmp_path):
    conn = _db(tmp_path / "t.db")
    result = set_goal(conn, name="Fund", target_amount=1000)
    try:
        set_goal_override(conn, result["goal_id"], override_amount=-1)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_no_source_account_no_inflows_is_zero(tmp_path):
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31")
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 0


# --- Item A: "behind" false-fire guards ------------------------------------


def test_freshly_created_far_deadline_no_funding_is_pending_not_behind(tmp_path):
    """A goal created today with a far-off deadline and no funding signal (no
    source account, no override, no matured inflows) is 'pending', never 'behind'.

    This is the false-fire the fix targets: on the day a goal is created the
    pace math saw ~0 elapsed-vs-required and flagged 'behind', which would have
    pushed a nag to Todoist. With no way to know the balance yet, the goal is an
    unstarted plan, not a lagging one.
    """
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Cushion", target_amount=10000, deadline="2027-12-31")
    conn.execute("UPDATE goals SET created_at = '2026-06-24'")
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["status"] == "pending"
    assert goals[0]["status"] != "behind"


def test_zero_override_far_deadline_day_one_is_on_track_not_behind(tmp_path):
    """A trackable goal (explicit balance_override=0) created today with a far
    deadline reads 'on_track', not 'behind': it is too young in its schedule to
    judge, so the pace grace window applies.
    """
    conn = _db(tmp_path / "t.db")
    result = set_goal(conn, name="Cushion", target_amount=10000,
                      deadline="2027-12-31", source_account="account:buffer")
    conn.execute("UPDATE goals SET created_at = '2026-06-24'")
    set_goal_override(conn, result["goal_id"], override_amount=0.0)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["current_progress"] == 0.0
    assert goals[0]["status"] == "on_track"
    assert goals[0]["status"] != "behind"


def test_no_deadline_no_funding_is_not_behind(tmp_path):
    """An open-ended goal with no funding signal is 'no_deadline', never 'behind'."""
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Someday", target_amount=10000)
    goals = list_goals(conn, "2026-06-24")
    assert goals[0]["status"] == "no_deadline"
    assert goals[0]["status"] != "behind"


def test_genuine_pace_shortfall_still_reads_behind(tmp_path):
    """The fix must not mask a real lag: with real progress data and a schedule
    well past the grace window, a true shortfall still reads 'behind'.
    """
    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Fund", target_amount=1000, deadline="2026-12-31",
             source_account="account:savings")
    conn.execute("UPDATE goals SET created_at = '2026-01-01'")
    # ~75% of the year elapsed, only $100 saved -> genuinely behind.
    _seed_inflow(conn, obligation_id="p", source="account:savings",
                 due_date="2026-06-01", amount=100)
    goals = list_goals(conn, "2026-10-01")
    assert goals[0]["status"] == "behind"


def test_pending_goal_not_surfaced_as_nag(tmp_path):
    """A pending (not-yet-trackable) goal must not appear in the surface queue's
    goal-behind items, so it never pushes a Todoist nag.
    """
    from financial_agent.surface_queue import _goal_behind_surface_items

    conn = _db(tmp_path / "t.db")
    set_goal(conn, name="Cushion", target_amount=10000, deadline="2027-12-31")
    conn.execute("UPDATE goals SET created_at = '2026-06-24'")
    items = _goal_behind_surface_items(conn, date(2026, 6, 24))
    assert items == []
