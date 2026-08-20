"""Surface queue: the single read for the daily async surfacing job.

The daily routine (e.g. a Claude Code cron) wants ONE call that returns
everything worth pushing today, instead of fanning out to five separate list
tools. ``get_surface_queue`` aggregates those sources into one compact,
prioritized list, each item carrying a ``type``, a human ``message``, and a
``suggested_todoist_due`` so the caller can turn it straight into a reminder.

Sources combined (priority high -> low within a severity tier):
1. Match confirmations - reconciliation matches awaiting a human confirm
   (``list_reconciliation_review_items``).
2. Goals behind / due-soon - active savings goals off pace or near deadline
   (``list_goals``). Skipped gracefully if the goals table is absent.
3. Estimate reviews - estimated obligation amounts past their ``review_after``
   (``list_obligation_review_candidates``).
4. Snapshot refreshes - balance-only accounts (slow feeds like the Apple Card)
   whose latest balance snapshot is older than one statement cycle (~30 days).
5. Trough breach risk - the daily digest's downside band says the projected low
   could cross the cash floor or zero.
6. Guardrail trips - cash-floor / drift / window-age findings
   (``evaluate_guardrails``), advisory findings excluded.

Strictly read-only: it reads the same grounded helpers the individual tools
use and writes nothing (guardrails are evaluated with ``persist=False``). Given
the same ``as_of_date`` and unchanged data it returns the same items, so the
daily job can poll it safely.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .check_suggestions import list_check_suggestions
from .config import get_finance_config
from .follow_ups import list_due_followups
from .goals import list_goals
from .guardrails import CASH_FLOOR, evaluate_guardrails
from .manual_balance import BALANCE_PRECEDENCE_ORDER_BY
from .obligations import list_obligation_review_candidates
from .onboarding import list_charge_onboarding_queue
from .reconciliation import list_reconciliation_review_items
from .schema import ensure_app_schema

# A balance-only account whose latest snapshot is older than this is treated as
# stale and surfaced for a manual refresh. No account carries a refresh-cadence
# column, so this is a documented heuristic: one statement cycle ~= 30 days
# (e.g. the Apple Card portal updates roughly monthly).
SNAPSHOT_STALE_DAYS = 30

# Default cap on returned items so the call stays compact (one Claude breath).
DEFAULT_LIMIT = 30

# Lead window for surfacing a manual (non-autopay) obligation that needs a human
# action before it is due (e.g. write the rent check, run an Apple Card paydown
# sweep). An instance due within this many days of as_of is surfaced; autopay
# bills are never surfaced here (they stay quiet and drift-detection catches a
# failed post). Suggested Todoist due is two days before the obligation's due
# date so there is time to act.
MANUAL_DUE_LEAD_DAYS = 5
MANUAL_DUE_TODOIST_LEAD_DAYS = 2

# Instance statuses that count as still expected/unpaid for manual-due surfacing.
# Mirrors cashflow.PROJECTABLE_STATUSES: a cleared/reconciled ('paid'), canceled,
# or deleted instance is done and must not surface.
_MANUAL_DUE_OPEN_STATUSES: frozenset[str] = frozenset(
    {"expected", "needs_review", "partially_paid"}
)

# Severity ordering, highest first.
_SEVERITY_RANK: dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1}

# Tie-break by source type within the same severity: confirmations first, then
# estimates, snapshots, guardrails, goals.
_TYPE_RANK: dict[str, int] = {
    # A stale daily job means every other item below may be out of date, so it
    # ranks above them within the high-severity tier.
    "stale_job": 6,
    "match_confirmation": 5,
    # A manual bill that needs a human action by its due date ranks just below a
    # match confirmation: missing it (an unpaid rent check) is a real consequence.
    "obligation_due": 4.5,
    "estimate_review": 4,
    "snapshot_refresh": 3,
    "confirm_live_balance": 3,
    "trough_breach_risk": 2.5,
    "guardrail_warning": 2,
    "goal_review": 1,
}

# Goal statuses that warrant surfacing.
_GOAL_SURFACE_STATUSES: frozenset[str] = frozenset({"behind", "due_soon"})

# Singleton surface key for the onboarding digest. One row in the emissions
# ledger, updated in place as the candidate count changes; never fans out.
_ONBOARDING_DIGEST_KEY = "onboarding-digest"
_FINANCE_STATUS_KEY = "finance-status"

# Max candidate names listed in the digest body (highest priority_score first).
_ONBOARDING_DIGEST_NAMES = 5

# Guardrail rule types that read current balances / transactions and so are
# untrustworthy when the day's sync failed (balances are stale). When the caller
# flags a stale run we drop these to avoid a FALSE cash-floor / drift alert; the
# freshness guardrail (window_age) is intentionally NOT in this set - it is the
# one guardrail that is more relevant, not less, when data is stale.
_BALANCE_DERIVED_GUARDRAILS: frozenset[str] = frozenset({"cash_floor", "drift_threshold"})


def get_surface_queue(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
    limit: int = DEFAULT_LIMIT,
    suppress_balance_guardrails: bool = False,
    trough_sensitivity: dict[str, Any] | None = None,
    working_account_balance_stale: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate everything the daily job should surface into one ranked list.

    Read-only. Returns ``as_of_date``, a ``trace_id``, ``total_items`` (the count
    BEFORE the limit is applied, so the caller knows if anything was truncated),
    and ``items`` sorted by severity then source type and capped at ``limit``.

    ``suppress_balance_guardrails`` (default off) drops balance-derived guardrail
    trips (cash floor / drift). The daily routine sets it when the day's sync
    FAILED: balances are stale, so a cash-floor or drift alert built on them would
    be false. Non-balance items (due follow-ups, manual obligations by date, the
    freshness guardrail) still surface.

    ``working_account_balance_stale`` is optional precomputed digest balance data.
    When it says the working account's own balance date is stale, the queue also
    drops balance-derived guardrails and trough sensitivity for this read and adds
    one confirm-live-balance item instead of recomputing staleness here. This covers
    feed-lagged checking balances even when the full daily sync succeeded.

    ``trough_sensitivity`` is optional. The daily-digest builder supplies the
    already-computed trough downside band; this module does not import
    ``digest.py`` because the digest already imports this queue module.
    """

    ensure_app_schema(conn)
    as_of = _coerce_date(as_of_date)
    working_balance_stale = bool((working_account_balance_stale or {}).get("stale"))

    items: list[dict[str, Any]] = []
    items += _stale_job_items(conn, as_of)
    items += _match_confirmation_items(conn, as_of)
    items += _manual_obligation_due_items(conn, as_of)
    items += _goal_review_items(conn, as_of)
    items += _estimate_review_items(conn, as_of)
    items += _snapshot_refresh_items(conn, as_of)
    if not working_balance_stale:
        items += _trough_breach_items(trough_sensitivity, as_of)
    items += _working_account_balance_stale_items(working_account_balance_stale)
    items += _guardrail_items(
        conn,
        as_of,
        suppress_balance_guardrails=suppress_balance_guardrails or working_balance_stale,
    )

    items.sort(
        key=lambda it: (
            -_SEVERITY_RANK.get(it["severity"], 0),
            -_TYPE_RANK.get(it["type"], 0),
            it["id"],
        )
    )

    total = len(items)
    if limit is not None and limit >= 0:
        items = items[:limit]

    return {
        "as_of_date": as_of.isoformat(),
        "trace_id": f"surfq_{uuid.uuid4().hex[:12]}",
        "source": "computed_would_surface",
        "source_note": (
            "Computed list of items that WOULD be surfaced to Todoist from local "
            "finance data - NOT a read of the live Todoist board. For actual board "
            "contents call list_todoist_project."
        ),
        "total_items": total,
        "returned_items": len(items),
        "items": items,
    }


