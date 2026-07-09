"""Tests for the read-only whole-project Todoist LIST tool (``#10``).

``list_todoist_project_for_db`` is a thin reader over
``reconcile_todoist_project_for_db``: it must LIST + classify the project
IDENTICALLY to a dry-run reconcile, while having NO path to delete a task. The
highest-value guard here is structural: even with live write-back ON and a
project full of deletable tasks (fa-auto orphans, stale-applied, duplicates),
the read tool deletes nothing and never reaches either delete entry point.

All HTTP is injected via a fake ``list_func``; no live network call is made.
"""

import inspect
import sqlite3

import pytest

import financial_agent.todoist_outbox as tb
from financial_agent.schema import ensure_app_schema
from financial_agent.todoist_outbox import (
    FA_AUTO_LABEL,
    list_today_tasks_all_projects_for_db,
    list_todoist_project_for_db,
    reconcile_todoist_project_for_db,
    surface_marker,
)

AS_OF = "2026-06-25"
_NOW = "2026-06-01T00:00:00+00:00"


def test_list_today_tasks_all_projects_surfaces_finance_items_filed_elsewhere(tmp_path):
    # IMP-20260708-6: list_todoist_project reads only the Finance project, so a
    # finance task filed under Personal/other projects was invisible. This
    # cross-project read must return today + overdue tasks from every project.
    conn = _db(tmp_path / "cross.sqlite")
    page = {
        "results": [
            {"id": "1", "content": "Pay Amex", "project_id": "PERSONAL", "due": {"date": "2026-06-25"}, "labels": []},
            {"id": "2", "content": "Board task", "project_id": "FIN", "due": {"date": "2026-06-25"}, "labels": [FA_AUTO_LABEL]},
            {"id": "3", "content": "Future thing", "project_id": "PERSONAL", "due": {"date": "2026-06-30"}, "labels": []},
            {"id": "4", "content": "Someday, no due", "project_id": "PERSONAL", "due": None, "labels": []},
            {"id": "5", "content": "Overdue water bill", "project_id": "WORK", "due": {"date": "2026-06-20"}, "labels": []},
        ],
        "next_cursor": None,
    }

    def fake_list(token, *, cursor=None, timeout=30):
        assert token == "tok"
        return page

    result = list_today_tasks_all_projects_for_db(
        conn, as_of_date=AS_OF, token="tok", finance_project_id="FIN", list_func=fake_list,
    )

    assert result["status"] == "ok"
    by_id = {t["id"]: t for t in result["tasks"]}
    # due<=as_of only: future (#3) and no-due (#4) excluded; overdue (#5) included.
    assert set(by_id) == {"1", "2", "5"}
    assert result["count"] == 3
    assert by_id["1"]["is_finance_project"] is False
    assert by_id["2"]["is_finance_project"] is True
    assert by_id["5"]["due_date"] == "2026-06-20"


def test_list_today_tasks_all_projects_no_token(tmp_path):
    conn = _db(tmp_path / "notoken.sqlite")
    result = list_today_tasks_all_projects_for_db(
        conn, as_of_date=AS_OF, token="", finance_project_id="FIN",
    )
    assert result["status"] == "no_token"
    assert result["tasks"] == []


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def _task(tid, content="task", *, marker=None, fa_auto=False, description="", labels=None):
    desc = description
    if marker:
        desc = (desc + "\n\n" + surface_marker(marker)).strip()
    tags = list(labels) if labels else []
    if fa_auto:
        tags.append(FA_AUTO_LABEL)
    return {"id": str(tid), "content": content, "description": desc, "labels": tags}


class _ListSpy:
    """Returns canned pages and records the cursor passed on each call."""

    def __init__(self, pages):
        self.pages = pages
        self.cursors_seen = []
        self._i = 0

    def __call__(self, token, project_id, *, cursor=None, timeout=30):
        self.cursors_seen.append(cursor)
        results, nxt = self.pages[self._i]
        self._i += 1
        return {"results": results, "next_cursor": nxt}


def _single_page(tasks):
    return _ListSpy([(tasks, None)])


class _DeleteSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, token, task_id, **kwargs):
        self.calls.append(task_id)
        return True


