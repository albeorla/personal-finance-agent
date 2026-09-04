"""Hermetic tests for the Todoist retire/cleanup-at-source flow (spec section 2)
and the single onboarding-digest surface item (spec section 4).

No live HTTP: every Todoist call is injected via fake ``send_func`` / ``delete_func``
mirroring the existing ledger tests (test_todoist_emissions.py). The two highest-
value guards here are the resurrection contrast in
``test_retire_then_recreate_resurfaces`` (a ``retired`` ledger row must NOT
suppress recreation, while ``deleted_by_user`` suppresses matching evidence) and the singleton digest
round-trip in ``test_digest_retire_then_resurface``.
"""

import sqlite3

from financial_agent.release_gate import promote_release
from financial_agent.schema import ensure_app_schema
from financial_agent.server import (
    record_charge_onboarding_decision,
    record_charge_onboarding_decisions,
)
from financial_agent.surface_queue import build_surface_items
from financial_agent.todoist_outbox import (
    content_hash_for,
    request_emission_retire,
    request_emission_retire_prefix,
    surface_to_todoist,
)

AS_OF = "2026-06-25"
_NOW = "2026-06-01T00:00:00+00:00"


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def _promote_server_db(path, conn):
    conn.commit()
    conn.close()
    promote_release(str(path))


def _seed_emission(conn, surface_key, task_id, *, status="open", content_hash="h", retire_at=None):
    conn.execute(
        "INSERT INTO todoist_emissions "
        "(surface_key, todoist_task_id, status, content_hash, created_at, last_seen, retire_requested_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (surface_key, str(task_id), status, content_hash, _NOW, _NOW, retire_at),
    )
    conn.commit()


def _seed_candidate(conn, cid, display_name, *, status="discovered", priority=1.0, existing_obligation_id=None):
    conn.execute(
        "INSERT INTO charge_onboarding_candidates "
        "(id, merchant_key, display_name, direction, status, priority_score, "
        " evidence_count, existing_obligation_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            cid, display_name.lower().replace(" ", "_"), display_name, "outflow",
            status, priority, 1, existing_obligation_id, _NOW, _NOW,
        ),
    )
    conn.commit()


def _emission(conn, surface_key):
    return conn.execute(
        "SELECT status, retire_requested_at, todoist_task_id FROM todoist_emissions WHERE surface_key = ?",
        (surface_key,),
    ).fetchone()


class _Send:
    """Records create/update HTTP sends; assigns incrementing task ids on create."""

    def __init__(self):
        self.calls = []
        self._next = 0

    def __call__(self, token, path, body, **kwargs):
        self.calls.append(path)
        if path == "/tasks":
            self._next += 1
            return {"id": f"NEW{self._next}", "url": "https://todoist.com/x"}
        return {}


class _Delete:
    """Records every delete; returns True (success / idempotent 404)."""

    def __init__(self):
        self.calls = []

    def __call__(self, token, task_id, **kwargs):
        self.calls.append(task_id)
        return True


def _live(conn, items, send, delete):
    from financial_agent.surface_queue import build_surface_retire_keys

    retire_keys = build_surface_retire_keys(conn, as_of_date=AS_OF)
    return surface_to_todoist(
        conn, items, AS_OF,
        write_enabled=True, token="tok", project_id="proj",
        send_func=send, delete_func=delete, retire_keys=retire_keys,
    )


# --- section 2: retire helpers ---------------------------------------------