# --- ledger-ready surfacing items ------------------------------------------


def build_sync_failed_item(as_of_date: date | str) -> dict[str, Any]:
    """The one extra item to surface when the day's source sync failed.

    A failed ``run_background_sync`` means balances did NOT refresh, so any
    cash-floor / drift alert built on them would be a false alarm (those are
    dropped via ``suppress_balance_guardrails`` on the read path). This item is
    the visible flag that the data is stale and the guardrail checks were skipped
    this run. Its ``surface_key`` is keyed by date (``data-sync-failed:<today>``)
    so the emissions ledger dedupes a same-day re-run instead of nagging twice.

    The body says what happened in plain words (no internal function name) and
    closes with the same dated action line every other surfaced task uses.
    """

    as_of = _coerce_date(as_of_date).isoformat()
    action = render_next_action(NextAction(verb="Re-run the daily sync", by=as_of))
    return {
        "surface_key": f"data-sync-failed:{as_of}",
        "content": "Data sync failed - balances stale",
        "description": (
            f"The {as_of} pull of bank and card data did not finish, so the "
            "balances behind these tasks are stale and the cash-floor and drift "
            f"checks were skipped. {action}"
        ),
        "due_date": as_of,
        # Todoist priority 4 = highest (p1 in the UI).
        "priority": 4,
    }


def build_surface_items(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
    headline: str | None = None,
) -> list[dict[str, Any]]:
    """Build de-dupe-ready items for ``surface_to_todoist``.

    Each item carries a STABLE ``surface_key`` (the spec's idempotency key), a
    ``content`` (task title), a ``description`` (body), and an optional
    ``due_date`` / ``priority``. The keys are deterministic from content, not
    random ids, so the same item maps to the same Todoist task across days and
    re-runs:
    - ``followup:<id>`` from due follow-ups
    - ``goal:<name>:behind`` from goals off pace (the ``behind`` status only)
    - ``obligation-due:<obligation_id>:<due_date>`` from manual (non-autopay)
      bills due within the lead window
    - ``estimate-review:<obligation_id>:<cycle>`` from obligations past review
    - ``snapshot-due:<account>`` from stale balance-only account snapshots
    - ``check-suggestion:<id>`` from generic checks that may pay a known bill

    Read-only. Returns items in a deterministic order (follow-ups, goals,
    manual-due, estimates, snapshots, check suggestions) for stable re-runs.

    This is the COMPLETE read of what needs attention today, and the daily digest
    renders from it, so nothing is dropped here. Items that two builders can raise
    for the same bill carry a ``subject``; the one-task-per-subject rule is
    applied by the Todoist push (``one_task_per_subject``), which is the only
    place a duplicate task can actually appear.
    """

    ensure_app_schema(conn)
    as_of = _coerce_date(as_of_date)
    items: list[dict[str, Any]] = []
    items += _followup_surface_items(conn, as_of)
    items += _goal_behind_surface_items(conn, as_of)
    items += _manual_obligation_due_surface_items(conn, as_of)
    items += _estimate_review_surface_items(conn, as_of)
    items += _snapshot_due_surface_items(conn, as_of)
    items += _check_suggestion_surface_items(conn, as_of)
    items += _onboarding_digest_surface_item(conn, as_of)
    items += _finance_status_surface_item(headline, as_of)
    return items


def build_surface_retire_keys(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str,
) -> list[str]:
    """Read-only: surface_keys the write path should retire this run.

    Returns the singleton onboarding digest when its queue is empty, plus open
    generic-check suggestion tasks whose pair is no longer eligible after a
    confirmation or rejection.
    """

    ensure_app_schema(conn)
    as_of = _coerce_date(as_of_date)
    retire_keys: list[str] = []
    try:
        candidates = list_charge_onboarding_queue(conn)
    except sqlite3.OperationalError:
        candidates = None
    if candidates == []:
        retire_keys.append(_ONBOARDING_DIGEST_KEY)

    try:
        active_check_keys = {
            f"check-suggestion:{item['suggestion_id']}"
            for item in list_check_suggestions(conn, as_of_date=as_of)
        }
    except sqlite3.OperationalError:
        active_check_keys = None
    if active_check_keys is not None:
        rows = conn.execute(
            "SELECT surface_key FROM todoist_emissions "
            "WHERE status = 'open' AND surface_key LIKE 'check-suggestion:%'"
        ).fetchall()
        retire_keys += [row[0] for row in rows if row[0] not in active_check_keys]

    retire_keys += _subject_dedupe_retire_keys(conn, as_of)
    retire_keys += _resolved_bill_retire_keys(conn, as_of)
    # Two rules can name the same key (a rejected suggestion that also lost its
    # subject); retire it once, in the order it was found.
    return list(dict.fromkeys(retire_keys))