def _seed_emission(conn, surface_key, task_id, *, status="open", content_hash="h"):
    conn.execute(
        "INSERT INTO todoist_emissions (surface_key, todoist_task_id, status, content_hash, created_at, last_seen) "
        "VALUES (?,?,?,?,?,?)",
        (surface_key, str(task_id), status, content_hash, _NOW, _NOW),
    )
    conn.commit()


def _seed_candidate(conn, cid, display_name, status, *, merchant_key=None):
    conn.execute(
        "INSERT INTO charge_onboarding_candidates "
        "(id, merchant_key, display_name, direction, status, evidence_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (cid, merchant_key or display_name.lower(), display_name, "outflow", status, 1, _NOW, _NOW),
    )
    conn.commit()


def _mixed_project_tasks():
    """One task per classification, including every deletable kind."""

    return [
        _task("MG", "Pay OB1", marker="obligation-due:OB1:2026-06-25"),  # managed
        _task("ST", "Onboard charge: Acme not modeled"),  # stale_applied (candidate applied)
        _task("FA", "lost task", fa_auto=True),  # fa_auto_orphan
        _task("601", "dup body", fa_auto=True),  # duplicate (survivor is numeric-min 601)
        _task("602", "dup body", fa_auto=True),
        _task("KP", "Pay the plumber"),  # kept (no marker, no fa-auto)
    ]


def _seed_mixed(conn):
    _seed_emission(conn, "obligation-due:OB1:2026-06-25", "MG")
    _seed_candidate(conn, "cand_acme", "Acme", "applied")


# --- parity: LIST == reconcile dry-run -------------------------------------


def test_lists_and_classifies_identically_to_reconcile_dry_run(tmp_path):
    """The read tool returns the EXACT report a dry-run reconcile produces over the
    same project + ledger: same counts, same per-task classifications, same
    ``would_delete`` actions. The only contract it adds is 'never delete'."""

    conn = _db(tmp_path / "f.db")
    _seed_mixed(conn)

    reconcile = reconcile_todoist_project_for_db(
        conn,
        as_of_date=AS_OF,
        apply=False,
        write_enabled=True,
        token="tok",
        project_id="proj",
        list_func=_single_page(_mixed_project_tasks()),
        delete_func=_DeleteSpy(),  # present but never called on a dry run
    )

    listed = list_todoist_project_for_db(
        conn,
        as_of_date=AS_OF,
        write_enabled=True,
        token="tok",
        project_id="proj",
        list_func=_single_page(_mixed_project_tasks()),
    )

    # Byte-for-byte the same report shape (no nondeterministic fields).
    assert listed == reconcile
    # And it actually classified the full mix, including the deletable kinds.
    assert listed["counts"]["managed"] == 1
    assert listed["counts"]["stale_applied"] == 1
    assert listed["counts"]["fa_auto_orphan"] == 1
    assert listed["counts"]["duplicate"] == 1
    # The duplicate's surviving copy (numeric-min 601) is itself classified kept,
    # alongside the plain user task -> two kept.
    assert listed["counts"]["kept"] == 2


# --- structural: no path to delete -----------------------------------------


def test_wrapper_exposes_no_apply_or_delete_parameter():
    """The read surface cannot be asked to mutate: there is no ``apply`` toggle and
    no ``delete_func`` seam to inject a deleter through."""

    params = inspect.signature(list_todoist_project_for_db).parameters
    assert "apply" not in params
    assert "delete_func" not in params

    # Trying to pass an apply-like input is a hard TypeError, not a silent apply.
    with pytest.raises(TypeError):
        list_todoist_project_for_db(  # type: ignore[call-arg]
            None, as_of_date=AS_OF, apply=True
        )


