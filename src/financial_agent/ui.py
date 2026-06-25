"""Local debugging / telemetry UI (BUILD_PLAN M6).

A tiny inspector for exercising and debugging the finance agent outside Claude
Code. It surfaces the things that make the agent's behavior legible: the current
finance status (the answer), the latest background run's operation timeline (the
tool-call timeline), the telemetry JSON, the review queue and drift findings (the
context snapshot), and any errors.

Stdlib only: there is NO web framework. The testable core is the pure ``route``
function ((db_path, path, query) -> (status, content_type, body)); the HTTP layer
(``serve`` / ``InspectorHandler``) is a thin wrapper around it. Run it with::

    uv run financial-agent-ui            # or: uv run python -m financial_agent.ui

This is a developer/test surface, not the product. It never mutates anything: it
only reads status, runs, queue, drift, and memory.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .drift import list_drift_findings
from .onboarding import list_charge_onboarding_queue
from .status import default_db_path, get_finance_status


def build_inspector_payload(db_path: str, *, as_of_date: str | None = None) -> dict[str, Any]:
    """Assemble everything the inspector shows into one read-only payload."""

    as_of = as_of_date or date.today().isoformat()
    payload: dict[str, Any] = {"generated_at": datetime.now().astimezone().isoformat(), "as_of_date": as_of}

    try:
        payload["status"] = get_finance_status(db_path=db_path, start_date=as_of)
    except Exception as exc:  # noqa: BLE001 - inspector must render even on a partial DB
        payload["status"] = None
        payload["status_error"] = str(exc)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        payload["latest_run"] = _latest_run(conn)
        payload["queue_top"] = _safe(lambda: list_charge_onboarding_queue(conn, limit=10), [])
        payload["drift_active"] = _safe(lambda: list_drift_findings(conn, status="active"), [])
        payload["memory_count"] = _safe(lambda: conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 0)
        payload["outbox"] = _safe(
            lambda: [dict(r) for r in conn.execute(
                "SELECT idempotency_key, status, item_count, dry_run FROM action_outbox ORDER BY created_at DESC LIMIT 10"
            ).fetchall()],
            [],
        )
    finally:
        conn.close()
    return payload


def route(db_path: str, path: str, query: dict[str, list[str]]) -> tuple[int, str, bytes]:
    """Pure request router. Returns (status_code, content_type, body)."""

    as_of = (query.get("as_of") or [None])[0]
    if path in ("/", "/index.html"):
        payload = build_inspector_payload(db_path, as_of_date=as_of)
        return 200, "text/html; charset=utf-8", render_inspector_html(payload).encode("utf-8")
    if path == "/api/inspect":
        payload = build_inspector_payload(db_path, as_of_date=as_of)
        return 200, "application/json", json.dumps(payload, indent=2, default=str).encode("utf-8")
    if path == "/healthz":
        return 200, "application/json", b'{"ok": true}'
    return 404, "application/json", b'{"error": "not found"}'


def render_inspector_html(payload: dict[str, Any]) -> str:
    status = payload.get("status") or {}
    balances = status.get("balances", {})
    # Deposit liquidity only (balance >= 0); raw total_available folds a card's
    # negative available into a "spendable" line (see digest.py).
    deposit_liquid = round(sum((a.get("available") or 0) for a in balances.get("accounts", []) if (a.get("balance") or 0) >= 0), 2)
    projections = status.get("cash_flow_projections", []) or []
    drift = payload.get("drift_active", []) or []
    queue = payload.get("queue_top", []) or []
    run = payload.get("latest_run")

    proj_rows = "".join(
        f"<tr><td>{p.get('window_days')}d</td><td>${_n(p.get('ending_balance'))}</td>"
        f"<td>${_n(p.get('lowest_balance'))}</td><td>{escape(str(p.get('lowest_balance_date')))}</td></tr>"
        for p in projections
    ) or "<tr><td colspan=4>no projection</td></tr>"

    drift_rows = "".join(
        f"<li><b>{escape(f['severity'])}</b> {escape(f['finding_type'])} "
        f"&mdash; {escape((f.get('recommended_action') or '')[:90])}</li>"
        for f in drift[:12]
    ) or "<li>no active drift findings</li>"

    queue_rows = "".join(
        f"<li>priority {_n(c.get('priority_score'))} &mdash; {escape(str(c.get('display_name')))} "
        f"<span class=tag>{escape(str(c.get('candidate_type')))}</span></li>"
        for c in queue[:12]
    ) or "<li>queue empty</li>"

    if run:
        events = "".join(
            f"<tr><td>{e['event_seq']}</td><td>{escape(e['event_type'])}</td>"
            f"<td>{escape(str(e.get('status')))}</td></tr>"
            for e in run.get("events", [])
        )
        run_block = (
            f"<p>run <code>{escape(run['run_id'])}</code> &middot; status <b>{escape(run['status'])}</b> "
            f"&middot; {run.get('duration_ms')}ms</p>"
            f"<table><tr><th>#</th><th>operation</th><th>status</th></tr>{events}</table>"
        )
    else:
        run_block = "<p>no background run recorded yet. Run <code>run_background_sync</code>.</p>"

    status_error = payload.get("status_error")
    error_block = f"<div class=err>status error: {escape(status_error)}</div>" if status_error else ""

    return f"""<!doctype html>
