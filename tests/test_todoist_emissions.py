"""Tests for idempotent Todoist surfacing (the todoist_emissions ledger).

The daily job pushes due items to Todoist and must NEVER duplicate: not on a
re-run, not across days, and not when the user already made the task by hand.
These tests drive surface_to_todoist / reconcile_emission with a mock send_func
so no real network call is ever made.
"""

import sqlite3

import financial_agent.todoist_outbox as tb
from financial_agent.release_gate import promote_release
from financial_agent.schema import ensure_app_schema
from financial_agent.follow_ups import capture_followup, list_due_followups
from financial_agent.todoist_outbox import (
    FA_AUTO_LABEL,
    TASK_NOT_FOUND,
    content_hash_for,
    extract_surface_key,
    mark_emission_status,
    reconcile_emission,
    reconcile_todoist_completions,
    surface_marker,
    surface_to_todoist,
    verify_surface_coverage,
)

AS_OF = "2026-06-24"


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


class _Spy:
    """Records every HTTP send; assigns incrementing task ids on create."""

    def __init__(self):
        self.calls = []
        self._next = 0

    def __call__(self, token, path, body, **kwargs):
        self.calls.append({"token": token, "path": path, "body": body})
        if path == "/tasks":  # create
            self._next += 1
            return {"id": f"T{self._next}", "url": f"https://todoist.com/showTask?id=T{self._next}"}
        return {}  # update returns empty body in the real client

    @property
    def creates(self):
        return [c for c in self.calls if c["path"] == "/tasks"]

    @property
    def updates(self):
        return [c for c in self.calls if c["path"].startswith("/tasks/")]


def _enabled(conn, items, spy):
    return surface_to_todoist(
        conn, items, AS_OF, write_enabled=True, token="tok", project_id="proj", send_func=spy
    )


# --- schema ----------------------------------------------------------------


def test_ledger_table_and_columns_exist(tmp_path):
    conn = _db(tmp_path / "f.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(todoist_emissions)").fetchall()}
    assert cols == {
        "surface_key", "todoist_task_id", "status", "content_hash", "created_at",
        "last_seen", "retire_requested_at",
    }
    # surface_key is the primary key.
    pk = [r[1] for r in conn.execute("PRAGMA table_info(todoist_emissions)").fetchall() if r[5]]
    assert pk == ["surface_key"]


# --- markers ---------------------------------------------------------------


def test_marker_round_trips():
    desc = "Pay electric\n\n" + surface_marker("snapshot-due:ACT-1")
    assert extract_surface_key(desc) == "snapshot-due:ACT-1"
    assert extract_surface_key("no marker here") is None
    assert extract_surface_key(None) is None


def test_content_hash_is_deterministic():
    assert content_hash_for("a", "b") == content_hash_for("a", "b")
    assert content_hash_for("a", "b") != content_hash_for("a", "c")
    # a due-date-only change must change the hash so the task is not skipped
    assert content_hash_for("a", "b", "2026-07-01") != content_hash_for("a", "b", "2026-06-28")


def test_due_date_only_change_updates_task_in_place(tmp_path):
    # Same content/description, shifted due date: must UPDATE the same task (push
    # the new date), not skip it as unchanged - the bug that left stale dates.
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    item = {
        "surface_key": "obligation-due:rent:2026-07-03",
        "content": "Rent due: $3,000.00",
        "description": "Pay rent.",
        "due_date": "2026-07-01",
    }
    first = _enabled(conn, [item], spy)
    assert first["created"] == 1 and len(spy.creates) == 1

    second = _enabled(conn, [{**item, "due_date": "2026-06-28"}], spy)
    assert second["updated"] == 1 and second["skipped"] == 0
    assert len(spy.updates) == 1
    assert spy.updates[-1]["body"].get("due_date") == "2026-06-28"

    # re-running with the same (new) due date is now idempotent
    third = _enabled(conn, [{**item, "due_date": "2026-06-28"}], spy)
    assert third["skipped"] == 1 and third["updated"] == 0


# --- create-once-then-skip -------------------------------------------------