def test_never_deletes_even_live_with_deletable_tasks(tmp_path, monkeypatch):
    """THE guard: with live write-back ON and a project full of deletable tasks,
    the read tool deletes nothing. Both delete entry points are replaced with
    spies (the real ``_delete_request`` and the structural ``_forbidden_delete``
    guard) and neither is ever called; every action count stays zero and the
    deletable tasks are reported ``would_delete``, never ``deleted``."""

    conn = _db(tmp_path / "f.db")
    _seed_mixed(conn)

    real_delete_spy = _DeleteSpy()
    forbidden_spy = _DeleteSpy()
    monkeypatch.setattr(tb, "_delete_request", real_delete_spy)
    monkeypatch.setattr(tb, "_forbidden_delete", forbidden_spy)

    report = list_todoist_project_for_db(
        conn,
        as_of_date=AS_OF,
        write_enabled=True,  # live; reconcile-apply WOULD delete here
        token="tok",
        project_id="proj",
        list_func=_single_page(_mixed_project_tasks()),
    )

    # No delete reached, by either route.
    assert real_delete_spy.calls == []
    assert forbidden_spy.calls == []
    # Read-only report: applied false, all actions zero.
    assert report["applied"] is False
    assert report["actions"] == {
        "deleted": 0,
        "ledger_resolved": 0,
        "skipped_not_live": 0,
        "failed": 0,
    }
    # Deletable tasks are surfaced as would_delete, NOT deleted.
    by_id = {t["task_id"]: t for t in report["tasks"]}
    assert by_id["FA"]["action"] == "would_delete"
    assert by_id["602"]["action"] == "would_delete"
    assert by_id["ST"]["action"] == "would_delete"


def test_reconcile_apply_would_delete_the_same_tasks(tmp_path):
    """Control: prove the deletable tasks are genuinely deletable. A live
    apply=True reconcile over the SAME project DOES delete them -- so the read
    tool's zero-delete result above is a real suppression, not an empty input."""

    conn = _db(tmp_path / "f.db")
    _seed_mixed(conn)
    delete = _DeleteSpy()
    report = reconcile_todoist_project_for_db(
        conn,
        as_of_date=AS_OF,
        apply=True,
        write_enabled=True,
        token="tok",
        project_id="proj",
        list_func=_single_page(_mixed_project_tasks()),
        delete_func=delete,
    )
    assert sorted(delete.calls) == ["602", "FA", "ST"]
    assert report["actions"]["deleted"] == 3


# --- gating shape ----------------------------------------------------------


def test_no_token_returns_awaiting_integration_with_zero_actions(tmp_path):
    """With no token the read tool reports awaiting-integration: applied false and
    every action count zero (the documented no-token shape)."""

    conn = _db(tmp_path / "f.db")
    report = list_todoist_project_for_db(
        conn,
        as_of_date=AS_OF,
        write_enabled=False,
        token=None,
        project_id=None,
        list_func=_single_page([_task("X", "anything")]),
    )
    assert report["status"] == "awaiting-integration"
    assert report["applied"] is False
    assert report["actions"] == {
        "deleted": 0,
        "ledger_resolved": 0,
        "skipped_not_live": 0,
        "failed": 0,
    }


# --- new fields: due_date + labels + description -----------------------------


def test_tasks_expose_due_date_labels_and_description(tmp_path):
    """Every ``tasks[]`` entry carries the task's ``due_date`` (the Todoist
    ``due.date`` string, or None when the task has no due), its ``labels``
    (the Todoist label names, [] when none), and its raw ``description``
    (or "" when blank), so callers never need the raw Todoist API for them."""

    conn = _db(tmp_path / "f.db")

    due_task = _task("DUE", "Pay rent", description="Send to landlord by ACH",
                     labels=["bills", "home"])
    due_task["due"] = {"date": "2026-07-23"}

    no_due_task = _task("NODUE", "Call the plumber", description="")
    no_due_task["due"] = None

    report = list_todoist_project_for_db(
        conn,
        as_of_date=AS_OF,
        write_enabled=False,
        token="tok",
        project_id="proj",
        list_func=_single_page([due_task, no_due_task]),
    )

    by_id = {t["task_id"]: t for t in report["tasks"]}

    # Every entry exposes all three fields.
    for entry in report["tasks"]:
        assert "due_date" in entry
        assert "labels" in entry
        assert "description" in entry

    assert by_id["DUE"]["due_date"] == "2026-07-23"
    assert by_id["DUE"]["labels"] == ["bills", "home"]
    assert by_id["DUE"]["description"] == "Send to landlord by ACH"

    assert by_id["NODUE"]["due_date"] is None
    assert by_id["NODUE"]["labels"] == []
    assert by_id["NODUE"]["description"] == ""
