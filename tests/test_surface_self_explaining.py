"""A finance task must explain itself, and one question must raise one task.

Two contracts under test:

1. Self-explaining: every item ``build_surface_items`` emits carries a trigger
   sentence (what happened) followed by exactly ONE dated action line, and a
   due date. Nobody should have to open the session that created a task to work
   out why it is on the board.
2. One task per question: a bill that is due AND has a check that may already
   have paid it raises ONE task, not two; and when a bill's due date moves the
   already-open task is updated in place instead of a second task appearing.

Hermetic: seeded SQLite plus a mock Todoist sender, no network.
"""

import sys

# The affected-test runner may launch this file directly with the system python.
# financial_agent needs 3.11+ (datetime.UTC), so exit clean instead of blowing up
# on the import; the real gate runs the suite under the project interpreter.
if sys.version_info < (3, 11):  # pragma: no cover - interpreter guard
    print("skipped: financial_agent requires Python 3.11+")
    raise SystemExit(0)

import re
import sqlite3
from datetime import date, timedelta

from financial_agent.check_suggestions import (
    confirm_check_suggestion,
    reject_check_suggestion,
)
from financial_agent.follow_ups import capture_followup
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent.surface_queue import (
    build_surface_items,
    build_surface_retire_keys,
    build_sync_failed_item,
    one_task_per_subject,
)
from financial_agent.todoist_outbox import surface_to_todoist

AS_OF = date(2026, 7, 12)

# "Action: <verb> ... by YYYY-MM-DD." - the one dated line every task ends with.
_ACTION_LINE = re.compile(r"Action: .+ by \d{4}-\d{2}-\d{2}\.$")


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT,
            first_seen_at TEXT, last_seen_at TEXT
        );
        CREATE TABLE transactions (
            id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,
            amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT,
            first_seen_at TEXT, last_seen_at TEXT, fetched_at TEXT
        );
        CREATE TABLE balance_snapshots (
            id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL,
            recorded_at TEXT, source TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO accounts (id,name,org,kind,currency) VALUES (?,?,?,?,?)",
        [("ACT-chk", "PREMIER PLUS CKG (4321)", "Chase Bank", "checking", "USD")],
    )
    ensure_app_schema(conn)
    return conn


def _manual_bill(conn, obligation_id, name, due_date, amount):
    """A manual (no autopay) bill instance, the kind that surfaces a pay task."""

    apply_obligation_instances(
        conn,
        obligation={
            "id": obligation_id,
            "name": name,
            "kind": "housing",
            "cadence": "monthly",
            "status": "active",
            "autopay": 0,
            "source": "seed",
        },
        instances=[
            {
                "id": f"{obligation_id}:{due_date}",
                "due_date": due_date,
                "amount": amount,
                "status": "expected",
                "source": "seed",
                "cash_flow_treatment": "direct_checking",
            }
        ],
    )


def _check(conn, transaction_id, posted, amount, identifier):
    conn.execute(
        """
        INSERT INTO transactions
            (id,account_id,posted,amount,payee,description,pending,source)
        VALUES (?, 'ACT-chk', ?, ?, ?, ?, 0, 'simplefin')
        """,
        (transaction_id, posted, amount, f"CHECK {identifier}", f"CHECK {identifier}"),
    )


class _Spy:
    """Records every send; hands out incrementing task ids on create."""

    def __init__(self):
        self.calls = []
        self._next = 0

    def __call__(self, token, path, body, **kwargs):
        self.calls.append({"path": path, "body": body})
        if path == "/tasks":
            self._next += 1
            return {"id": f"T{self._next}"}
        return {}

    @property
    def creates(self):
        return [c for c in self.calls if c["path"] == "/tasks"]

    @property
    def updates(self):
        return [c for c in self.calls if c["path"].startswith("/tasks/")]


# --- 1. every task explains itself -----------------------------------------


