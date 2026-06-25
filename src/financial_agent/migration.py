"""Obligation migration from the legacy sources (cutover slice H).

DEPRECATED ENTRY POINT: this is a one-time legacy bootstrap, not an ongoing
source of truth. Cash-flow truth now lives entirely in the `obligation_instances`
table (see cashflow.py, which reads only that table). `obligations.yaml` and
`cash-flow.md` are retired and no longer authoritative; this module exists solely
to seed a fresh database from them once. Day-to-day cash flows must be entered via
the MCP tools (apply_obligation_instances, apply_charge_onboarding_candidate,
import_todoist_obligations), never by editing the legacy files.

Brings the complete current obligation set into canonical rows. The trustworthy,
machine-readable source is `obligations.yaml` (a JSON object with an `items`
array); `cash-flow.md` is a stale human narrative, so it is parsed cautiously and
its rows are always imported as `needs_review`.

Safety:
- Dedup is instance-level against everything ALREADY modeled (seeded obligations,
  income, Todoist one-offs): an item that matches an existing instance by name
  tokens + amount bucket + direction + a +/-7 day window is skipped as
  already_modeled, so migration never duplicates what is already there.
- Ambiguous rows (estimates, ranges, detector-neutralizing hacks) become
  `needs_review` instances rather than trusted obligations.
- `dry_run=True` (default) computes the full plan and writes nothing.
- The legacy files are read-only; this never writes to ~/dev/areas/finances.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .obligations import apply_obligation_instances
from .schema import ensure_app_schema


DEDUP_WINDOW_DAYS = 7

_LABEL_STOPWORDS: frozenset[str] = frozenset(
    {"pay", "payment", "the", "for", "and", "via", "from", "into", "due", "est",
     "estimate", "bill", "charge", "charges", "fee", "fees", "to", "of", "on",
     "tax", "taxes", "premium", "insurance", "subscription", "utility", "utilities",
     "rent", "mortgage", "loan", "credit", "debit", "service", "interest", "deposit",
     "statement", "monthly", "annual", "account", "balance", "minimum", "recurring"}
)

# Label/source hints that make a row needs_review rather than a trusted import.
_NEEDS_REVIEW_HINTS: tuple[str, ...] = (
    "estimate", "range", "could be", "rough", "midpoint", "uncertain", "tbd",
    "approx", "cancel", "neutralize", "neutralise", "offset", "dedup", "~",
)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_obligations_yaml(path: str) -> list[dict[str, Any]]:
    """Parse the legacy obligations.yaml (JSON) into normalized rows."""

    with open(path) as fh:
        data = json.load(fh)
    rows: list[dict[str, Any]] = []
    for item in data.get("items", []):
        amount = item.get("amount")
        due = item.get("date")
        if amount is None or not due:
            continue
        rows.append(_normalize_row(date_str=str(due)[:10], label=item.get("label", ""),
                                   signed=float(amount), source=item.get("source", "obligations_yaml")))
    return rows


def parse_cashflow_md(path: str, *, base_year: int = 2026) -> list[dict[str, Any]]:
    """Best-effort parse of cash-flow.md obligation tables. Every row is needs_review."""

    rows: list[dict[str, Any]] = []
    section: str | None = None
    with open(path) as fh:
        for line in fh:
            stripped = line.strip()
            if stripped.startswith("#"):
                low = stripped.lower()
                if "incoming pay" in low:
                    section = "inflow"
                elif "obligations due" in low or "anthem" in low:
                    section = "outflow" if "obligations" in low else "inflow"
                else:
                    section = None
                continue
            if section is None or not stripped.startswith("|"):
                continue
            cells = [c.strip().strip("*") for c in stripped.strip("|").split("|")]
            if len(cells) < 3 or cells[0].lower() in {"date", "due", "who"} or set(cells[0]) <= {"-", " "}:
                continue
            parsed_date = _parse_loose_date(cells[0], base_year)
            amount = _parse_money(" ".join(cells))
            if parsed_date is None or amount is None:
                continue
            signed = amount if section == "inflow" else -amount
            label = cells[1] if len(cells) > 1 else stripped
            rows.append(_normalize_row(date_str=parsed_date, label=label, signed=signed,
                                       source="cash-flow.md", force_needs_review=True))
    return rows


def apply_obligation_migration(
    conn: sqlite3.Connection,
    *,
    source: str = "obligations_yaml",
    path: str,
    dry_run: bool = True,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Migrate obligations from a legacy source, deduped, with a needs_review fallback."""

    ensure_app_schema(conn)
    opts = options or {}
    if source == "obligations_yaml":
        rows = parse_obligations_yaml(path)
    elif source == "cashflow_md":
        rows = parse_cashflow_md(path, base_year=int(opts.get("base_year", 2026)))
    else:
        raise ValueError(f"unsupported migration source: {source!r}")

    # Classify every row up front against the current DB state (deterministic).
    plan: list[dict[str, Any]] = []
    for row in rows:
        existing = _already_modeled(conn, row)
        if existing is not None:
            plan.append({**row, "decision": "already_modeled", "existing_obligation_id": existing})
        elif row["needs_review"]:
            plan.append({**row, "decision": "needs_review"})
        else:
            plan.append({**row, "decision": "new"})

    # Group importable rows by label into migrated obligations.
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in plan:
        if row["decision"] in {"new", "needs_review"}:
            groups.setdefault(_label_slug(row["label"]), []).append(row)

    created_obligations = 0
    created_instances = 0
    if not dry_run:
        for slug, items in groups.items():
            obligation_id = f"migrated_{slug}"
            cadence = "recurring" if len(items) > 1 else None
            instances = [
                {
                    "id": f"{obligation_id}:{it['date']}",
                    "due_date": it["date"],
                    "amount": it["amount"],
                    "direction": it["direction"],
                    "status": "needs_review" if it["decision"] == "needs_review" else "expected",
                    "source": f"migration:{source}",
                    "confidence": "low" if it["decision"] == "needs_review" else "medium",
                    "notes": f"Migrated from {source}: {it['label'][:80]} (src: {it['source'][:60]})",
                }
                for it in items
            ]
            apply_obligation_instances(
                conn,
                obligation={"id": obligation_id, "name": items[0]["label"], "kind": "migrated",
                            "cadence": cadence, "status": "active", "source": f"migration:{source}"},
                instances=instances,
            )
            created_obligations += 1
            created_instances += len(instances)

    skipped = sum(1 for r in plan if r["decision"] == "already_modeled")
    needs_review = sum(1 for r in plan if r["decision"] == "needs_review")
    log_id = f"migration_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO obligation_migration_log (
            id, run_timestamp, source_type, source_path, dry_run, parsed,
            created_obligations, created_instances, skipped_already_modeled,
            needs_review, errors_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (log_id, _now(), source, path, 1 if dry_run else 0, len(rows),
         created_obligations, created_instances, skipped, needs_review),
    )

    return {
        "source": source,
        "dry_run": dry_run,
        "parsed": len(rows),
        "obligations_to_create": len(groups),
        "created_obligations": created_obligations,
        "created_instances": created_instances,
        "skipped_already_modeled": skipped,
        "needs_review": needs_review,
        "plan": [
            {"date": r["date"], "label": r["label"][:60], "amount": r["amount"],
             "direction": r["direction"], "decision": r["decision"],
             "existing_obligation_id": r.get("existing_obligation_id")}
            for r in plan
        ],
        "migration_log_id": log_id,
    }


