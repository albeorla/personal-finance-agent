"""Tests for idempotent Todoist surfacing (the todoist_emissions ledger).

The daily job pushes due items to Todoist and must NEVER duplicate: not on a
re-run, not across days, and not when the user already made the task by hand.
These tests drive surface_to_todoist / reconcile_emission with a mock send_func
so no real network call is ever made.
"""

import sqlite3

import financial_agent.todoist_outbox as tb
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


def test_completed_task_resolves_source_no_recreate(tmp_path):
    conn = _db(tmp_path / "f.db")
    spy = _Spy()
    key = "goal:Emergency Fund:behind"
    _enabled(conn, [{"surface_key": key, "content": "goal behind", "description": "x"}], spy)

    # User completes the task in Todoist; the ledger records it.
    mark_emission_status(conn, key, "completed")

    # Even with changed content, do not recreate or update; source is resolved.
    r = _enabled(conn, [{"surface_key": key, "content": "changed", "description": "y"}], spy)
    assert r["resolved"] == 1 and r["created"] == 0 and r["updated"] == 0
    assert len(spy.creates) == 1  # still only the original create
    assert not spy.updates


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
    # Ledger not written on failure, so a later run cleanly retries the create.
    n = conn.execute("SELECT COUNT(*) FROM todoist_emissions").fetchone()[0]
    assert n == 0


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

    # No ledger row for the bad item (so a later run retries the create cleanly);
    # a row for the good one.
    keys = {row["surface_key"] for row in conn.execute("SELECT surface_key FROM todoist_emissions").fetchall()}
    assert keys == {"followup:good"}


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
