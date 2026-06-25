from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from typing import Any

from .schema import ensure_app_schema

# A goal whose deadline is within this many days is flagged ``due_soon`` so the
# user surfaces it before it lapses, regardless of progress pace.
DUE_SOON_DAYS = 14

# ~30.44 days per month (365.25 / 12); used to translate the remaining days into
# a monthly run-rate the user can compare against a paycheck cadence.
DAYS_PER_MONTH = 30.44

# A goal only counts as "behind" once a meaningful slice of its schedule has
# elapsed. Before this fraction of the created-to-deadline window has passed, a
# zero/low balance is just an unstarted plan, not a lagging one -- so a goal
# created today with a far-off deadline never nags on day one.
PACE_GRACE_FRACTION = 0.10


def set_goal(
    conn: sqlite3.Connection,
    name: str,
    target_amount: float,
    deadline: str | None = None,
    source_account: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Create or update a savings goal.

    The goal id is derived deterministically from the name and source account so
    re-running ``set_goal`` with the same name and account updates the existing
    target instead of creating a duplicate.
    """

    ensure_app_schema(conn)

    if not name or not name.strip():
        raise ValueError("Goal name must be non-empty.")
    target = float(target_amount)
    if target <= 0:
        raise ValueError("Goal target_amount must be greater than zero.")
    deadline_iso = _optional_date(deadline)

    goal_id = _goal_id(name, source_account)
    now = _now()

    existing = conn.execute(
        "SELECT created_at FROM goals WHERE id = ?",
        (goal_id,),
    ).fetchone()
    created = existing is None
    created_at = now if created else existing["created_at"]

    conn.execute(
        """
        INSERT INTO goals (
            id, name, target_amount, deadline, source_account,
            current_progress, status, note, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 0, 'active', ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            target_amount = excluded.target_amount,
            deadline = excluded.deadline,
            source_account = excluded.source_account,
            note = excluded.note,
            updated_at = excluded.updated_at
        """,
        (
            goal_id,
            name.strip(),
            round(target, 2),
            deadline_iso,
            source_account,
            note,
            created_at,
            now,
        ),
    )

    return {
        "goal_id": goal_id,
        "name": name.strip(),
        "target_amount": round(target, 2),
        "deadline": deadline_iso,
        "source_account": source_account,
        "note": note,
        "created": created,
        "updated": not created,
    }


def list_goals(
    conn: sqlite3.Connection,
    as_of_date: date | str,
) -> list[dict[str, Any]]:
    """List active goals with computed progress and an on-track assessment.

    Progress is computed on demand (not stored). Priority: a manual override if
    one is set, else the goal's source-account live balance (latest balance
    snapshot on or before ``as_of_date``), else the sum of matured inflow
    obligation instances. A goal with no source account counts all matured
    inflows.
    """

    ensure_app_schema(conn)
    as_of = _coerce_date(as_of_date)

    rows = conn.execute(
        """
        SELECT id, name, target_amount, deadline, source_account, note,
               created_at, updated_at
        FROM goals
        WHERE status = 'active'
        ORDER BY deadline IS NULL, deadline, name, id
        """
    ).fetchall()

    goals: list[dict[str, Any]] = []
    for row in rows:
        target = round(float(row["target_amount"]), 2)
        progress = _current_progress(
            conn,
            goal_id=row["id"],
            source_account=row["source_account"],
            as_of=as_of,
            created_at=row["created_at"],
        )
        trackable = _is_trackable(
            conn,
            goal_id=row["id"],
            source_account=row["source_account"],
            as_of=as_of,
        )
        assessment = _assess(
            target=target,
            progress=progress,
            deadline=row["deadline"],
            as_of=as_of,
            created_at=row["created_at"],
            trackable=trackable,
        )
        goals.append(
            {
                "goal_id": row["id"],
                "name": row["name"],
                "target_amount": target,
                "deadline": row["deadline"],
                "source_account": row["source_account"],
                "note": row["note"],
                "current_progress": progress,
                "remaining_amount": round(max(target - progress, 0.0), 2),
                "progress_pct": round(progress / target, 4) if target else None,
                "status": assessment["status"],
                "required_monthly_rate": assessment["required_monthly_rate"],
                "months_remaining": assessment["months_remaining"],
                "days_remaining": assessment["days_remaining"],
            }
        )
    return goals


def _current_progress(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    source_account: str | None,
    as_of: date,
    created_at: str,
) -> float:
    """Return current goal progress from override, live balance, or inflows.

    Priority (highest to lowest):
      1. ``goals.balance_override_amount`` if set (manual override; 0.0 is a
         valid override distinct from NULL/unset).
      2. The source account's live balance: the latest ``balance_snapshots``
         row for ``source_account`` recorded on or before ``as_of``.
      3. The sum of matured inflow obligation instances (fallback).
      4. ``0.0`` when no source account and no matured inflows exist.
    """

    override = _balance_override(conn, goal_id)
    if override is not None:
        return round(override, 2)

    if source_account is not None:
        balance = _live_balance(conn, source_account=source_account, as_of=as_of)
        if balance is not None:
            return round(balance, 2)

    where = [
        "oi.direction = 'inflow'",
        "oi.status != 'deleted'",
        "oi.due_date <= ?",
    ]
    params: list[Any] = [as_of.isoformat()]

    if source_account is not None:
        # Inflows linked to a goal account are matched via the obligation's
        # source field, which encodes the routing account for income-style
        # obligations (e.g. 'account:savings').
        where.append("o.source = ?")
        params.append(source_account)

    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(oi.amount), 0) AS total
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE {" AND ".join(where)}
        """,
        params,
    ).fetchone()
    total = float(row["total"]) if row and row["total"] is not None else 0.0
    return round(total, 2)


