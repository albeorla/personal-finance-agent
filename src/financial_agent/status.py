from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .cashflow import build_cash_flow_projections
from .drift import detect_drift
from .guardrails import evaluate_guardrails
from .manual_balance import BALANCE_PRECEDENCE_ORDER_BY


SCHEMA_VERSION = "finance_status.v1"
DEFAULT_WINDOWS = [7, 14, 30]
DEFAULT_FRESHNESS_HOURS = 24
BALANCE_DATE_STALE_DAYS = 3
# The working (checking) account gets a tighter staleness bar than other
# accounts: a 1-day-old checking balance can already hide a payday or a big
# debit, so cash-danger colors should not trust it. Balance-only feeds like
# the Apple Card ("Updated Monthly") stay on the general 3-day threshold above
# so they are not spuriously flagged every day.
WORKING_BALANCE_STALE_DAYS = 1


def default_db_path() -> Path:
    """Resolve the finance DB path from FINANCE_AGENT_DB_PATH, bootstrapping its dir.

    The server repo holds code only - it ships no database and does NOT fall back
    to an in-repo file (a silent fallback would let the engine quietly read a
    stale local copy). The caller points FINANCE_AGENT_DB_PATH at the DB in their
    own working directory; we create that directory if it is missing, and sqlite
    plus ensure_app_schema then create the file and app tables on first use - so
    pointing at a fresh path in a chosen directory bootstraps cleanly.
    """

    configured = os.environ.get("FINANCE_AGENT_DB_PATH")
    if not configured:
        raise RuntimeError(
            "FINANCE_AGENT_DB_PATH is not set. The MCP server ships no built-in "
            "database; set FINANCE_AGENT_DB_PATH to your finance DB path (it is "
            "created, with its parent directory, if it does not yet exist)."
        )
    path = Path(configured).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_finance_status(
    *,
    db_path: str | Path | None = None,
    windows: list[int] | None = None,
    working_account_id: str | None = None,
    start_date: str | None = None,
    now: datetime | None = None,
    compact: bool = False,
) -> dict[str, Any]:
    """Return the first read-only finance status shape.

    This intentionally starts with balances and source freshness. Cash-flow,
    drift, recurring, and Todoist review candidates keep stable empty slots so
    callers can integrate against the production-shaped contract now.
    """

    resolved_db_path = Path(db_path).expanduser() if db_path is not None else default_db_path()
    observed_at = now or datetime.now(UTC)
    requested_windows = windows or DEFAULT_WINDOWS
    projection_start_date = _parse_date(start_date) if start_date else observed_at.date()
    trace_id = _new_id("trace")
    balances_result_id = _new_id("result")

    with _connect(resolved_db_path) as conn:
        accounts = _latest_balances(conn, as_of=projection_start_date)
        source_freshness = _source_freshness(conn, observed_at)
        cash_flow_projections, cash_flow_warnings = build_cash_flow_projections(
            conn,
            accounts=accounts,
            windows=requested_windows,
            start_date=projection_start_date,
            working_account_id=working_account_id,
            working_balance_stale_days=WORKING_BALANCE_STALE_DAYS,
        )
        drift_result = detect_drift(conn, as_of_date=projection_start_date, persist=False)
        guardrail_result = evaluate_guardrails(
            conn,
            as_of_date=projection_start_date,
            accounts=accounts,
            drift_findings=drift_result["findings"],
            now=observed_at,
            persist=False,
        )

    drift_warnings = [f for f in drift_result["findings"] if f["finding_type"] != "unexpected_recurring"]
    recurring_candidates = [f for f in drift_result["findings"] if f["finding_type"] == "unexpected_recurring"]
    guardrail_findings = guardrail_result["findings"]

    total_balance = round(sum(account["balance"] for account in accounts), 2)
    total_available = round(sum(account["available"] for account in accounts), 2)

    if compact:
        cash_flow_projections = _compact_projections(cash_flow_projections)

    return {
        "schema_version": SCHEMA_VERSION,
        "trace_id": trace_id,
        "result_refs": [balances_result_id],
        "observed_at": observed_at.isoformat(),
        "requested_windows_days": requested_windows,
        "balances": {
            "result_id": balances_result_id,
            "currency": "USD",
            "total_balance": total_balance,
            "total_available": total_available,
            # Spendable deposit cash: available over deposit accounts only
            # (balance >= 0). Prefer this over total_available, which folds a
            # card's negative available into the sum. (See digest/grounding.)
            "liquid_available": round(sum(a["available"] for a in accounts if a["balance"] >= 0), 2),
            "accounts": accounts,
            "provenance": {
                "database": str(resolved_db_path),
                "tables": ["accounts", "balance_snapshots"],
                "selection": "latest balance snapshot per account by recorded_at and id",
            },
        },
        "source_freshness": source_freshness,
        "cash_flow_projections": cash_flow_projections,
        "drift_warnings": drift_warnings,
        "recurring_candidates": recurring_candidates,
        "guardrail_findings": guardrail_findings,
        "todoist_review_candidates": [],
        "warnings": [
            *cash_flow_warnings,
            *[g["message"] for g in guardrail_findings if not g.get("advisory")],
            "Todoist review candidates are not implemented yet",
        ],
    }