# --- internals -------------------------------------------------------------


def _normalize_row(*, date_str, label, signed, source, force_needs_review=False) -> dict[str, Any]:
    text = f"{label} {source}".lower()
    needs_review = force_needs_review or any(h in text for h in _NEEDS_REVIEW_HINTS)
    return {
        "date": date_str,
        "label": label,
        "signed_amount": round(float(signed), 2),
        "amount": round(abs(float(signed)), 2),
        "direction": "inflow" if float(signed) >= 0 else "outflow",
        "source": source,
        "needs_review": needs_review,
    }


def _already_modeled(conn: sqlite3.Connection, row: dict[str, Any]) -> str | None:
    due = _coerce_date(row["date"])
    lo = (due - timedelta(days=DEDUP_WINDOW_DAYS)).isoformat()
    hi = (due + timedelta(days=DEDUP_WINDOW_DAYS)).isoformat()
    tokens = _label_tokens(row["label"])
    if not tokens:
        return None
    rows = conn.execute(
        """
        SELECT oi.amount, o.id AS obligation_id, o.name AS obligation_name
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE oi.direction = ?
          AND oi.due_date BETWEEN ? AND ?
          AND o.status = 'active'
          AND oi.status != 'canceled'
        """,
        (row["direction"], lo, hi),
    ).fetchall()
    for r in rows:
        if not _amount_close(row["amount"], float(r["amount"])):
            continue
        if tokens & _label_tokens(r["obligation_name"]):
            return r["obligation_id"]
    return None


def _amount_close(a: float, b: float, abs_tol: float = 5.0, pct_tol: float = 0.05) -> bool:
    return abs(a - b) <= max(abs_tol, pct_tol * max(abs(a), abs(b)))


def _label_tokens(text: str) -> set[str]:
    toks = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {t for t in toks if len(t) >= 3 and t not in _LABEL_STOPWORDS and not t.isdigit()}


def _label_slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
    return slug[:48] or "unlabeled"


def _parse_loose_date(text: str, base_year: int) -> str | None:
    m = re.search(r"([a-z]{3})[a-z]*\s+(\d{1,2})", (text or "").lower())
    if not m or m.group(1) not in _MONTHS:
        return None
    try:
        return date(base_year, _MONTHS[m.group(1)], int(m.group(2))).isoformat()
    except ValueError:
        return None


def _parse_money(text: str) -> float | None:
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text or "")
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _now() -> str:
    return datetime.now().astimezone().isoformat()
