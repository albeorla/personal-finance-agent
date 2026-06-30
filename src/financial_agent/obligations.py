from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from statistics import median as _stat_median
from typing import Any

from .manual_balance import BALANCE_PRECEDENCE_ORDER_BY
from .schema import ensure_app_schema


# Auto-detected amount policies whose estimate is a derived average rather than a
# statement-confirmed figure. These are the only obligations eligible for
# dormancy auto-suppression; statement-confirmed amounts (estimation_method NULL,
# or amount_status='confirmed') are never auto-touched.
AVG_ESTIMATE_METHODS = {"seasonal_card_spend", "seasonal_multiplier", "average", "fixed"}

# Status applied to an obligation whose source account has gone dormant. It is
# excluded from projections (see cashflow.PROJECTABLE_STATUSES) but the row stays
# in place so the suppression is fully reversible.
DORMANT_SUPPRESSED_STATUS = "dormant_suppressed"

# Marker stamped on instance ``amount_source`` when an averaged estimate is
# lowered to the account's actual observed burn (see
# ``suppress_contradicted_estimates``). The obligation stays ``active`` so it keeps
# projecting -- at the smaller real figure -- rather than dropping off the runway.
ESTIMATE_CONTRADICTED_STATUS = "estimate_contradicted"

# Averaged methods that are NOT eligible for contradiction. Seasonal estimators
# carry their own multiplier policy (peak-month ramp), so a low summer burn is not
# evidence the estimate is stale; exclude them from the contradiction test.
CONTRADICTION_EXCLUDED_METHODS = {"seasonal_card_spend", "seasonal_multiplier"}

# Cadence -> occurrences per ~30-day month, used to roll a per-occurrence estimate
# up to a modeled monthly outflow for the contradiction comparison.
CADENCE_PER_MONTH: dict[str, float] = {
    "weekly": 4.345,
    "biweekly": 2.173,
    "monthly": 1.0,
    "quarterly": 1.0 / 3.0,
    "semiannual": 1.0 / 6.0,
    "annual": 1.0 / 12.0,
    "yearly": 1.0 / 12.0,
}