def _compact_projections(projections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace each projection's per-event array with a count.

    Keeps all summary stats (window, balances, lowest point, provenance) so the
    shape stays useful for triage, but drops the large `events` list that makes
    the full status response blow past model token limits.
    """
    compact = []
    for projection in projections:
        if "events" not in projection:
            compact.append(projection)
            continue
        trimmed = {k: v for k, v in projection.items() if k != "events"}
        trimmed["events_count"] = len(projection["events"])
        compact.append(trimmed)
    return compact


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Finance database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# Canonical balance-resolution precedence. A manual snapshot is STICKY: if any
# manual correction exists (within the as_of filter), the latest manual wins over
# every feed (simplefin) snapshot regardless of calendar day. A manual balance
# for an "Updated Monthly" account (e.g. Apple Card) stays authoritative until the
# user records a NEWER manual correction (which replaces the older one) or clears
# it -- so the next day's feed sync cannot shadow it. When NO manual snapshot
# exists, the latest feed snapshot wins. Every consumer that needs "the current
# balance for an account" (status, the debts layer, the avalanche, etc.) MUST
# order by this so they agree. Defined once in manual_balance and shared here.
_BALANCE_PRECEDENCE_ORDER_BY = BALANCE_PRECEDENCE_ORDER_BY


def resolve_account_balance(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    as_of: date | None = None,
) -> float | None:
    """Return one account's signed current balance using canonical precedence.

    This is the single source of truth for "what is this account's balance".
    It applies the same ``_BALANCE_PRECEDENCE_ORDER_BY`` that
    ``_latest_balances`` (and thus ``get_finance_status``) uses: a manual
    correction is sticky and wins over every feed snapshot regardless of calendar
    day, until a newer manual replaces it; when no manual exists, the latest feed
    snapshot wins. When ``as_of`` is given, snapshots recorded after that date are
    ignored (end-of-day inclusive), so callers can resolve a balance "as of" a
    projection date. Returns None when no snapshot qualifies.
    """

    params: list[Any] = [account_id]
    where = "WHERE account_id = ?"
    if as_of is not None:
        where += " AND recorded_at <= ?"
        params.append(f"{as_of.isoformat()}T23:59:59.999999")
    row = conn.execute(
        f"""
        SELECT balance
        FROM balance_snapshots
        {where}
        {_BALANCE_PRECEDENCE_ORDER_BY.format(alias="balance_snapshots")}
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None or row["balance"] is None:
        return None
    return round(float(row["balance"]), 2)


def _latest_balances(conn: sqlite3.Connection, *, as_of: date | None = None) -> list[dict[str, Any]]:
    # An app-only DB (obligations seeded, no SimpleFIN sync yet) has no source
    # tables. Degrade to "no balances" rather than crash, so digest/surface
    # consumers work on a balances-free DB the same way they tolerate a missing
    # balance_date column below.
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='balance_snapshots'"
    ).fetchone():
        return []
    columns = {row[1] for row in conn.execute("PRAGMA table_info(balance_snapshots)").fetchall()}
    balance_date_select = "bs.balance_date" if "balance_date" in columns else "NULL"
    rows = conn.execute(
        f"""
        SELECT
            a.id AS account_id,
            a.name AS account_name,
            a.org,
            a.kind,
            a.currency,
            bs.balance,
            bs.available,
            bs.recorded_at,
            bs.source,
            {balance_date_select} AS balance_date
        FROM balance_snapshots bs
        JOIN accounts a ON a.id = bs.account_id
        WHERE bs.id = (
            SELECT inner_bs.id
            FROM balance_snapshots inner_bs
            WHERE inner_bs.account_id = bs.account_id
            {_BALANCE_PRECEDENCE_ORDER_BY.format(alias="inner_bs")}
            LIMIT 1
        )
        ORDER BY a.name COLLATE NOCASE
        """
    ).fetchall()
    as_of_date = as_of or date.today()
    accounts = []
    for row in rows:
        balance_date = _snapshot_date(row["balance_date"]) or _snapshot_date(row["recorded_at"])
        age_days = (as_of_date - balance_date).days if balance_date else None
        accounts.append(
            {
                "account_id": row["account_id"],
                "account_name": row["account_name"],
                "org": row["org"],
                "kind": row["kind"],
                "currency": row["currency"],
                "balance": round(float(row["balance"]), 2),
                "available": round(float(row["available"]), 2),
                "recorded_at": row["recorded_at"],
                "balance_date": balance_date.isoformat() if balance_date else None,
                "balance_age_days": age_days,
                "balance_date_stale": bool(age_days is not None and age_days > BALANCE_DATE_STALE_DAYS),
                "source": row["source"],
            }
        )
    return accounts


