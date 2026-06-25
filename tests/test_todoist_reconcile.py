"""Tests for the whole-project Todoist reconcile + cleanup pass (spec sections 1
and 3). All HTTP is injected via fake ``list_func`` / ``delete_func``; no live
network call is ever made. The single highest-value guard is
``test_kept_tasks_never_deleted``: the ritual reminders and every hand-made task
carry no ``[fa:]`` marker and no ``fa-auto`` label, so "no marker" must NEVER be
a delete signal.

Assertions in each test lead with the BEHAVIORAL guarantees (was delete_func
called, what do the counts/actions/ledger say) and finish with the per-task
``tasks[]`` report contract, so a failure pinpoints which contract broke.
"""

import pathlib
import sqlite3

import pytest

import financial_agent.todoist_outbox as tb
from financial_agent.schema import ensure_app_schema
from financial_agent.todoist_outbox import (
    FA_AUTO_LABEL,
    MAX_DELETES_PER_RUN,
    MAX_LIST_PAGES,
    content_hash_for,
    reconcile_todoist_project_for_db,
    surface_marker,
)

AS_OF = "2026-06-25"
_NOW = "2026-06-01T00:00:00+00:00"


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def _task(tid, content="task", *, marker=None, fa_auto=False, description="", labels=None):
    """Build a Todoist v1 list task object as the LIST endpoint returns it."""

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
        # pages: list of (results_list, next_cursor)
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
    """Records every delete; optionally raises for selected task ids."""

    def __init__(self, *, raise_on=None, raise_exc=None):
        self.calls = []
        self.raise_on = set(raise_on or ())
        self.raise_exc = raise_exc or RuntimeError("boom")

    def __call__(self, token, task_id, **kwargs):
        self.calls.append(task_id)
        if task_id in self.raise_on:
            raise self.raise_exc
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


def _reconcile(conn, list_func, *, apply=False, delete_func=None, write_enabled=True, token="tok", project_id="proj"):
    kwargs = dict(
        as_of_date=AS_OF,
        apply=apply,
        write_enabled=write_enabled,
        token=token,
        project_id=project_id,
        list_func=list_func,
    )
    if delete_func is not None:
        kwargs["delete_func"] = delete_func
    return reconcile_todoist_project_for_db(conn, **kwargs)


def _emission_status(conn, surface_key):
    row = conn.execute(
        "SELECT status FROM todoist_emissions WHERE surface_key = ?", (surface_key,)
    ).fetchone()
    return row["status"] if row else None


def _by_id(report, tid):
    return next(t for t in report["tasks"] if t["task_id"] == str(tid))


# --- pagination ------------------------------------------------------------


def test_list_paginates(tmp_path):
    conn = _db(tmp_path / "f.db")
    lst = _ListSpy([([_task("1", "a")], "c1"), ([_task("2", "b")], None)])
    report = _reconcile(conn, lst, apply=True, delete_func=_DeleteSpy())
    # All pages drained, every task classified, cursor threaded through.
    assert report["listed"] == 2
    assert lst.cursors_seen == [None, "c1"]
    # tasks[] report contract
    assert {t["task_id"] for t in report["tasks"]} == {"1", "2"}


# --- managed ---------------------------------------------------------------


def test_classify_managed(tmp_path):
    conn = _db(tmp_path / "f.db")
    key = "obligation-due:OB1:2026-06-25"
    _seed_emission(conn, key, "T100")
    delete = _DeleteSpy()
    report = _reconcile(conn, _single_page([_task("T100", "Pay OB1", marker=key)]), apply=True, delete_func=delete)
    assert delete.calls == []
    assert report["counts"]["managed"] == 1
    assert report["actions"]["deleted"] == 0
    # tasks[] report contract
    row = _by_id(report, "T100")
    assert row["classification"] == "managed" and row["action"] == "kept"


# --- HIGHEST VALUE: kept tasks are never deleted ---------------------------