def test_every_surfaced_task_states_a_trigger_and_one_action(tmp_path):
    conn = _db(tmp_path / "t.db")
    capture_followup(
        conn, text="Call Anthem about reimbursement", surface_when=AS_OF.isoformat()
    )
    _manual_bill(conn, "rent", "Rent", (AS_OF + timedelta(days=2)).isoformat(), -3000.0)
    conn.commit()

    items = build_surface_items(
        conn, as_of_date=AS_OF, headline="YELLOW: cushion is thin"
    )
    assert items, "seeded sources should surface something"

    for item in items:
        where = item["surface_key"]
        body = item["description"]
        # Dated: a surfaced task is never dateless.
        assert item.get("due_date"), f"{where} has no due date"
        # One action, and it is the last thing said.
        assert body.count("Action:") == 1, f"{where} does not have exactly one action"
        assert _ACTION_LINE.search(body), f"{where} has no dated action line: {body}"
        # A trigger sentence comes first, so the task explains why it exists.
        trigger = body.split("Action:")[0].strip()
        assert not body.startswith("Action:"), f"{where} states no trigger"
        assert "." in trigger, f"{where} trigger is not a sentence: {trigger}"
        # Plain language: no internal field or function name in the body.
        assert not re.search(r"\b[a-z]+_[a-z_]+\b", body), f"{where} leaks an identifier: {body}"

    # The sync-failure item follows the same contract.
    failed = build_sync_failed_item(AS_OF)
    assert failed["due_date"] == AS_OF.isoformat()
    assert failed["description"].count("Action:") == 1
    assert _ACTION_LINE.search(failed["description"])
    assert "run_background_sync" not in failed["description"]


# --- 2. one question, one task ---------------------------------------------


def test_bill_that_may_already_be_paid_raises_one_task_not_two(tmp_path):
    """A bill due today with a check that may have paid it: ONE task.

    Both builders fire on the same bill (pay it / confirm the check paid it).
    Asking Albert to pay a bill that may have already cleared is the duplicate he
    had to dedupe by hand, so the check question wins on the board and the pay
    task is held. The suppression happens at the push, so the read stays complete.
    """

    conn = _db(tmp_path / "t.db")
    board = _Board()
    _manual_bill(conn, "santiguida", "Santiguida rent", AS_OF.isoformat(), -3000.0)
    _check(conn, "check-july", f"{AS_OF.isoformat()}T09:00:00", -3000.0, "1233")
    conn.commit()

    # The read names both: the digest renders from this and must not lose the bill.
    items = build_surface_items(conn, as_of_date=AS_OF)
    keys = [item["surface_key"] for item in items]
    assert any(k.startswith("obligation-due:santiguida:") for k in keys), keys
    assert any(k.startswith("check-suggestion:") for k in keys), keys

    # The board gets exactly one of them.
    _surface(conn, AS_OF, board)
    open_keys = _open_keys(conn)
    about_the_bill = [
        k for k in open_keys if "santiguida" in k or k.startswith("check-suggestion:")
    ]
    assert len(about_the_bill) == 1, f"one task per bill, got {about_the_bill}"
    assert about_the_bill[0].startswith("check-suggestion:")
    # The surviving task still names the bill, its amount and its due date, so
    # nothing is lost by holding the pay task.
    body = next(i["description"] for i in items if i["surface_key"] == about_the_bill[0])
    assert "Santiguida rent" in body
    assert "$3,000.00" in body
    assert AS_OF.isoformat() in body

    # The routing field lives on the read and is stripped before the push.
    assert any("subject" in item for item in items)
    assert all("subject" not in item for item in one_task_per_subject(items))


def test_digest_still_names_the_due_bill_when_a_check_question_wins(tmp_path):
    """The board holds the pay task; the daily close must still name the bill.

    The digest's "Do this today" section reads the same builder the push does. If
    the push-time suppression happened in the builder, the close would print
    "nothing manual due today" while a manual bill sat due and unconfirmed.
    """

    db_file = tmp_path / "t.db"
    conn = _db(db_file)
    _manual_bill(conn, "santiguida", "Santiguida rent", AS_OF.isoformat(), -3000.0)
    _check(conn, "check-july", f"{AS_OF.isoformat()}T09:00:00", -3000.0, "1233")
    conn.commit()
    # Push first, so the check question is the one live task on the board.
    _surface(conn, AS_OF, _Board())
    conn.commit()
    conn.close()

    from financial_agent.digest import _render_do_this_today

    lines: list[str] = []
    _render_do_this_today(
        {"as_of_date": AS_OF.isoformat(), "provenance": {"db_file": str(db_file)}}, lines
    )
    rendered = "\n".join(lines)
    assert "Santiguida rent" in rendered, rendered
    assert "nothing manual due today" not in rendered, rendered