def _snapshot_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _source_freshness(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sync_runs)").fetchall()}
    warnings_select = "warnings_json" if "warnings_json" in columns else "NULL AS warnings_json"
    latest_sync = conn.execute(
        f"""
        SELECT finished_at, mode, accounts_seen, transactions_inserted,
               transactions_updated, error, {warnings_select}
        FROM sync_runs
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    warnings: list[str] = []
    if latest_sync is not None and latest_sync["warnings_json"]:
        try:
            warnings = json.loads(latest_sync["warnings_json"]) or []
        except (ValueError, TypeError):
            warnings = []
    return {
        "simplefin": _freshness_record(
            latest_sync,
            now,
            extra_fields=["mode", "accounts_seen", "transactions_inserted", "transactions_updated"],
            warnings=warnings,
        ),
    }


def _freshness_record(
    row: sqlite3.Row | None,
    now: datetime,
    *,
    extra_fields: list[str],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    if row is None:
        return {
            "status": "unknown",
            "last_finished_at": None,
            "age_hours": None,
            "error": "no sync run recorded",
        }

    warnings = warnings or []
    finished_at = _parse_datetime(row["finished_at"])
    age_hours = round((now - finished_at).total_seconds() / 3600, 2)
    # A per-connection warning (one feed needs attention) is NOT a failed sync:
    # reserve "error" for a sync that actually threw or returned no usable data,
    # so a single flaky feed does not turn the whole source red. Warnings surface
    # as their own state instead of being promoted to error.
    if row["error"]:
        status = "error"
    elif warnings:
        status = "warning"
    elif age_hours > DEFAULT_FRESHNESS_HOURS:
        status = "stale"
    else:
        status = "fresh"

    record = {
        "status": status,
        "last_finished_at": finished_at.isoformat(),
        "age_hours": age_hours,
        "max_fresh_age_hours": DEFAULT_FRESHNESS_HOURS,
        "error": row["error"],
        "warnings": warnings,
    }
    for field in extra_fields:
        record[field] = row[field]
    return record


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