def test_kept_tasks_never_deleted(tmp_path):
    """A marker-less, fa-auto-less manual task (as create_todoist_task writes) AND
    a cashflow-labelled ritual task (as send_review_batch writes) must BOTH
    classify ``kept`` and never reach delete_func, even in live apply mode. This
    directly guards the data-loss path: 'no marker' is NOT a delete signal."""

    conn = _db(tmp_path / "f.db")
    manual = _task("M1", "Pay the plumber")  # no marker, no fa-auto
    ritual = _task("R1", "Finance review 2026-06-25", labels=["cashflow"])  # ritual reminder
    delete = _DeleteSpy()
    report = _reconcile(conn, _single_page([manual, ritual]), apply=True, delete_func=delete)

    # THE guard: neither task is ever passed to delete_func, even in live apply.
    assert delete.calls == []
    assert report["actions"]["deleted"] == 0
    assert report["counts"]["kept"] == 2
    # tasks[] report contract
    assert _by_id(report, "M1")["classification"] == "kept"
    assert _by_id(report, "R1")["classification"] == "kept"
    assert _by_id(report, "M1")["action"] == "kept"
    assert _by_id(report, "R1")["action"] == "kept"


# --- fa-auto orphan (delete rule a) ----------------------------------------


def test_classify_fa_auto_orphan(tmp_path):
    conn = _db(tmp_path / "f.db")

    # Dry-run: nothing touched.
    dry_delete = _DeleteSpy()
    dry = _reconcile(conn, _single_page([_task("T1", "lost task", fa_auto=True)]), apply=False, delete_func=dry_delete)
    assert dry_delete.calls == []
    assert dry["actions"]["deleted"] == 0
    assert dry["counts"]["fa_auto_orphan"] == 1
    assert _by_id(dry, "T1")["classification"] == "fa_auto_orphan"
    assert _by_id(dry, "T1")["action"] == "would_delete"

    # Apply (live): deleted exactly once.
    delete = _DeleteSpy()
    applied = _reconcile(conn, _single_page([_task("T1", "lost task", fa_auto=True)]), apply=True, delete_func=delete)
    assert delete.calls == ["T1"]
    assert applied["actions"]["deleted"] == 1
    assert _by_id(applied, "T1")["action"] == "deleted"


def test_no_fa_auto_no_pattern_is_kept(tmp_path):
    conn = _db(tmp_path / "f.db")
    delete = _DeleteSpy()
    report = _reconcile(conn, _single_page([_task("U1", "buy milk")]), apply=True, delete_func=delete)
    assert delete.calls == []
    assert report["actions"]["deleted"] == 0
    assert report["counts"]["kept"] == 1
    assert _by_id(report, "U1")["classification"] == "kept"


# --- stale-applied (delete rule b) -----------------------------------------