def test_moved_due_date_updates_the_open_task_instead_of_adding_one(tmp_path):
    """The bill's due date shifts -> new surface_key, same open task.

    Without this the new key had no ledger row and a SECOND "Pay Santiguida" task
    appeared next to the first one.
    """

    conn = _db(tmp_path / "t.db")
    spy = _Spy()
    first = [
        {
            "surface_key": "obligation-due:santiguida:2026-07-14",
            "content": "Pay Santiguida rent $3,000.00",
            "description": "Santiguida rent is due 2026-07-14 and has no autopay, so "
            "nothing pays it unless you do. Action: Pay $3,000.00 from PREMIER PLUS "
            "CKG (4321) by 2026-07-12.",
            "due_date": "2026-07-12",
        }
    ]
    surface_to_todoist(
        conn, first, AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=spy,
    )
    assert len(spy.creates) == 1

    moved = [dict(first[0], surface_key="obligation-due:santiguida:2026-07-16")]
    summary = surface_to_todoist(
        conn, moved, AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=spy,
    )

    assert len(spy.creates) == 1, "the moved date must not create a second task"
    assert len(spy.updates) == 1
    assert spy.updates[0]["path"] == "/tasks/T1"
    assert summary["created"] == 0 and summary["updated"] == 1
    assert summary["items"][0]["rekeyed_from"] == "obligation-due:santiguida:2026-07-14"

    # The ledger now tracks the new key against the SAME task, so the next run is
    # an idempotent skip rather than another create.
    rows = conn.execute(
        "SELECT surface_key, todoist_task_id, status FROM todoist_emissions"
    ).fetchall()
    assert [(r["surface_key"], r["todoist_task_id"], r["status"]) for r in rows] == [
        ("obligation-due:santiguida:2026-07-16", "T1", "open")
    ]
    again = surface_to_todoist(
        conn, moved, AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=spy,
    )
    assert again["skipped"] == 1 and len(spy.creates) == 1


def test_resolved_bill_does_not_resurrect_through_the_rekey(tmp_path):
    """A completed task stays closed: only OPEN emissions are re-pointed."""

    conn = _db(tmp_path / "t.db")
    spy = _Spy()
    item = {
        "surface_key": "obligation-due:santiguida:2026-07-14",
        "content": "Pay Santiguida rent $3,000.00",
        "description": "Santiguida rent is due 2026-07-14. Action: Pay by 2026-07-12.",
        "due_date": "2026-07-12",
    }
    surface_to_todoist(
        conn, [item], AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=spy,
    )
    conn.execute(
        "UPDATE todoist_emissions SET status = 'completed' WHERE surface_key = ?",
        (item["surface_key"],),
    )

    nxt = [dict(item, surface_key="obligation-due:santiguida:2026-08-14")]
    summary = surface_to_todoist(
        conn, nxt, AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=spy,
    )
    # Next cycle's bill is a new question: a fresh task, and the completed one is
    # never reopened or updated.
    assert summary["created"] == 1
    assert len(spy.updates) == 0


def test_pay_task_already_on_the_board_stays_when_the_check_question_arrives(tmp_path):
    """Two runs, three days apart: the pay task goes up first, then the check.

    The pay task is raised days before the bill is due; a check that may have paid
    it can only turn up later, once Albert has written it. The check question goes
    up next to the pay task, and the pay task STAYS: an unpaid bill never loses
    its task until the bill is confirmed paid (owner decision 2026-08-20). The
    check task, once answered, is what settles whether the pay task comes down
    (approve resolves the bill) or stays (reject leaves it due).
    """

    conn = _db(tmp_path / "t.db")
    board = _Board()
    day_one = AS_OF - timedelta(days=3)
    _manual_bill(conn, "santiguida", "Santiguida rent", AS_OF.isoformat(), -3000.0)
    conn.commit()

    first = _surface(conn, day_one, board)
    assert first["created"] == 1
    assert board.live_task_ids == ["T1"]
    assert _open_keys(conn) == ["obligation-due:santiguida:2026-07-12"]

    # Albert writes the check; it clears three days later.
    _check(conn, "check-july", f"{AS_OF.isoformat()}T09:00:00", -3000.0, "1233")
    conn.commit()

    second = _surface(conn, AS_OF, board)

    open_keys = _open_keys(conn)
    assert len(open_keys) == 2, f"pay task and check question, got {open_keys}"
    assert any(k.startswith("check-suggestion:") for k in open_keys)
    assert "obligation-due:santiguida:2026-07-12" in open_keys
    assert board.deleted == [], "an unpaid bill's task is never deleted"
    assert len(board.live_task_ids) == 2
    assert second["created"] == 1 and second["retired"] == 0
    assert _status(conn, "obligation-due:santiguida:2026-07-12") == "open"


