"""Reconciliation: match observed transactions to expected obligation instances.

Deterministic matching is the bridge between what the plan expected and what the
bank actually did. It scores each expected obligation instance against nearby
transactions by amount, date, and merchant, then records the best match as
evidence.

Conservative by design (per BUILD_PLAN):
- A match is recorded as review evidence, not silently marked paid. Marking an
  instance paid is opt-in (``auto_mark_paid``).
- An unmatched, past-grace instance becomes ``needs_review``, never ``overdue``.
- Card-statement-input instances are skipped here: they settle through a card
  statement, not a direct checking transaction.

The scoring is fully deterministic, so reconciliation is idempotent: re-running
on the same data produces the same matches and the same review state.
"""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
from datetime import date, datetime
from typing import Any

from .schema import ensure_app_schema


RECONCILABLE_STATUSES: tuple[str, ...] = ("expected", "needs_review", "partially_paid")

DEFAULT_OPTIONS: dict[str, Any] = {
    "date_window_days": 3,
    "amount_abs_tolerance": 2.0,
    "amount_pct_tolerance": 0.025,
    "auto_threshold": 0.85,
    "review_threshold": 0.55,
    "grace_period_days": 7,
    "exact_match_date_window": 2,
    "auto_mark_paid": False,
    "flag_unmatched_needs_review": False,
}

# Tokens too generic to carry merchant identity on their own.
_STOP_TOKENS: frozenset[str] = frozenset(
    {"the", "inc", "llc", "co", "card", "payment", "web", "online", "bill", "autopay", "ppd", "id", "pos", "purchase", "estimate", "estimates"}
)