def test_classify_stale_applied(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_candidate(conn, "cand_acme", "Acme", "applied")
    delete = _DeleteSpy()
    report = _reconcile(
        conn, _single_page([_task("S1", "Onboard charge: Acme not modeled")]), apply=True, delete_func=delete
    )
    assert delete.calls == ["S1"]
    assert report["counts"]["stale_applied"] == 1
    # The candidate row itself is untouched by cleanup.
    assert conn.execute(
        "SELECT status FROM charge_onboarding_candidates WHERE id = 'cand_acme'"
    ).fetchone()["status"] == "applied"
    # tasks[] report contract
    row = _by_id(report, "S1")
    assert row["classification"] == "stale_applied"
    assert row["candidate_id"] == "cand_acme" and row["candidate_status"] == "applied"
    assert row["match_confidence"] == "heuristic"
    assert row["action"] == "deleted"


def test_stale_applied_unmatched_is_needs_review(tmp_path):
    conn = _db(tmp_path / "f.db")
    # Legacy prefix but no candidate matches the parsed name -> never auto-delete.
    delete = _DeleteSpy()
    report = _reconcile(
        conn, _single_page([_task("S2", "Onboard charge: Ghost not modeled")]), apply=True, delete_func=delete
    )
    assert delete.calls == []
    assert report["counts"]["needs_review"] == 1
    assert report["counts"]["stale_applied"] == 1
    # tasks[] report contract
    row = _by_id(report, "S2")
    assert row["classification"] == "stale_applied"
    assert row["candidate_id"] is None
    assert row["action"] == "needs_review"


# --- duplicates (delete rule c) --------------------------------------------


def test_classify_duplicate_numeric_tiebreak(tmp_path):
    """Three fa-auto copies share one content hash; survivor is the NUMERICALLY
    smallest id ('99'), not the lexical min ('100'). Apply deletes the rest."""

    conn = _db(tmp_path / "f.db")
    tasks = [
        _task("100", "dup body", fa_auto=True),
        _task("99", "dup body", fa_auto=True),
        _task("1000", "dup body", fa_auto=True),
    ]
    delete = _DeleteSpy()
    report = _reconcile(conn, _single_page(tasks), apply=True, delete_func=delete)
    # 99 (numeric min) is kept; the lexical-min "100" and "1000" are deleted.
    assert sorted(delete.calls) == ["100", "1000"]
    assert report["actions"]["deleted"] == 2
    # tasks[] report contract
    assert _by_id(report, "99")["action"] == "kept"
    assert _by_id(report, "99")["classification"] == "kept"
    assert _by_id(report, "100")["classification"] == "duplicate"
    assert _by_id(report, "1000")["classification"] == "duplicate"


def test_duplicate_keeps_emission_match(tmp_path):
    """When one copy matches an open emission id it survives regardless of numeric
    order; the others are deleted."""

    conn = _db(tmp_path / "f.db")
    key = "obligation-due:OB2:2026-06-25"
    _seed_emission(conn, key, "500")  # the managed survivor (not the numeric min)
    tasks = [
        _task("100", "x", marker=key),
        _task("500", "x", marker=key),
        _task("999", "x", marker=key),
    ]
    delete = _DeleteSpy()
    report = _reconcile(conn, _single_page(tasks), apply=True, delete_func=delete)
    assert sorted(delete.calls) == ["100", "999"]
    assert "500" not in delete.calls
    assert report["actions"]["deleted"] == 2
    # tasks[] report contract
    assert _by_id(report, "500")["classification"] == "managed"


def test_duplicate_of_kept_survivor_not_deleted(tmp_path):
    """Duplicates whose survivor is a kept user task (no fa-auto, no marker) are
    themselves kept -- a duplicate is only deletable when its survivor is managed
    or fa-auto."""

    conn = _db(tmp_path / "f.db")
    tasks = [
        _task("10", "same user task"),
        _task("20", "same user task"),
    ]
    delete = _DeleteSpy()
    report = _reconcile(conn, _single_page(tasks), apply=True, delete_func=delete)
    assert delete.calls == []
    assert report["actions"]["deleted"] == 0
    # tasks[] report contract
    assert _by_id(report, "10")["classification"] == "kept"
    assert _by_id(report, "20")["classification"] == "kept"


# --- ledger orphan ---------------------------------------------------------


def test_ledger_orphan_resolved_retired(tmp_path):
    """An open emission whose task id is absent from a fully-drained, non-truncated
    LIST is resolved to 'retired' (NOT deleted_by_user), with no delete_func call."""

    conn = _db(tmp_path / "f.db")
    key = "obligation-due:OB3:2026-06-25"
    _seed_emission(conn, key, "GONE")
    delete = _DeleteSpy()
    report = _reconcile(conn, _single_page([_task("K1", "kept task")]), apply=True, delete_func=delete)
    assert report["ledger_findings"]["ledger_orphan"] == 1
    assert report["actions"]["ledger_resolved"] == 1
    assert delete.calls == []  # task already gone; no HTTP delete
    assert _emission_status(conn, key) == "retired"


# --- truncation / list-failure force report-only ---------------------------


def test_apply_blocked_when_truncated(tmp_path):
    """Page cap hit => truncated => report-only: zero deletes, zero ledger
    resolutions, status 'truncated', applied False, report still populated."""

    conn = _db(tmp_path / "f.db")
    key = "obligation-due:OB4:2026-06-25"
    _seed_emission(conn, key, "GONE")  # would-be ledger orphan, must NOT resolve

    class _Infinite:
        def __init__(self):
            self.calls = 0

        def __call__(self, token, project_id, *, cursor=None, timeout=30):
            self.calls += 1
            return {"results": [_task(f"T{self.calls}", f"orphan {self.calls}", fa_auto=True)], "next_cursor": "more"}

    lst = _Infinite()
    delete = _DeleteSpy()
    report = _reconcile(conn, lst, apply=True, delete_func=delete)
    assert lst.calls == MAX_LIST_PAGES  # stopped at the cap
    assert report["truncated"] is True and report["status"] == "truncated"
    assert report["applied"] is False
    assert delete.calls == []
    assert report["actions"]["deleted"] == 0 and report["actions"]["ledger_resolved"] == 0
    assert _emission_status(conn, key) == "open"  # ledger orphan NOT resolved
    assert report["listed"] > 0  # report still populated


def test_apply_blocked_when_list_page_fails(tmp_path):
    """A failed LIST page makes the view partial => same report-only behavior as
    truncation: no deletes, no ledger resolution."""

    conn = _db(tmp_path / "f.db")
    key = "obligation-due:OB5:2026-06-25"
    _seed_emission(conn, key, "GONE")

    class _FailSecondPage:
        def __init__(self):
            self.calls = 0

        def __call__(self, token, project_id, *, cursor=None, timeout=30):
            self.calls += 1
            if self.calls == 1:
                return {"results": [_task("T1", fa_auto=True)], "next_cursor": "c2"}
            raise RuntimeError("list page request failed")

    delete = _DeleteSpy()
    report = _reconcile(conn, _FailSecondPage(), apply=True, delete_func=delete)
    assert report["truncated"] is True and report["status"] == "truncated"
    assert report["applied"] is False
    assert delete.calls == []
    assert report["actions"]["deleted"] == 0 and report["actions"]["ledger_resolved"] == 0
    assert _emission_status(conn, key) == "open"


# --- count invariant -------------------------------------------------------


def test_report_count_invariant(tmp_path):
    """listed == managed + stale_applied + duplicate + fa_auto_orphan + kept, and
    ledger_orphan lives under ledger_findings, not counts."""

    conn = _db(tmp_path / "f.db")
    managed_key = "obligation-due:OB6:2026-06-25"
    _seed_emission(conn, managed_key, "MG")
    _seed_candidate(conn, "cand_x", "Xco", "applied")
    _seed_emission(conn, "obligation-due:OB7:2026-06-25", "ABSENT")  # ledger orphan
    tasks = [
        _task("MG", "managed task", marker=managed_key),
        _task("ST", "Onboard charge: Xco not modeled"),
        _task("FA", "orphan", fa_auto=True),
        _task("KP", "user task"),
        _task("601", "dup", fa_auto=True),
        _task("602", "dup", fa_auto=True),
    ]
    report = _reconcile(conn, _single_page(tasks), apply=False, delete_func=_DeleteSpy())
    c = report["counts"]
    assert report["listed"] == c["managed"] + c["stale_applied"] + c["duplicate"] + c["fa_auto_orphan"] + c["kept"]
    assert "ledger_orphan" not in report["counts"]
    assert report["ledger_findings"]["ledger_orphan"] == 1


# --- gating: dry-run default + apply requires live -------------------------


def test_dry_run_default_no_writes(tmp_path):
    """apply defaults to False => delete_func never called, emissions unchanged,
    applied False, deletable tasks reported as would_delete."""

    conn = _db(tmp_path / "f.db")
    delete = _DeleteSpy()
    report = reconcile_todoist_project_for_db(
        conn,
        as_of_date=AS_OF,
        write_enabled=True,
        token="tok",
        project_id="proj",
        list_func=_single_page([_task("T1", "orphan", fa_auto=True)]),
        delete_func=delete,
    )
    assert report["applied"] is False
    assert delete.calls == []
    assert report["actions"]["deleted"] == 0
    assert _by_id(report, "T1")["action"] == "would_delete"


def test_apply_requires_live(tmp_path):
    """apply=True but write_enabled=False => awaiting-integration: no delete, no
    ledger write, report still populated."""

    conn = _db(tmp_path / "f.db")
    delete = _DeleteSpy()
    report = _reconcile(
        conn,
        _single_page([_task("T1", "orphan", fa_auto=True)]),
        apply=True,
        delete_func=delete,
        write_enabled=False,
    )
    assert report["status"] == "awaiting-integration"
    assert report["reason"] == "awaiting-integration"
    assert report["applied"] is False
    assert delete.calls == []
    assert report["counts"]["fa_auto_orphan"] == 1  # report populated
    assert report["actions"]["skipped_not_live"] == 1


# --- delete helper semantics: 404 idempotent + 429 backoff -----------------


def test_delete_404_idempotent(tmp_path, monkeypatch):
    """_delete_request maps a 404 to success (already gone), and a deletable task
    using such a delete_func is counted deleted."""

    def fake_urlopen(req, timeout=30):
        raise tb.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    assert tb._delete_request("tok", "T1") is True

    conn = _db(tmp_path / "f.db")
    report = _reconcile(
        conn,
        _single_page([_task("T1", "orphan", fa_auto=True)]),
        apply=True,
        delete_func=lambda token, task_id, **kw: True,  # 404-as-success shape
    )
    assert report["actions"]["deleted"] == 1
    assert _by_id(report, "T1")["action"] == "deleted"


def test_delete_429_backoff(monkeypatch):
    """_delete_request retries after a 429 (honoring Retry-After) and then
    succeeds, without surfacing a failure."""

    sleeps = []
    monkeypatch.setattr(tb.time, "sleep", lambda s: sleeps.append(s))

    state = {"calls": 0}

    class _OkResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout=30):
        state["calls"] += 1
        if state["calls"] == 1:
            raise tb.urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "0"}, None)
        return _OkResp()

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    assert tb._delete_request("tok", "T1") is True
    assert state["calls"] == 2  # one retry
    assert sleeps == [0.0]  # honored Retry-After: 0