def test_approving_the_check_leaves_the_board_clean(tmp_path):
    """Approve = the bill is paid: the check task goes, and no pay task returns."""

    conn = _db(tmp_path / "t.db")
    board = _Board()
    suggestion_id = _bill_with_pending_check(conn, board)

    confirm_check_suggestion(conn, suggestion_id, as_of_date=AS_OF)
    conn.commit()

    after = _surface(conn, AS_OF + timedelta(days=1), board)
    assert _open_keys(conn) == [], "a paid bill leaves nothing on the board"
    assert board.live_task_ids == []
    assert after["created"] == 0, "a paid bill must not raise a pay task"


def test_rejecting_the_check_leaves_the_pay_task_standing(tmp_path):
    """Reject = nobody paid it: the bill stays visible, the question comes down.

    The pay task never left the board while the check question was open, so a
    rejection only removes the answered question. The bill is past due by now,
    and its task stays up on later days too.
    """

    conn = _db(tmp_path / "t.db")
    board = _Board()
    suggestion_id = _bill_with_pending_check(conn, board)
    pay_task_id, check_task_id = board.live_task_ids

    reject_check_suggestion(conn, suggestion_id)
    conn.commit()

    after = _surface(conn, AS_OF + timedelta(days=1), board)

    assert board.deleted == [check_task_id], "only the answered question comes down"
    open_keys = _open_keys(conn)
    assert open_keys == ["obligation-due:santiguida:2026-07-12"], open_keys
    assert after["created"] == 0, "the pay task was never taken down, so none is created"
    assert board.live_task_ids == [pay_task_id]

    # And it stays up on later days, not just the day of the rejection.
    _surface(conn, AS_OF + timedelta(days=5), board)
    assert _open_keys(conn) == ["obligation-due:santiguida:2026-07-12"]
    assert board.live_task_ids == [pay_task_id]


def _bill_with_pending_check(conn, board):
    """Bill due, pay task up, then a check question joins it. -> suggestion id.

    The pay task stays up next to the check question (owner decision 2026-08-20);
    the question only decides how the pay task eventually comes down.
    """

    _manual_bill(conn, "santiguida", "Santiguida rent", AS_OF.isoformat(), -3000.0)
    conn.commit()
    _surface(conn, AS_OF - timedelta(days=3), board)

    _check(conn, "check-july", f"{AS_OF.isoformat()}T09:00:00", -3000.0, "1233")
    conn.commit()
    _surface(conn, AS_OF, board)

    open_keys = _open_keys(conn)
    check_keys = [k for k in open_keys if k.startswith("check-suggestion:")]
    assert check_keys and "obligation-due:santiguida:2026-07-12" in open_keys, open_keys
    return check_keys[0].split(":", 1)[1]


def _surface(conn, as_of, board):
    """One full surface run: build items + retire intent, then push."""

    return surface_to_todoist(
        conn,
        build_surface_items(conn, as_of_date=as_of),
        as_of,
        write_enabled=True,
        token="tok",
        project_id="proj",
        send_func=board,
        delete_func=board.delete,
        retire_keys=build_surface_retire_keys(conn, as_of_date=as_of),
    )


def _open_keys(conn):
    return [
        r[0]
        for r in conn.execute(
            "SELECT surface_key FROM todoist_emissions WHERE status = 'open' "
            "ORDER BY surface_key"
        )
    ]


def _status(conn, surface_key):
    row = conn.execute(
        "SELECT status FROM todoist_emissions WHERE surface_key = ?", (surface_key,)
    ).fetchone()
    return row[0] if row else None


class _Board(_Spy):
    """A _Spy that also models the board: created tasks stay until deleted."""

    def __init__(self):
        super().__init__()
        self.live_task_ids = []
        self.deleted = []

    def __call__(self, token, path, body, **kwargs):
        result = super().__call__(token, path, body, **kwargs)
        if path == "/tasks":
            self.live_task_ids.append(result["id"])
        return result

    def delete(self, token, task_id):
        self.live_task_ids.remove(task_id)
        self.deleted.append(task_id)
        return True


# --- 3. a month is its own question -----------------------------------------