def _subject_dedupe_retire_keys(conn: sqlite3.Connection, as_of: date) -> list[str]:
    """Tasks already on the board for a bill that a different task now speaks for.

    ``one_task_per_subject`` only trims THIS run's pushed items; an already-open
    loser is retired here so the winner is the one task asking about the bill.

    Two hard limits protect the board:
    - A pay task (``obligation-due:``) is NEVER retired by this rule. An unpaid
      bill keeps its task until the bill is confirmed paid (owner decision
      2026-08-20); a check question may hold a NEW pay task off the board, but it
      never takes an existing one down. ``_resolved_bill_retire_keys`` is what
      removes a pay task, and only once its instance is actually settled.
    - A key is only judged by the subject of the item that built it THIS run
      (subjects are per bill instance). An open task the current run did not
      rebuild has no subject here and is left alone, so a task about a different
      month can never be swept up as a "duplicate".

    A stale key in the WINNER's own family (the bill's due date moved) is
    re-pointed in place by ``surface_to_todoist``'s re-key path, which keeps the
    same Todoist task instead of churning it.
    """

    try:
        tagged = (
            _manual_obligation_due_surface_items(conn, as_of)
            + _estimate_review_surface_items(conn, as_of)
            + _check_suggestion_surface_items(conn, as_of)
        )
    except sqlite3.OperationalError:
        return []
    winners = {
        subject: tagged[idx]["surface_key"] for subject, idx in _subject_winners(tagged).items()
    }
    if not winners:
        return []
    subject_by_key = {
        item["surface_key"]: item["subject"] for item in tagged if item.get("subject")
    }

    stale: list[str] = []
    rows = conn.execute(
        "SELECT surface_key FROM todoist_emissions WHERE status = 'open'"
    ).fetchall()
    for (key,) in rows:
        family = key.split(":", 1)[0]
        if family == "obligation-due":
            continue
        subject = subject_by_key.get(key)
        winner = winners.get(subject) if subject else None
        if winner is None or winner == key or winner.split(":", 1)[0] == family:
            continue
        stale.append(key)
    return stale


def _resolved_bill_retire_keys(conn: sqlite3.Connection, as_of: date) -> list[str]:
    """Open pay tasks whose bill instance is settled (paid, canceled, deleted).

    Approving a check match, reconciling a payment, or canceling the bill all
    resolve the instance without anyone ticking the Todoist task, and the subject
    dedupe above never deletes a pay task, so this is the one place a settled
    bill's task comes down. A key whose bill still surfaces today under a moved
    key is skipped: the re-key path re-points that task instead of churning it.
    """

    try:
        live_stems = {
            item["surface_key"].rsplit(":", 1)[0]
            for item in _manual_obligation_due_surface_items(conn, as_of)
        }
    except sqlite3.OperationalError:
        return []
    open_statuses = tuple(sorted(_MANUAL_DUE_OPEN_STATUSES))
    placeholders = ",".join("?" for _ in open_statuses)
    stale: list[str] = []
    rows = conn.execute(
        "SELECT surface_key FROM todoist_emissions "
        "WHERE status = 'open' AND surface_key LIKE 'obligation-due:%'"
    ).fetchall()
    for (key,) in rows:
        stem, _, due = key.rpartition(":")
        obligation_id = stem.split(":", 1)[1] if ":" in stem else ""
        if not obligation_id or stem in live_stems:
            continue
        try:
            still_open = conn.execute(
                f"SELECT 1 FROM obligation_instances "
                f"WHERE obligation_id = ? AND due_date = ? "
                f"AND status IN ({placeholders}) LIMIT 1",
                (obligation_id, due, *open_statuses),
            ).fetchone()
        except sqlite3.OperationalError:
            continue
        if still_open is None:
            stale.append(key)
    return stale


# --- one task per subject --------------------------------------------------
# Three builders can raise the SAME bill in the same run: the payment task, the
# estimate refresh, and a check that may already have paid it. All three on the
# BOARD reads as duplicates (and the worst pair asks for a payment that may have
# already cleared), so the builders that can collide tag their item with a
# ``subject`` and the Todoist push raises only the highest-precedence item per
# subject. This is a push-time rule, not a read-time one: the digest still names
# every one of them, so a bill is never silently missing from the daily close.
# Precedence is answer-before-act: settle whether the bill is already paid, then
# pay it, then refine its estimate. The loser is not retired here - the winner is
# simply the only task raised while it is open.
_SUBJECT_PRECEDENCE: dict[str, int] = {
    "check-suggestion": 0,
    "obligation-due": 1,
    "estimate-review": 2,
}


def _subject_winners(items: list[dict[str, Any]]) -> dict[str, int]:
    """subject -> index of the one item that gets to speak for it this run."""

    ranked: dict[str, tuple[int, int]] = {}  # subject -> (precedence, index)
    for idx, item in enumerate(items):
        subject = item.get("subject")
        if not subject:
            continue
        rank = _SUBJECT_PRECEDENCE.get(item["surface_key"].split(":", 1)[0], 99)
        if subject not in ranked or rank < ranked[subject][0]:
            ranked[subject] = (rank, idx)
    return {subject: idx for subject, (_, idx) in ranked.items()}