# --- delete ceiling + isolated failure -------------------------------------


def test_delete_ceiling_truncates(tmp_path):
    """More deletable tasks than MAX_DELETES_PER_RUN: deletes stop at the ceiling
    and truncated_deletes is set."""

    conn = _db(tmp_path / "f.db")
    tasks = [_task(f"T{i}", f"orphan {i}", fa_auto=True) for i in range(MAX_DELETES_PER_RUN + 1)]
    delete = _DeleteSpy()
    report = _reconcile(conn, _single_page(tasks), apply=True, delete_func=delete)
    assert report["actions"]["deleted"] == MAX_DELETES_PER_RUN
    assert len(delete.calls) == MAX_DELETES_PER_RUN
    assert report["truncated_deletes"] is True


def test_delete_failure_isolated(tmp_path):
    """One delete raising (post-retry) is counted failed; the others still
    process."""

    conn = _db(tmp_path / "f.db")
    tasks = [_task("T1", "orphan a", fa_auto=True), _task("T2", "orphan b", fa_auto=True)]
    delete = _DeleteSpy(raise_on=["T1"])
    report = _reconcile(conn, _single_page(tasks), apply=True, delete_func=delete)
    assert sorted(delete.calls) == ["T1", "T2"]  # both attempted
    assert report["actions"]["failed"] == 1
    assert report["actions"]["deleted"] == 1
    # tasks[] report contract
    assert _by_id(report, "T1")["action"] == "failed"
    assert _by_id(report, "T2")["action"] == "deleted"


# --- section 3: v1 endpoint guards -----------------------------------------


def test_no_v2_rest_endpoints():
    """CI guard: no deprecated rest/v2 endpoint string anywhere under src/."""

    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "rest/v2" in text or "api.todoist.com/rest" in text:
            offenders.append(str(py))
    assert offenders == [], f"deprecated REST endpoints found in: {offenders}"


def test_list_request_parses_v1_pagination(monkeypatch):
    """The transport helper parses the v1 {results,next_cursor} envelope unchanged
    and builds the project_id + cursor query."""

    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"results": [{"id": "1"}], "next_cursor": "c2"}'

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp()

    monkeypatch.setattr(tb.urllib.request, "urlopen", fake_urlopen)
    out = tb._list_tasks_request("tok", "proj", cursor="c2")
    assert out == {"results": [{"id": "1"}], "next_cursor": "c2"}
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.todoist.com/api/v1/tasks?project_id=proj&cursor=c2"