def test_rekey_never_hijacks_a_still_unpaid_previous_month(tmp_path):
    """July unpaid, August due: August gets a NEW task, July's stays as July.

    The re-key path exists for a nudged due date. A month-apart sibling with its
    own still-open instance is a different unpaid bill, and adopting its task
    would silently erase an overdue bill from the board.
    """

    conn = _db(tmp_path / "t.db")
    board = _Board()
    _manual_bill(conn, "rent", "Rent", AS_OF.isoformat(), -3000.0)
    conn.commit()
    _surface(conn, AS_OF, board)
    assert board.live_task_ids == ["T1"]

    # August's instance arrives; July is still unpaid (status 'expected').
    _manual_bill(conn, "rent", "Rent", "2026-08-12", -3000.0)
    conn.commit()
    august = date(2026, 8, 12)
    result = _surface(conn, august, board)

    assert result["created"] == 1, result["items"]
    assert board.live_task_ids == ["T1", "T2"], "July's task must survive untouched"
    assert board.deleted == []
    assert board.updates == [], "July's task must not be rewritten into August"
    assert _open_keys(conn) == [
        "obligation-due:rent:2026-07-12",
        "obligation-due:rent:2026-08-12",
    ]


def test_past_due_bill_and_current_bill_both_surface_next_to_the_check_question(tmp_path):
    """A re-raised past-due bill is never suppressed, and a sibling month keeps
    its own task.

    The past-due July-1 instance (an earlier check was rejected after it came
    due) pushes even while a NEW check question about it is open (owner decision
    2026-08-20). The July-14 instance is a different question entirely and is
    untouched by either.
    """

    conn = _db(tmp_path / "t.db")
    _manual_bill(conn, "rent", "Rent", "2026-07-01", -3000.0)
    _manual_bill(conn, "rent", "Rent", "2026-07-14", -3000.0)
    conn.execute(
        "INSERT INTO check_suggestion_rejections "
        "(suggestion_id, obligation_instance_id, transaction_id, rejected_at) "
        "VALUES ('s-1', 'rent:2026-07-01', 'check-june', ?)",
        (f"{AS_OF.isoformat()}T08:00:00",),
    )
    # Posted inside the suggestion grace window of the July-1 due date.
    _check(conn, "check-july", "2026-07-05T09:00:00", -3000.0, "1233")
    conn.commit()

    pushed = one_task_per_subject(build_surface_items(conn, as_of_date=AS_OF))
    keys = [i["surface_key"] for i in pushed]
    assert "obligation-due:rent:2026-07-01" in keys, keys
    assert any(k.startswith("check-suggestion:") for k in keys), keys
    assert "obligation-due:rent:2026-07-14" in keys, keys


def test_rekey_does_not_adopt_a_task_already_flagged_for_removal(tmp_path):
    """A condemned task (retire failed mid-run) is not recycled for a new key."""

    from financial_agent.todoist_outbox import request_emission_retire

    conn = _db(tmp_path / "t.db")
    spy = _Spy()
    first = [
        {
            "surface_key": "obligation-due:rent:2026-07-14",
            "content": "Pay Rent $3,000.00",
            "description": "Rent is due 2026-07-14. Action: Pay by 2026-07-12.",
            "due_date": "2026-07-12",
        }
    ]
    surface_to_todoist(
        conn, first, AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=spy,
    )
    request_emission_retire(conn, "obligation-due:rent:2026-07-14")

    def failing_delete(token, task_id):
        raise RuntimeError("todoist down")

    moved = [dict(first[0], surface_key="obligation-due:rent:2026-07-16")]
    summary = surface_to_todoist(
        conn, moved, AS_OF, write_enabled=True, token="tok", project_id="proj",
        send_func=spy, delete_func=failing_delete,
    )

    assert summary["created"] == 1, "a fresh task, not an adoption"
    assert not any("rekeyed_from" in i for i in summary["items"])
    row = conn.execute(
        "SELECT status, retire_requested_at FROM todoist_emissions "
        "WHERE surface_key = 'obligation-due:rent:2026-07-14'"
    ).fetchone()
    assert row is not None and row["status"] == "open" and row["retire_requested_at"]


def test_whitespace_headline_raises_no_status_task(tmp_path):
    conn = _db(tmp_path / "t.db")
    items = build_surface_items(conn, as_of_date=AS_OF, headline="   ")
    assert not any(i["surface_key"] == "finance-status" for i in items)
