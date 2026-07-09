"""Statement-cycle aggregation for card-statement-input charges.

Card charges do not reduce checking directly; they roll into a monthly card
statement that is then paid from checking. This module groups
``card_statement_input`` obligation instances into the statement cycle that will
pay them, so a future statement-payment estimate can be built from real modeled
card spend instead of a blind guess.

Safety: this never overrides a portal/confirmed statement amount. A portal
balance is the best evidence we have, so ``recompute_statement_estimates`` only
fills in statement instances whose amount is an unconfirmed projection, and even
then it records full provenance. Aggregation is deterministic and idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from .schema import ensure_app_schema


CARD_STATEMENT_INPUT = "card_statement_input"

# Amount sources that represent real, observed statement evidence. A rollup
# estimate must never overwrite these.
PROTECTED_AMOUNT_SOURCES: frozenset[str] = frozenset(
    {
        "portal_current_balance_estimate",
        "portal_statement_amount",
        "statement_known",
        "statement_amount",
        "actual",
        "observed",
    }
)

PROJECTABLE_INPUT_STATUSES: tuple[str, ...] = ("expected", "needs_review", "partially_paid")

_CONFIDENCE_RANK: dict[str | None, int] = {"high": 3, "medium": 2, "low": 1, "very_low": 0, None: 0}
_RANK_CONFIDENCE: dict[int, str] = {3: "high", 2: "medium", 1: "low", 0: "low"}

ROLLUP_AMOUNT_SOURCE = "statement_input_rollup"


def aggregate_statement_inputs(
    conn: sqlite3.Connection,
    *,
    target_obligation_id: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Group card-statement-input instances into the statement cycle that pays them.

    Cycles are defined by the target obligation's statement instances that carry
    a ``statement_close_date``. Each card input is assigned to the first cycle
    whose close date is on or after the input's due date (and after the previous
    close). Inputs past the last known close are reported as ``unrolled``.
    """

    ensure_app_schema(conn)
    now = _now()

    cycles = _build_cycles(conn, target_obligation_id)
    inputs = conn.execute(
        f"""
        SELECT id, due_date, amount, confidence
        FROM obligation_instances
        WHERE cash_flow_treatment = ?
          AND statement_target_obligation_id = ?
          AND status IN ({",".join("?" for _ in PROJECTABLE_INPUT_STATUSES)})
        ORDER BY due_date, id
        """,
        (CARD_STATEMENT_INPUT, target_obligation_id, *PROJECTABLE_INPUT_STATUSES),
    ).fetchall()

    # Converted card inputs with no statement_target_obligation_id are bound to
    # no cycle; surface them so they are not silently invisible to projections.
    unbound = conn.execute(
        f"""
        SELECT id
        FROM obligation_instances
        WHERE cash_flow_treatment = ?
          AND statement_target_obligation_id IS NULL
          AND status IN ({",".join("?" for _ in PROJECTABLE_INPUT_STATUSES)})
        ORDER BY due_date, id
        """,
        (CARD_STATEMENT_INPUT, *PROJECTABLE_INPUT_STATUSES),
    ).fetchall()

    assignments: dict[str, list[sqlite3.Row]] = defaultdict(list)
    unrolled: list[sqlite3.Row] = []
    for inp in inputs:
        due = date.fromisoformat(inp["due_date"])
        cycle = _assign_cycle(due, cycles)
        if cycle is None:
            unrolled.append(inp)
        else:
            assignments[cycle["id"]].append(inp)

    live_cycle_ids: set[str] = set()
    for cycle in cycles:
        items = assignments.get(cycle["id"], [])
        input_sum = round(sum(abs(float(i["amount"])) for i in items), 2)
        confidence = _min_confidence([i["confidence"] for i in items]) if items else None
        _upsert_cycle(conn, cycle, target_obligation_id, len(items), input_sum, confidence, now)
        conn.execute(
            "DELETE FROM statement_cycle_inputs WHERE statement_cycle_id = ?", (cycle["id"],)
        )
        for item in items:
            conn.execute(
                """
                INSERT INTO statement_cycle_inputs (
                    statement_cycle_id, obligation_instance_id, input_amount,
                    input_confidence, due_date, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cycle["id"], item["id"], round(abs(float(item["amount"])), 2), item["confidence"], item["due_date"], now),
            )
        live_cycle_ids.add(cycle["id"])

    # Drop cycles (and their inputs) that no longer correspond to a statement instance.
    for row in conn.execute(
        "SELECT id FROM statement_cycles WHERE target_obligation_id = ?", (target_obligation_id,)
    ).fetchall():
        if row["id"] not in live_cycle_ids:
            conn.execute("DELETE FROM statement_cycle_inputs WHERE statement_cycle_id = ?", (row["id"],))
            conn.execute("DELETE FROM statement_cycles WHERE id = ?", (row["id"],))

    return {
        "target_obligation_id": target_obligation_id,
        "cycles": len(cycles),
        "inputs_total": len(inputs),
        "inputs_assigned": sum(len(v) for v in assignments.values()),
        "unrolled_inputs": len(unrolled),
        "unrolled_instance_ids": [i["id"] for i in unrolled],
        "unbound_inputs": len(unbound),
        "unbound_instance_ids": [i["id"] for i in unbound],
    }


def list_statement_cycles(
    conn: sqlite3.Connection,
    *,
    target_obligation_id: str,
) -> list[dict[str, Any]]:
    """List statement cycles with their aggregated card-input evidence."""

    ensure_app_schema(conn)
    rows = conn.execute(
        """
        SELECT id, target_obligation_id, statement_instance_id, cycle_open_date,
               cycle_close_date, due_date, input_count, input_sum, confidence
        FROM statement_cycles
        WHERE target_obligation_id = ?
        ORDER BY cycle_close_date
        """,
        (target_obligation_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        inputs = conn.execute(
            """
            SELECT obligation_instance_id, input_amount, input_confidence, due_date
            FROM statement_cycle_inputs
            WHERE statement_cycle_id = ?
            ORDER BY due_date, obligation_instance_id
            """,
            (row["id"],),
        ).fetchall()
        result.append(
            {
                "id": row["id"],
                "target_obligation_id": row["target_obligation_id"],
                "statement_instance_id": row["statement_instance_id"],
                "cycle_open_date": row["cycle_open_date"],
                "cycle_close_date": row["cycle_close_date"],
                "due_date": row["due_date"],
                "input_count": row["input_count"],
                "input_sum": round(float(row["input_sum"]), 2),
                "confidence": row["confidence"],
                "inputs": [
                    {
                        "obligation_instance_id": i["obligation_instance_id"],
                        "input_amount": round(float(i["input_amount"]), 2),
                        "input_confidence": i["input_confidence"],
                        "due_date": i["due_date"],
                    }
                    for i in inputs
                ],
            }
        )
    return result


def get_statement_status(
    conn: sqlite3.Connection,
    *,
    obligation_id: str,
    as_of_date: str | date | None = None,
) -> dict[str, Any]:
    """Return closed-statement and open-cycle status for one statement obligation."""

    as_of = date.today() if as_of_date is None else date.fromisoformat(str(as_of_date)[:10])
    aggregate_statement_inputs(conn, target_obligation_id=obligation_id)
    cycles = list_statement_cycles(conn, target_obligation_id=obligation_id)

    closed_cycle: dict[str, Any] | None = None
    open_cycle_row: dict[str, Any] | None = None
    for cycle in cycles:
        close = date.fromisoformat(cycle["cycle_close_date"])
        if close <= as_of:
            closed_cycle = cycle
        elif open_cycle_row is None:
            open_cycle_row = cycle

    notes: list[str] = []
    closed_statement = None
    if closed_cycle is None:
        notes.append("no statement cycle closes on or before as_of_date")
    else:
        row = conn.execute(
            """
            SELECT amount, amount_status, amount_source, due_date
            FROM obligation_instances
            WHERE id = ?
            """,
            (closed_cycle["statement_instance_id"],),
        ).fetchone()
        if row is None:
            notes.append("closed statement instance is missing")
        else:
            closed_statement = {
                "statement_instance_id": closed_cycle["statement_instance_id"],
                "cycle_close_date": closed_cycle["cycle_close_date"],
                "amount": round(float(row["amount"]), 2) if row["amount"] is not None else None,
                "amount_status": row["amount_status"],
                "amount_source": row["amount_source"],
                "due_date": row["due_date"],
            }
            if row["amount_status"] != "confirmed":
                notes.append(f"closed statement amount is {row['amount_status']}")

    open_cycle = None
    modeled_amount_for_open_cycle = None
    modeled_amount = None
    if open_cycle_row is None:
        notes.append("no statement cycle closes after as_of_date")
    else:
        spend_so_far = (
            round(float(open_cycle_row["input_sum"]), 2)
            if open_cycle_row["input_sum"] is not None
            else None
        )
        open_cycle = {
            "cycle_open_date": open_cycle_row["cycle_open_date"],
            "cycle_close_date": open_cycle_row["cycle_close_date"],
            "spend_so_far": spend_so_far,
            "input_count": open_cycle_row["input_count"],
            "confidence": open_cycle_row["confidence"],
        }
        row = conn.execute(
            """
            SELECT amount, amount_status, amount_source
            FROM obligation_instances
            WHERE id = ?
            """,
            (open_cycle_row["statement_instance_id"],),
        ).fetchone()
        if row is None:
            notes.append("open cycle statement instance is missing")
        else:
            modeled_amount = round(float(row["amount"]), 2) if row["amount"] is not None else None
            modeled_amount_for_open_cycle = {
                "amount": modeled_amount,
                "amount_status": row["amount_status"],
                "amount_source": row["amount_source"],
            }
            if row["amount_status"] != "confirmed":
                notes.append(f"open cycle modeled amount is {row['amount_status']}")

    variance = (
        round(modeled_amount - open_cycle["spend_so_far"], 2)
        if modeled_amount is not None
        and open_cycle is not None
        and open_cycle["spend_so_far"] is not None
        else None
    )

    on_track = None
    if (
        open_cycle is not None
        and open_cycle["spend_so_far"] is not None
        and modeled_amount is not None
        and modeled_amount != 0
    ):
        if not open_cycle["cycle_open_date"] or not open_cycle["cycle_close_date"]:
            notes.append("on_track unavailable because open cycle dates are missing")
        else:
            opened = date.fromisoformat(open_cycle["cycle_open_date"])
            closed = date.fromisoformat(open_cycle["cycle_close_date"])
            cycle_days = (closed - opened).days
            if cycle_days <= 0:
                notes.append("on_track unavailable because open cycle length is zero")
            else:
                elapsed_fraction = max(0.0, min(1.0, (as_of - opened).days / cycle_days))
                expected_so_far = modeled_amount * elapsed_fraction
                on_track = "ahead" if open_cycle["spend_so_far"] <= expected_so_far else "behind"
    elif open_cycle is not None:
        notes.append("on_track unavailable because open cycle has no spend total or modeled amount")

    return {
        "obligation_id": obligation_id,
        "as_of_date": as_of.isoformat(),
        "closed_statement": closed_statement,
        "open_cycle": open_cycle,
        "modeled_amount_for_open_cycle": modeled_amount_for_open_cycle,
        "variance": variance,
        "on_track": on_track,
        "notes": notes,
    }


def _prior_rollup_baseline(inst: sqlite3.Row) -> float | None:
    """Return the non-modeled baseline recorded by a previous rollup, or None.

    Only trusted when the prior estimate itself came from this rollup source;
    otherwise the caller falls back to backing the baseline out of the amount.
    """
    if (inst["amount_source"] or "") != ROLLUP_AMOUNT_SOURCE:
        return None
    raw = inst["estimation_inputs_json"]
    if not raw:
        return None
    try:
        prior = json.loads(raw)
    except (ValueError, TypeError):
        return None
    baseline = prior.get("baseline") if isinstance(prior, dict) else None
    if isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
        return None
    return round(float(baseline), 2)


def recompute_statement_estimates(
    conn: sqlite3.Connection,
    *,
    target_obligation_id: str,
    baseline: float | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill unconfirmed statement estimates from the card-input rollup.

    Guarded on purpose: only statement instances whose ``amount_status`` is
    ``estimated`` and whose ``amount_source`` is not a protected portal/observed
    source are recomputed, as ``baseline`` (the caller's expected non-modeled
    card spend) plus the rolled-up modeled card inputs for that cycle. Portal and
    confirmed amounts are left untouched. Idempotent.

    When no ``baseline`` is supplied, the existing estimate is preserved rather
    than clobbered down to inputs-only: the non-modeled portion is backed out of
    the current amount so the recompute never lowers an estimate it cannot
    improve (money-safe default).
    """

    ensure_app_schema(conn)
    agg = aggregate_statement_inputs(conn, target_obligation_id=target_obligation_id)
    now = _now()
    explicit_baseline = baseline is not None
    baseline = round(float(baseline), 2) if explicit_baseline else 0.0

    cycles_by_instance = {
        row["statement_instance_id"]: row
        for row in conn.execute(
            "SELECT statement_instance_id, input_sum, input_count, confidence, cycle_close_date "
            "FROM statement_cycles WHERE target_obligation_id = ? AND statement_instance_id IS NOT NULL",
            (target_obligation_id,),
        ).fetchall()
    }

    statement_instances = conn.execute(
        """
        SELECT id, amount, amount_status, amount_source, estimation_inputs_json
        FROM obligation_instances
        WHERE obligation_id = ?
          AND statement_close_date IS NOT NULL
        ORDER BY due_date
        """,
        (target_obligation_id,),
    ).fetchall()

    updated = 0
    skipped_protected = 0
    skipped_no_cycle = 0
    floored = 0
    warnings_floored: list[str] = []
    details: list[dict[str, Any]] = []
    for inst in statement_instances:
        if inst["amount_status"] != "estimated":
            continue
        if (inst["amount_source"] or "") in PROTECTED_AMOUNT_SOURCES:
            skipped_protected += 1
            continue
        cycle = cycles_by_instance.get(inst["id"])
        if cycle is None:
            skipped_no_cycle += 1
            continue
        prior_amount = round(abs(float(inst["amount"])), 2) if inst["amount"] is not None else 0.0
        input_sum = round(float(cycle["input_sum"]), 2)
        if explicit_baseline:
            inst_baseline = baseline
        else:
            # Prefer the non-modeled baseline recorded the last time this
            # estimate was rolled up. Backing it out of the current amount
            # (existing - input_sum) silently drops the baseline to 0 once the
            # card-input total grows past the old estimate, so a rising input
            # sum would erase real non-modeled spend. The stored baseline is
            # stable across input growth.
            inst_baseline = _prior_rollup_baseline(inst)
            if inst_baseline is None:
                existing = round(abs(float(inst["amount"])), 2) if inst["amount"] is not None else 0.0
                inst_baseline = max(round(existing - input_sum, 2), 0.0)
        new_amount = round(inst_baseline + input_sum, 2)
        if not explicit_baseline and new_amount < prior_amount:
            # Money-safe floor: an auto-recompute (no explicit baseline) never
            # silently lowers a standing estimate. A shrinking modeled input_sum
            # means charges moved cycle or unbound (evidence lost), not that
            # spend fell, so the smaller number is corruption, not a correction.
            # INC-20260708-3: a no-baseline recompute once dropped $6,156.66 to
            # ~$1,604. Leave the instance untouched and surface it; only an
            # explicit baseline (an authoritative caller decision) may lower it.
            floored += 1
            warnings_floored.append(
                f"recompute would have lowered {inst['id']} from {prior_amount} to "
                f"{new_amount} (modeled inputs shrank); kept the prior estimate. Pass an "
                f"explicit baseline to authorize a lower amount."
            )
            continue
        estimation_inputs = {
            "baseline": inst_baseline,
            "card_input_sum": input_sum,
            "input_count": cycle["input_count"],
            "cycle_close_date": cycle["cycle_close_date"],
        }
        confidence = cycle["confidence"] or "low"
        conn.execute(
            """
            UPDATE obligation_instances
            SET amount = ?,
                amount_status = 'estimated',
                amount_source = ?,
                estimation_method = ?,
                estimation_inputs_json = ?,
                confidence = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (new_amount, ROLLUP_AMOUNT_SOURCE, ROLLUP_AMOUNT_SOURCE, json.dumps(estimation_inputs, sort_keys=True), confidence, now, inst["id"]),
        )
        updated += 1
        details.append({"instance_id": inst["id"], "amount": new_amount, "card_input_sum": estimation_inputs["card_input_sum"]})

    warnings: list[str] = list(warnings_floored)
    if explicit_baseline and baseline == 0.0 and updated:
        warnings.append(
            "baseline is 0, so recomputed estimates reflect only modeled card inputs and likely "
            "underestimate the full statement; pass a baseline for normal non-modeled card spend"
        )
    if agg["unbound_inputs"]:
        warnings.append(
            f"{agg['unbound_inputs']} converted card input(s) have no statement target and are "
            "excluded from every statement rollup; bind them to the paying statement obligation"
        )
    return {
        "target_obligation_id": target_obligation_id,
        "baseline": baseline if explicit_baseline else None,
        "updated": updated,
        "skipped_protected": skipped_protected,
        "skipped_no_cycle": skipped_no_cycle,
        "floored": floored,
        "unbound_inputs": agg["unbound_inputs"],
        "unbound_instance_ids": agg["unbound_instance_ids"],
        "details": details,
        "warnings": warnings,
    }


def set_statement_actual(
    conn: sqlite3.Connection,
    *,
    obligation_id: str,
    amount: float,
    cycle_close_date: str | None = None,
    due_date: str | None = None,
    source: str = "portal_statement_amount",
    note: str | None = None,
) -> dict[str, Any]:
    """Record an observed statement balance directly on the matching statement instance.

    The direct-entry path for the monthly portal-reading ritual (Apple Card is
    balance-only forever, so its statement amount arrives by human observation).
    Selects the statement instance by ``cycle_close_date`` or ``due_date``,
    writes the observed amount with ``amount_status='confirmed'`` plus
    provenance, and the rollup estimator will never overwrite it (confirmed
    amounts are protected).
    """

    ensure_app_schema(conn)
    if not cycle_close_date and not due_date:
        raise ValueError("pass cycle_close_date or due_date to pick the statement instance")

    where = ["obligation_id = ?", "statement_close_date IS NOT NULL"]
    params: list[Any] = [obligation_id]
    if cycle_close_date:
        where.append("statement_close_date = ?")
        params.append(str(cycle_close_date)[:10])
    if due_date:
        where.append("due_date = ?")
        params.append(str(due_date)[:10])
    rows = conn.execute(
        f"""
        SELECT id, due_date, statement_close_date, amount
        FROM obligation_instances
        WHERE {" AND ".join(where)}
        ORDER BY due_date, id
        """,
        params,
    ).fetchall()
    if not rows:
        known = conn.execute(
            "SELECT due_date, statement_close_date FROM obligation_instances "
            "WHERE obligation_id = ? AND statement_close_date IS NOT NULL ORDER BY due_date",
            (obligation_id,),
        ).fetchall()
        cycles = (
            "; ".join(f"close {r['statement_close_date']} due {r['due_date']}" for r in known)
            or "none"
        )
        raise ValueError(
            f"no statement instance for {obligation_id} matching "
            f"cycle_close_date={cycle_close_date!r} due_date={due_date!r}; known cycles: {cycles}"
        )
    if len(rows) > 1:
        raise ValueError(
            f"ambiguous match for {obligation_id}: {[r['id'] for r in rows]}; "
            "pass both cycle_close_date and due_date"
        )

    inst = rows[0]
    now = _now()
    new_amount = round(abs(float(amount)), 2)
    conn.execute(
        """
        UPDATE obligation_instances
        SET amount = ?,
            amount_status = 'confirmed',
            amount_source = ?,
            amount_observed_at = ?,
            confidence = 'high',
            notes = COALESCE(?, notes),
            updated_at = ?
        WHERE id = ?
        """,
        (new_amount, source, now, note, now, inst["id"]),
    )
    return {
        "instance_id": inst["id"],
        "obligation_id": obligation_id,
        "due_date": inst["due_date"],
        "statement_close_date": inst["statement_close_date"],
        "previous_amount": round(float(inst["amount"]), 2),
        "amount": new_amount,
        "amount_status": "confirmed",
        "amount_source": source,
        "amount_observed_at": now,
    }


# --- helpers ---------------------------------------------------------------


def _build_cycles(conn: sqlite3.Connection, target_obligation_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, due_date, statement_close_date
        FROM obligation_instances
        WHERE obligation_id = ?
          AND statement_close_date IS NOT NULL
        ORDER BY statement_close_date, due_date, id
        """,
        (target_obligation_id,),
    ).fetchall()
    cycles: list[dict[str, Any]] = []
    prev_close: date | None = None
    for row in rows:
        close = date.fromisoformat(row["statement_close_date"])
        open_date = (prev_close + timedelta(days=1)) if prev_close else None
        cycles.append(
            {
                "id": f"cycle:{target_obligation_id}:{close.isoformat()}",
                "close": close,
                "open": open_date,
                "due_date": row["due_date"],
                "statement_instance_id": row["id"],
            }
        )
        prev_close = close
    return cycles


def _assign_cycle(due: date, cycles: list[dict[str, Any]]) -> dict[str, Any] | None:
    for cycle in cycles:
        if due <= cycle["close"] and (cycle["open"] is None or due >= cycle["open"]):
            return cycle
    return None


def _upsert_cycle(
    conn: sqlite3.Connection,
    cycle: dict[str, Any],
    target_obligation_id: str,
    input_count: int,
    input_sum: float,
    confidence: str | None,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO statement_cycles (
            id, target_obligation_id, statement_instance_id, cycle_open_date,
            cycle_close_date, due_date, input_count, input_sum, confidence,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            target_obligation_id = excluded.target_obligation_id,
            statement_instance_id = excluded.statement_instance_id,
            cycle_open_date = excluded.cycle_open_date,
            cycle_close_date = excluded.cycle_close_date,
            due_date = excluded.due_date,
            input_count = excluded.input_count,
            input_sum = excluded.input_sum,
            confidence = excluded.confidence,
            updated_at = excluded.updated_at
        """,
        (
            cycle["id"],
            target_obligation_id,
            cycle["statement_instance_id"],
            cycle["open"].isoformat() if cycle["open"] else None,
            cycle["close"].isoformat(),
            cycle["due_date"],
            input_count,
            input_sum,
            confidence,
            now,
            now,
        ),
    )


def _min_confidence(confidences: list[str | None]) -> str | None:
    # Absent confidence (all None) stays None; do not invent a "low" reading.
    known = [c for c in confidences if c is not None]
    if not known:
        return None
    rank = min(_CONFIDENCE_RANK.get(c, 1) for c in known)
    return _RANK_CONFIDENCE[rank]


def _now() -> str:
    return datetime.now().astimezone().isoformat()
