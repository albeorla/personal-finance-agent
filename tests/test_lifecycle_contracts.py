"""Lifecycle / contract tests that lock the system's core invariants.

These are deliberately end-to-end-ish: each test drives the real code paths
(onboarding scan/apply, reconciliation match/confirm, the Todoist emission
ledger, and the cash-flow projection) against temp DBs and asserts the
load-bearing guarantees the maintainer cares about:

1. A discovered charge candidate has ZERO effect on projections / obligation
   instances until it is explicitly applied.
2. An obligation instance is only marked paid with recorded match evidence -
   there is no auto-pay path that flips it without a real transaction match.
3. Surfacing is idempotent: an already-open emission is never duplicated, and
   resolve/retire are real status transitions.
4. The cash-flow projection reads ONLY from obligation_instances - a raw
   candidate-table row with no applied instance cannot move the projected
   balance.
"""

import sqlite3
from datetime import date

import pytest

from financial_agent.cashflow import build_cash_flow_projections
from financial_agent.obligations import apply_obligation_instances
from financial_agent.onboarding import (
    apply_charge_onboarding_candidate,
    list_charge_onboarding_queue,
    record_charge_onboarding_decision,
    scan_charge_onboarding_candidates,
)
from financial_agent.reconciliation import (
    confirm_reconciliation_match,
    reconcile_obligation_instances,
)
from financial_agent.schema import ensure_app_schema
from financial_agent.todoist_outbox import (
    mark_emission_status,
    request_emission_retire,
    surface_to_todoist,
)


# --- shared fixtures (mirror tests/test_onboarding.py real amounts) --------

CHECKING = ("ACT-chk", "PREMIER PLUS CKG (4321)", "Chase Bank", "", "USD")