def reconcile_obligation_instances(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Match expected obligation instances against transactions, up to as_of_date."""

    ensure_app_schema(conn)
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    as_of = _coerce_date(as_of_date)
    now = _now()

    instances = conn.execute(
        f"""
        SELECT oi.id, oi.obligation_id, o.name AS obligation_name, oi.due_date,
               oi.amount, oi.direction, oi.status, oi.cash_flow_treatment
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE oi.status IN ({",".join("?" for _ in RECONCILABLE_STATUSES)})
          AND oi.due_date <= ?
          AND o.status = 'active'
          AND COALESCE(oi.cash_flow_treatment, 'direct_checking') != 'card_statement_input'
        ORDER BY oi.due_date, oi.id
        """,
        (*RECONCILABLE_STATUSES, as_of.isoformat()),
    ).fetchall()

    summary = {
        "as_of_date": as_of.isoformat(),
        "considered": len(instances),
        "matched_auto": 0,
        "matched_needs_review": 0,
        "matched_shared": 0,
        "unmatched": 0,
        "marked_paid": 0,
        "flagged_needs_review": 0,
        "skipped_card_statement_input": 0,
    }

    # A transaction can settle at most one obligation instance. Process in due
    # order and let the earliest instance claim a transaction, so two obligations
    # with the same amount/merchant cannot both match the same transaction.
    # Pre-pass: one bank transaction that settles a GROUP of instances at once (a
    # single lump payment equal to the summed amount of several same-direction,
    # nearby-date bills). Records that transaction against each group member so it
    # stops projecting as a phantom future outflow. Runs before the 1:1 loop and
    # claims its transactions so they are not reused below.
    grouped, group_claimed = _match_shared_transactions(conn, instances, opts, as_of, now, summary)
    claimed: set[str] = set(group_claimed)
    for inst in instances:
        if inst["id"] in grouped:
            continue  # already recorded by the shared-transaction pre-pass
        best = _best_match(conn, inst, opts, claimed)
        if best is not None and best["match_type"] in {"auto", "needs_review"}:
            claimed.add(best["transaction_id"])
            _record_match(conn, inst, best, as_of, now)
            _clear_unmatched(conn, inst["id"])
            if best["match_type"] == "auto":
                summary["matched_auto"] += 1
                if opts["auto_mark_paid"]:
                    _mark_paid(conn, inst["id"], best, now)
                    summary["marked_paid"] += 1
            else:
                summary["matched_needs_review"] += 1
        else:
            past_grace = (as_of - _coerce_date(inst["due_date"])).days > int(opts["grace_period_days"])
            _record_unmatched(conn, inst, as_of, past_grace, now)
            _clear_match(conn, inst["id"])
            summary["unmatched"] += 1
            if past_grace and opts["flag_unmatched_needs_review"] and inst["status"] == "expected":
                conn.execute(
                    "UPDATE obligation_instances SET status = 'needs_review', updated_at = ? WHERE id = ?",
                    (now, inst["id"]),
                )
                summary["flagged_needs_review"] += 1

    return summary


# Bound the subset search so a wide unmatched window can never blow up
# combinatorially. ponytail: naive combinations scan, capped; if real groups ever
# exceed these sizes, raise the caps or switch to a subset-sum DP.
_SHARED_MAX_GROUP = 4
_SHARED_MAX_POOL = 12


def _match_shared_transactions(
    conn: sqlite3.Connection,
    instances: list[sqlite3.Row],
    opts: dict[str, Any],
    as_of: date,
    now: str,
    summary: dict[str, Any],
) -> tuple[set[str], set[str]]:
    """Match one transaction to a group of instances whose amounts sum to it.

    Only considers instances that would NOT reconcile 1:1 on their own (otherwise
    the normal path owns them), same-direction and within the date window of the
    transaction, and only acts when EXACTLY ONE subset sums to the amount (an
    ambiguous set is skipped). This is a shared-transaction match, not a merge: the
    obligations stay separate; each member gets its own needs_review match row
    pointing at the shared transaction. Returns (grouped_instance_ids, claimed_txn_ids).
    """

    if not _has_transactions_table(conn):
        return set(), set()
    window = int(opts["date_window_days"])

    # Eligible = reconcilable instances with no individual (1:1) match. Grouping is
    # only for the phantom-outflow case the 1:1 loop cannot resolve.
    eligible = [inst for inst in instances if not _individually_matchable(conn, inst, opts)]
    if len(eligible) < 2:
        return set(), set()

    # BUG B guard: a transaction already recorded against another obligation (a
    # confirmed/paid match, or a 1:1 match on an instance we are not regrouping)
    # must not be reused to invent a group. Excludes matches on instances OUTSIDE
    # the eligible set only, so a prior shared match re-groups idempotently.
    eligible_ids = {inst["id"] for inst in eligible}
    already_matched: set[str] = {
        tid
        for tid, oiid in conn.execute(
            "SELECT transaction_id, obligation_instance_id FROM transaction_obligation_matches WHERE transaction_id IS NOT NULL"
        )
        if oiid not in eligible_ids
    }
    already_matched.update(
        tid
        for (tid,) in conn.execute(
            "SELECT matched_transaction_id FROM obligation_instances WHERE matched_transaction_id IS NOT NULL"
        )
    )

    dues = {inst["id"]: _coerce_date(inst["due_date"]) for inst in eligible}
    start = min(dues.values()).toordinal() - window
    end = max(dues.values()).toordinal() + window
    txns = conn.execute(
        """
        SELECT id, posted, transacted_at, amount, payee, description
        FROM transactions
        WHERE substr(COALESCE(posted, transacted_at), 1, 10) >= ?
          AND substr(COALESCE(posted, transacted_at), 1, 10) <= ?
        ORDER BY substr(COALESCE(posted, transacted_at), 1, 10), id
        """,
        (date.fromordinal(start).isoformat(), date.fromordinal(end).isoformat()),
    ).fetchall()

    grouped: set[str] = set()
    claimed: set[str] = set()
    used: set[str] = set()
    for txn in txns:
        if txn["id"] in already_matched:
            continue  # BUG B: already settles another obligation, cannot back a new group
        posted = (txn["posted"] or txn["transacted_at"] or "")[:10]
        if not posted:
            continue
        txn_amount = float(txn["amount"])
        txn_direction = "inflow" if txn_amount > 0 else "outflow"
        txn_date = date.fromisoformat(posted)
        target = round(abs(txn_amount), 2)
        tol = max(float(opts["amount_abs_tolerance"]), target * float(opts["amount_pct_tolerance"]))
        txn_tokens = _tokens(f"{txn['payee'] or ''} {txn['description'] or ''}")

        # BUG A: grouping requires real merchant evidence, not amount+date alone. A
        # member with zero merchant overlap with the transaction is a coincidence
        # (a $75 car wash "summing" to internet + phone), so drop it from the pool
        # before subset-summing. Parallels the 1:1 path's zero-merchant weak guard.
        pool = [
            inst for inst in eligible
            if inst["id"] not in used
            and inst["direction"] == txn_direction
            and abs((txn_date - dues[inst["id"]]).days) <= window
            and _merchant_score(_tokens(inst["obligation_name"] or ""), txn_tokens) > 0.0
        ]
        if len(pool) < 2 or len(pool) > _SHARED_MAX_POOL:
            continue  # too few to group, or too many to search safely

        combo = _unique_summing_subset(pool, target, tol)
        if combo is None:
            continue

        group_sum = round(sum(abs(float(i["amount"])) for i in combo), 2)
        amount_score = 1.0 if abs(group_sum - target) <= 0.005 else 0.7
        group_ids = sorted(i["id"] for i in combo)
        for inst in combo:
            date_delta = abs((txn_date - dues[inst["id"]]).days)
            date_score = 1.0 if date_delta <= 1 else max(0.0, 1.0 - (date_delta - 1) / max(window, 1))
            merchant_score = _merchant_score(_tokens(inst["obligation_name"] or ""), txn_tokens)
            best = {
                "transaction_id": txn["id"],
                "match_type": "needs_review",  # shared matches are never auto-marked paid
                "match_score": round(amount_score * 0.5 + date_score * 0.3 + merchant_score * 0.2, 3),
                "amount_score": amount_score,
                "date_score": round(date_score, 3),
                "merchant_score": merchant_score,
                "amount_delta": round(abs(group_sum - target), 2),
                "date_delta_days": (txn_date - dues[inst["id"]]).days,
                "txn_amount": round(txn_amount, 2),
                "txn_payee": txn["payee"],
                "txn_date": posted,
                "group_evidence": {
                    "shared_transaction": True,
                    "group_instance_ids": group_ids,
                    "group_amount": group_sum,
                    "member_amount": round(abs(float(inst["amount"])), 2),
                },
            }
            _record_match(conn, inst, best, as_of, now)
            _clear_unmatched(conn, inst["id"])
            grouped.add(inst["id"])
            used.add(inst["id"])
            summary["matched_needs_review"] += 1
            summary["matched_shared"] += 1
        claimed.add(txn["id"])

    return grouped, claimed


def _unique_summing_subset(
    pool: list[sqlite3.Row], target: float, tol: float
) -> tuple[sqlite3.Row, ...] | None:
    """The one subset (size >= 2) of pool that sums to target within tol, or None.

    Returns None if no subset matches OR if more than one does (ambiguous group).
    """

    found: tuple[sqlite3.Row, ...] | None = None
    for size in range(2, min(_SHARED_MAX_GROUP, len(pool)) + 1):
        for combo in itertools.combinations(pool, size):
            if abs(round(sum(abs(float(i["amount"])) for i in combo), 2) - target) <= tol:
                if found is not None:
                    return None  # ambiguous: two distinct subsets both sum to target
                found = combo
    return found


def _individually_matchable(conn: sqlite3.Connection, inst: sqlite3.Row, opts: dict[str, Any]) -> bool:
    best = _best_match(conn, inst, opts)
    return best is not None and best["match_type"] in {"auto", "needs_review"}


def confirm_reconciliation_match(
    conn: sqlite3.Connection, instance_id: str, transaction_id: str | None = None
) -> dict[str, Any]:
    """Mark a reviewed obligation instance paid, using its recorded match.

    Guarded: there must be a recorded transaction match (run reconcile first);
    marking paid is never automatic. Records the matched transaction as evidence.

    ``transaction_id`` force-matches that specific transaction to the instance
    (recorded in the match ledger as a user-asserted ``manual`` match, then
    confirmed like any other). Use it when the scorer's tolerance rejected the
    real payment.
    """

    ensure_app_schema(conn)
    inst = conn.execute(
        """
        SELECT oi.id, oi.status, oi.due_date, oi.amount, oi.direction,
               o.name AS obligation_name
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE oi.id = ?
        """,
        (instance_id,),
    ).fetchone()
    if inst is None:
        raise ValueError(f"unknown obligation instance: {instance_id}")
    if transaction_id is not None:
        _record_forced_match(conn, inst, transaction_id)
        _clear_unmatched(conn, instance_id)
    match = conn.execute(
        "SELECT transaction_id, match_score FROM transaction_obligation_matches WHERE obligation_instance_id = ?",
        (instance_id,),
    ).fetchone()
    if match is None:
        raise ValueError(_no_match_reason(conn, dict(inst)))
    now = _now()
    conn.execute(
        """
        UPDATE obligation_instances
        SET status = 'paid', matched_transaction_id = ?, matched_at = ?, match_confidence = ?, updated_at = ?
        WHERE id = ?
        """,
        (match["transaction_id"], now, match["match_score"], now, instance_id),
    )
    return {"instance_id": instance_id, "status": "paid",
            "matched_transaction_id": match["transaction_id"], "match_confidence": round(float(match["match_score"]), 3)}


def _record_forced_match(conn: sqlite3.Connection, inst: sqlite3.Row, transaction_id: str) -> None:
    """Record a user-asserted match for a specific transaction (match_type 'manual')."""

    if not _has_transactions_table(conn):
        raise ValueError(f"unknown transaction: {transaction_id} (no transactions table)")
    txn = conn.execute(
        "SELECT id, posted, transacted_at, amount, payee, description FROM transactions WHERE id = ?",
        (transaction_id,),
    ).fetchone()
    if txn is None:
        raise ValueError(f"unknown transaction: {transaction_id}")
    due = _coerce_date(inst["due_date"])
    posted = (txn["posted"] or txn["transacted_at"] or "")[:10]
    txn_amount = float(txn["amount"])
    expected = abs(float(inst["amount"]))
    best = {
        "transaction_id": txn["id"],
        "match_type": "manual",
        # User assertion is ground truth; confidence is by definition full.
        "match_score": 1.0,
        "amount_score": 1.0,
        "date_score": 1.0,
        "merchant_score": _merchant_score(
            _tokens(inst["obligation_name"] or ""),
            _tokens(f"{txn['payee'] or ''} {txn['description'] or ''}"),
        ),
        "amount_delta": round(abs(abs(txn_amount) - expected), 2),
        "date_delta_days": (date.fromisoformat(posted) - due).days if posted else 0,
        "txn_amount": round(txn_amount, 2),
        "txn_payee": txn["payee"],
        "txn_date": posted or None,
    }
    _record_match(conn, inst, best, date.fromisoformat(posted) if posted else due, _now())


def _no_match_reason(conn: sqlite3.Connection, inst: dict[str, Any]) -> str:
    """Explain WHY there is no recorded match: no candidates vs amount tolerance."""

    base = f"no recorded transaction match for {inst['id']}"
    window = int(DEFAULT_OPTIONS["date_window_days"])
    # Re-scan the date window with amount tolerance disabled so near-miss
    # transactions (right merchant/date, wrong amount) become visible.
    candidates = _scored_candidates(
        conn, inst, {**DEFAULT_OPTIONS, "amount_abs_tolerance": float("inf")}
    )
    if not candidates:
        return (
            f"{base}: no {inst['direction']} transactions within {window} days of due date "
            f"{inst['due_date']}. Run reconcile after the transaction posts, or pass "
            f"transaction_id to force-match."
        )
    expected = abs(float(inst["amount"]))
    tol = max(
        float(DEFAULT_OPTIONS["amount_abs_tolerance"]),
        expected * float(DEFAULT_OPTIONS["amount_pct_tolerance"]),
    )
    nearest = sorted(candidates, key=lambda c: c["amount_delta"])[:3]
    listed = "; ".join(
        f"{c['transaction_id']} ({c['txn_payee'] or 'no payee'}, {c['txn_date']}, "
        f"${abs(c['txn_amount']):,.2f}, delta ${c['amount_delta']:,.2f})"
        for c in nearest
    )
    return (
        f"{base}: {len(candidates)} candidate {inst['direction']} txn(s) within {window} days "
        f"of {inst['due_date']}, but none within the ${tol:,.2f} amount tolerance of "
        f"${expected:,.2f}. Nearest: {listed}. Pass transaction_id to force-match the right one."
    )


def unconfirm_reconciliation_match(conn: sqlite3.Connection, instance_id: str) -> dict[str, Any]:
    """Reverse a confirmation: return the instance to 'expected' and clear evidence."""

    ensure_app_schema(conn)
    inst = conn.execute("SELECT status FROM obligation_instances WHERE id = ?", (instance_id,)).fetchone()
    if inst is None:
        raise ValueError(f"unknown obligation instance: {instance_id}")
    now = _now()
    conn.execute(
        """
        UPDATE obligation_instances
        SET status = 'expected', matched_transaction_id = NULL, matched_at = NULL, match_confidence = NULL, updated_at = ?
        WHERE id = ?
        """,
        (now, instance_id),
    )
    return {"instance_id": instance_id, "status": "expected"}


def list_reconciliation_review_items(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str | None = None,
) -> list[dict[str, Any]]:
    """List recorded matches whose obligation instance still awaits confirmation."""

    ensure_app_schema(conn)
    # Only needs_review matches genuinely AWAIT confirmation. auto matches are
    # high-confidence and already surface as "cleared" (Recently Cleared); listing
    # them here too double-reports the same payment as both cleared and awaiting-confirm.
    where = ["oi.status IN ('expected', 'needs_review', 'partially_paid')", "m.match_type = 'needs_review'"]
    params: list[Any] = []
    if as_of_date is not None:
        where.append("oi.due_date <= ?")
        params.append(_coerce_date(as_of_date).isoformat())
    rows = conn.execute(
        f"""
        SELECT m.obligation_instance_id, m.transaction_id, m.match_type, m.match_score, m.amount_delta,
               oi.obligation_id, oi.due_date, oi.amount, oi.direction, oi.status, o.name AS obligation_name
        FROM transaction_obligation_matches m
        JOIN obligation_instances oi ON oi.id = m.obligation_instance_id
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE {" AND ".join(where)}
          AND o.status = 'active'
        ORDER BY m.match_score DESC, oi.due_date
        """,
        params,
    ).fetchall()
    return [
        {
            "obligation_instance_id": r["obligation_instance_id"],
            "obligation_id": r["obligation_id"],
            "obligation_name": r["obligation_name"],
            "due_date": r["due_date"],
            "amount": round(float(r["amount"]), 2),
            "direction": r["direction"],
            "transaction_id": r["transaction_id"],
            "match_type": r["match_type"],
            "match_score": round(float(r["match_score"]), 3),
            "amount_delta": round(float(r["amount_delta"]), 2) if r["amount_delta"] is not None else None,
        }
        for r in rows
    ]


def find_transaction_matches(
    conn: sqlite3.Connection,
    *,
    obligation_instance: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return candidate transactions for one instance, scored and ranked."""

    opts = {**DEFAULT_OPTIONS, **(options or {})}
    return _scored_candidates(conn, obligation_instance, opts)


def list_matched_obligation_instances(
    conn: sqlite3.Connection,
    *,
    match_type: str | None = None,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where = ""
    params: list[Any] = []
    if match_type is not None:
        where = "WHERE m.match_type = ?"
        params.append(match_type)
    rows = conn.execute(
        f"""
        SELECT m.obligation_instance_id, m.transaction_id, m.match_type, m.match_score,
               m.amount_delta, m.date_delta_days, m.evidence_json,
               oi.obligation_id, oi.due_date, oi.amount, oi.status
        FROM transaction_obligation_matches m
        JOIN obligation_instances oi ON oi.id = m.obligation_instance_id
        {where}
        ORDER BY m.match_score DESC, oi.due_date
        """,
        params,
    ).fetchall()
    return [
        {
            "obligation_instance_id": r["obligation_instance_id"],
            "transaction_id": r["transaction_id"],
            "match_type": r["match_type"],
            "match_score": round(float(r["match_score"]), 3),
            "amount_delta": round(float(r["amount_delta"]), 2) if r["amount_delta"] is not None else None,
            "date_delta_days": r["date_delta_days"],
            "obligation_id": r["obligation_id"],
            "due_date": r["due_date"],
            "instance_amount": round(float(r["amount"]), 2),
            "instance_status": r["status"],
            "evidence": _loads(r["evidence_json"]),
        }
        for r in rows
    ]


def list_unmatched_obligation_instances(
    conn: sqlite3.Connection,
    *,
    past_grace_only: bool = False,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where = "WHERE u.past_grace = 1" if past_grace_only else ""
    rows = conn.execute(
        f"""
        SELECT u.obligation_instance_id, u.obligation_id, u.due_date, u.as_of_date,
               u.age_days, u.grace_period_days, u.past_grace, u.status,
               oi.amount, oi.direction, o.name AS obligation_name
        FROM unmatched_obligations u
        JOIN obligation_instances oi ON oi.id = u.obligation_instance_id
        JOIN obligations o ON o.id = oi.obligation_id
        {where}
        ORDER BY u.past_grace DESC, u.age_days DESC, u.due_date
        """
    ).fetchall()
    return [
        {
            "obligation_instance_id": r["obligation_instance_id"],
            "obligation_id": r["obligation_id"],
            "obligation_name": r["obligation_name"],
            "due_date": r["due_date"],
            "as_of_date": r["as_of_date"],
            "age_days": r["age_days"],
            "grace_period_days": r["grace_period_days"],
            "past_grace": bool(r["past_grace"]),
            "amount": round(float(r["amount"]), 2),
            "direction": r["direction"],
            "status": r["status"],
        }
        for r in rows
    ]


# --- scoring ---------------------------------------------------------------


def _best_match(
    conn: sqlite3.Connection, inst: sqlite3.Row, opts: dict[str, Any], claimed: set[str] | None = None
) -> dict[str, Any] | None:
    candidates = _scored_candidates(conn, dict(inst), opts, claimed)
    return candidates[0] if candidates else None


def _scored_candidates(
    conn: sqlite3.Connection, inst: dict[str, Any], opts: dict[str, Any], claimed: set[str] | None = None
) -> list[dict[str, Any]]:
    if not _has_transactions_table(conn):
        return []
    due = _coerce_date(inst["due_date"])
    window = int(opts["date_window_days"])
    start = (due.toordinal() - window)
    end = (due.toordinal() + window)
    direction = inst["direction"]
    amount = abs(float(inst["amount"]))
    name_tokens = _tokens(inst.get("obligation_name") or "")

    rows = conn.execute(
        """
        SELECT id, account_id, posted, transacted_at, amount, payee, description
        FROM transactions
        WHERE substr(COALESCE(posted, transacted_at), 1, 10) >= ?
          AND substr(COALESCE(posted, transacted_at), 1, 10) <= ?
        """,
        (date.fromordinal(start).isoformat(), date.fromordinal(end).isoformat()),
    ).fetchall()

    tol = max(float(opts["amount_abs_tolerance"]), amount * float(opts["amount_pct_tolerance"]))
    scored: list[dict[str, Any]] = []
    for txn in rows:
        if claimed and txn["id"] in claimed:
            continue
        txn_amount = float(txn["amount"])
        txn_direction = "inflow" if txn_amount > 0 else "outflow"
        if txn_direction != direction:
            continue
        posted = (txn["posted"] or txn["transacted_at"] or "")[:10]
        if not posted:
            continue
        txn_date = date.fromisoformat(posted)
        date_delta = abs((txn_date - due).days)

        amount_delta = round(abs(abs(txn_amount) - amount), 2)
        exact = amount_delta < 0.005
        if amount_delta <= 0.005:
            amount_score = 1.0
        elif amount_delta <= tol:
            amount_score = 0.7
        else:
            amount_score = 0.0
        if amount_score == 0.0:
            continue  # amount must at least be within tolerance to be a candidate

        date_score = 1.0 if date_delta <= 1 else max(0.0, 1.0 - (date_delta - 1) / max(window, 1))
        merchant_score = _merchant_score(name_tokens, _tokens(f"{txn['payee'] or ''} {txn['description'] or ''}"))

        score = round(amount_score * 0.5 + date_score * 0.3 + merchant_score * 0.2, 3)

        # A near-but-not-exact amount with NO merchant overlap is too weak to
        # propose (it is just a coincidental amount on a nearby date), so do not
        # let it reach needs_review/auto. Exact-amount matches are still allowed
        # (handled by the exact floor below) since some legit payments - a rent
        # check - carry no merchant name.
        no_merchant_weak = merchant_score == 0.0 and not exact

        if exact and date_delta <= int(opts["exact_match_date_window"]):
            score = max(score, 0.9)
            match_type = "auto"
        elif no_merchant_weak:
            match_type = "below_threshold"
        elif score >= float(opts["auto_threshold"]):
            match_type = "auto"
        elif score >= float(opts["review_threshold"]):
            match_type = "needs_review"
        else:
            match_type = "below_threshold"

        scored.append(
            {
                "transaction_id": txn["id"],
                "match_type": match_type,
                "match_score": score,
                "amount_score": amount_score,
                "date_score": round(date_score, 3),
                "merchant_score": round(merchant_score, 3),
                "amount_delta": amount_delta,
                "date_delta_days": (txn_date - due).days,
                "txn_amount": round(txn_amount, 2),
                "txn_payee": txn["payee"],
                "txn_date": posted,
                "txn_account_id": txn["account_id"],
            }
        )

    # Best by score, then closest date, then smallest amount delta, then id (stable).
    scored.sort(key=lambda c: (-c["match_score"], abs(c["date_delta_days"]), c["amount_delta"], c["transaction_id"]))
    return scored


def _merchant_score(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    overlap = a & b
    if not overlap:
        return 0.0
    return round(len(overlap) / len(a | b), 3)


def _tokens(text: str) -> set[str]:
    raw = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {t for t in raw if len(t) >= 3 and t not in _STOP_TOKENS and not t.isdigit()}


# --- persistence -----------------------------------------------------------


def _record_match(conn: sqlite3.Connection, inst: sqlite3.Row, best: dict[str, Any], as_of: date, now: str) -> None:
    evidence = {
        "amount_score": best["amount_score"],
        "date_score": best["date_score"],
        "merchant_score": best["merchant_score"],
        "txn_amount": best["txn_amount"],
        "txn_payee": best["txn_payee"],
        "txn_date": best["txn_date"],
        "instance_amount": round(abs(float(inst["amount"])), 2),
        "instance_due_date": inst["due_date"],
    }
    if best.get("group_evidence"):
        evidence["group"] = best["group_evidence"]
    conn.execute(
        """
        INSERT INTO transaction_obligation_matches (
            obligation_instance_id, transaction_id, match_type, match_score,
            amount_score, date_score, merchant_score, amount_delta, date_delta_days,
            as_of_date, evidence_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(obligation_instance_id) DO UPDATE SET
            transaction_id = excluded.transaction_id,
            match_type = excluded.match_type,
            match_score = excluded.match_score,
            amount_score = excluded.amount_score,
            date_score = excluded.date_score,
            merchant_score = excluded.merchant_score,
            amount_delta = excluded.amount_delta,
            date_delta_days = excluded.date_delta_days,
            as_of_date = excluded.as_of_date,
            evidence_json = excluded.evidence_json,
            updated_at = excluded.updated_at
        """,
        (
            inst["id"], best["transaction_id"], best["match_type"], best["match_score"],
            best["amount_score"], best["date_score"], best["merchant_score"], best["amount_delta"],
            best["date_delta_days"], as_of.isoformat(), json.dumps(evidence, sort_keys=True), now, now,
        ),
    )


def _mark_paid(conn: sqlite3.Connection, instance_id: str, best: dict[str, Any], now: str) -> None:
    conn.execute(
        """
        UPDATE obligation_instances
        SET status = 'paid', matched_transaction_id = ?, matched_at = ?,
            match_confidence = ?, updated_at = ?
        WHERE id = ?
        """,
        (best["transaction_id"], now, best["match_score"], now, instance_id),
    )


def _record_unmatched(conn: sqlite3.Connection, inst: sqlite3.Row, as_of: date, past_grace: bool, now: str) -> None:
    age_days = (as_of - _coerce_date(inst["due_date"])).days
    conn.execute(
        """
        INSERT INTO unmatched_obligations (
            obligation_instance_id, obligation_id, due_date, as_of_date, age_days,
            grace_period_days, past_grace, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(obligation_instance_id) DO UPDATE SET
            obligation_id = excluded.obligation_id,
            due_date = excluded.due_date,
            as_of_date = excluded.as_of_date,
            age_days = excluded.age_days,
            grace_period_days = excluded.grace_period_days,
            past_grace = excluded.past_grace,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            inst["id"], inst["obligation_id"], inst["due_date"], as_of.isoformat(), age_days,
            DEFAULT_OPTIONS["grace_period_days"], 1 if past_grace else 0, inst["status"], now, now,
        ),
    )


def _clear_unmatched(conn: sqlite3.Connection, instance_id: str) -> None:
    conn.execute("DELETE FROM unmatched_obligations WHERE obligation_instance_id = ?", (instance_id,))


def _clear_match(conn: sqlite3.Connection, instance_id: str) -> None:
    conn.execute("DELETE FROM transaction_obligation_matches WHERE obligation_instance_id = ?", (instance_id,))


def _has_transactions_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions' LIMIT 1"
    ).fetchone()
    return row is not None


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _loads(value: str | None) -> Any:
    return json.loads(value) if value else None


def _now() -> str:
    return datetime.now().astimezone().isoformat()