def test_create_once_then_skip(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    item = {"surface_key": "followup:abc", "content": "Call bank", "description": "ring them"}

    r1 = _enabled(conn, [item], spy)
    assert r1["created"] == 1 and r1["skipped"] == 0
    assert len(spy.creates) == 1
    body = spy.creates[0]["body"]
    assert surface_marker("followup:abc") in body["description"]
    assert body["labels"] == [FA_AUTO_LABEL]
    row = conn.execute("SELECT * FROM todoist_emissions WHERE surface_key = 'followup:abc'").fetchone()
    assert row["todoist_task_id"] == "T1" and row["status"] == "open"

    # Second run, same item, same date: no new task, skip.
    r2 = _enabled(conn, [item], spy)
    assert r2["created"] == 0 and r2["skipped"] == 1
    assert len(spy.creates) == 1  # still exactly one create ever


# --- update-on-change ------------------------------------------------------


def test_update_on_change_keeps_same_task(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    key = "estimate-review:OBL1:c1"
    _enabled(conn, [{"surface_key": key, "content": "old", "description": "old body"}], spy)
    assert spy.creates and not spy.updates

    r = _enabled(conn, [{"surface_key": key, "content": "new", "description": "new body"}], spy)
    assert r["updated"] == 1 and r["created"] == 0
    assert len(spy.creates) == 1  # not recreated
    assert spy.updates and spy.updates[0]["path"] == "/tasks/T1"
    new_hash = content_hash_for("new", "new body")
    row = conn.execute("SELECT content_hash FROM todoist_emissions WHERE surface_key = ?", (key,)).fetchone()
    assert row["content_hash"] == new_hash


# --- completed-task-resolves-source ----------------------------------------


def test_completed_snapshot_acknowledges_only_current_evidence(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    key = "finance-status"
    item = {"surface_key": key, "content": "Finance status", "description": "one warning"}
    _enabled(conn, [item], spy)

    # The checkbox acknowledges this exact snapshot, not every future snapshot.
    mark_emission_status(conn, key, "completed")
    same = _enabled(conn, [item], spy)
    assert same["resolved"] == 1 and same["created"] == 0

    changed = _enabled(
        conn,
        [{"surface_key": key, "content": "Finance status", "description": "two warnings"}],
        spy,
    )
    assert changed["created"] == 1 and changed["resolved"] == 0
    assert len(spy.creates) == 2


def test_completed_followup_still_resolves_the_source_permanently(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    key = "followup:abc"
    _enabled(conn, [{"surface_key": key, "content": "Call bank", "description": "today"}], spy)
    mark_emission_status(conn, key, "completed")

    result = _enabled(
        conn,
        [{"surface_key": key, "content": "Call bank", "description": "new wording"}],
        spy,
    )

    assert result["resolved"] == 1 and result["created"] == 0
    assert len(spy.creates) == 1


def test_deleted_by_user_also_resolves(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    key = "snapshot-due:ACT-9"
    _enabled(conn, [{"surface_key": key, "content": "snap", "description": "x"}], spy)
    mark_emission_status(conn, key, "deleted_by_user")
    r = _enabled(conn, [{"surface_key": key, "content": "snap", "description": "x"}], spy)
    assert r["resolved"] == 1 and r["created"] == 0


# --- manual-task-adoption --------------------------------------------------


def test_manual_task_adoption_then_skip(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    key = "snapshot-due:ACT-1"
    # A task the user made by hand, found while scanning Todoist.
    chash = content_hash_for("Pay electric", "body")
    adopt = reconcile_emission(conn, key, "T55", chash)
    assert adopt["action"] == "adopted"
    row = conn.execute("SELECT * FROM todoist_emissions WHERE surface_key = ?", (key,)).fetchone()
    assert row["todoist_task_id"] == "T55" and row["status"] == "open"

    # The next surfacing run with the SAME content skips instead of duplicating.
    r = _enabled(conn, [{"surface_key": key, "content": "Pay electric", "description": "body"}], spy)
    assert r["skipped"] == 1 and r["created"] == 0
    assert spy.calls == []  # never touched the network


def test_reconcile_is_idempotent(tmp_path):
    conn = _db(tmp_path / "f.db")
    chash = content_hash_for("x", "y")
    reconcile_emission(conn, "followup:k", "T1", chash)
    second = reconcile_emission(conn, "followup:k", "T1", chash)
    assert second["action"] == "updated"
    n = conn.execute("SELECT COUNT(*) FROM todoist_emissions WHERE surface_key = 'followup:k'").fetchone()[0]
    assert n == 1


# --- never-duplicates-on-rerun (stress) ------------------------------------


def test_never_duplicates_on_rerun(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    items = [
        {"surface_key": "followup:1", "content": "a", "description": "a"},
        {"surface_key": "goal:Roth:behind", "content": "b", "description": "b"},
        {"surface_key": "snapshot-due:ACT-1", "content": "c", "description": "c"},
    ]
    _enabled(conn, items, spy)
    _enabled(conn, items, spy)
    _enabled(conn, items, spy)
    assert len(spy.creates) == 3  # one per key, ever
    rows = conn.execute("SELECT COUNT(*) FROM todoist_emissions").fetchone()[0]
    assert rows == 3


# --- gating ----------------------------------------------------------------


def test_gated_off_makes_no_call_and_no_ledger_row(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    r = surface_to_todoist(
        conn,
        [{"surface_key": "followup:abc", "content": "x", "description": "y"}],
        AS_OF,
        write_enabled=False,
        token="tok",
        project_id="proj",
        send_func=spy,
    )
    assert spy.calls == []
    assert r["status"] == "awaiting-integration" and r["sent"] is False
    assert r["created"] == 0
    n = conn.execute("SELECT COUNT(*) FROM todoist_emissions").fetchone()[0]
    assert n == 0


def test_gated_off_when_no_token(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    r = surface_to_todoist(
        conn,
        [{"surface_key": "followup:abc", "content": "x", "description": "y"}],
        AS_OF,
        write_enabled=True,
        token="",
        project_id="proj",
        send_func=spy,
    )
    assert spy.calls == []
    assert r["status"] == "awaiting-integration"


# --- edge cases ------------------------------------------------------------


def test_empty_items_is_noop(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    r = _enabled(conn, [], spy)
    assert r["created"] == 0 and r["updated"] == 0 and r["skipped"] == 0
    assert spy.calls == []


def test_missing_surface_key_is_failed_not_raised(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    r = _enabled(conn, [{"content": "x", "description": "y"}], spy)
    assert r["failed"] == 1 and r["created"] == 0
    assert spy.calls == []


def test_send_failure_recorded_not_raised(tmp_path):
    conn = _db(tmp_path / "f.db")

    def boom(token, path, body, **kwargs):
        raise RuntimeError("network down")

    r = surface_to_todoist(
        conn,
        [{"surface_key": "followup:abc", "content": "x", "description": "y"}],
        AS_OF,
        write_enabled=True,
        token="tok",
        project_id="proj",
        send_func=boom,
    )
    assert r["failed"] == 1 and r["created"] == 0
    row = conn.execute(
        "SELECT status, content_hash FROM todoist_emissions WHERE surface_key = 'followup:abc'"
    ).fetchone()
    assert row["status"] == "create_pending"


def test_ambiguous_create_adopts_task_found_by_marker_before_retry(tmp_path):
    conn = _db(tmp_path / "f.db")
    item = {"surface_key": "snapshot-due:ACT-1", "content": "Update balance", "description": "Portal"}
    sends = []

    def lost_response(token, path, body, **kwargs):
        pending = conn.execute(
            "SELECT status FROM todoist_emissions WHERE surface_key = 'snapshot-due:ACT-1'"
        ).fetchone()
        assert pending["status"] == "create_pending"
        sends.append(body)
        raise TimeoutError("response lost after remote create")

    first = surface_to_todoist(
        conn, [item], AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=lost_response,
    )
    assert first["failed"] == 1 and len(sends) == 1

    def found(token, project_id, *, cursor=None, timeout=30):
        return {
            "results": [{
                "id": "REMOTE-1",
                "content": "Update balance",
                "description": "Portal\n\n[fa:snapshot-due:ACT-1]",
                "labels": [FA_AUTO_LABEL],
            }],
            "next_cursor": None,
        }

    second = surface_to_todoist(
        conn, [item], AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("blind retry")),
        list_func=found,
    )
    assert second["adopted"] == 1 and second["created"] == 0
    row = conn.execute(
        "SELECT todoist_task_id, status FROM todoist_emissions WHERE surface_key = ?",
        (item["surface_key"],),
    ).fetchone()
    assert tuple(row) == ("REMOTE-1", "open")


def test_pending_create_does_not_retry_after_partial_todoist_read(tmp_path, monkeypatch):
    conn = _db(tmp_path / "f.db")
    item = {"surface_key": "snapshot-due:ACT-2", "content": "Update balance"}
    conn.execute(
        "INSERT INTO todoist_emissions (surface_key,todoist_task_id,status,content_hash,created_at,last_seen) "
        "VALUES ('snapshot-due:ACT-2','intent:abc','create_pending','h','x','x')"
    )
    monkeypatch.setattr(tb, "MAX_LIST_PAGES", 1)

    result = surface_to_todoist(
        conn, [item], AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("blind retry")),
        list_func=lambda token, project_id, *, cursor=None, timeout=30: {
            "results": [], "next_cursor": "more"
        },
    )

    assert result["failed"] == 1
    assert result["items"][0]["action"] == "verify_required"


def test_pending_create_retries_only_after_complete_absence_check(tmp_path):
    conn = _db(tmp_path / "f.db")
    item = {"surface_key": "snapshot-due:ACT-3", "content": "Update balance"}
    conn.execute(
        "INSERT INTO todoist_emissions (surface_key,todoist_task_id,status,content_hash,created_at,last_seen) "
        "VALUES ('snapshot-due:ACT-3','intent:abc','create_pending','h','x','x')"
    )
    sends = []

    result = surface_to_todoist(
        conn, [item], AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=lambda token, path, body, **kwargs: sends.append(body) or {"id": "T3"},
        list_func=lambda token, project_id, *, cursor=None, timeout=30: {
            "results": [], "next_cursor": None
        },
    )

    assert result["created"] == 1 and len(sends) == 1
    assert tuple(conn.execute(
        "SELECT todoist_task_id, status FROM todoist_emissions WHERE surface_key = 'snapshot-due:ACT-3'"
    ).fetchone()) == ("T3", "open")


def test_surface_coverage_accepts_open_task_or_current_evidence_dismissal(tmp_path):
    conn = _db(tmp_path / "f.db")
    queue = {
        "items": [{
            "id": "match:rent:2026-06-24",
            "coverage": {"kind": "rollup", "surface_key": "finance-status"},
        }]
    }
    surface_items = [{
        "surface_key": "finance-status",
        "content": "Finance status",
        "description": "Queue member: match:rent:2026-06-24",
    }]
    _enabled(conn, surface_items, _Spy())

    def listed(token, project_id, *, cursor=None, timeout=30):
        return {
            "results": [{
                "id": "T1", "content": "Finance status",
                "description": "Queue member: match:rent:2026-06-24\n\n[fa:finance-status]",
                "labels": [FA_AUTO_LABEL],
            }],
            "next_cursor": None,
        }

    open_report = verify_surface_coverage(
        conn, action_queue=queue, surface_items=surface_items, as_of_date=AS_OF,
        token="tok", project_id="proj", list_func=listed,
    )
    assert open_report["ok"] is True and open_report["covered_open"] == 1

    missing_membership = verify_surface_coverage(
        conn, action_queue=queue, surface_items=surface_items, as_of_date=AS_OF,
        token="tok", project_id="proj",
        list_func=lambda token, project_id, *, cursor=None, timeout=30: {
            "results": [{
                "id": "T1", "content": "Finance status",
                "description": "Summary without member list\n\n[fa:finance-status]",
                "labels": [FA_AUTO_LABEL],
            }],
            "next_cursor": None,
        },
    )
    assert missing_membership["ok"] is False
    assert missing_membership["missing"] == ["match:rent:2026-06-24"]

    mark_emission_status(conn, "finance-status", "completed")
    dismissed_report = verify_surface_coverage(
        conn, action_queue=queue, surface_items=surface_items, as_of_date=AS_OF,
        token="tok", project_id="proj",
        list_func=lambda token, project_id, *, cursor=None, timeout=30: {
            "results": [], "next_cursor": None
        },
    )
    assert dismissed_report["ok"] is True
    assert dismissed_report["dismissed_current_evidence"] == 1


def test_surface_coverage_is_non_green_for_missing_or_partial_board(tmp_path, monkeypatch):
    conn = _db(tmp_path / "f.db")
    queue = {"items": [{
        "id": "match:x",
        "coverage": {"kind": "rollup", "surface_key": "finance-status"},
    }]}
    surface_items = [{
        "surface_key": "finance-status", "content": "Finance status",
        "description": "Queue member: match:x",
    }]
    missing = verify_surface_coverage(
        conn, action_queue=queue, surface_items=surface_items, as_of_date=AS_OF,
        token="tok", project_id="proj",
        list_func=lambda token, project_id, *, cursor=None, timeout=30: {
            "results": [], "next_cursor": None
        },
    )
    assert missing["ok"] is False and missing["status"] == "warn"
    assert missing["missing"] == ["match:x"]

    monkeypatch.setattr(tb, "MAX_LIST_PAGES", 1)
    partial = verify_surface_coverage(
        conn, action_queue={"items": []}, surface_items=[], as_of_date=AS_OF,
        token="tok", project_id="proj",
        list_func=lambda token, project_id, *, cursor=None, timeout=30: {
            "results": [], "next_cursor": "more"
        },
    )
    assert partial["ok"] is False and partial["todoist_read_complete"] is False


def test_update_404_closes_emission_and_resolves_followup(tmp_path):
    """A 404 on the in-place update means the task was deleted in Todoist.
    Instead of failing the same push every day (the fup_352f... loop), the
    emission is closed as deleted_by_user and the linked follow-up is resolved,
    so the item is never retried."""

    import urllib.error

    conn = _db(tmp_path / "f.db")
    fup = capture_followup(conn, "Call the bank", AS_OF)
    key = f"followup:{fup['id']}"
    spy = _Spy()
    _enabled(conn, [{"surface_key": key, "content": "old", "description": "x"}], spy)
    assert list_due_followups(conn, as_of_date=AS_OF)

    def gone(token, path, body, **kwargs):
        raise urllib.error.HTTPError(f"https://api.todoist.com{path}", 404, "Not Found", None, None)

    # Changed content forces the in-place update, which 404s: self-heal.
    r = surface_to_todoist(
        conn,
        [{"surface_key": key, "content": "new", "description": "x"}],
        AS_OF,
        write_enabled=True, token="tok", project_id="proj", send_func=gone,
    )
    assert r["failed"] == 0
    assert r["resolved"] == 1 and r["followups_resolved"] == 1
    row = conn.execute("SELECT status FROM todoist_emissions WHERE surface_key = ?", (key,)).fetchone()
    assert row["status"] == "deleted_by_user"
    assert not list_due_followups(conn, as_of_date=AS_OF)

    # Next daily run: resolved short-circuit, no HTTP retry, ever.
    spy2 = _Spy()
    r2 = _enabled(conn, [{"surface_key": key, "content": "new", "description": "x"}], spy2)
    assert r2["resolved"] == 1 and r2["failed"] == 0
    assert spy2.calls == []


def test_missing_task_id_in_create_response_fails_that_item_only(tmp_path):
    """A malformed-but-200 create (no id) records that item failed and continues
    the batch instead of raising IntegrityError on the NOT-NULL ledger column."""

    conn = _db(tmp_path / "f.db")

    class _NoIdSpy:
        def __init__(self):
            self.calls = []
            self._next = 0

        def __call__(self, token, path, body, **kwargs):
            self.calls.append({"path": path, "body": body})
            if path == "/tasks":  # create
                self._next += 1
                # The first create returns a payload WITHOUT an id (malformed);
                # later creates are well-formed.
                if self._next == 1:
                    return {"url": "https://todoist.com/showTask"}  # no "id"
                return {"id": f"T{self._next}"}
            return {}

    spy = _NoIdSpy()
    items = [
        {"surface_key": "followup:bad", "content": "no id back", "description": "x"},
        {"surface_key": "followup:good", "content": "fine", "description": "y"},
    ]
    r = surface_to_todoist(
        conn, items, AS_OF, write_enabled=True, token="tok", project_id="proj", send_func=spy
    )

    # The bad item is failed; the good one still goes through.
    assert r["failed"] == 1 and r["created"] == 1
    bad = next(i for i in r["items"] if i["surface_key"] == "followup:bad")
    assert bad["action"] == "failed" and "no task id" in bad["reason"]
    good = next(i for i in r["items"] if i["surface_key"] == "followup:good")
    assert good["action"] == "created"

    # The ambiguous bad response keeps durable intent for marker verification;
    # the good item is open normally.
    states = {
        row["surface_key"]: row["status"]
        for row in conn.execute("SELECT surface_key, status FROM todoist_emissions").fetchall()
    }
    assert states == {"followup:bad": "create_pending", "followup:good": "open"}


# --- sync-failed flag (surface_due_items_to_todoist prepends the stale-data item) ---
# When the day's run_background_sync failed, balances are stale. The push tool
# builds items itself, so it (not the caller) prepends ONE "Data sync failed"
# item, keyed by date so a same-day re-run is deduped.


def test_build_sync_failed_item_shape():
    from financial_agent.surface_queue import build_sync_failed_item

    item = build_sync_failed_item("2026-06-24")
    assert item["surface_key"] == "data-sync-failed:2026-06-24"
    assert item["content"] == "Data sync failed - balances stale"
    assert item["priority"] == 4  # highest in Todoist
    assert "did not refresh" in item["description"]


def test_sync_failed_item_prepended_then_pushed_and_deduped(tmp_path):
    """The prepend logic the tool runs, dep-free: prepend the stale-data item to
    the day's built items, push, and it creates once then dedupes on re-run.
    (Mirrors surface_due_items_to_todoist(sync_failed=True) without the MCP layer
    so coverage holds even when the mcp server dep is absent.)"""

    from financial_agent.surface_queue import build_surface_items, build_sync_failed_item

    conn = _db(tmp_path / "f.db")  # empty: no due items, so the prepend is the only item
    spy = _Spy()

    built = build_surface_items(conn, as_of_date=AS_OF)
    items = [build_sync_failed_item(AS_OF), *built]
    assert items[0]["surface_key"] == f"data-sync-failed:{AS_OF}"

    r1 = _enabled(conn, items, spy)
    assert r1["created"] == 1  # the stale-data item created
    created_body = spy.creates[0]["body"]
    assert created_body["priority"] == 4

    # Same-day re-run: the emissions ledger dedupes it, no duplicate task.
    r2 = _enabled(conn, items, spy)
    assert r2["created"] == 0 and r2["skipped"] == 1
    assert len(spy.creates) == 1


def test_surface_tool_prepends_sync_failed_item_when_flag_set(tmp_path, monkeypatch):
    """surface_due_items_to_todoist(today, sync_failed=True) prepends the stale-data
    item. Driven gated-off so no network: the disposition list still names every
    item it would push, and the sync-failed item must lead it."""

    import pytest

    pytest.importorskip("mcp", reason="MCP server deps not installed")
    from financial_agent import server

    db = tmp_path / "f.db"
    conn = _db(db)  # empty app db: no due items to build, so the prepend is the only item
    conn.close()
    promote_release(str(db))

    # Force the gate closed regardless of any ambient .env so the call is hermetic.
    # The tool resolves the gate via todoist_outbox.get_finance_config.
    monkeypatch.setattr(
        tb,
        "get_finance_config",
        lambda **kw: {"todoist_write_enabled": False, "todoist_api_token": None, "todoist_project_id": None},
    )

    result = server.surface_due_items_to_todoist("2026-06-24", db_path=str(db), sync_failed=True)
    keys = [i["surface_key"] for i in result["items"]]
    assert keys[0] == "data-sync-failed:2026-06-24"  # prepended, leads the list

    # Without the flag, the stale-data item is NOT added.
    result_ok = server.surface_due_items_to_todoist("2026-06-24", db_path=str(db))
    assert all(k != "data-sync-failed:2026-06-24" for k in [i["surface_key"] for i in result_ok["items"]])


def test_surface_tool_with_synced_sources_does_not_lock_itself(tmp_path, monkeypatch):
    import pytest

    pytest.importorskip("mcp", reason="MCP server deps not installed")
    from financial_agent import server

    db = tmp_path / "f.db"
    conn = _db(db)
    conn.executescript(
        """
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT
        );
        CREATE TABLE balance_snapshots (
            id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL,
            recorded_at TEXT, source TEXT, balance_date TEXT
        );
        CREATE TABLE sync_runs (
            id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT,
            accounts_seen INT, transactions_inserted INT,
            transactions_updated INT, error TEXT
        );
        CREATE TABLE transactions (
            id TEXT PRIMARY KEY, account_id TEXT, posted TEXT,
            transacted_at TEXT, amount REAL, payee TEXT, description TEXT,
            pending INTEGER, source TEXT
        );
        INSERT INTO accounts VALUES (
            'chk', 'Checking 4321', 'Chase', 'checking', 'USD'
        );
        INSERT INTO balance_snapshots (
            account_id, balance, available, recorded_at, source, balance_date
        ) VALUES (
            'chk', 9000, 9000, '2026-06-24T08:00:00-04:00',
            'simplefin', '2026-06-24'
        );
        INSERT INTO sync_runs (
            started_at, finished_at, mode, accounts_seen,
            transactions_inserted, transactions_updated, error
        ) VALUES (
            '2026-06-24T08:00:00-04:00', '2026-06-24T08:01:00-04:00',
            'i', 1, 0, 0, NULL
        );
        """
    )
    conn.commit()
    conn.close()
    promote_release(str(db))
    monkeypatch.setattr(
        tb,
        "get_finance_config",
        lambda **kw: {
            "todoist_write_enabled": False,
            "todoist_api_token": None,
            "todoist_project_id": None,
        },
    )

    result = server.surface_due_items_to_todoist(AS_OF, db_path=str(db))

    assert result["status"] == "awaiting-integration"


def test_due_date_and_priority_passed_through(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    _enabled(
        conn,
        [{"surface_key": "followup:abc", "content": "x", "description": "y", "due_date": "2026-07-01", "priority": 3}],
        spy,
    )
    body = spy.creates[0]["body"]
    assert body["due_date"] == "2026-07-01" and body["priority"] == 3


def test_real_write_request_uses_v1_endpoint(tmp_path, monkeypatch):
    """The default send_func (the real _write_request) posts to /api/v1/tasks."""
    conn = _db(tmp_path / "f.db")
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": "T1"}'

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp()

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    surface_to_todoist(
        conn,
        [{"surface_key": "followup:abc", "content": "x", "description": "y"}],
        AS_OF,
        write_enabled=True,
        token="tok",
        project_id="proj",
    )
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks"
    assert captured["method"] == "POST"


# --- completion sync (reconcile_todoist_completions) -----------------------
# Closes the re-nag gap: a task the user checks off / deletes in Todoist must
# map back to the ledger so the next surface run does not recreate it.


def _seed_open_emission(conn, surface_key, task_id):
    """Put one open emission row in the ledger directly (no network)."""

    surface_to_todoist(
        conn,
        [{"surface_key": surface_key, "content": "x", "description": "y"}],
        AS_OF,
        write_enabled=True,
        token="tok",
        project_id="proj",
        send_func=lambda token, path, body, **kw: {"id": task_id},
    )


def _reconcile(conn, read_func):
    return reconcile_todoist_completions(
        conn, as_of_date=AS_OF, write_enabled=True, token="tok", read_func=read_func
    )


def test_completed_task_marks_emission_resolved(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_open_emission(conn, "estimate-review:OBL1:c1", "T1")

    # Todoist returns the task with checked=true (user completed it).
    def read(token, task_id):
        return {"id": task_id, "checked": True}

    r = _reconcile(conn, read)
    assert r["resolved"] == 1 and r["still_open"] == 0 and r["failed"] == 0
    row = conn.execute(
        "SELECT status FROM todoist_emissions WHERE surface_key = 'estimate-review:OBL1:c1'"
    ).fetchone()
    assert row["status"] == "completed"


def test_check_suggestion_checkbox_never_counts_as_financial_approval(tmp_path):
    conn = _db(tmp_path / "f.db")
    key = "check-suggestion:rent-check-1233"
    _seed_open_emission(conn, key, "T1")

    reconciled = _reconcile(
        conn, lambda token, task_id: {"id": task_id, "checked": True}
    )

    assert reconciled["resolved"] == 0
    assert reconciled["review_tasks_to_resurface"] == 1
    assert conn.execute(
        "SELECT status FROM todoist_emissions WHERE surface_key = ?", (key,)
    ).fetchone()["status"] == "retired"

    created = surface_to_todoist(
        conn,
        [{"surface_key": key, "content": "Review check match: Rent"}],
        AS_OF,
        write_enabled=True,
        token="tok",
        project_id="proj",
        send_func=lambda token, path, body, **kw: {"id": "T2"},
    )
    assert created["created"] == 1
    assert tuple(conn.execute(
        "SELECT todoist_task_id, status FROM todoist_emissions WHERE surface_key = ?",
        (key,),
    ).fetchone()) == ("T2", "open")


def test_is_completed_alias_also_resolves(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_open_emission(conn, "snapshot-due:ACT-1", "T1")
    r = _reconcile(conn, lambda token, task_id: {"id": task_id, "is_completed": True})
    assert r["resolved"] == 1
    row = conn.execute("SELECT status FROM todoist_emissions WHERE surface_key = 'snapshot-due:ACT-1'").fetchone()
    assert row["status"] == "completed"


def test_deleted_task_404_marks_emission_resolved(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_open_emission(conn, "goal:Roth:behind", "T1")

    # A 404 from Todoist means the task was completed or deleted (left active list).
    def read(token, task_id):
        return TASK_NOT_FOUND

    r = _reconcile(conn, read)
    assert r["resolved"] == 1
    row = conn.execute("SELECT status FROM todoist_emissions WHERE surface_key = 'goal:Roth:behind'").fetchone()
    assert row["status"] == "deleted_by_user"


def test_open_task_left_unchanged(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_open_emission(conn, "followup:keep", "T1")

    # Still active, not checked: nothing changes.
    def read(token, task_id):
        return {"id": task_id, "checked": False}

    r = _reconcile(conn, read)
    assert r["resolved"] == 0 and r["still_open"] == 1
    row = conn.execute("SELECT status FROM todoist_emissions WHERE surface_key = 'followup:keep'").fetchone()
    assert row["status"] == "open"


def test_completing_task_also_resolves_linked_followup(tmp_path):
    conn = _db(tmp_path / "f.db")
    # A real follow-up that is currently due, surfaced under its followup:<id> key.
    fup = capture_followup(conn, "Call the bank", AS_OF)
    key = f"followup:{fup['id']}"
    _seed_open_emission(conn, key, "T1")
    assert list_due_followups(conn, as_of_date=AS_OF)  # due before reconcile

    r = _reconcile(conn, lambda token, task_id: TASK_NOT_FOUND)
    assert r["resolved"] == 1 and r["followups_resolved"] == 1
    # The follow-up is resolved, so it no longer surfaces.
    assert not list_due_followups(conn, as_of_date=AS_OF)


def test_reconcile_skips_non_open_emissions(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_open_emission(conn, "followup:already", "T1")
    mark_emission_status(conn, "followup:already", "completed")

    # An already-resolved emission is not re-checked (it is not 'open').
    calls = []

    def read(token, task_id):
        calls.append(task_id)
        return {"id": task_id, "checked": False}

    r = _reconcile(conn, read)
    assert calls == [] and r["checked"] == 0


def test_reconcile_gated_off_is_noop(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_open_emission(conn, "followup:x", "T1")
    calls = []

    def read(token, task_id):
        calls.append(task_id)
        return TASK_NOT_FOUND

    r = reconcile_todoist_completions(
        conn, as_of_date=AS_OF, write_enabled=False, token="tok", read_func=read
    )
    assert calls == []
    assert r["status"] == "awaiting-integration" and r["resolved"] == 0
    # Ledger untouched.
    row = conn.execute("SELECT status FROM todoist_emissions WHERE surface_key = 'followup:x'").fetchone()
    assert row["status"] == "open"


def test_reconcile_read_failure_recorded_not_raised(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_open_emission(conn, "followup:boom", "T1")

    def read(token, task_id):
        raise RuntimeError("network down")

    r = _reconcile(conn, read)
    assert r["failed"] == 1 and r["resolved"] == 0
    # Still open: a transient read error must not resolve a live item.
    row = conn.execute("SELECT status FROM todoist_emissions WHERE surface_key = 'followup:boom'").fetchone()
    assert row["status"] == "open"


def test_read_task_404_returns_sentinel(tmp_path, monkeypatch):
    """The default read client maps an HTTP 404 to the TASK_NOT_FOUND sentinel."""

    def fake_urlopen(req, timeout=30):
        raise tb.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    assert tb._read_task("tok", "T1") == TASK_NOT_FOUND


def test_read_task_uses_v1_get_endpoint(tmp_path, monkeypatch):
    """The default read client GETs /api/v1/tasks/<id>."""
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": "T1", "checked": true}'

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp()

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    task = tb._read_task("tok", "T1")
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks/T1"
    assert captured["method"] == "GET"
    assert task["checked"] is True
