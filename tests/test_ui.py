"""Tests for the local inspector UI (M6). The pure router is the testable core."""

import http.client
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer

from financial_agent.background import run_background_sync
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema
from financial_agent.ui import (
    build_inspector_payload,
    make_handler,
    render_inspector_html,
    route,
)


def _status_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY, account_id TEXT, balance REAL, available REAL, recorded_at TEXT, source TEXT);
        CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, accounts_seen INT, transactions_inserted INT, transactions_updated INT, error TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT, amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);
        """
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('ACT-chk','PREMIER PLUS CKG (XXXX)','Chase','checking','USD')")
    conn.execute("INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) VALUES ('ACT-chk',5000,5000,'2026-06-20T00:00:00+00:00','simplefin')")
    conn.execute("INSERT INTO sync_runs (started_at,finished_at,mode,accounts_seen,transactions_inserted,transactions_updated,error) VALUES ('2026-06-20T09:58:00+00:00','2026-06-20T10:00:00+00:00','incremental',1,3,0,NULL)")
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    apply_obligation_instances(
        conn,
        obligation={"id": "rent", "name": "Rent check", "kind": "housing", "status": "active", "source": "seed"},
        instances=[{"id": "rent:2026-06-25", "due_date": "2026-06-25", "amount": -3000.0, "source": "seed"}],
    )
    conn.commit()
    return conn


def test_render_inspector_html_is_self_contained(tmp_path):
    payload = {
        "as_of_date": "2026-06-21",
        "status": {"balances": {"total_available": 2912.29, "total_balance": 3000.0},
                   "cash_flow_projections": [{"window_days": 30, "ending_balance": 8008.5,
                                              "lowest_balance": 5440.28, "lowest_balance_date": "2026-06-25"}]},
        "drift_active": [{"severity": "high", "finding_type": "missing_expected", "recommended_action": "Confirm payment"}],
        "queue_top": [{"priority_score": 367.58, "display_name": "Gault Energy", "candidate_type": "card_statement_input"}],
        "latest_run": {"run_id": "run_abc", "status": "succeeded", "duration_ms": 25,
                       "events": [{"event_seq": 1, "event_type": "run_started", "status": "ok"}]},
        "memory_count": 3,
    }
    html = render_inspector_html(payload)
    assert "<!doctype html>" in html
    assert "Finance Agent Inspector" in html
    assert "Gault Energy" in html
    assert "8,008.50" in html
    assert "run_abc" in html
    assert "missing_expected" in html


def test_route_index_returns_html(tmp_path):
    db = tmp_path / "ui.sqlite"
    _status_db(db)
    status, content_type, body = route(str(db), "/", {"as_of": ["2026-06-20"]})
    assert status == 200
    assert "text/html" in content_type
    assert b"Finance Agent Inspector" in body


def test_route_api_returns_valid_json(tmp_path):
    db = tmp_path / "ui.sqlite"
    _status_db(db)
    status, content_type, body = route(str(db), "/api/inspect", {"as_of": ["2026-06-20"]})
    assert status == 200
    assert content_type == "application/json"
    payload = json.loads(body)
    assert payload["as_of_date"] == "2026-06-20"
    assert "status" in payload and "queue_top" in payload and "drift_active" in payload


def test_route_unknown_path_404(tmp_path):
    db = tmp_path / "ui.sqlite"
    _status_db(db)
    status, _, _ = route(str(db), "/nope", {})
    assert status == 404


def test_build_inspector_payload_includes_latest_run(tmp_path):
    db = tmp_path / "ui.sqlite"
    conn = _status_db(db)
    run_background_sync(conn, as_of_date="2026-06-20")
    conn.commit()
    conn.close()

    payload = build_inspector_payload(str(db), as_of_date="2026-06-20")
    assert payload["latest_run"] is not None
    assert payload["latest_run"]["status"] in ("succeeded", "partial_success")
    assert [e["event_type"] for e in payload["latest_run"]["events"]][0] == "run_started"


def test_live_server_serves_requests(tmp_path):
    db = tmp_path / "ui.sqlite"
    _status_db(db)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(str(db)))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True

        conn.request("GET", "/?as_of=2026-06-20")
        resp = conn.getresponse()
        assert resp.status == 200
        assert b"Finance Agent Inspector" in resp.read()
    finally:
        server.shutdown()
        thread.join(timeout=5)