EVERSOURCE_CHECKING_ROWS = [
    ("ever-1", "ACT-chk", "2025-11-28T08:00:00", -79.50, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: 3020181050"),
    ("ever-2", "ACT-chk", "2025-12-30T08:00:00", -98.86, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: 3020181050"),
    ("ever-3", "ACT-chk", "2026-02-02T08:00:00", -148.36, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: 3020181050"),
    ("ever-4", "ACT-chk", "2026-03-02T08:00:00", -121.80, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: XXXXXX1050"),
    ("ever-5", "ACT-chk", "2026-03-30T08:00:00", -111.93, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: XXXXXX1050"),
    ("ever-6", "ACT-chk", "2026-04-28T08:00:00", -106.39, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: XXXXXX1050"),
    ("ever-7", "ACT-chk", "2026-05-28T08:00:00", -144.22, "Eversource Energy", "EVERSOURCE WEB_PAY PPD ID: XXXXXX1050"),
]

AS_OF = "2026-06-24"


def _seed_source_db(path, *, accounts, transactions, with_balances=False):
    """Build a copied-style source DB with accounts + transactions tables.

    (Verbatim shape from tests/test_onboarding.py so the same merchant amounts
    and account placements drive the real onboarding scanner.)
    """

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            org TEXT,
            kind TEXT,
            currency TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT
        );

        CREATE TABLE transactions (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            posted TEXT,
            transacted_at TEXT,
            amount REAL NOT NULL,
            payee TEXT,
            description TEXT,
            pending INTEGER,
            source TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            fetched_at TEXT
        );
        """
    )
    if with_balances:
        conn.executescript(
            """
            CREATE TABLE balance_snapshots (
                id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL,
                balance REAL NOT NULL,
                available REAL NOT NULL,
                recorded_at TEXT NOT NULL,
                source TEXT NOT NULL
            );
            """
        )
    conn.executemany(
        "INSERT INTO accounts (id, name, org, kind, currency, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, '2025-01-01', '2026-06-20')",
        accounts,
    )
    conn.executemany(
        "INSERT INTO transactions (id, account_id, posted, amount, payee, description, pending, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 'simplefin')",
        transactions,
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    conn.commit()
    return conn


def _seed_recon_db(path, transactions=()):
    """Minimal accounts+transactions DB for reconciliation contracts."""

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT,
            first_seen_at TEXT, last_seen_at TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,
            amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT,
            first_seen_at TEXT, last_seen_at TEXT, fetched_at TEXT);
        """
    )
    conn.execute(
        "INSERT INTO accounts (id,name,org,kind,currency) VALUES "
        "('ACT-chk','PREMIER PLUS CKG (4321)','Chase Bank','','USD')"
    )
    conn.executemany(
        "INSERT INTO transactions (id,account_id,posted,amount,payee,description,pending,source) "
        "VALUES (?,?,?,?,?,?,0,'simplefin')",
        transactions,
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    conn.commit()
    return conn


def _find(queue, merchant_key, *, treatment=None):
    for candidate in queue:
        if candidate["merchant_key"] == merchant_key:
            if treatment is None or candidate["cash_flow_treatment"] == treatment:
                return candidate
    return None


def _checking_accounts(available):
    return [
        {
            "account_id": "ACT-chk",
            "account_name": "PREMIER PLUS CKG (4321)",
            "kind": "checking",
            "available": available,
            "recorded_at": "2026-06-20T00:00:00+00:00",
        }
    ]


def _emission_db(path):
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


class _DeleteSpy:
    """Records every retire delete and returns success."""

    def __init__(self):
        self.deleted = []

    def __call__(self, token, task_id, **kwargs):
        self.deleted.append(task_id)
        return True


def _surface(conn, items, spy, delete_spy=None):
    return surface_to_todoist(
        conn,
        items,
        AS_OF,
        write_enabled=True,
        token="tok",
        project_id="proj",
        send_func=spy,
        delete_func=delete_spy if delete_spy is not None else _DeleteSpy(),
    )


# ---------------------------------------------------------------------------
# CONTRACT 1: candidate boundary - no projection effect until applied
# ---------------------------------------------------------------------------


def test_contract_candidate_does_not_project_until_applied(tmp_path):
    conn = _seed_source_db(
        tmp_path / "c1.sqlite",
        accounts=[CHECKING],
        transactions=EVERSOURCE_CHECKING_ROWS,
        with_balances=True,
    )
    accounts = _checking_accounts(1000.0)
    start = date(2026, 7, 1)
    windows = [120]

    before_proj, _ = build_cash_flow_projections(
        conn, accounts=accounts, windows=windows, start_date=start
    )
    before_instances = conn.execute("SELECT COUNT(*) FROM obligation_instances").fetchone()[0]
    assert before_instances == 0

    # Discover the candidate. It lands in its own queue table only.
    scan_charge_onboarding_candidates(conn)
    candidate = _find(list_charge_onboarding_queue(conn), "eversource_energy")
    assert candidate is not None

    # A discovered-but-unapplied candidate must have ZERO projection effect.
    mid_proj, _ = build_cash_flow_projections(
        conn, accounts=accounts, windows=windows, start_date=start
    )
    assert conn.execute("SELECT COUNT(*) FROM obligation_instances").fetchone()[0] == before_instances
    assert mid_proj[0]["ending_balance"] == before_proj[0]["ending_balance"]
    assert mid_proj[0]["events"] == before_proj[0]["events"]

    # Apply it: accept decision, then promote to a canonical obligation.
    cid = candidate["id"]
    record_charge_onboarding_decision(conn, cid, {"action": "accept"})
    apply_charge_onboarding_candidate(
        conn, cid, start_date="2026-07-01", through_date="2026-09-30"
    )

    # EXACTLY ONE obligation now traces back to this candidate, with its instances.
    rows = conn.execute(
        "SELECT id FROM obligations WHERE source = ?", (f"charge_onboarding:{cid}",)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == "onboarded_eversource_energy_checking"
    assert conn.execute(
        "SELECT COUNT(*) FROM obligation_instances WHERE obligation_id = ?", (rows[0]["id"],)
    ).fetchone()[0] == 3

    # And only NOW does it influence the projection.
    after_proj, _ = build_cash_flow_projections(
        conn, accounts=accounts, windows=windows, start_date=start
    )
    assert after_proj[0]["ending_balance"] != before_proj[0]["ending_balance"]
    assert after_proj[0]["ending_balance"] == 652.39  # 1000 - 3 * 115.87
    assert [e["obligation_id"] for e in after_proj[0]["events"]] == [
        "onboarded_eversource_energy_checking"
    ] * 3


# ---------------------------------------------------------------------------
# CONTRACT 2: reconciliation evidence-first - no paid without a recorded match
# ---------------------------------------------------------------------------


def _seed_instance(conn, oid, name, kind, instances):
    apply_obligation_instances(
        conn,
        obligation={"id": oid, "name": name, "kind": kind, "status": "active", "source": "seed"},
        instances=instances,
    )


def test_contract_paid_requires_recorded_match_evidence(tmp_path):
    conn = _seed_recon_db(
        tmp_path / "c2a.sqlite",
        transactions=[("t-ever", "ACT-chk", "2026-06-25T08:00:00", -115.87, "Eversource Energy", "EVERSOURCE WEB_PAY")],
    )
    _seed_instance(
        conn, "eversource", "Eversource electric estimates", "utility",
        [{"id": "eversource:2026-06-25", "due_date": "2026-06-25", "amount": -115.87, "source": "seed"}],
    )

    # EVIDENCE-FIRST GUARD: confirming with no recorded match is refused outright.
    with pytest.raises(ValueError):
        confirm_reconciliation_match(conn, "eversource:2026-06-25")

    # Reconcile records the match as evidence but does NOT auto-pay.
    summary = reconcile_obligation_instances(conn, as_of_date="2026-06-30")
    assert summary["matched_auto"] == 1
    assert summary["marked_paid"] == 0

    status = conn.execute(
        "SELECT status FROM obligation_instances WHERE id = 'eversource:2026-06-25'"
    ).fetchone()[0]
    assert status != "paid"
    assert status == "expected"
    assert conn.execute(
        "SELECT COUNT(*) FROM transaction_obligation_matches WHERE obligation_instance_id = 'eversource:2026-06-25'"
    ).fetchone()[0] == 1

    # Only an explicit confirm, backed by the recorded transaction, flips it paid.
    res = confirm_reconciliation_match(conn, "eversource:2026-06-25")
    assert res["status"] == "paid"
    row = conn.execute(
        "SELECT status, matched_transaction_id, match_confidence FROM obligation_instances "
        "WHERE id = 'eversource:2026-06-25'"
    ).fetchone()
    assert row["status"] == "paid"
    assert row["matched_transaction_id"] == "t-ever"
    assert row["match_confidence"] is not None


def test_contract_no_autopay_without_exact_match(tmp_path):
    conn = _seed_recon_db(
        tmp_path / "c2b.sqlite",
        transactions=[("t-amex", "ACT-chk", "2026-07-16T08:00:00", -3450.00, "American Express", "AMEX EPAYMENT")],
    )
    _seed_instance(
        conn, "amex_statement_payment", "Amex statement payment", "credit_card_statement",
        [{"id": "amex_statement_payment:2026-07-16", "due_date": "2026-07-16", "amount": -3456.78, "source": "seed"}],
    )

    # Within tolerance but not exact -> needs_review. Even with auto_mark_paid on,
    # the only auto-pay path is an EXACT auto match, so nothing flips to paid.
    summary = reconcile_obligation_instances(
        conn, as_of_date="2026-07-20", options={"auto_mark_paid": True}
    )
    assert summary["matched_needs_review"] == 1
    assert summary["matched_auto"] == 0
    assert summary["marked_paid"] == 0

    status = conn.execute(
        "SELECT status FROM obligation_instances WHERE id = 'amex_statement_payment:2026-07-16'"
    ).fetchone()[0]
    assert status != "paid"

    # The ONLY paid path is the evidence-backed explicit confirm.
    res = confirm_reconciliation_match(conn, "amex_statement_payment:2026-07-16")
    assert res["status"] == "paid"
    assert res["matched_transaction_id"] == "t-amex"


# ---------------------------------------------------------------------------
# CONTRACT 3: emission lifecycle - open, idempotent dedup, resolve, retire
# ---------------------------------------------------------------------------


def test_contract_emission_open_then_idempotent_dedup(tmp_path):
    conn = _emission_db(tmp_path / "c3a.db")
    spy = _Spy()
    item = {"surface_key": "contract:item-1", "content": "Pay rent", "description": "due soon"}

    s1 = _surface(conn, [item], spy)
    assert s1["created"] == 1
    assert len(spy.creates) == 1
    row = conn.execute(
        "SELECT status, todoist_task_id FROM todoist_emissions WHERE surface_key = 'contract:item-1'"
    ).fetchone()
    assert row["status"] == "open"
    assert row["todoist_task_id"] == "T1"

    # Second pass, same item: idempotent skip, no duplicate create, one ledger row.
    s2 = _surface(conn, [item], spy)
    assert s2["created"] == 0
    assert s2["skipped"] == 1
    assert len(spy.creates) == 1
    assert conn.execute("SELECT COUNT(*) FROM todoist_emissions").fetchone()[0] == 1


def test_contract_emission_resolve_and_retire_transitions(tmp_path):
    conn = _emission_db(tmp_path / "c3b.db")
    spy = _Spy()
    item = {"surface_key": "contract:item-1", "content": "Pay rent", "description": "due soon"}

    _surface(conn, [item], spy)

    # RESOLVE: a completed emission is not recreated on the next pass.
    mark_emission_status(conn, "contract:item-1", "completed")
    resolved_summary = _surface(conn, [item], spy)
    assert resolved_summary["resolved"] >= 1
    assert resolved_summary["created"] == 0
    assert conn.execute(
        "SELECT status FROM todoist_emissions WHERE surface_key = 'contract:item-1'"
    ).fetchone()[0] == "completed"

    # RETIRE: a fresh key, flagged for retire, is deleted in Todoist and the ledger
    # row flips to 'retired' on the next surface drain.
    item2 = {"surface_key": "contract:item-2", "content": "Cancel sub", "description": "no longer due"}
    _surface(conn, [item2], spy)
    request_emission_retire(conn, "contract:item-2")
    # Drain without re-listing item2: the retire drain runs regardless of the
    # items list, and re-listing a retired key would resurface it (INSERT OR
    # REPLACE), which is the intended recurring-key behavior, not what we assert.
    delete_spy = _DeleteSpy()
    retire_summary = _surface(conn, [], spy, delete_spy=delete_spy)
    assert retire_summary["retired"] >= 1
    assert delete_spy.deleted  # the task id was passed to the delete path
    assert conn.execute(
        "SELECT status FROM todoist_emissions WHERE surface_key = 'contract:item-2'"
    ).fetchone()[0] == "retired"


# ---------------------------------------------------------------------------
# CONTRACT 4: projection reads ONLY obligation_instances
# ---------------------------------------------------------------------------


def test_contract_projection_ignores_raw_candidate_table_rows(tmp_path):
    conn = _seed_source_db(
        tmp_path / "c4.sqlite",
        accounts=[CHECKING],
        transactions=EVERSOURCE_CHECKING_ROWS,
        with_balances=True,
    )
    accounts = _checking_accounts(5000.0)
    start = date(2026, 7, 1)
    windows = [120]

    before, _ = build_cash_flow_projections(
        conn, accounts=accounts, windows=windows, start_date=start
    )
    assert conn.execute("SELECT COUNT(*) FROM obligation_instances").fetchone()[0] == 0
    assert before[0]["events"] == []
    assert before[0]["ending_balance"] == 5000.0

    # Insert a large candidate DIRECTLY into the candidate table (bypassing scan)
    # with NO applied obligation/instance. The projection must ignore it entirely.
    now = "2026-06-24T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO charge_onboarding_candidates (
            id, merchant_key, display_name, account_class, direction, status,
            candidate_type, cash_flow_treatment, priority_score, evidence_count,
            confidence, created_at, updated_at
        ) VALUES (
            'raw-candidate-1', 'phantom_landlord', 'Phantom Rent', 'checking', 'outflow', 'proposed',
            'recurring_bill', 'direct_checking', 99.0, 5,
            'high', ?, ?
        )
        """,
        (now, now),
    )
    conn.commit()

    after, _ = build_cash_flow_projections(
        conn, accounts=accounts, windows=windows, start_date=start
    )
    assert after[0]["ending_balance"] == before[0]["ending_balance"] == 5000.0
    assert after[0]["events"] == []