def one_task_per_subject(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one item per ``subject``; drop the rest. Untagged items pass through.

    Called by the Todoist push, never by the digest read. Returns copies so the
    caller's item list (which the digest may still be rendering from) is left
    intact, and strips ``subject`` so the internal routing field never ships.
    """

    keep = set(_subject_winners(items).values())
    kept: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        if item.get("subject") and idx not in keep:
            continue
        if "subject" in item:
            item = {k: v for k, v in item.items() if k != "subject"}
        kept.append(item)
    return kept


def _check_suggestion_surface_items(
    conn: sqlite3.Connection, as_of: date
) -> list[dict[str, Any]]:
    try:
        suggestions = list_check_suggestions(conn, as_of_date=as_of)
    except sqlite3.OperationalError:
        return []

    items: list[dict[str, Any]] = []
    for suggestion in suggestions:
        bill = suggestion["bill"]
        transaction = suggestion["transaction"]
        eligible = suggestion["evidence"]["competition"]["eligible_bill_count"]
        ambiguous = "yes" if suggestion["ambiguous"] else "no"
        suggestion_id = suggestion["suggestion_id"]
        by = as_of.isoformat()
        action = render_next_action(
            NextAction(
                verb=f"Reply approve {suggestion_id} or reject {suggestion_id} to the agent",
                by=by,
            )
        )
        items.append(
            {
                "surface_key": f"check-suggestion:{suggestion_id}",
                "subject": f"obligation-instance:{bill['instance_id']}",
                "content": f"Review check match: {bill['name']}",
                "description": (
                    f"{bill['name']} for ${_money(abs(bill['amount']))} is due "
                    f"{bill['due_date']} and still open, and check "
                    f"{transaction['check_identifier']} for "
                    f"${_money(abs(transaction['amount']))} cleared "
                    f"{transaction['account_name']} on {transaction['date']}. That "
                    "check may be this payment (ambiguous: "
                    f"{ambiguous}, {eligible} eligible "
                    f"{'bill' if eligible == 1 else 'bills'}). Approving marks the "
                    "bill paid; rejecting keeps it open. Checking this Todoist task "
                    f"off is not financial approval. {action}"
                ),
                "due_date": by,
            }
        )
    return items


def _finance_status_surface_item(headline: str | None, as_of: date) -> list[dict[str, Any]]:
    """The standing status task: says what it is, and closes with a dated action.

    The headline alone reads as a bare number with no reason for existing, so the
    body names the run it came from and ends with the same action line every other
    surfaced task uses (reading it IS the whole action here).
    """

    summary = (headline or "").strip()
    if not summary:
        return []
    by = as_of.isoformat()
    action = render_next_action(NextAction(verb="Read today's finance status", by=by))
    # The headline is free text and often ends mid-clause; close it so it does not
    # run into the action line.
    if summary[-1] not in ".!?":
        summary += "."
    return [
        {
            "surface_key": _FINANCE_STATUS_KEY,
            "content": "Finance status",
            "description": f"Daily finance check for {by}. {summary} {action}",
            "due_date": by,
        }
    ]


def _onboarding_digest_surface_item(
    conn: sqlite3.Connection, as_of: date
) -> list[dict[str, Any]]:
    """ONE digest item for the active charge-onboarding queue, never one-per-candidate.

    Counts candidates still awaiting a human decision (status in
    ``ACTIVE_STATUSES``). When the queue is empty we emit nothing; the retire of a
    stale digest task left over from a prior non-empty run is reported as data by
    ``build_surface_retire_keys`` and applied by the write path (``surface_to_todoist``),
    so this builder writes nothing. When the queue is non-empty we emit a single item
    whose stable ``surface_key`` keeps the emissions ledger updating one task in place
    as the count changes.

    ``as_of`` is unused (the queue is not date-filtered) but kept for a uniform
    builder signature.
    """

    try:
        candidates = list_charge_onboarding_queue(conn)
    except sqlite3.OperationalError:
        return []

    count = len(candidates)
    if count == 0:
        return []

    # Queue is already ordered by priority_score DESC, so the head is highest.
    names = [c["display_name"] for c in candidates[:_ONBOARDING_DIGEST_NAMES] if c["display_name"]]
    top = "; ".join(names)
    extra = count - len(names)
    more = f" (+{extra} more)" if extra > 0 else ""
    by = (as_of + timedelta(days=_ONBOARDING_TRIAGE_LEAD_DAYS)).isoformat()
    action = render_next_action(
        NextAction(
            verb="Triage",
            by=by,
            account="the charge-onboarding review",
            preposition="in",
        )
    )
    noun = "charge" if count == 1 else "charges"
    description = f"{count} {noun} awaiting review. Top: {top}{more}. {action}"
    return [
        {
            "surface_key": _ONBOARDING_DIGEST_KEY,
            "content": f"{count} {noun} to review",
            "description": description,
            "due_date": by,
        }
    ]


def _manual_obligation_due_surface_items(
    conn: sqlite3.Connection, as_of: date
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    account = _operating_account_name(conn)
    for it in _manual_obligation_due_items(conn, as_of):
        ev = it["evidence"]
        by = it["suggested_todoist_due"]
        name = it["obligation_name"]
        if ev["amount_discretionary"]:
            # The user decides the amount each time; the modeled figure is a floor.
            action = NextAction(
                verb="Decide amount + pay",
                by=by,
                amount=ev["amount"],
                direction=ev["direction"],
                account=account,
                modeled_min=True,
            )
            # Title is a plain action; the modeled minimum, due date, and account
            # live in the description so the Todoist board reads as a task list.
            content = f"Decide amount + pay {name}"
        else:
            action = NextAction(
                verb="Pay",
                by=by,
                amount=ev["amount"],
                direction=ev["direction"],
                account=account,
            )
            content = f"Pay {name} ${_money(ev['amount'])}"
        item = {
            "surface_key": it["id"],  # obligation-due:<obligation_id>:<due_date>
            "content": content,
            "description": (
                f"{name} is due {ev['due_date']} and has no autopay, so nothing "
                f"pays it unless you do. {render_next_action(action)}"
            ),
            "due_date": by,
        }
        # A bill already past due is never held off the board by another
        # question about it (owner decision 2026-08-20): re-raised past-due
        # rows carry no subject, so they always push.
        if ev["days_until_due"] >= 0:
            item["subject"] = f"obligation-instance:{ev['obligation_instance_id']}"
        items.append(item)
    return items


def _short_title(text: str, *, limit: int = 80) -> str:
    """A scannable Todoist title from a possibly-long follow-up note.

    A follow-up note can be a paragraph (context, internal ids, a stray dev
    note). Todoist shows the title as one line, so dumping the whole note there
    reads as a wall of text. This takes the first line's first sentence and caps
    it at a word boundary; the full note still lands in the description. Keeping
    notes clean at the source is the primary discipline (see the finance skill's
    "Todoist task style"); this is the defensive backstop so an over-long note
    can never produce an ugly title.
    """
    head = text.strip().splitlines()[0].strip() if text.strip() else text.strip()
    first = re.split(r"(?<=[.!?])\s+", head, maxsplit=1)[0].strip()
    # A title reads better without a trailing sentence period.
    if first.endswith("."):
        first = first[:-1]
    if len(first) <= limit:
        return first
    cut = first[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{cut}..."


def _followup_surface_items(conn: sqlite3.Connection, as_of: date) -> list[dict[str, Any]]:
    try:
        rows = list_due_followups(conn, as_of_date=as_of.isoformat())
    except sqlite3.OperationalError:
        return []
    items: list[dict[str, Any]] = []
    for r in rows:
        by = r["surface_when"]
        # The follow-up text IS the action verb/instruction; render the full text
        # on the standard dated line (description) so the deadline is explicit,
        # and derive a short one-line title so the board stays scannable. The
        # trigger sentence says where the note came from, so the task does not
        # need the session that raised it to make sense.
        action = render_next_action(NextAction(verb=r["text"], by=by))
        items.append(
            {
                "surface_key": f"followup:{r['id']}",
                "content": _short_title(r["text"]),
                "description": f"{_followup_trigger(r)} {action}",
                "due_date": by,
            }
        )
    return items


def _followup_trigger(row: dict[str, Any]) -> str:
    """One plain sentence naming who raised this follow-up and when."""

    raised_on = _date_part(row.get("created_at"))
    who = (row.get("source") or "").strip()
    tail = f" on {raised_on.isoformat()}" if raised_on else ""
    if not who or who == "manual":
        # "manual" means Albert asked for the reminder himself.
        return f"Follow-up you asked for{tail}." if tail else "Follow-up you asked for."
    return f"Follow-up raised by {who}{tail}."


def _goal_behind_surface_items(conn: sqlite3.Connection, as_of: date) -> list[dict[str, Any]]:
    try:
        goals = list_goals(conn, as_of_date=as_of)
    except sqlite3.OperationalError:
        return []
    items: list[dict[str, Any]] = []
    for g in goals:
        if g["status"] != "behind":
            continue
        # Deadline may be open-ended; fall back to as_of so the action line and
        # due_date are never empty.
        by = g["deadline"] or as_of.isoformat()
        # The amount to move is the catch-up monthly rate; fall back to the full
        # remaining amount when no rate is computed.
        move_amount = g.get("required_monthly_rate") or g.get("remaining_amount")
        action = render_next_action(
            NextAction(
                verb="Move",
                by=by,
                amount=move_amount,
                direction="inflow",
                account=f'"{g["name"]}"',
            )
        )
        items.append(
            {
                "surface_key": f"goal:{g['name']}:behind",
                "content": f"Goal behind: {g['name']}",
                "description": (
                    f"{g['name']} is behind the pace it needs: "
                    f"${_money(g['current_progress'])} saved of "
                    f"${_money(g['target_amount'])}. {action}"
                ),
                "due_date": by,
            }
        )
    return items


def _estimate_review_surface_items(conn: sqlite3.Connection, as_of: date) -> list[dict[str, Any]]:
    try:
        rows = list_obligation_review_candidates(conn, as_of_date=as_of)
    except sqlite3.OperationalError:
        return []
    items: list[dict[str, Any]] = []
    for r in rows:
        # The cycle component identifies the statement period; fall back to the
        # instance id when no close date is carried, which still yields a stable
        # per-instance key.
        cycle = r.get("statement_close_date") or r["instance_id"]
        # review_after has passed, so the refresh is actionable now: by = today.
        by = as_of.isoformat()
        # Refresh comes from a statement (balance-only framing): never imply a feed.
        action = render_next_action(
            NextAction(
                verb="Refresh estimate",
                by=by,
                account="the statement",
                preposition="from",
            )
        )
        items.append(
            {
                "surface_key": f"estimate-review:{r['obligation_id']}:{cycle}",
                "subject": f"obligation-instance:{r['instance_id']}",
                "content": f"Refresh estimate: {r['obligation_name']}",
                "description": (
                    f"{r['obligation_name']} is still carrying an estimated amount "
                    f"(${_money(r['amount'])}) and its {r['review_after']} check-back "
                    f"date has passed. {action}"
                ),
                "due_date": by,
            }
        )
    return items


def _snapshot_due_surface_items(conn: sqlite3.Connection, as_of: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for it in _snapshot_refresh_items(conn, as_of):
        ev = it["evidence"]
        acct = ev["account_id"]
        name = ev["account_name"]
        days_old = ev["days_old"]
        # The snapshot is stale now, so the refresh is due today. Balance-only
        # account: update from its portal (no live feed implied).
        by = as_of.isoformat()
        action = render_next_action(
            NextAction(
                verb="Update balance",
                by=by,
                account=f"the {name} portal",
                preposition="from",
            )
        )
        items.append(
            {
                "surface_key": f"snapshot-due:{acct}",
                "content": f"Update balance: {name}",
                "description": f"{name} snapshot is {days_old} days old. {action}",
                "due_date": by,
            }
        )
    return items


# --- sources ---------------------------------------------------------------


def _stale_job_items(conn: sqlite3.Connection, as_of: date) -> list[dict[str, Any]]:
    """A HIGH-priority alert when the daily sync job has stopped running.

    A silently-stopped scheduler is invisible - nothing fails, data just ages.
    Surfacing it here makes a dead job visible the next time ANYTHING reads the
    queue. Imported lazily because ``background`` imports the Todoist outbox layer;
    keeping this import local avoids dragging that cycle into the queue module.
    """

    from .background import get_job_health

    try:
        health = get_job_health(conn, as_of_date=as_of.isoformat())
    except sqlite3.OperationalError:
        return []
    if not health["is_stale"]:
        return []

    hours = health["hours_since_last_run"]
    threshold = health["stale_threshold_hours"]
    if hours is None:
        detail = f"no successful daily sync on record (threshold: {threshold}h)"
    else:
        detail = f"last completed {hours:.1f}h ago (threshold: {threshold}h)"
    return [
        {
            "id": "stale_daily_job",
            "type": "stale_job",
            "severity": "high",
            "message": (
                f"Daily sync job may be stopped: {detail}. "
                "Check cron/scheduler logs and restart the daily runner."
            ),
            "suggested_todoist_due": "today",
            "related_ids": [health["last_run_id"]] if health["last_run_id"] else [],
            "evidence": {
                "last_run_id": health["last_run_id"],
                "last_run_status": health["last_run_status"],
                "last_run_finished_at": health["last_run_finished_at"],
                "hours_since_last_run": hours,
                "stale_threshold_hours": threshold,
            },
        }
    ]


def _match_confirmation_items(conn: sqlite3.Connection, as_of: date) -> list[dict[str, Any]]:
    try:
        rows = list_reconciliation_review_items(conn, as_of_date=as_of)
    except sqlite3.OperationalError:
        return []
    items: list[dict[str, Any]] = []
    for r in rows:
        iid = r["obligation_instance_id"]
        items.append(
            {
                "id": f"match:{iid}",
                "type": "match_confirmation",
                "severity": "high",
                "message": (
                    f"{r['obligation_name']} ({r['due_date']}, ${_money(r['amount'])}) "
                    f"matched txn {r['transaction_id']} - confirm it cleared."
                ),
                "suggested_todoist_due": "today",
                "related_ids": [iid, r["obligation_id"], r["transaction_id"]],
                "evidence": {
                    "obligation_id": r["obligation_id"],
                    "obligation_instance_id": iid,
                    "transaction_id": r["transaction_id"],
                    "match_score": r["match_score"],
                    "amount_delta": r.get("amount_delta"),
                    "due_date": r["due_date"],
                },
            }
        )
    return items


def _manual_obligation_due_rows(conn: sqlite3.Connection, as_of: date) -> list[sqlite3.Row]:
    """Manual (non-autopay) obligation instances due within the lead window.

    Selects only obligations explicitly classified manual (``autopay = 0``) whose
    active obligation has a still-expected/unpaid instance due in
    ``[as_of, as_of + MANUAL_DUE_LEAD_DAYS]``. Autopay obligations are excluded so
    they stay quiet. Older databases without the ``autopay`` column raise
    OperationalError, which the callers turn into an empty list.

    Past-due instances are normally left out (the window starts today), with ONE
    exception: a bill whose "did this check pay it?" question was answered NO
    after it came due. Saying no means nobody has paid it, so it comes back
    (see ``_rejected_check_past_due_rows``).
    """

    window_start = as_of.isoformat()
    window_end = (as_of + timedelta(days=MANUAL_DUE_LEAD_DAYS)).isoformat()
    open_statuses = tuple(sorted(_MANUAL_DUE_OPEN_STATUSES))
    placeholders = ",".join("?" for _ in open_statuses)
    rows = conn.execute(
        f"""
        SELECT
            o.id AS obligation_id,
            o.name AS obligation_name,
            o.amount_discretionary AS amount_discretionary,
            oi.id AS instance_id,
            oi.due_date,
            oi.amount,
            oi.direction,
            oi.status
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE o.status = 'active'
          AND o.autopay = 0
          AND oi.status IN ({placeholders})
          AND oi.due_date >= ?
          AND oi.due_date <= ?
        ORDER BY oi.due_date, oi.id
        """,
        (*open_statuses, window_start, window_end),
    ).fetchall()
    rows += _rejected_check_past_due_rows(conn, as_of, open_statuses, placeholders)
    rows.sort(key=lambda r: (r["due_date"], r["instance_id"]))
    return rows


def _rejected_check_past_due_rows(
    conn: sqlite3.Connection,
    as_of: date,
    open_statuses: tuple[str, ...],
    placeholders: str,
) -> list[sqlite3.Row]:
    """Past-due manual bills whose check match was rejected after they came due.

    When a check may have paid a bill, the push takes the pay task down and asks
    about the check instead. If Albert then says that check is NOT this payment,
    the bill is unpaid, past its due date, and no longer inside the normal window,
    so without this it would have no task at all. Rejecting re-raises it.

    The rejection must be dated on or after the due date: an older rejection was
    about a check written before the bill came due and never took a pay task
    down. Databases predating the rejections table raise OperationalError, which
    degrades to "no re-raise" instead of losing the whole manual-due read.
    """

    try:
        return conn.execute(
            f"""
            SELECT
                o.id AS obligation_id,
                o.name AS obligation_name,
                o.amount_discretionary AS amount_discretionary,
                oi.id AS instance_id,
                oi.due_date,
                oi.amount,
                oi.direction,
                oi.status
            FROM obligation_instances oi
            JOIN obligations o ON o.id = oi.obligation_id
            WHERE o.status = 'active'
              AND o.autopay = 0
              AND oi.status IN ({placeholders})
              AND oi.due_date < ?
              AND EXISTS (
                  SELECT 1 FROM check_suggestion_rejections r
                  WHERE r.obligation_instance_id = oi.id
                    AND substr(r.rejected_at, 1, 10) >= oi.due_date
              )
            ORDER BY oi.due_date, oi.id
            """,
            (*open_statuses, as_of.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _manual_due_severity(days_until: int) -> str:
    """Severity rises as the due date nears: due today/overdue is critical."""

    if days_until <= 0:
        return "critical"
    if days_until <= 2:
        return "high"
    return "medium"


def _manual_due_todoist_due(due: date) -> str:
    """Suggested Todoist due: a couple of days before the obligation is due."""

    return (due - timedelta(days=MANUAL_DUE_TODOIST_LEAD_DAYS)).isoformat()


def _manual_obligation_due_items(conn: sqlite3.Connection, as_of: date) -> list[dict[str, Any]]:
    try:
        rows = _manual_obligation_due_rows(conn, as_of)
    except sqlite3.OperationalError:
        return []
    items: list[dict[str, Any]] = []
    for r in rows:
        due = _date_part(r["due_date"])
        if due is None:
            continue
        days_until = (due - as_of).days
        discretionary = bool(r["amount_discretionary"])
        if discretionary:
            # The user decides the amount each time; the modeled figure is only a
            # floor. Frame it as a decision ("decide amount + pay") rather than a
            # fixed bill, with the modeled minimum shown as guidance.
            message = (
                f"{r['obligation_name']} due {r['due_date']} - "
                f"decide amount + pay (modeled min ~${_money(r['amount'])}) (manual)"
            )
        else:
            message = (
                f"{r['obligation_name']} due {r['due_date']}: "
                f"${_money(r['amount'])} (manual)"
            )
        items.append(
            {
                # The stable key dedups + updates in place via the emissions
                # ledger: same obligation + due date -> same Todoist task.
                "id": f"obligation-due:{r['obligation_id']}:{r['due_date']}",
                "type": "obligation_due",
                "severity": _manual_due_severity(days_until),
                "obligation_name": r["obligation_name"],
                "message": message,
                "suggested_todoist_due": _manual_due_todoist_due(due),
                "related_ids": [r["instance_id"], r["obligation_id"]],
                "evidence": {
                    "obligation_id": r["obligation_id"],
                    "obligation_instance_id": r["instance_id"],
                    "due_date": r["due_date"],
                    "amount": round(float(r["amount"]), 2),
                    "direction": r["direction"],
                    "status": r["status"],
                    "days_until_due": days_until,
                    "autopay": False,
                    "amount_discretionary": discretionary,
                },
            }
        )
    return items


def _goal_review_items(conn: sqlite3.Connection, as_of: date) -> list[dict[str, Any]]:
    # The goals table may not exist on older databases; skip silently if so.
    try:
        goals = list_goals(conn, as_of_date=as_of)
    except sqlite3.OperationalError:
        return []
    items: list[dict[str, Any]] = []
    for g in goals:
        if g["status"] not in _GOAL_SURFACE_STATUSES:
            continue
        # A near-deadline goal is more urgent than one merely off pace.
        severity = "high" if g["status"] == "due_soon" else "medium"
        if g["status"] == "due_soon":
            tail = f"deadline {g['deadline']} is near"
        else:
            tail = "behind the pace needed to hit target"
        items.append(
            {
                "id": f"goal:{g['goal_id']}",
                "type": "goal_review",
                "severity": severity,
                "message": (
                    f"Goal {g['name']}: ${_money(g['current_progress'])} of "
                    f"${_money(g['target_amount'])} - {tail}."
                ),
                "suggested_todoist_due": g["deadline"] or "today",
                "related_ids": [g["goal_id"]],
                "evidence": {
                    "goal_id": g["goal_id"],
                    "status": g["status"],
                    "deadline": g["deadline"],
                    "remaining_amount": g["remaining_amount"],
                    "required_monthly_rate": g["required_monthly_rate"],
                },
            }
        )
    return items


def _estimate_review_items(conn: sqlite3.Connection, as_of: date) -> list[dict[str, Any]]:
    try:
        rows = list_obligation_review_candidates(conn, as_of_date=as_of)
    except sqlite3.OperationalError:
        return []
    items: list[dict[str, Any]] = []
    for r in rows:
        iid = r["instance_id"]
        # A large estimated outflow whose real amount is unknown is more urgent.
        severity = "high" if abs(r["amount"]) >= 1000 else "medium"
        items.append(
            {
                "id": f"estimate:{iid}",
                "type": "estimate_review",
                "severity": severity,
                "message": (
                    f"{r['obligation_name']} amount is estimated (${_money(r['amount'])}); "
                    f"review_after {r['review_after']} has passed - refresh from the statement."
                ),
                # The estimate is ready to refresh now (review_after has passed),
                # so it is due today rather than on the obligation's due date.
                "suggested_todoist_due": "today",
                "related_ids": [iid, r["obligation_id"]],
                "evidence": {
                    "obligation_id": r["obligation_id"],
                    "instance_id": iid,
                    "amount_status": r["amount_status"],
                    "review_after": r["review_after"],
                    "due_date": r["due_date"],
                    "amount": r["amount"],
                },
            }
        )
    return items


def _snapshot_refresh_items(conn: sqlite3.Connection, as_of: date) -> list[dict[str, Any]]:
    """Balance-only accounts whose latest snapshot is older than one cycle.

    "Balance-only" is inferred from the latest snapshot's source: a slow feed
    that only ever gets ``manual`` corrections (e.g. the Apple Card) is the case
    this targets. Actively-synced accounts keep a fresh ``simplefin`` snapshot,
    so they never trip this. If the snapshot tables are missing, skip silently.
    """

    if not _has_table(conn, "balance_snapshots") or not _has_table(conn, "accounts"):
        return []
    rows = conn.execute(
        f"""
        SELECT a.id AS account_id, a.name AS account_name, a.org,
               bs.recorded_at, bs.source
        FROM balance_snapshots bs
        JOIN accounts a ON a.id = bs.account_id
        WHERE bs.id = (
            SELECT inner_bs.id FROM balance_snapshots inner_bs
            WHERE inner_bs.account_id = bs.account_id
            {BALANCE_PRECEDENCE_ORDER_BY.format(alias="inner_bs")} LIMIT 1
        )
        """
    ).fetchall()

    items: list[dict[str, Any]] = []
    for r in rows:
        if (r["source"] or "").lower() != "manual":
            # Only balance-only / manually-maintained feeds are surfaced here.
            continue
        recorded = _date_part(r["recorded_at"])
        if recorded is None:
            continue
        days_old = (as_of - recorded).days
        if days_old < SNAPSHOT_STALE_DAYS:
            continue
        items.append(
            {
                "id": f"snapshot:{r['account_id']}",
                "type": "snapshot_refresh",
                "severity": "medium",
                "message": (
                    f"{r['account_name']} balance snapshot is {days_old} days old "
                    f"(last recorded {recorded.isoformat()}); update it from the portal."
                ),
                "suggested_todoist_due": "today",
                "related_ids": [r["account_id"]],
                "evidence": {
                    "account_id": r["account_id"],
                    "account_name": r["account_name"],
                    "org": r["org"],
                    "days_old": days_old,
                    "last_recorded_at": r["recorded_at"],
                    "stale_threshold_days": SNAPSHOT_STALE_DAYS,
                },
            }
        )
    return items


def _trough_breach_items(
    trough_sensitivity: dict[str, Any] | None, as_of: date
) -> list[dict[str, Any]]:
    """One item when the projected trough's downside band could breach the
    cash floor or cross zero, naming the single top driver (e.g. "Apple
    Card statement estimate could drop your Jul low to ~$840"). Fed by
    digest.py's `_trough_sensitivity` headline band for the longest
    window, passed in by the caller rather than imported here (digest.py
    already imports this module, so importing it back would cycle).
    Singleton id (no date suffix) so a downstream write path keyed on this
    id updates one task in place across days instead of piling up
    duplicates - severity flips between high/medium as the risk moves but
    the id stays the same.
    """

    ts = trough_sensitivity or {}
    low_estimate = ts.get("low_estimate")
    if low_estimate is None or not ts.get("breach_risk") or low_estimate >= CASH_FLOOR:
        return []
    severity = "high" if low_estimate < 0 else "medium"
    drivers = ts.get("drivers") or []
    top = drivers[0] if drivers else None
    driver_name = (top or {}).get("obligation_name") or "an estimated outflow"
    trough_date = ts.get("lowest_balance_date")
    trough_day = _date_part(trough_date)
    month_label = trough_day.strftime("%b") if trough_day else "projected"
    message = f"{driver_name} could drop your {month_label} low to ~${_money(low_estimate)}."
    return [
        {
            "id": "trough-breach",
            "type": "trough_breach_risk",
            "severity": severity,
            "message": message,
            "suggested_todoist_due": "today",
            "related_ids": [],
            "evidence": {
                "lowest_balance": ts.get("lowest_balance"),
                "lowest_balance_date": trough_date,
                "low_estimate": low_estimate,
                "high_estimate": ts.get("high_estimate"),
                "top_driver": top,
            },
        }
    ]


def _working_account_balance_stale_items(
    working_account_balance_stale: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    info = working_account_balance_stale or {}
    if not info.get("stale"):
        return []
    account_name = info.get("account_name") or info.get("working_account") or "Working account"
    age = info.get("balance_age_days")
    age_text = (
        f"{age} {'day' if age == 1 else 'days'} old"
        if age is not None
        else "stale"
    )
    balance_date = info.get("balance_date")
    date_text = f" (last balance date {balance_date})" if balance_date else ""
    return [
        {
            "id": "confirm-live-balance",
            "type": "confirm_live_balance",
            "severity": "medium",
            "message": (
                f"{account_name} balance is {age_text}{date_text}; confirm the live "
                "balance before trusting any cash-floor/drift alert."
            ),
            "suggested_todoist_due": "today",
            "related_ids": [],
            "evidence": {
                "account_name": account_name,
                "balance_age_days": age,
                "balance_date": balance_date,
            },
        }
    ]


def _guardrail_items(
    conn: sqlite3.Connection,
    as_of: date,
    *,
    suppress_balance_guardrails: bool = False,
) -> list[dict[str, Any]]:
    try:
        result = evaluate_guardrails(conn, as_of_date=as_of, persist=False)
    except sqlite3.OperationalError:
        return []
    items: list[dict[str, Any]] = []
    for f in result["findings"]:
        if f.get("advisory"):
            # Advisory findings (e.g. the static debt-avalanche reminder) are not
            # action items for the daily push.
            continue
        if suppress_balance_guardrails and f["rule_type"] in _BALANCE_DERIVED_GUARDRAILS:
            # Balance data is stale for this read, so a cash-floor / drift trip
            # built on it would be a false alarm. Drop it for this run.
            continue
        if f["rule_type"] == "drift_threshold" and _drift_warning_snoozed(conn, f, as_of):
            # Already surfaced on an earlier day and unchanged since: snoozed
            # until its underlying drift findings change (no daily re-alert).
            continue
        severity = f["severity"]
        # High/critical guardrail trips need attention today; lower ones can wait.
        due = "today" if _SEVERITY_RANK.get(severity, 0) >= 3 else "3 days"
        impact = f.get("cash_flow_impact")
        impact_txt = f" (impact ${_money(impact)})" if impact is not None else ""
        items.append(
            {
                "id": f"guardrail:{f['id']}",
                "type": "guardrail_warning",
                "severity": severity,
                "message": f"{f['message']}{impact_txt}",
                "suggested_todoist_due": due,
                "related_ids": [f["id"]],
                "evidence": {
                    "rule_type": f["rule_type"],
                    "finding_type": f.get("finding_type"),
                    "cash_flow_impact": impact,
                    "detail": f.get("evidence"),
                },
            }
        )
    return items


def _drift_warning_snoozed(conn: sqlite3.Connection, finding: dict[str, Any], as_of: date) -> bool:
    """Whether the drift-threshold warning is an unchanged repeat of a prior day.

    The same "Total drift impact $X exceeds ..." message re-alerted every day
    while the underlying findings sat unchanged. The persisted drift_findings
    ledger bumps ``updated_at`` only when a finding's content materially changes
    (see drift._persist), so: if every contributing finding is persisted, active,
    unchanged since before ``as_of``, and the persisted impacts still sum to the
    warning's total, the warning already surfaced once and is snoozed. Any new,
    changed, or not-yet-persisted contributor re-surfaces it. Read-only.
    """

    evidence = finding.get("evidence") or {}
    contributing = evidence.get("contributing") or []
    if not contributing:
        return False
    placeholders = ",".join("?" for _ in contributing)
    rows = conn.execute(
        f"SELECT id, status, updated_at, cash_flow_impact FROM drift_findings WHERE id IN ({placeholders})",
        contributing,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    for fid in contributing:
        row = by_id.get(fid)
        if row is None or row["status"] != "active":
            return False  # never persisted (never emitted) or was resolved: surface it
        changed_on = _date_part(row["updated_at"])
        if changed_on is None or changed_on >= as_of:
            return False  # new or changed today: surface it
    # Guard against a stale ledger: if the live total no longer matches the
    # persisted impacts, the content changed and must surface.
    persisted_total = round(
        sum(abs(float(by_id[fid]["cash_flow_impact"] or 0.0)) for fid in contributing), 2
    )
    return persisted_total == evidence.get("total_drift_impact")


# --- next-action contract --------------------------------------------------
# Every surfaced item closes with ONE consistent dated next-action line so the
# task tells the user exactly what to do without opening the app: WHAT (verb), HOW
# MUCH (amount), WHICH account (from/to), BY WHEN (by). The builders compose their
# own context sentence, then append render_next_action(...) and set due_date from
# the same `by`, so the action deadline and the Todoist reminder agree.

# Phrase used when no operating/working checking account name can be resolved.
_DEFAULT_OPERATING_ACCOUNT = "your operating account"

# Lead time for the onboarding-digest triage deadline: a soft "by when to triage"
# a couple of days out, so the digest item is never dateless.
_ONBOARDING_TRIAGE_LEAD_DAYS = 2


@dataclass
class NextAction:
    """Structured next-action every surface builder fills.

    ``verb`` and ``by`` are REQUIRED (a dateless action is never rendered); every
    other piece degrades gracefully. ``direction`` chooses the preposition for a
    money move (outflow -> "from", inflow -> "to") unless ``preposition`` overrides
    it (e.g. "in the charge-onboarding review"). ``amount_status == "estimated"``
    renders a trailing "(est)" so an estimate never reads as a fixed figure;
    ``modeled_min`` renders the discretionary "(modeled min ~$X)" framing instead.
    """

    verb: str
    by: str
    amount: float | None = None
    direction: str | None = None
    account: str | None = None
    preposition: str | None = None
    amount_status: str | None = None
    confidence: str | None = None
    modeled_min: bool = False


def render_next_action(action: NextAction) -> str:
    """Render one deterministic trailing action line from a ``NextAction``.

    Shape: ``Action: {verb} ${amount}{(est)} {from|to} {account} by {by}.`` Missing
    pieces drop out cleanly - no amount means no money clause (never "$-"), no
    account means no preposition clause - but verb and by-date are required.
    """

    if not action.verb or not action.verb.strip():
        raise ValueError("NextAction requires a verb")
    if not action.by or not str(action.by).strip():
        raise ValueError("NextAction requires a by-date")

    parts: list[str] = [action.verb.strip()]
    if action.amount is not None:
        if action.modeled_min:
            money = f"(modeled min ~${_money(action.amount)})"
        else:
            money = f"${_money(action.amount)}"
            if action.amount_status == "estimated":
                money += " (est)"
        parts.append(money)
    if action.account:
        prep = action.preposition
        if prep is None:
            prep = "to" if action.direction == "inflow" else "from"
        parts.append(f"{prep} {action.account}")
    return "Action: " + " ".join(parts) + f" by {action.by}."


def _operating_account_name(conn: sqlite3.Connection) -> str:
    """Resolve the operating/working checking account NAME for the action line.

    Uses the same ``WORKING_ACCOUNT_HINT`` config the cashflow projection uses to
    pick the operating account (matched against account names), falling back to the
    first checking account, then to a generic phrase. Read-only and never raises -
    a missing accounts table or no match degrades to "your operating account" so
    the action line is still meaningful.
    """

    try:
        hint = get_finance_config().get("working_account_hint")
    except Exception:  # noqa: BLE001 - config is best-effort here; degrade to default
        hint = None
    try:
        rows = conn.execute("SELECT name, kind FROM accounts").fetchall()
    except sqlite3.OperationalError:
        return _DEFAULT_OPERATING_ACCOUNT
    if hint:
        for r in rows:
            if hint in (r["name"] or ""):
                return r["name"]
    for r in rows:
        if (r["kind"] or "") == "checking" and r["name"]:
            return r["name"]
    return _DEFAULT_OPERATING_ACCOUNT


# --- helpers ---------------------------------------------------------------


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
        ).fetchone()
        is not None
    )


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _date_part(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"