def apply_obligation_instances(
    conn: sqlite3.Connection,
    *,
    obligation: dict[str, Any],
    instances: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create or update an obligation and its dated instances.

    ``obligation`` keys: ``id`` (req), ``name`` (req), ``kind`` (req), ``source``
    (req); ``cadence`` (e.g. 'monthly'), ``status`` (default 'active'), ``autopay``
    (default True - set False to surface it as a manual-due reminder),
    ``amount_discretionary`` (default False - True when the modeled amount is only
    a floor the user finalizes, e.g. a card minimum).

    Each ``instances`` item: ``due_date`` (req), ``amount`` (req; negative => outflow
    when ``direction`` is omitted, and the stored amount is the magnitude with the
    sign carried by ``direction``), ``source`` (req); optional ``id`` (defaults to
    ``"<obligation_id>:<due_date>"``, auto-suffixed ``:1``, ``:2`` for additional
    instances sharing a date so they never overwrite each other), ``direction``
    ('inflow'/'outflow', inferred from amount sign), ``status`` (default 'expected'),
    ``confidence``, ``notes``, ``amount_status``, ``amount_source``,
    ``amount_observed_at``, ``statement_close_date``, ``review_after``,
    ``estimation_method``, ``estimation_inputs`` (dict), ``cash_flow_treatment``,
    ``statement_target_obligation_id``.

    Returns ``{"obligation_id", "created", "updated", "instance_ids"}`` so the
    caller can tell new inserts from re-applied upserts (never a silent no-op).
    """

    ensure_app_schema(conn)
    now = _now()
    # autopay defaults to True (quiet): an obligation is only surfaced as a
    # manual-due reminder once it is explicitly classified as autopay=False.
    autopay = 1 if obligation.get("autopay", True) else 0
    # amount_discretionary defaults to False: the modeled amount is the amount to
    # pay unless the obligation is explicitly marked as a user-decided amount
    # (e.g. the Apple Card payment, where the modeled figure is only a floor).
    amount_discretionary = 1 if obligation.get("amount_discretionary", False) else 0
    conn.execute(
        """
        INSERT INTO obligations (
            id, name, kind, cadence, status, source, autopay,
            amount_discretionary, active_until, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            kind = excluded.kind,
            cadence = excluded.cadence,
            status = excluded.status,
            source = excluded.source,
            autopay = excluded.autopay,
            amount_discretionary = excluded.amount_discretionary,
            active_until = excluded.active_until,
            updated_at = excluded.updated_at
        """,
        (
            obligation["id"],
            obligation["name"],
            obligation["kind"],
            obligation.get("cadence"),
            obligation.get("status", "active"),
            obligation["source"],
            autopay,
            amount_discretionary,
            _optional_date(obligation.get("active_until")),
            now,
            now,
        ),
    )

    created = 0
    updated = 0
    instance_ids: list[str] = []
    claimed_ids: set[str] = set()
    for instance in instances:
        normalized = _normalize_instance(conn, obligation["id"], instance, claimed_ids)
        claimed_ids.add(normalized["id"])
        before = conn.execute(
            "SELECT 1 FROM obligation_instances WHERE id = ?",
            (normalized["id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO obligation_instances (
                id, obligation_id, due_date, amount, direction, status, source,
                confidence, notes, amount_status, amount_source, amount_observed_at,
                statement_close_date, review_after, estimation_method,
                estimation_inputs_json, cash_flow_treatment,
                statement_target_obligation_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                obligation_id = excluded.obligation_id,
                due_date = excluded.due_date,
                amount = excluded.amount,
                direction = excluded.direction,
                status = excluded.status,
                source = excluded.source,
                confidence = excluded.confidence,
                notes = excluded.notes,
                amount_status = excluded.amount_status,
                amount_source = excluded.amount_source,
                amount_observed_at = excluded.amount_observed_at,
                statement_close_date = excluded.statement_close_date,
                review_after = excluded.review_after,
                estimation_method = excluded.estimation_method,
                estimation_inputs_json = excluded.estimation_inputs_json,
                cash_flow_treatment = excluded.cash_flow_treatment,
                statement_target_obligation_id = excluded.statement_target_obligation_id,
                updated_at = excluded.updated_at
            """,
            (
                normalized["id"],
                obligation["id"],
                normalized["due_date"],
                normalized["amount"],
                normalized["direction"],
                normalized["status"],
                normalized["source"],
                normalized.get("confidence"),
                normalized.get("notes"),
                normalized.get("amount_status"),
                normalized.get("amount_source"),
                normalized.get("amount_observed_at"),
                normalized.get("statement_close_date"),
                normalized.get("review_after"),
                normalized.get("estimation_method"),
                normalized.get("estimation_inputs_json"),
                normalized.get("cash_flow_treatment"),
                normalized.get("statement_target_obligation_id"),
                now,
                now,
            ),
        )
        instance_ids.append(normalized["id"])
        if before:
            updated += 1
        else:
            created += 1

    return {"obligation_id": obligation["id"], "created": created, "updated": updated, "instance_ids": instance_ids}


def _compact_obligation(obligation: dict[str, Any]) -> dict[str, Any]:
    """Drop the heavy `instances` array, replacing it with a count.

    Used by compact mode to keep the obligations list small enough to stay
    under model token limits while preserving each obligation's metadata.
    """
    if "instances" not in obligation:
        return obligation
    compact = {k: v for k, v in obligation.items() if k != "instances"}
    compact["instance_count"] = len(obligation["instances"])
    return compact


def list_obligations(
    conn: sqlite3.Connection,
    *,
    obligation_id: str | None = None,
    kind: str | None = None,
    status: str | None = "active",
    include_instances: bool = True,
    compact: bool = False,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where = []
    params: list[Any] = []
    if obligation_id is not None:
        # A by-id lookup returns that obligation regardless of status (so you can
        # pull one inactive/superseded row without dumping the whole roster).
        where.append("id = ?")
        params.append(obligation_id)
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    if status is not None and obligation_id is None:
        where.append("status = ?")
        params.append(status)

    query = """
        SELECT id, name, kind, cadence, status, source, autopay, amount_discretionary
        FROM obligations
    """
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY kind, name, id"

    rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        obligation = {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "cadence": row["cadence"],
            "status": row["status"],
            "source": row["source"],
            "autopay": bool(row["autopay"]),
            "amount_discretionary": bool(row["amount_discretionary"]),
        }
        if include_instances:
            obligation["instances"] = _instances_for_obligation(conn, row["id"])
        if compact:
            obligation = _compact_obligation(obligation)
        result.append(obligation)
    return result


def list_obligation_review_candidates(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    review_date = _coerce_date(as_of_date).isoformat()
    rows = conn.execute(
        """
        SELECT
            oi.id AS instance_id,
            oi.obligation_id,
            o.name AS obligation_name,
            o.kind AS obligation_kind,
            oi.due_date,
            oi.amount,
            oi.direction,
            oi.status,
            oi.source,
            oi.confidence,
            oi.notes,
            oi.amount_status,
            oi.amount_source,
            oi.amount_observed_at,
            oi.statement_close_date,
            oi.review_after,
            oi.estimation_method,
            oi.estimation_inputs_json,
            oi.cash_flow_treatment,
            oi.statement_target_obligation_id
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE o.status = 'active'
          AND oi.status IN ('expected', 'needs_review', 'partially_paid')
          AND oi.amount_status = 'estimated'
          AND oi.review_after IS NOT NULL
          AND oi.review_after <= ?
        ORDER BY oi.review_after, oi.due_date, oi.id
        """,
        (review_date,),
    ).fetchall()
    return [
        {
            "review_type": "estimated_amount_ready_for_refresh",
            "instance_id": row["instance_id"],
            "obligation_id": row["obligation_id"],
            "obligation_name": row["obligation_name"],
            "obligation_kind": row["obligation_kind"],
            "due_date": row["due_date"],
            "amount": round(float(row["amount"]), 2),
            "direction": row["direction"],
            "status": row["status"],
            "confidence": row["confidence"],
            "amount_status": row["amount_status"],
            "amount_source": row["amount_source"],
            "amount_observed_at": row["amount_observed_at"],
            "statement_close_date": row["statement_close_date"],
            "review_after": row["review_after"],
            "estimation_method": row["estimation_method"],
            "estimation_inputs": _decode_json(row["estimation_inputs_json"]),
            "cash_flow_treatment": row["cash_flow_treatment"],
            "statement_target_obligation_id": row["statement_target_obligation_id"],
            "source": row["source"],
            "notes": row["notes"],
            "recommended_action": "Refresh amount from source and replace the estimate with the statement amount.",
        }
        for row in rows
    ]


def list_statement_input_estimates(
    conn: sqlite3.Connection,
    *,
    target_obligation_id: str | None = None,
    start_date: date | str | None = None,
    through_date: date | str | None = None,
    status: str | None = "expected",
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where = ["oi.cash_flow_treatment = 'card_statement_input'"]
    params: list[Any] = []
    if target_obligation_id is not None:
        where.append("oi.statement_target_obligation_id = ?")
        params.append(target_obligation_id)
    if start_date is not None:
        where.append("oi.due_date >= ?")
        params.append(_coerce_date(start_date).isoformat())
    if through_date is not None:
        where.append("oi.due_date <= ?")
        params.append(_coerce_date(through_date).isoformat())
    if status is not None:
        where.append("oi.status = ?")
        params.append(status)

    rows = conn.execute(
        f"""
        SELECT
            oi.id AS instance_id,
            oi.obligation_id,
            o.name AS obligation_name,
            oi.due_date,
            oi.amount,
            oi.direction,
            oi.status,
            oi.confidence,
            oi.notes,
            oi.amount_status,
            oi.amount_source,
            oi.estimation_method,
            oi.estimation_inputs_json,
            oi.cash_flow_treatment,
            oi.statement_target_obligation_id
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE {" AND ".join(where)}
          AND o.status = 'active'
        ORDER BY oi.due_date, oi.id
        """,
        params,
    ).fetchall()
    return [
        {
            "instance_id": row["instance_id"],
            "obligation_id": row["obligation_id"],
            "obligation_name": row["obligation_name"],
            "due_date": row["due_date"],
            "amount": round(float(row["amount"]), 2),
            "direction": row["direction"],
            "status": row["status"],
            "confidence": row["confidence"],
            "amount_status": row["amount_status"],
            "amount_source": row["amount_source"],
            "estimation_method": row["estimation_method"],
            "estimation_inputs": _decode_json(row["estimation_inputs_json"]),
            "cash_flow_treatment": row["cash_flow_treatment"],
            "statement_target_obligation_id": row["statement_target_obligation_id"],
            "notes": row["notes"],
        }
        for row in rows
    ]


def delete_obligation_instance(
    conn: sqlite3.Connection,
    instance_id: str,
) -> dict[str, Any]:
    """Soft-delete a single obligation instance by marking it ``deleted``.

    Soft delete preserves the audit trail (the row, its matches, and any
    statement links stay in place) and removes the instance from every read
    path that filters to projectable/active statuses. The instance can be
    revived by re-applying it with an explicit ``id``.
    """

    ensure_app_schema(conn)
    row = conn.execute(
        "SELECT id, status FROM obligation_instances WHERE id = ?",
        (instance_id,),
    ).fetchone()
    if row is None:
        return {"instance_id": instance_id, "deleted": False, "reason": "not_found"}
    if row["status"] == "deleted":
        return {"instance_id": instance_id, "deleted": False, "reason": "already_deleted"}

    now = _now()
    conn.execute(
        "UPDATE obligation_instances SET status = 'deleted', updated_at = ? WHERE id = ?",
        (now, instance_id),
    )
    return {
        "instance_id": instance_id,
        "deleted": True,
        "previous_status": row["status"],
        "status": "deleted",
        "updated_at": now,
    }


def set_obligation_end(
    conn: sqlite3.Connection,
    obligation_id: str,
    active_until: str | None,
) -> dict[str, Any]:
    """Set (or clear) the date a bill stops projecting.

    A bill with a known end - a lease, a loan payoff, a subscription being
    cancelled - should not keep filling the runway forever. Setting ``active_until``
    (ISO date) hard-stops its instances from the projection on and after that date;
    passing None clears it (open-ended again). Reversible, no instances are
    deleted - they are simply excluded past the end date.
    """

    ensure_app_schema(conn)
    row = conn.execute(
        "SELECT id, name, active_until FROM obligations WHERE id = ?", (obligation_id,)
    ).fetchone()
    if row is None:
        return {"obligation_id": obligation_id, "updated": False, "reason": "not_found"}

    end = _optional_date(active_until)
    now = _now()
    conn.execute(
        "UPDATE obligations SET active_until = ?, updated_at = ? WHERE id = ?",
        (end, now, obligation_id),
    )
    return {
        "obligation_id": obligation_id,
        "name": row["name"] if isinstance(row, sqlite3.Row) else row[1],
        "updated": True,
        "previous_active_until": row["active_until"] if isinstance(row, sqlite3.Row) else row[2],
        "active_until": end,
    }


def suppress_dormant_avg_estimates(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Auto-suppress auto-detected average-estimate obligations on dormant accounts.

    An auto-modeled estimate (e.g. a credit-card payment estimate derived from
    averaged spend) keeps projecting every cycle even when its source account has
    gone to zero with no activity - for example a paid-off, dormant card. That
    silently overstates upcoming outflows on the runway.

    This conservatively deactivates such estimates when the source account shows a
    zero balance AND no transactions for at least ``dormancy_cycles`` statement
    cycles (default 2, roughly ``lookback_days`` of history). The obligation's
    status is set to ``dormant_suppressed`` (kept, not deleted, so it is fully
    reversible) and a low-severity ``auto_suppressed_dormant_estimate`` drift
    finding records the decision and the balance history.

    Hard guards (never auto-touched):
      - user-confirmed manual obligations (source not from charge onboarding),
      - statement-confirmed amounts (``estimation_method`` NULL or
        ``amount_status = 'confirmed'`` on every projectable instance),
      - obligations whose method is not an averaged estimate,
      - obligations with no resolvable source account, or whose source account
        is not dormant (non-zero balance or any recent transaction).

    A single transaction or a non-zero balance in the window resets dormancy, so
    an account that is merely paused between charges is left projecting.
    """

    ensure_app_schema(conn)
    opts = options or {}
    dormancy_cycles = int(opts.get("dormancy_cycles", 2))
    # ~30 days per statement cycle; require at least that much history per cycle.
    lookback_days = int(opts.get("lookback_days", max(60, dormancy_cycles * 30)))
    as_of = _coerce_date(as_of_date)
    window_start = (as_of - timedelta(days=lookback_days)).isoformat()
    now = _now()

    have_balances = _has_table(conn, "balance_snapshots")
    have_transactions = _has_table(conn, "transactions")
    have_candidates = _has_table(conn, "charge_onboarding_candidates")

    suppressed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    evaluated = 0

    if not (have_balances and have_candidates):
        # Without balance snapshots we cannot prove dormancy, and without the
        # onboarding candidate table we cannot resolve a source account. Either
        # way there is nothing safe to suppress; return a no-op result.
        return _suppression_summary(as_of, dormancy_cycles, lookback_days, evaluated, suppressed, skipped)

    candidate_obligations = conn.execute(
        """
        SELECT id, name, status, source
        FROM obligations
        WHERE status = 'active'
          AND source LIKE 'charge_onboarding:%'
        ORDER BY id
        """
    ).fetchall()

    for obligation in candidate_obligations:
        evaluated += 1
        instances = conn.execute(
            """
            SELECT id, amount, direction, estimation_method, amount_status
            FROM obligation_instances
            WHERE obligation_id = ?
              AND status IN ('expected', 'needs_review', 'partially_paid')
            """,
            (obligation["id"],),
        ).fetchall()
        if not instances:
            continue

        # Only suppress when every projectable instance is an averaged auto-estimate.
        # A confirmed amount, or a method outside the averaged set, opts the whole
        # obligation out (we never auto-touch statement-confirmed figures).
        if not all(_is_avg_estimate_instance(inst) for inst in instances):
            continue

        account_ids = _source_account_ids(conn, obligation["id"], obligation["source"])
        if not account_ids:
            skipped.append({"obligation_id": obligation["id"], "reason": "no_source_account"})
            continue

        dormancy = _account_dormancy(
            conn,
            account_ids,
            window_start=window_start,
            as_of=as_of.isoformat(),
            have_transactions=have_transactions,
        )
        if not dormancy["dormant"]:
            continue

        conn.execute(
            "UPDATE obligations SET status = ?, updated_at = ? WHERE id = ?",
            (DORMANT_SUPPRESSED_STATUS, now, obligation["id"]),
        )

        # Auto-cleanup hook (spec section 2, OQ2 owner): this is the function that
        # flips an obligation to dormant_suppressed, so it owns retiring the stale
        # Todoist reminders for that obligation's due-date instances. Flag every
        # open obligation-due emission for removal on the next live surface run.
        # Imported locally to avoid the obligations -> todoist_outbox -> onboarding
        # -> obligations import cycle.
        from .todoist_outbox import request_emission_retire_prefix

        request_emission_retire_prefix(conn, f"obligation-due:{obligation['id']}:")

        finding_id = f"drift:auto_suppressed_dormant_estimate:{obligation['id']}"
        evidence = {
            "obligation_name": obligation["name"],
            "previous_status": obligation["status"],
            "account_ids": account_ids,
            "balance_history": dormancy["balance_history"],
            "latest_balance": dormancy["latest_balance"],
            "transactions_in_window": dormancy["transaction_count"],
            "dormancy_cycles_required": dormancy_cycles,
            "lookback_days": lookback_days,
            "window_start": window_start,
            "as_of_date": as_of.isoformat(),
        }
        conn.execute(
            """
            INSERT INTO drift_findings (
                id, finding_type, severity, obligation_id, obligation_instance_id,
                related_transaction_ids_json, cash_flow_impact, confidence,
                evidence_json, recommended_action, status, as_of_date,
                created_at, updated_at, resolved_at
            ) VALUES (?, ?, 'low', ?, NULL, '[]', NULL, 'medium', ?, ?, 'active', ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                severity = 'low',
                evidence_json = excluded.evidence_json,
                recommended_action = excluded.recommended_action,
                status = 'active',
                as_of_date = excluded.as_of_date,
                updated_at = excluded.updated_at,
                resolved_at = NULL
            """,
            (
                finding_id,
                "auto_suppressed_dormant_estimate",
                obligation["id"],
                json.dumps(evidence, sort_keys=True),
                "Source account is dormant (zero balance, no recent activity). The "
                "estimate was auto-suppressed; reactivate the obligation (status "
                "back to 'active') if the account resumes activity.",
                as_of.isoformat(),
                now,
                now,
            ),
        )
        suppressed.append(
            {
                "obligation_id": obligation["id"],
                "obligation_name": obligation["name"],
                "account_ids": account_ids,
                "finding_id": finding_id,
            }
        )

    return _suppression_summary(as_of, dormancy_cycles, lookback_days, evaluated, suppressed, skipped)


def _suppression_summary(
    as_of: date,
    dormancy_cycles: int,
    lookback_days: int,
    evaluated: int,
    suppressed: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "as_of_date": as_of.isoformat(),
        "dormancy_cycles": dormancy_cycles,
        "lookback_days": lookback_days,
        "evaluated": evaluated,
        "suppressed_count": len(suppressed),
        "suppressed": suppressed,
        "skipped": skipped,
    }


def suppress_contradicted_estimates(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lower or suppress an averaged estimate that the account's burn contradicts.

    Dormancy (``suppress_dormant_avg_estimates``) only fires on a clean zero
    balance with zero activity. This is the "shrunk but not dead" case from data
    caveat #2: a card carrying a large averaged estimate (e.g. ~$1,162/mo) whose
    live balance is low/flat and whose real merchant burn has collapsed to a small
    figure. The estimate keeps projecting at full size and overstates the trough.

    For each active charge-onboarding obligation whose every projectable instance
    is an averaged auto-estimate (seasonal methods excluded -- they carry their own
    multiplier policy), this compares the modeled monthly outflow against the
    account's actual recent burn on that merchant:

      - ``modeled_monthly`` = per-occurrence estimate x cadence-per-month.
      - ``observed_monthly`` = summed merchant burn / months in the window.
      - Fires when BOTH ``modeled_monthly >= max(modeled_floor, observed_monthly x
        ratio)`` AND the gap is sustained across at least ``contradiction_cycles``
        consecutive ~30-day sub-windows. A flat balance (statement not growing)
        lowers the ratio (``flat_balance_ratio``) because there is no hidden spend.

    Resolution is graceful, not binary:
      - ``observed_monthly`` at/under ``near_zero_monthly`` -> route to dormant
        (reuse ``DORMANT_SUPPRESSED_STATUS``); effectively dead without a clean $0.
      - ``observed_monthly`` real but much smaller -> keep the obligation ``active``
        and rewrite each projectable instance amount down to the observed figure
        (``amount_source = 'estimate_contradicted'``), so the runway uses the
        smaller real number instead of dropping the obligation entirely.

    Both paths are fully reversible (rows kept, previous amounts recorded in the
    finding evidence) and emit a low-severity ``auto_contradicted_estimate`` drift
    finding -- never a silent edit.

    Modes: ``report`` (default) emits findings but mutates nothing -- the observe
    posture for the first live run; ``enforce`` applies the resolution. Insufficient
    evidence (no transactions on the account at all, e.g. the balance-only Apple
    Card) is always a no-op: never suppress on missing data.
    """

    ensure_app_schema(conn)
    opts = options or {}
    mode = str(opts.get("mode", "report"))
    ratio = float(opts.get("contradiction_ratio", 2.0))
    flat_ratio = float(opts.get("flat_balance_ratio", 1.5))
    floor = float(opts.get("modeled_floor", 150.0))
    cycles = int(opts.get("contradiction_cycles", 2))
    lookback_days = int(opts.get("contradiction_lookback_days", 90))
    near_zero = float(opts.get("near_zero_monthly", 5.0))
    as_of = _coerce_date(as_of_date)
    window_start = (as_of - timedelta(days=lookback_days)).isoformat()
    now = _now()

    have_transactions = _has_table(conn, "transactions")
    have_balances = _has_table(conn, "balance_snapshots")
    have_candidates = _has_table(conn, "charge_onboarding_candidates")

    contradicted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    evaluated = 0

    if not (have_transactions and have_candidates):
        # Without transactions we cannot observe burn, and without the onboarding
        # candidate table we cannot resolve a source account/merchant. No-op.
        return _contradiction_summary(as_of, mode, lookback_days, cycles, evaluated, contradicted, skipped)

    candidate_obligations = conn.execute(
        """
        SELECT id, name, status, source, cadence
        FROM obligations
        WHERE status = 'active'
          AND source LIKE 'charge_onboarding:%'
        ORDER BY id
        """
    ).fetchall()

    for obligation in candidate_obligations:
        evaluated += 1
        instances = conn.execute(
            """
            SELECT id, amount, direction, estimation_method, amount_status
            FROM obligation_instances
            WHERE obligation_id = ?
              AND status IN ('expected', 'needs_review', 'partially_paid')
            """,
            (obligation["id"],),
        ).fetchall()
        if not instances:
            continue
        # Same guard as dormancy: only averaged auto-estimates are eligible.
        if not all(_is_avg_estimate_instance(inst) for inst in instances):
            continue
        # Seasonal estimators opt out (their own peak-month policy explains low burn).
        if any((inst["estimation_method"] or "") in CONTRADICTION_EXCLUDED_METHODS for inst in instances):
            continue
        # Contradiction is an outflow concept (overstated burn); skip inflows.
        if any(inst["direction"] != "outflow" for inst in instances):
            continue

        account_ids = _source_account_ids(conn, obligation["id"], obligation["source"])
        if not account_ids:
            skipped.append({"obligation_id": obligation["id"], "reason": "no_source_account"})
            continue
        merchant_key = _obligation_merchant_key(conn, obligation["id"], obligation["source"])
        if not merchant_key:
            skipped.append({"obligation_id": obligation["id"], "reason": "no_merchant_key"})
            continue

        burn = _merchant_monthly_burn(
            conn, account_ids, merchant_key,
            window_start=window_start, as_of=as_of.isoformat(), sub_windows=cycles,
        )
        if burn["insufficient"]:
            # No transactions on the account at all (e.g. balance-only Apple Card):
            # cannot observe burn -> never suppress on missing data.
            skipped.append({"obligation_id": obligation["id"], "reason": "insufficient_evidence"})
            continue

        observed_monthly = burn["observed_monthly"]
        cadence_factor = CADENCE_PER_MONTH.get((obligation["cadence"] or "monthly").lower(), 1.0)
        amounts = sorted(round(float(inst["amount"]), 2) for inst in instances)
        per_occurrence = float(_stat_median(amounts)) if amounts else 0.0
        modeled_monthly = round(per_occurrence * cadence_factor, 2)

        balance_flat = (
            _account_balance_flat(conn, account_ids, window_start) if have_balances else False
        )
        effective_ratio = flat_ratio if balance_flat else ratio
        threshold = max(floor, observed_monthly * effective_ratio)

        # Sustained check: the gap must hold across the recent consecutive
        # sub-windows, not just on average (a single quiet month is not enough).
        sustained = 0
        for sub_burn in burn["sub_window_burn"]:  # recent-first
            if modeled_monthly >= max(floor, sub_burn * effective_ratio):
                sustained += 1
            else:
                break
        fires = (modeled_monthly >= threshold) and sustained >= cycles
        if not fires:
            continue

        previous_amounts = {inst["id"]: round(float(inst["amount"]), 2) for inst in instances}
        if observed_monthly <= near_zero:
            resolution = "dormant"
            new_per_occurrence = None
        else:
            resolution = "rewrite"
            new_per_occurrence = round(observed_monthly / cadence_factor, 2) if cadence_factor else observed_monthly

        if mode == "enforce":
            if resolution == "dormant":
                conn.execute(
                    "UPDATE obligations SET status = ?, updated_at = ? WHERE id = ?",
                    (DORMANT_SUPPRESSED_STATUS, now, obligation["id"]),
                )
                # Retire any stale due reminders for the now-suppressed obligation.
                from .todoist_outbox import request_emission_retire_prefix

                request_emission_retire_prefix(conn, f"obligation-due:{obligation['id']}:")
            else:
                for inst in instances:
                    conn.execute(
                        """
                        UPDATE obligation_instances
                        SET amount = ?, amount_status = 'estimated', amount_source = ?,
                            notes = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            new_per_occurrence,
                            ESTIMATE_CONTRADICTED_STATUS,
                            f"Auto-lowered to observed burn (~${observed_monthly:.0f}/mo); "
                            f"was ${previous_amounts[inst['id']]:.2f}.",
                            now,
                            inst["id"],
                        ),
                    )

        finding_id = f"drift:auto_contradicted_estimate:{obligation['id']}"
        evidence = {
            "obligation_name": obligation["name"],
            "resolution": resolution,
            "previous_status": obligation["status"],
            "account_ids": account_ids,
            "merchant_key": merchant_key,
            "modeled_monthly": modeled_monthly,
            "observed_monthly": observed_monthly,
            "ratio": effective_ratio,
            "threshold": round(threshold, 2),
            "sub_window_burn": burn["sub_window_burn"],
            "balance_flat": balance_flat,
            "previous_amounts": previous_amounts,
            "rewritten_amount": new_per_occurrence,
            "lookback_days": lookback_days,
            "contradiction_cycles": cycles,
            "window_start": window_start,
            "as_of_date": as_of.isoformat(),
            "mode": mode,
            "applied": mode == "enforce",
        }
        if resolution == "dormant":
            recommended = (
                f"Source account barely used (~${observed_monthly:.0f}/mo) but the estimate "
                f"projects ~${modeled_monthly:.0f}/mo. Treated as dormant; reactivate the "
                f"obligation (status back to 'active') if it ramps back up."
            )
        else:
            recommended = (
                f"Estimate projected ~${modeled_monthly:.0f}/mo but the merchant has run "
                f"~${observed_monthly:.0f}/mo for {cycles} cycles"
                f"{' (balance flat)' if balance_flat else ''}. Lowered the forecast to the "
                f"smaller real figure; re-estimate or reactivate if the card ramps back up."
            )
        conn.execute(
            """
            INSERT INTO drift_findings (
                id, finding_type, severity, obligation_id, obligation_instance_id,
                related_transaction_ids_json, cash_flow_impact, confidence,
                evidence_json, recommended_action, status, as_of_date,
                created_at, updated_at, resolved_at
            ) VALUES (?, ?, 'low', ?, NULL, '[]', NULL, 'medium', ?, ?, 'active', ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                severity = 'low',
                evidence_json = excluded.evidence_json,
                recommended_action = excluded.recommended_action,
                status = 'active',
                as_of_date = excluded.as_of_date,
                updated_at = excluded.updated_at,
                resolved_at = NULL
            """,
            (
                finding_id,
                "auto_contradicted_estimate",
                obligation["id"],
                json.dumps(evidence, sort_keys=True),
                recommended,
                as_of.isoformat(),
                now,
                now,
            ),
        )
        contradicted.append(
            {
                "obligation_id": obligation["id"],
                "obligation_name": obligation["name"],
                "resolution": resolution,
                "modeled_monthly": modeled_monthly,
                "observed_monthly": observed_monthly,
                "rewritten_amount": new_per_occurrence,
                "applied": mode == "enforce",
                "finding_id": finding_id,
            }
        )

    return _contradiction_summary(as_of, mode, lookback_days, cycles, evaluated, contradicted, skipped)


def _contradiction_summary(
    as_of: date,
    mode: str,
    lookback_days: int,
    cycles: int,
    evaluated: int,
    contradicted: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "as_of_date": as_of.isoformat(),
        "mode": mode,
        "lookback_days": lookback_days,
        "contradiction_cycles": cycles,
        "evaluated": evaluated,
        "contradicted_count": len(contradicted),
        "contradicted": contradicted,
        "skipped": skipped,
    }


def _obligation_merchant_key(
    conn: sqlite3.Connection, obligation_id: str, source: str | None
) -> str | None:
    """Resolve the merchant key for an onboarded obligation via its candidate."""

    candidate_id = None
    if source and source.startswith("charge_onboarding:"):
        candidate_id = source.split(":", 1)[1]
    row = conn.execute(
        """
        SELECT merchant_key
        FROM charge_onboarding_candidates
        WHERE existing_obligation_id = ? OR id = ?
        LIMIT 1
        """,
        (obligation_id, candidate_id),
    ).fetchone()
    return row["merchant_key"] if row else None


def _merchant_monthly_burn(
    conn: sqlite3.Connection,
    account_ids: list[str],
    merchant_key: str,
    *,
    window_start: str,
    as_of: str,
    sub_windows: int,
) -> dict[str, Any]:
    """Observed monthly burn for ``merchant_key`` on the given accounts.

    Same ``transactions`` shape as ``_account_dormancy`` but summing per ~30-day
    sub-window (most recent first). ``insufficient`` is True when the accounts have
    NO transactions at all in the window (e.g. a balance-only card) so the caller
    can no-op rather than suppress on missing data.
    """

    # Local import avoids the obligations <-> onboarding import cycle.
    from .onboarding import normalize_merchant_key

    placeholders = ",".join("?" for _ in account_ids)
    rows = conn.execute(
        f"""
        SELECT amount, payee, COALESCE(posted, transacted_at) AS ts
        FROM transactions
        WHERE account_id IN ({placeholders})
          AND COALESCE(posted, transacted_at) >= ?
          AND COALESCE(posted, transacted_at) <= ?
        """,
        (*account_ids, window_start, as_of + "T23:59:59"),
    ).fetchall()

    account_txn_count = len(rows)
    matched = [
        (abs(float(r["amount"])), (r["ts"] or "")[:10])
        for r in rows
        if r["payee"] and normalize_merchant_key(r["payee"]) == merchant_key
    ]

    as_of_date = date.fromisoformat(as_of)
    sub_window_burn: list[float] = []
    for i in range(max(1, sub_windows)):
        win_end = (as_of_date - timedelta(days=30 * i)).isoformat()
        win_start = (as_of_date - timedelta(days=30 * (i + 1))).isoformat()
        sub_window_burn.append(
            round(sum(amt for amt, d in matched if win_start < d <= win_end), 2)
        )

    total = sum(amt for amt, _ in matched)
    months = max(1.0, (as_of_date - date.fromisoformat(window_start)).days / 30.0)
    return {
        "insufficient": account_txn_count == 0,
        "account_txn_count": account_txn_count,
        "matched_count": len(matched),
        "observed_monthly": round(total / months, 2),
        "sub_window_burn": sub_window_burn,
        "months": round(months, 2),
    }


def _account_balance_flat(
    conn: sqlite3.Connection, account_ids: list[str], window_start: str
) -> bool:
    """True when total balance magnitude has not grown across the window.

    For a card, a non-growing statement balance means no hidden spend is masking
    the low observed burn, so the contradiction test uses the gentler ratio.
    Compared by magnitude (``abs``) because card balances are negative.
    """

    oldest = newest = 0.0
    have = False
    for account_id in account_ids:
        first = conn.execute(
            "SELECT balance FROM balance_snapshots WHERE account_id = ? AND recorded_at >= ? "
            "ORDER BY recorded_at ASC LIMIT 1",
            (account_id, window_start),
        ).fetchone()
        last = conn.execute(
            "SELECT balance FROM balance_snapshots WHERE account_id = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        if first is None or last is None:
            continue
        have = True
        oldest += abs(float(first["balance"]))
        newest += abs(float(last["balance"]))
    if not have:
        return False
    return newest <= oldest + 1e-9


def _is_avg_estimate_instance(inst: sqlite3.Row) -> bool:
    """True only for an auto-detected averaged estimate that is safe to suppress.

    Statement-confirmed amounts (no estimation method, or an explicitly confirmed
    amount) are never eligible, regardless of method.
    """

    if inst["estimation_method"] is None:
        return False
    if (inst["amount_status"] or "") == "confirmed":
        return False
    return inst["estimation_method"] in AVG_ESTIMATE_METHODS


def _source_account_ids(
    conn: sqlite3.Connection, obligation_id: str, source: str | None
) -> list[str]:
    """Resolve the source account(s) for an onboarded obligation.

    The link survives via the onboarding candidate: its
    ``proposed_cash_impact_policy.evidence_account_ids`` lists the accounts the
    charge was observed on. We match the candidate by ``existing_obligation_id``
    (set when the candidate is applied) and fall back to the candidate id encoded
    in the obligation ``source`` (``charge_onboarding:<candidate_id>``).
    """

    candidate_id = None
    if source and source.startswith("charge_onboarding:"):
        candidate_id = source.split(":", 1)[1]

    rows = conn.execute(
        """
        SELECT proposed_cash_impact_policy_json
        FROM charge_onboarding_candidates
        WHERE existing_obligation_id = ? OR id = ?
        """,
        (obligation_id, candidate_id),
    ).fetchall()

    account_ids: set[str] = set()
    for row in rows:
        policy = _decode_json(row["proposed_cash_impact_policy_json"]) or {}
        for account_id in policy.get("evidence_account_ids", []) or []:
            if account_id:
                account_ids.add(str(account_id))
    return sorted(account_ids)


def _account_dormancy(
    conn: sqlite3.Connection,
    account_ids: list[str],
    *,
    window_start: str,
    as_of: str,
    have_transactions: bool,
) -> dict[str, Any]:
    """Decide whether every source account is dormant over the window.

    Dormant means: the latest known balance is zero (available and balance) AND
    no transaction posted in the lookback window. A single non-zero balance or a
    single posted transaction on any source account resets dormancy.
    """

    placeholders = ",".join("?" for _ in account_ids)
    balance_history: list[dict[str, Any]] = []
    latest_balance: dict[str, Any] = {}
    any_nonzero = False
    have_any_snapshot = False

    for account_id in account_ids:
        row = conn.execute(
            f"""
            SELECT balance, available, recorded_at
            FROM balance_snapshots
            WHERE account_id = ?
            {BALANCE_PRECEDENCE_ORDER_BY.format(alias="balance_snapshots")}
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            # No balance evidence for this account: cannot prove dormancy.
            return {"dormant": False, "balance_history": balance_history, "latest_balance": {}, "transaction_count": 0}
        have_any_snapshot = True
        balance = round(float(row["balance"]), 2)
        available = round(float(row["available"]), 2)
        latest_balance[account_id] = {
            "balance": balance,
            "available": available,
            "recorded_at": row["recorded_at"],
        }
        balance_history.append(
            {
                "account_id": account_id,
                "balance": balance,
                "available": available,
                "recorded_at": row["recorded_at"],
            }
        )
        if balance != 0 or available != 0:
            any_nonzero = True

    transaction_count = 0
    if have_transactions:
        transaction_count = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM transactions
            WHERE account_id IN ({placeholders})
              AND COALESCE(posted, transacted_at) >= ?
              AND COALESCE(posted, transacted_at) <= ?
            """,
            (*account_ids, window_start, as_of + "T23:59:59"),
        ).fetchone()[0]

    dormant = have_any_snapshot and not any_nonzero and transaction_count == 0
    return {
        "dormant": dormant,
        "balance_history": balance_history,
        "latest_balance": latest_balance,
        "transaction_count": transaction_count,
    }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone() is not None


def _next_instance_id(
    conn: sqlite3.Connection,
    obligation_id: str,
    due_date: str,
    claimed_ids: set[str],
) -> str:
    """Pick the instance id for an instance applied without an explicit id.

    Backward compatibility and idempotency: the base id is
    ``obligation_id:due_date`` with no index suffix, which is also the legacy
    format. Re-applying an obligation on the same date reuses that base id so it
    upserts the existing row instead of creating a duplicate. Only when the base
    id has already been claimed by an earlier instance *in the same apply call*
    do we allocate the next free numeric index, which is what lets two distinct
    same-date instances coexist (e.g. a tax estimate and a tax payment both due
    2026-07-31).
    """

    base_id = f"{obligation_id}:{due_date}"
    prefix = f"{base_id}:"

    db_ids = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM obligation_instances WHERE obligation_id = ? AND due_date = ?",
            (obligation_id, due_date),
        ).fetchall()
    }
    all_ids = db_ids | claimed_ids

    has_indexed_sibling = any(
        existing_id.startswith(prefix) and existing_id[len(prefix):].isdigit()
        for existing_id in all_ids
    )

    # Reuse the base id (an upsert) when the date holds at most a single
    # instance and that instance was not just created earlier in this same
    # apply call. This preserves the legacy single-per-date format and makes
    # re-applying one instance idempotent. A new numeric index is minted only
    # when the date already has multiple instances (an indexed sibling exists)
    # or the base id was already claimed within this call.
    if base_id not in claimed_ids and not has_indexed_sibling:
        return base_id

    used: set[int] = {0}  # base id occupies index 0
    for existing_id in all_ids:
        if existing_id.startswith(prefix):
            suffix = existing_id[len(prefix):]
            if suffix.isdigit():
                used.add(int(suffix))
    index = 1
    while index in used:
        index += 1
    return f"{base_id}:{index}"


def _normalize_instance(
    conn: sqlite3.Connection,
    obligation_id: str,
    instance: dict[str, Any],
    claimed_ids: set[str],
) -> dict[str, Any]:
    due_date = _coerce_date(instance["due_date"]).isoformat()
    raw_amount = float(instance["amount"])
    direction = instance.get("direction")
    if direction is None:
        direction = "outflow" if raw_amount < 0 else "inflow"
    if direction not in {"inflow", "outflow"}:
        raise ValueError(f"Unsupported obligation direction: {direction}")
    instance_id = instance.get("id") or _next_instance_id(
        conn, obligation_id, due_date, claimed_ids
    )
    return {
        "id": instance_id,
        "due_date": due_date,
        "amount": round(abs(raw_amount), 2),
        "direction": direction,
        "status": instance.get("status", "expected"),
        "source": instance["source"],
        "confidence": instance.get("confidence"),
        "notes": instance.get("notes"),
        "amount_status": instance.get("amount_status"),
        "amount_source": instance.get("amount_source"),
        "amount_observed_at": instance.get("amount_observed_at"),
        "statement_close_date": _optional_date(instance.get("statement_close_date")),
        "review_after": _optional_date(instance.get("review_after")),
        "estimation_method": instance.get("estimation_method"),
        "estimation_inputs_json": _encode_json(instance.get("estimation_inputs")),
        "cash_flow_treatment": instance.get("cash_flow_treatment"),
        "statement_target_obligation_id": instance.get("statement_target_obligation_id"),
    }


def _instances_for_obligation(conn: sqlite3.Connection, obligation_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id, due_date, amount, direction, status, source, confidence, notes,
            amount_status, amount_source, amount_observed_at, statement_close_date,
            review_after, estimation_method, estimation_inputs_json,
            cash_flow_treatment, statement_target_obligation_id
        FROM obligation_instances
        WHERE obligation_id = ?
          AND status NOT IN ('deleted', 'canceled')
        ORDER BY due_date, id
        """,
        (obligation_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "due_date": row["due_date"],
            "amount": round(float(row["amount"]), 2),
            "direction": row["direction"],
            "status": row["status"],
            "source": row["source"],
            "confidence": row["confidence"],
            "notes": row["notes"],
            "amount_status": row["amount_status"],
            "amount_source": row["amount_source"],
            "amount_observed_at": row["amount_observed_at"],
            "statement_close_date": row["statement_close_date"],
            "review_after": row["review_after"],
            "estimation_method": row["estimation_method"],
            "estimation_inputs": _decode_json(row["estimation_inputs_json"]),
            "cash_flow_treatment": row["cash_flow_treatment"],
            "statement_target_obligation_id": row["statement_target_obligation_id"],
        }
        for row in rows
    ]


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _optional_date(value: date | str | None) -> str | None:
    if value is None:
        return None
    return _coerce_date(value).isoformat()


def _encode_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)


def _decode_json(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _now() -> str:
    return datetime.now().astimezone().isoformat()