def test_request_emission_retire_sets_flag(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_emission(conn, "onboarding-digest", "T1", status="open")
    _seed_emission(conn, "snapshot-due:closed", "T2", status="completed")

    res = request_emission_retire(conn, "onboarding-digest")

    assert res == {"matched": "onboarding-digest", "retire_requested": 1}
    assert _emission(conn, "onboarding-digest")["retire_requested_at"] is not None
    # A non-open row is never flagged.
    assert _emission(conn, "snapshot-due:closed")["retire_requested_at"] is None


def test_request_emission_retire_prefix_multi_instance(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_emission(conn, "obligation-due:OB1:2026-06-01", "A1")
    _seed_emission(conn, "obligation-due:OB1:2026-07-01", "A2")
    _seed_emission(conn, "obligation-due:OB2:2026-06-01", "B1")

    res = request_emission_retire_prefix(conn, "obligation-due:OB1:")

    assert res == {"prefix": "obligation-due:OB1:", "retire_requested": 2}
    assert _emission(conn, "obligation-due:OB1:2026-06-01")["retire_requested_at"] is not None
    assert _emission(conn, "obligation-due:OB1:2026-07-01")["retire_requested_at"] is not None
    # The unrelated obligation is untouched.
    assert _emission(conn, "obligation-due:OB2:2026-06-01")["retire_requested_at"] is None


def test_reject_decision_marks_retire(tmp_path):
    db = tmp_path / "f.db"
    conn = _db(db)
    _seed_candidate(conn, "cand:acme", "Acme", existing_obligation_id="OB1")
    _seed_emission(conn, "obligation-due:OB1:2026-06-01", "A1")
    _seed_emission(conn, "obligation-due:OB1:2026-07-01", "A2")
    _promote_server_db(db, conn)

    record_charge_onboarding_decision("cand:acme", {"action": "reject"}, db_path=str(db))

    conn = _db(db)
    assert _emission(conn, "obligation-due:OB1:2026-06-01")["retire_requested_at"] is not None
    assert _emission(conn, "obligation-due:OB1:2026-07-01")["retire_requested_at"] is not None


def test_surface_drains_retire_live(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_emission(conn, "obligation-due:OB1:2026-06-01", "A1", retire_at=_NOW)
    send, delete = _Send(), _Delete()

    summary = _live(conn, [], send, delete)

    assert delete.calls == ["A1"]
    assert summary["retired"] == 1
    assert _emission(conn, "obligation-due:OB1:2026-06-01")["status"] == "retired"


def test_retire_then_recreate_resurfaces(tmp_path):
    """Retired resurfaces; deleted_by_user suppresses only matching evidence."""
    conn = _db(tmp_path / "f.db")
    # A previously-retired recurring instance: surfacing the SAME key again
    # recreates it (the underlying need recurred).
    _seed_emission(conn, "obligation-due:OB1:2026-06-01", "OLD1", status="retired")
    # A user-deleted instance with the same evidence stays acknowledged.
    _seed_emission(
        conn,
        "obligation-due:OB2:2026-06-01",
        "OLD2",
        status="deleted_by_user",
        content_hash=content_hash_for("Pay OB2", "d"),
    )
    send, delete = _Send(), _Delete()

    summary = _live(
        conn,
        [
            {"surface_key": "obligation-due:OB1:2026-06-01", "content": "Pay OB1", "description": "d"},
            {"surface_key": "obligation-due:OB2:2026-06-01", "content": "Pay OB2", "description": "d"},
        ],
        send, delete,
    )

    assert summary["created"] == 1  # only the retired one resurrected
    assert summary["resolved"] == 1  # the deleted_by_user one stayed suppressed
    ob1 = _emission(conn, "obligation-due:OB1:2026-06-01")
    assert ob1["status"] == "open" and ob1["todoist_task_id"] == "NEW1"
    assert _emission(conn, "obligation-due:OB2:2026-06-01")["status"] == "deleted_by_user"


def test_digest_retire_then_resurface(tmp_path):
    """Singleton onboarding-digest: retire when N->0, recreate when N>0 again."""
    conn = _db(tmp_path / "f.db")
    # Pretend the digest task already exists in the ledger from a prior run.
    _seed_emission(conn, "onboarding-digest", "DIG1", status="open")
    send, delete = _Send(), _Delete()

    # N == 0: build_surface_items emits nothing and flags the digest for retire.
    items = build_surface_items(conn, as_of_date=AS_OF)
    assert all(it["surface_key"] != "onboarding-digest" for it in items)
    drained = _live(conn, items, send, delete)
    assert drained["retired"] == 1
    assert _emission(conn, "onboarding-digest")["status"] == "retired"

    # N > 0 again: the digest is recreated, not permanently suppressed.
    _seed_candidate(conn, "cand:x", "Netflix")
    items2 = build_surface_items(conn, as_of_date=AS_OF)
    digest = [it for it in items2 if it["surface_key"] == "onboarding-digest"]
    assert len(digest) == 1
    summary = _live(conn, items2, send, delete)
    assert summary["created"] == 1
    assert _emission(conn, "onboarding-digest")["status"] == "open"


def test_surface_drain_skipped_when_not_live(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_emission(conn, "obligation-due:OB1:2026-06-01", "A1", retire_at=_NOW)
    delete = _Delete()

    summary = surface_to_todoist(
        conn, [], AS_OF, write_enabled=False, token="tok", project_id="proj",
        send_func=_Send(), delete_func=delete,
    )

    assert delete.calls == []
    assert summary["retired"] == 0
    row = _emission(conn, "obligation-due:OB1:2026-06-01")
    assert row["status"] == "open"
    assert row["retire_requested_at"] is not None  # flag survives for the next live run


def test_reset_clears_retire(tmp_path):
    db = tmp_path / "f.db"
    conn = _db(db)
    _seed_candidate(conn, "cand:acme", "Acme", status="deferred", existing_obligation_id="OB1")
    _seed_emission(conn, "obligation-due:OB1:2026-06-01", "A1", retire_at=_NOW)
    _promote_server_db(db, conn)

    record_charge_onboarding_decision("cand:acme", {"action": "reset"}, db_path=str(db))

    conn = _db(db)
    assert _emission(conn, "obligation-due:OB1:2026-06-01")["retire_requested_at"] is None


# --- section 4: onboarding digest surface item ------------------------------


def test_batch_decisions_apply_independently(tmp_path):
    db = tmp_path / "f.db"
    conn = _db(db)
    _seed_candidate(conn, "c1", "Acme")
    _seed_candidate(conn, "c2", "Beta")
    _promote_server_db(db, conn)

    res = record_charge_onboarding_decisions(
        [
            {"candidate_id": "c1", "decision": "defer"},
            {"candidate_id": "c2", "decision": {"action": "reject"}},
            {"candidate_id": "missing", "decision": "defer"},  # bad item must not abort the batch
        ],
        db_path=str(db),
    )
    assert res["total"] == 3 and res["applied"] == 2 and res["failed"] == 1
    assert {r["candidate_id"]: r["ok"] for r in res["results"]} == {"c1": True, "c2": True, "missing": False}


def test_digest_single_item_for_many_candidates(tmp_path):
    conn = _db(tmp_path / "f.db")
    for i in range(56):
        _seed_candidate(conn, f"cand:{i}", f"Merchant {i}", priority=float(i))

    items = build_surface_items(conn, as_of_date=AS_OF)

    digest = [it for it in items if it["surface_key"] == "onboarding-digest"]
    assert len(digest) == 1
    assert digest[0]["content"] == "56 charges to review"


def test_digest_uses_singular_for_one_candidate(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_candidate(conn, "cand:1", "Youtube TV", priority=1.0)

    items = build_surface_items(conn, as_of_date=AS_OF)

    digest = [it for it in items if it["surface_key"] == "onboarding-digest"]
    assert digest[0]["content"] == "1 charge to review"
    assert digest[0]["description"].startswith("1 charge awaiting review.")


def test_digest_absent_when_zero(tmp_path):
    conn = _db(tmp_path / "f.db")

    items = build_surface_items(conn, as_of_date=AS_OF)

    assert all(it["surface_key"] != "onboarding-digest" for it in items)


def test_digest_updates_not_recreates(tmp_path):
    conn = _db(tmp_path / "f.db")
    _seed_candidate(conn, "cand:1", "Netflix")
    send, delete = _Send(), _Delete()

    first = _live(conn, build_surface_items(conn, as_of_date=AS_OF), send, delete)
    assert first["created"] == 1
    task_id = _emission(conn, "onboarding-digest")["todoist_task_id"]

    # Count changes -> content "2 charges to review" -> hash differs -> update.
    _seed_candidate(conn, "cand:2", "Hulu")
    second = _live(conn, build_surface_items(conn, as_of_date=AS_OF), send, delete)

    assert second["created"] == 0 and second["updated"] == 1
    assert _emission(conn, "onboarding-digest")["todoist_task_id"] == task_id


def test_digest_retire_when_zero(tmp_path):
    conn = _db(tmp_path / "f.db")
    # An existing open digest task from a prior non-empty run.
    _seed_emission(conn, "onboarding-digest", "DIG1", status="open")

    # N == 0: the read-only builder writes nothing; the retire is applied by the
    # write path, which reports the digest key via build_surface_retire_keys.
    send, delete = _Send(), _Delete()
    summary = _live(conn, build_surface_items(conn, as_of_date=AS_OF), send, delete)

    assert summary["retired"] == 1
    assert _emission(conn, "onboarding-digest")["status"] == "retired"