<html><head><meta charset=utf-8><title>Finance Agent Inspector</title>
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem; max-width: 60rem; color: #1a1a1a; }}
 h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.05rem; margin-top: 1.6rem; border-bottom: 1px solid #ddd; }}
 table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
 td, th {{ border: 1px solid #e2e2e2; padding: .3rem .5rem; text-align: left; }}
 code {{ background: #f2f2f2; padding: 0 .2rem; }} .tag {{ background: #eef; padding: 0 .3rem; border-radius: 3px; font-size: .8rem; }}
 .err {{ background: #fee; border: 1px solid #f99; padding: .5rem; margin: .5rem 0; }}
 form {{ margin-bottom: 1rem; }} pre {{ background: #f7f7f7; padding: .6rem; overflow:auto; font-size: .8rem; }}
</style></head><body>
<h1>Finance Agent Inspector</h1>
<form method=get action=/>
  as-of date <input name=as_of value="{escape(payload['as_of_date'])}" placeholder=YYYY-MM-DD>
  <button>refresh</button>
  &middot; <a href="/api/inspect?as_of={escape(payload['as_of_date'])}">raw JSON</a>
</form>
{error_block}
<h2>Balances</h2>
<p>liquid (deposit accounts) <b>${_n(deposit_liquid)}</b> &middot; net incl. debt ${_n(balances.get('total_balance'))}</p>
<h2>Cash-flow projections</h2>
<table><tr><th>window</th><th>ending</th><th>lowest</th><th>lowest date</th></tr>{proj_rows}</table>
<h2>Drift &amp; review ({len(drift)})</h2><ul>{drift_rows}</ul>
<h2>Onboarding queue ({len(queue)})</h2><ul>{queue_rows}</ul>
<h2>Latest background run (tool-call timeline)</h2>{run_block}
<h2>Memory</h2><p>{payload.get('memory_count', 0)} stored memory record(s)</p>
<h2>Telemetry (raw)</h2>
<pre>{escape(json.dumps(run, indent=2, default=str)) if run else 'no run'}</pre>
</body></html>"""


# --- http wrapper ----------------------------------------------------------


def make_handler(db_path: str) -> type[BaseHTTPRequestHandler]:
    class InspectorHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server interface
            parsed = urlparse(self.path)
            status, content_type, body = route(db_path, parsed.path, parse_qs(parsed.query))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:  # quiet by default
            pass

    return InspectorHandler


def serve(db_path: str | None = None, host: str = "127.0.0.1", port: int = 8765) -> None:
    resolved = db_path or str(default_db_path())
    server = ThreadingHTTPServer((host, port), make_handler(resolved))
    print(f"finance-agent inspector on http://{host}:{port}  (db: {resolved})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def _latest_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT id FROM background_runs ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    from .background import get_background_run

    return get_background_run(conn, row["id"])


def _safe(fn: Any, default: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _n(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def main() -> None:
    import os

    serve(port=int(os.environ.get("FINANCE_AGENT_UI_PORT", "8765")))


if __name__ == "__main__":
    main()