def _is_trackable(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    source_account: str | None,
    as_of: date,
) -> bool:
    """Return whether the goal has a real funding signal to measure pace against.

    A goal is trackable when at least one progress source actually exists:
      - an explicit ``balance_override_amount`` (even 0.0 -- the user has
        asserted a real current balance), or
      - a source account with a live balance snapshot on or before ``as_of``, or
      - at least one matured inflow obligation instance routed to it.

    A goal with no source account, no override, and no matured inflows has no way
    to know where it stands yet (e.g. a shared buffer that has not been opened).
    It is "pending" / not-yet-trackable and must never be flagged ``behind``.
    """

    if _balance_override(conn, goal_id) is not None:
        return True

    if source_account is not None and _has_table(conn, "balance_snapshots"):
        balance = _live_balance(conn, source_account=source_account, as_of=as_of)
        if balance is not None:
            return True

    where = [
        "oi.direction = 'inflow'",
        "oi.status != 'deleted'",
        "oi.due_date <= ?",
    ]
    params: list[Any] = [as_of.isoformat()]
    if source_account is not None:
        where.append("o.source = ?")
        params.append(source_account)

    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM obligation_instances oi
        JOIN obligations o ON o.id = oi.obligation_id
        WHERE {" AND ".join(where)}
        """,
        params,
    ).fetchone()
    return bool(row and row["n"])


def _balance_override(conn: sqlite3.Connection, goal_id: str) -> float | None:
    """Return a goal's manual progress override, or None when unset.

    NULL in the column means "use the live balance"; an explicit 0.0 is a real
    override and is returned as 0.0 (not treated as unset).
    """

    if not _has_column(conn, "goals", "balance_override_amount"):
        return None
    row = conn.execute(
        "SELECT balance_override_amount FROM goals WHERE id = ?",
        (goal_id,),
    ).fetchone()
    if row is None or row["balance_override_amount"] is None:
        return None
    return float(row["balance_override_amount"])


def _live_balance(
    conn: sqlite3.Connection,
    *,
    source_account: str,
    as_of: date,
) -> float | None:
    """Return the latest balance for an account on or before ``as_of``.

    Reads the most recent ``balance_snapshots`` row (by ``recorded_at``) for the
    account, ignoring snapshots recorded after ``as_of``. Returns None when the
    table is absent or no snapshot qualifies.
    """

    if not _has_table(conn, "balance_snapshots"):
        return None
    row = conn.execute(
        """
        SELECT balance
        FROM balance_snapshots
        WHERE account_id = ?
          AND recorded_at <= ?
        ORDER BY recorded_at DESC, id DESC
        LIMIT 1
        """,
        (source_account, _as_of_bound(as_of)),
    ).fetchone()
    if row is None or row["balance"] is None:
        return None
    return float(row["balance"])


def set_goal_override(
    conn: sqlite3.Connection,
    goal_id: str,
    override_amount: float | None = None,
) -> dict[str, Any]:
    """Set or clear a manual progress override for a goal.

    When ``override_amount`` is None the override is cleared and the goal reverts
    to its live source-account balance (or matured inflows). A non-None amount
    forces the goal's progress; it must be >= 0. Returns the updated goal dict.
    """

    ensure_app_schema(conn)

    row = conn.execute("SELECT id FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if row is None:
        raise ValueError(f"No goal found with id {goal_id!r}.")

    now = _now()
    if override_amount is None:
        conn.execute(
            """
            UPDATE goals
            SET balance_override_amount = NULL,
                balance_override_set_at = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now, goal_id),
        )
    else:
        amount = float(override_amount)
        if amount < 0:
            raise ValueError("override_amount must be greater than or equal to zero.")
        conn.execute(
            """
            UPDATE goals
            SET balance_override_amount = ?,
                balance_override_set_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (round(amount, 2), now, now, goal_id),
        )

    today = date.today()
    for goal in list_goals(conn, today):
        if goal["goal_id"] == goal_id:
            return goal
    # Goal is inactive (not returned by list_goals); fall back to its row.
    return _goal_summary(conn, goal_id, as_of=today)


def _goal_summary(
    conn: sqlite3.Connection,
    goal_id: str,
    *,
    as_of: date,
) -> dict[str, Any]:
    """Return a goal's current state even when it is not active.

    Mirrors the shape of a ``list_goals`` entry for callers that need to read
    back a goal that ``list_goals`` filters out (status != 'active').
    """

    row = conn.execute(
        """
        SELECT id, name, target_amount, deadline, source_account, note, created_at
        FROM goals
        WHERE id = ?
        """,
        (goal_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No goal found with id {goal_id!r}.")
    target = round(float(row["target_amount"]), 2)
    progress = _current_progress(
        conn,
        goal_id=row["id"],
        source_account=row["source_account"],
        as_of=as_of,
        created_at=row["created_at"],
    )
    return {
        "goal_id": row["id"],
        "name": row["name"],
        "target_amount": target,
        "deadline": row["deadline"],
        "source_account": row["source_account"],
        "note": row["note"],
        "current_progress": progress,
        "remaining_amount": round(max(target - progress, 0.0), 2),
        "progress_pct": round(progress / target, 4) if target else None,
    }


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _as_of_bound(as_of: date) -> str:
    """Inclusive upper bound for a date compared against ``recorded_at``.

    ``recorded_at`` may carry a time component, so an ISO date string of
    ``as_of`` would sort before same-day timestamps. Bounding at the end of the
    day keeps snapshots recorded on ``as_of`` itself in range.
    """

    return f"{as_of.isoformat()}T23:59:59.999999"


def _assess(
    *,
    target: float,
    progress: float,
    deadline: str | None,
    as_of: date,
    created_at: str,
    trackable: bool = True,
) -> dict[str, Any]:
    """Classify a goal and compute the monthly run-rate required to hit target.

    Status precedence:
      - ``completed``: progress has reached or passed target.
      - ``due_soon``: a deadline lands within DUE_SOON_DAYS (deadline-driven, so
        it fires even before there is a funding signal).
      - ``no_deadline``: open-ended target, not yet completed.
      - ``pending``: there is a future deadline but no funding signal yet (no
        source account, no override, no matured inflows). Not yet trackable;
        treated as on track and never surfaced as a nag.
      - ``behind``: real progress data exists and elapsed pace genuinely lags it.
      - ``on_track``: progress keeps pace with elapsed time (or the goal is too
        young in its schedule to be judged behind yet).

    ``trackable`` is False when the goal has no way to know its current balance
    (e.g. a shared buffer that has not been opened yet). Such a goal is reported
    ``pending`` rather than ``behind``: only a goal with real progress data can
    fall behind. The pace judgment (``behind``/``on_track``) is the ONLY branch
    gated by trackability; deadline-driven statuses are unaffected so a genuinely
    near-due unfunded goal still surfaces as ``due_soon``.
    """

    remaining = max(target - progress, 0.0)

    if progress >= target:
        return {
            "status": "completed",
            "required_monthly_rate": 0.0,
            "months_remaining": None,
            "days_remaining": None,
        }

    if deadline is None:
        return {
            "status": "no_deadline",
            "required_monthly_rate": None,
            "months_remaining": None,
            "days_remaining": None,
        }

    deadline_date = _coerce_date(deadline)
    days_remaining = (deadline_date - as_of).days
    months_remaining = round(max(days_remaining, 0) / DAYS_PER_MONTH, 2)

    if days_remaining > 0:
        required_monthly_rate = round(remaining / (days_remaining / DAYS_PER_MONTH), 2)
    else:
        # Deadline today or past with target unmet: the full remainder is owed now.
        required_monthly_rate = round(remaining, 2)

    if 0 <= days_remaining <= DUE_SOON_DAYS:
        status = "due_soon"
    elif not trackable:
        # Future deadline, no funding signal yet: this is an unstarted plan, not
        # a lagging balance. Report ``pending`` (on-track family) so it is never
        # mistaken for ``behind`` and never nags on the day it was created.
        status = "pending"
    else:
        status = _pace_status(
            target=target,
            progress=progress,
            as_of=as_of,
            created_at=created_at,
            deadline_date=deadline_date,
        )

    return {
        "status": status,
        "required_monthly_rate": required_monthly_rate,
        "months_remaining": months_remaining,
        "days_remaining": days_remaining,
    }


def _pace_status(
    *,
    target: float,
    progress: float,
    as_of: date,
    created_at: str,
    deadline_date: date,
) -> str:
    """Compare elapsed-time fraction to progress fraction.

    The schedule runs from when the goal was created to its deadline. If the
    progress fraction keeps up with the elapsed-time fraction the goal is
    ``on_track``; otherwise ``behind``.

    A grace window protects newly-created goals: until ``PACE_GRACE_FRACTION`` of
    the created-to-deadline schedule has elapsed, the goal is too young to be
    judged ``behind`` -- a goal created today with a far-off deadline reads
    ``on_track`` even at 0 progress. Once enough of the schedule has passed, a
    genuine pace shortfall reads ``behind``.
    """

    start = _created_date(created_at)
    if start > deadline_date:
        start = deadline_date

    days_total = (deadline_date - start).days
    if days_total <= 0:
        # Same-day created-to-deadline window with target unmet: there is no
        # runway to fall behind over, so treat it as on track rather than nag.
        return "on_track"

    days_elapsed = (as_of - start).days
    days_elapsed = min(max(days_elapsed, 0), days_total)

    elapsed_fraction = days_elapsed / days_total
    progress_fraction = progress / target if target else 0.0

    # Too early in the schedule to judge: an unstarted plan is not a lagging one.
    if elapsed_fraction < PACE_GRACE_FRACTION:
        return "on_track"

    return "on_track" if progress_fraction >= elapsed_fraction else "behind"


def _goal_id(name: str, source_account: str | None) -> str:
    slug = _slugify(name)
    account_part = _slugify(source_account) if source_account else "general"
    return f"goal_{slug}_{account_part}"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "untitled"


def _created_date(created_at: str) -> date:
    try:
        return datetime.fromisoformat(created_at).date()
    except ValueError:
        return date.fromisoformat(created_at[:10])


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _optional_date(value: date | str | None) -> str | None:
    if value is None:
        return None
    return _coerce_date(value).isoformat()


def _now() -> str:
    return datetime.now().astimezone().isoformat()
