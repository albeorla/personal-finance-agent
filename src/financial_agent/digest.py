"""Daily digest: the human-readable morning summary (cutover slice P).

This is the user-facing replacement for the legacy `just daily` / `cash-flow.md`
ritual. It composes the already-grounded tools (balances, cash-flow projection,
upcoming obligations, drift/review items, recurring candidates, guardrails) into
one summary with provenance, and renders a `cash-flow.md`-style markdown so the
output can be diffed against the legacy file during parallel-run.

Pure composition: no new external calls and no writes. Every number traces to
`get_finance_status` (balances + projection from obligation instances, drift,
guardrails) - see the ``provenance`` block.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from .backfill import list_recently_cleared
from .config import get_finance_config
from .guardrails import CASH_FLOOR
from .obligations import list_obligations
from .onboarding import ACTIVE_STATUSES
from .reconciliation import list_reconciliation_review_items
from .status import default_db_path, get_finance_status
from .surface_queue import MANUAL_DUE_LEAD_DAYS, _manual_obligation_due_rows

# #7 sensitivity: per-confidence-tier downside fraction used when a per-instance
# coefficient-of-variation (cv) is not available on the estimate. very_low/null
# are the softest guesses (widest band); medium barely moves. Mirrors the tiers
# the projection already carries on each event (cashflow.py:127).
SENSITIVITY_PCT: dict[str | None, float] = {
    "very_low": 0.35,
    None: 0.35,
    "low": 0.20,
    "medium": 0.10,
}
# Cap a per-instance cv so one wild historical month cannot blow the band open.
_CV_CAP = 0.75


def build_daily_digest(
    db_path: str | None = None,
    *,
    as_of_date: str | None = None,
    windows: tuple[int, ...] = (7, 14, 30, 60),
    max_recurring: int = 15,
) -> dict[str, Any]:
    """Assemble the daily digest from a single grounded finance-status call."""

    resolved_db_path = db_path or str(default_db_path())
    status = get_finance_status(
        db_path=resolved_db_path,
        windows=list(windows),
        start_date=as_of_date,
    )
    projections = status["cash_flow_projections"]
    as_of = projections[0]["start_date"] if projections else (as_of_date or status["observed_at"][:10])

    # The longest window's events are the upcoming obligations, already projected
    # with running balances.
    longest = projections[-1] if projections else None
    upcoming = [
        {
            "due_date": e["due_date"],
            "obligation_name": e["obligation_name"],
            "amount": e["amount"],
            "signed_amount": e["signed_amount"],
            "direction": e["direction"],
            "status": e["status"],
            "confidence": e["confidence"],
            "amount_status": e.get("amount_status"),
            "running_balance": e["running_balance"],
        }
        for e in (longest["events"] if longest else [])
    ]

    # Working cash (the operating checking account) is the number that matters
    # day to day; total_available sums every account, including card debt.
    working_account = projections[0].get("working_account") if projections else None
    # Liquid cash = available across DEPOSIT accounts only. account.kind is empty
    # in the source, so deposit is inferred by a non-negative balance (cards/loans
    # carry a negative balance). Summing raw total_available would fold a card's
    # negative `available` into a line labeled "deposit accounts".
    acct_list = status["balances"]["accounts"]
    deposit_liquid = round(sum(a["available"] for a in acct_list if a["balance"] >= 0), 2)
    # #7: per-window trough sensitivity bands, computed from the estimated,
    # low-confidence outflows landing on or before each window's low point (only
    # those can move it). The full event dicts (with confidence / amount_status /
    # estimation_inputs) live on each projection, so this stays pure composition.
    trough_bands = [_trough_band(p) for p in projections]
    digest = {
        "as_of_date": as_of,
        "trace_id": status["trace_id"],
        "balances": {
            "working_cash": working_account["available"] if working_account else None,
            "working_account": working_account["account_name"] if working_account else None,
            # True net worth across all accounts (sum of balances, so card/loan debt
            # is included as negative). total_available sums only `available`, which
            # is 0 for cards/loans - it is deposit liquidity, NOT net.
            "net_across_accounts": status["balances"]["total_balance"],
            "liquid_available": deposit_liquid,
            "accounts": [
                {"name": a["account_name"], "org": a.get("org"), "balance": a["balance"], "available": a["available"]}
                for a in status["balances"]["accounts"]
            ],
        },
        "source_freshness": {k: v.get("status") for k, v in status["source_freshness"].items()},
        "cash_flow": [
            {"window_days": p["window_days"], "ending_balance": p["ending_balance"],
             "lowest_balance": p["lowest_balance"], "lowest_balance_date": p["lowest_balance_date"],
             "trough_low_estimate": b["trough_low_estimate"],
             "trough_high_estimate": b["trough_high_estimate"],
             "trough_band_drivers": b["trough_band_drivers"],
             "trough_breach_risk": b["trough_breach_risk"]}
            for p, b in zip(projections, trough_bands)
        ],
        "upcoming_obligations": upcoming,
        "estimated_material": [
            {"obligation_name": o["obligation_name"], "amount": o["amount"], "due_date": o["due_date"]}
            for o in upcoming
            if o.get("amount_status") == "estimated" and abs(o.get("amount") or 0) >= 1000
        ],
        "drift": status["drift_warnings"],
        "matches_to_confirm": _matches_to_confirm(resolved_db_path, as_of),
        "recently_cleared": _recently_cleared(resolved_db_path, as_of),
        **_recurring_summary(status["recurring_candidates"], max_recurring),
        "guardrails": status["guardrail_findings"],
        "warnings": status["warnings"],
        "provenance": {
            "balances": "accounts + balance_snapshots (SimpleFIN sync)",
            "cash_flow": "deterministic projection over obligation_instances",
            "upcoming_obligations": "obligation_instances in the projection window",
            "drift": "detect_drift (missing/stale/amount-changed)",
            "matches_to_confirm": "transaction_obligation_matches awaiting confirmation",
            "recently_cleared": "backfilled past instances matched to posted transactions",
            "recurring_candidates": "charge_onboarding_candidates not yet applied",
            "guardrails": "evaluate_guardrails (cash floor / drift / window-age / avalanche)",
            "coverage": "obligation roster + projection events + manual-due surface rows + onboarding queue + board freshness",
            "trough_sensitivity": "estimated low-confidence outflows before the projected low point",
        },
    }
    # #7: headline band = the longest window (matches the lowest_balance headline).
    digest["trough_sensitivity"] = _trough_sensitivity(longest, trough_bands[-1] if trough_bands else None)
    # #3: honest coverage census (modeled / autopay-silent / manual-surfaced /
    # not-yet-modeled) plus surfacing-board freshness. One read-only connection,
    # reusing the already-grounded freshness, accounts, and drift from `status`.
    digest["coverage"] = _coverage(
        resolved_db_path,
        as_of,
        source_freshness=status["source_freshness"],
        accounts=status["balances"]["accounts"],
        drift_warnings=status["drift_warnings"],
        longest_projection=longest,
    )
    digest["status_color"] = _status_color(digest)
    return digest


def render_digest_markdown(digest: dict[str, Any]) -> str:
    bal = digest["balances"]
    lines: list[str] = []
    lines.append(f"# Finance Daily Digest - {digest['as_of_date']}")
    lines.append("")
    lines.append(f"Cash runway (modeled bills only): {digest['status_color']}")
    lines.append("")
    _render_coverage(digest.get("coverage"), lines)
    if digest.get("recurring_checking_count"):
        # These genuinely lower the runway and are NOT in the projection.
        eg = digest.get("recurring_checking_top")
        eg_txt = f", e.g. {eg}" if eg else ""
        lines.append(f"_WARNING: {digest['recurring_checking_count']} unmodeled recurring CHECKING charges (~${_money(digest.get('recurring_checking_monthly'))}/mo{eg_txt}) are NOT in the projection and will lower the runway - review and apply them below. (Of {digest['recurring_total']} recurring discovered; most are card spend - only partly captured by the modeled statement payments.)_")
    elif digest.get("recurring_total"):
        lines.append(f"_Note: {digest['recurring_total']} recurring charges are discovered but not yet modeled (mostly card spend; only the modeled card statement payments are in the projection). See below._")
    est = digest.get("estimated_material", [])
    if est:
        names = ", ".join(f"{e['obligation_name']} ~${_money(e['amount'])}" for e in est[:3])
        lines.append(f"_CAUTION: the runway leans on ESTIMATED bills whose real amount varies month to month ({names}). Actual may be higher - confirm before trusting the headroom._")
    lines.append("")

    lines.append("## Balances")
    lines.append(f"Working cash ({bal.get('working_account') or 'operating account'}, available): ${_money(bal.get('working_cash'))}")
    lines.append(f"Net across all accounts (incl. card debt): ${_money(bal['net_across_accounts'])}")
    lines.append(f"Liquid available (deposit accounts): ${_money(bal['liquid_available'])}")
    for a in bal["accounts"]:
        # Show posted balance; for deposit accounts with pending activity, also
        # show available so it ties to the headline working-cash (available) figure.
        note = ""
        bal_v, avail_v = a.get("balance"), a.get("available")
        if bal_v is not None and avail_v is not None and bal_v >= 0 and abs(avail_v - bal_v) > 0.01:
            note = f" (avail ${_money(avail_v)})"
        lines.append(f"- {_account_label(a)}: ${_money(bal_v)}{note}")
    lines.append("")

    lines.append("## Cash-Flow Projection")
    lines.append("| Window | Ending balance | Lowest | Lowest date |")
    lines.append("|--------|----------------|--------|-------------|")
    for c in digest["cash_flow"]:
        lines.append(f"| {c['window_days']}d | ${_money(c['ending_balance'])} | ${_money(c['lowest_balance'])} | {c['lowest_balance_date']} |")
    if not digest["cash_flow"]:
        lines.append("| - | (no projection) | - | - |")
    _render_trough_sensitivity(digest, lines)
    lines.append("")

    lines.append(f"## Upcoming Obligations ({len(digest['upcoming_obligations'])})")
    for o in digest["upcoming_obligations"]:
        sign = "-" if o["direction"] == "outflow" else "+"
        est = " (est)" if o.get("amount_status") == "estimated" else ""
        lines.append(f"- {o['due_date']}  {sign}${_money(o['amount'])}{est}  {o['obligation_name']} ({o['status']}, {o['confidence'] or 'n/a'}) -> ${_money(o['running_balance'])}")
    if not digest["upcoming_obligations"]:
        lines.append("- none in window")
    lines.append("")

    lines.append(f"## Drift & Review ({len(digest['drift'])}) - confirm whether these cleared")
    for d in digest["drift"]:
        ev = d.get("evidence") or {}
        name = ev.get("obligation_name") or d.get("obligation_id") or d.get("finding_type")
        iid = d.get("obligation_instance_id") or ""
        due = ev.get("due_date") or (iid.split(":")[-1] if ":" in iid else "")
        lines.append(f"- [{d['severity']}] {d['finding_type']}: {name} {due} ${_money(d.get('cash_flow_impact'))} - {(d.get('recommended_action') or '')[:60]}")
    if not digest["drift"]:
        lines.append("- no active drift")
    lines.append("")

    confirm = digest.get("matches_to_confirm", [])
    lines.append(f"## Matches to Confirm ({len(confirm)})")
    for m in confirm:
        lines.append(f"- {m['due_date']}  {m['obligation_name']} ${_money(m['amount'])} <- txn {m['transaction_id']} (score {m['match_score']}, {m['match_type']})")
    if not confirm:
        # "0" here means nothing is queued for confirmation, NOT that every bill
        # cleared - reconciliation only covers modeled obligations with a match.
        lines.append("- none queued (covers modeled obligations only; this is NOT a confirmation that rent/cards/etc. cleared)")
    lines.append("")

    cleared = digest.get("recently_cleared", [])
    lines.append(f"## Recently Cleared (last 30d) ({len(cleared)})")
    for c in cleared:
        tag = "cleared" if c["cleared"] else "likely - confirm"
        lines.append(f"- {c['due_date']}  {c['obligation_name']} ${_money(c['amount'])} <- txn {c['transaction_id']} ({tag})")
    if not cleared:
        lines.append("- no matched payments in the last 30d (run backfill to populate)")
    lines.append("")

    lines.append(f"## Guardrails ({len(digest['guardrails'])})")
    for g in digest["guardrails"]:
        lines.append(f"- [{g['severity']}] {g['message']}")
    if not digest["guardrails"]:
        lines.append("- all guardrails pass")
    lines.append("")

    rc = digest["recurring_candidates"]
    lines.append(f"## Recurring Charges Not Yet Modeled ({digest['recurring_total']}, showing top {len(rc)})")
    for r in rc:
        ev = r.get("evidence") or {}
        lines.append(f"- {ev.get('merchant', '?')} ~${_money(ev.get('estimated_monthly_impact'))}/mo")
    if not rc:
        lines.append("- none")
    elif digest.get("recurring_more_count", 0) > 0:
        lines.append(f"- ...and {digest['recurring_more_count']} more (~${_money(digest['recurring_more_monthly'])}/mo)")
    lines.append("")

    lines.append("---")
    lines.append("Provenance: " + "; ".join(f"{k} <- {v}" for k, v in digest["provenance"].items()))
    return "\n".join(lines)


def _render_coverage(cov: dict[str, Any] | None, lines: list[str]) -> None:
    """The ## Coverage block: leads with the human consequence (autopay = "no
    action", surfaced = "will appear in Todoist", board = clean vs needs you)."""

    if not cov:
        return
    lines.append("## Coverage")
    lines.append(
        f"Modeled: {cov['modeled_obligations']} obligations "
        f"({cov['autopay_silent']} autopay/silent, {cov['manual_attention']} need you)."
    )
    lines.append(
        f"This window: {cov['in_window_obligations']} hit checking; "
        f"{cov['in_window_autopay']} autopay (no action), {cov['in_window_manual']} manual."
    )
    sc, im, lead = cov["surfaced_count"], cov["in_window_manual"], cov["surfaced_lead_days"]
    if sc:
        tail = (
            "will appear in Todoist"
            if cov.get("surfaced_writes_enabled", True)
            else "would appear in Todoist (writes are off)"
        )
        bills = "bill" if sc == 1 else "bills"
        lines.append(
            f"Surfacing to you: {sc} of those {im} manual {bills} within {lead} days and {tail}."
        )
    else:
        lines.append(f"Surfacing to you: nothing within {lead} days needs you.")
    if cov["onboarding_active"]:
        parts = ", ".join(
            f"{n} {status.replace('_', ' ')}"
            for status, n in cov["onboarding_by_status"].items()
            if n
        )
        lines.append(
            f"Not yet modeled: {cov['onboarding_active']} charges awaiting review "
            f"({parts}) - these are NOT in the runway."
        )
    else:
        lines.append("Not yet modeled: 0 charges awaiting review.")
    # Apple Card spend is invisible without a pasted statement - footnote, not a
    # count, so card activity is never implied covered (ties to #4).
    if (cov.get("board_health") or {}).get("apple_card_stale"):
        lines.append(
            "_Note: in-window counts reflect modeled statement payments only; Apple Card "
            "spend is not visible until a statement is pasted._"
        )
    lines.append(_board_line(cov.get("board_health")))
    lines.append("")


def _board_line(bh: dict[str, Any] | None) -> str:
    """Deterministic board-freshness line: always three facts, never drop one."""

    if not bh:
        return "Board: status unknown."
    if bh.get("managed_clean"):
        lead = "clean and current"
    elif not bh.get("last_surfaced_at"):
        lead = "NEEDS ATTENTION - not yet surfaced today"
    else:
        lead = "NEEDS ATTENTION - drift over $200 is open"
    sync_note = " and Apple Card spend has not been pasted this cycle" if bh.get("apple_card_stale") else ""
    return (
        f"Board: {lead}. Last surfaced {_relative_time(bh.get('last_surfaced_at'))}; "
        f"bank data synced {_relative_time(bh.get('last_sync_at'))}{sync_note}."
    )


def _render_trough_sensitivity(digest: dict[str, Any], lines: list[str]) -> None:
    """Band line + single-largest-driver counterfactual under the Cash-Flow table.

    Rendered only when there are estimated drivers before the trough; otherwise
    the band collapses to the point estimate and the line is omitted (no
    zero-width band)."""

    ts = digest.get("trough_sensitivity")
    if not ts or not ts.get("drivers"):
        return
    window_days = digest["cash_flow"][-1]["window_days"] if digest["cash_flow"] else None
    low = ts["lowest_balance"]
    lo, hi = ts["low_estimate"], ts["high_estimate"]
    breach = ts.get("breach_risk")
    prefix = "CAUTION: " if breach else ""
    lines.append(
        f"{prefix}Trough sensitivity ({window_days}d): low point ${_money(low)} could land "
        f"between ~${_money(lo)} and ~${_money(hi)}."
    )
    drivers = ts["drivers"]
    names = ", ".join(f"{d['obligation_name']} ~${_money(d['amount'])} est" for d in drivers)
    noun = "bill" if len(drivers) == 1 else "bills"
    lines.append(f"The soft part is {len(drivers)} estimated {noun} before that date ({names}).")
    lines.append(f"If those run high, the low point drops to ~${_money(lo)}.")

    # Per-driver counterfactual: the single largest driver running hot, holding
    # the rest at their modeled value. Only this driver moves the trough depth,
    # not its date (same event set).
    d0 = drivers[0]
    recomputed_low = round(low - d0["downside"], 2)
    one_breach = recomputed_low < CASH_FLOOR or recomputed_low < 0
    cprefix = "CAUTION: " if one_breach else ""
    lines.append(
        f"{cprefix}Biggest single swing: if {d0['obligation_name']} comes in ~${_money(d0['downside'])} "
        f"higher than estimated, the low point alone drops to ~${_money(recomputed_low)} "
        f"on {ts['lowest_balance_date']}."
    )


def _impact(candidate: dict[str, Any]) -> float:
    return abs(float((candidate.get("evidence") or {}).get("estimated_monthly_impact") or 0.0))


def _recurring_summary(candidates: list[dict[str, Any]], max_recurring: int) -> dict[str, Any]:
    """Top recurring candidates by monthly impact, plus the hidden remainder.

    Sorting by impact (not the drift default order) ensures the digest never
    truncates away the biggest unmodeled charges, and the header/remainder make
    the full count honest instead of implying there are only ``max_recurring``.
    """

    ranked = sorted(candidates, key=_impact, reverse=True)
    top = ranked[:max_recurring]
    rest = ranked[max_recurring:]
    # The runway warning counts only CONFIDENT (medium/high) candidates, so
    # low-confidence variable spend (gas, dining) does not inflate the headline.
    confident = [c for c in ranked if c.get("confidence") in ("medium", "high")]
    # Direct-checking unmodeled recurring genuinely lowers the runway (unlike card
    # spend, which the statement payment already captures).
    checking = [c for c in ranked if (c.get("evidence") or {}).get("cash_flow_treatment") == "direct_checking"]
    checking_top = (checking[0].get("evidence") or {}).get("merchant") if checking else None
    return {
        "recurring_candidates": top,
        "recurring_total": len(candidates),
        "recurring_more_count": len(rest),
        "recurring_more_monthly": round(sum(_impact(c) for c in rest), 2),
        "recurring_all_monthly": round(sum(_impact(c) for c in ranked), 2),
        "recurring_confident_count": len(confident),
        "recurring_confident_monthly": round(sum(_impact(c) for c in confident), 2),
        "recurring_checking_count": len(checking),
        "recurring_checking_monthly": round(sum(_impact(c) for c in checking), 2),
        "recurring_checking_top": checking_top,
    }


def _matches_to_confirm(db_path: str, as_of: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list_reconciliation_review_items(conn, as_of_date=as_of)
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _recently_cleared(db_path: str, as_of: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list_recently_cleared(conn, as_of_date=as_of)
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# --- #7 trough sensitivity ---------------------------------------------------


def _driver_downside(event: dict[str, Any]) -> float:
    """How far this one estimated outflow could run hot.

    Prefer a per-instance coefficient of variation carried on the estimate
    (``estimation_inputs.cv``, derived at onboarding.py:363/477), capped so one
    wild month cannot blow the band open. Fall back to the per-confidence-tier
    fraction when no usable cv is present. Never throws on a missing/odd cv.
    """

    amount = abs(float(event.get("amount") or 0.0))
    inputs = event.get("estimation_inputs")
    if isinstance(inputs, dict) and inputs.get("cv") is not None:
        try:
            cv = float(inputs["cv"])
        except (TypeError, ValueError):
            cv = None
        if cv is not None and cv > 0:
            return amount * min(cv, _CV_CAP)
    pct = SENSITIVITY_PCT.get(event.get("confidence"), SENSITIVITY_PCT[None])
    return amount * pct


def _trough_band(projection: dict[str, Any] | None) -> dict[str, Any]:
    """Downside/upside band around one window's low point.

    Only estimated, low-confidence outflows due on or before the trough date can
    move it, so those are the drivers. ``downside`` is the magnitude that matters;
    the band is symmetric. ``breach_risk`` flags a low estimate that crosses the
    $2,500 cash floor (or zero) even when the point estimate clears.
    """

    lowest = (projection or {}).get("lowest_balance")
    trough_date = (projection or {}).get("lowest_balance_date")
    events = (projection or {}).get("events") or []
    drivers: list[dict[str, Any]] = []
    if lowest is not None and trough_date is not None:
        for e in events:
            if (
                e.get("due_date")
                and e["due_date"] <= trough_date
                and e.get("direction") == "outflow"
                and e.get("amount_status") == "estimated"
                and e.get("confidence") in (None, "low", "very_low")
            ):
                downside = _driver_downside(e)
                if downside <= 0:
                    continue
                name = e.get("obligation_name") or ""
                drivers.append(
                    {
                        "obligation_name": name,
                        "amount": e.get("amount"),
                        "confidence": e.get("confidence"),
                        "downside": round(downside, 2),
                        # Apple Card has no live transaction feed, so its estimated
                        # statement payment is doubly soft (ties to #4). Name-based
                        # until the card-spend paste-import lands.
                        "balance_only": "apple" in name.lower(),
                    }
                )
    drivers.sort(key=lambda d: d["downside"], reverse=True)
    total_downside = round(sum(d["downside"] for d in drivers), 2)
    low_estimate = round(lowest - total_downside, 2) if lowest is not None else None
    high_estimate = round(lowest + total_downside, 2) if lowest is not None else None
    breach_risk = bool(
        low_estimate is not None and total_downside > 0 and (low_estimate < CASH_FLOOR or low_estimate < 0)
    )
    return {
        "trough_low_estimate": low_estimate,
        "trough_high_estimate": high_estimate,
        "trough_band_drivers": len(drivers),
        "trough_breach_risk": breach_risk,
        # Internal: the ranked drivers feed the top-level headline (top 3).
        "_drivers": drivers,
    }


def _trough_sensitivity(
    projection: dict[str, Any] | None, band: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Headline trough band for the longest window, with the top-3 drivers."""

    if projection is None or band is None:
        return None
    return {
        "lowest_balance": projection.get("lowest_balance"),
        "lowest_balance_date": projection.get("lowest_balance_date"),
        "low_estimate": band["trough_low_estimate"],
        "high_estimate": band["trough_high_estimate"],
        "drivers": [
            {
                "obligation_name": d["obligation_name"],
                "amount": d["amount"],
                "confidence": d["confidence"],
                "downside": d["downside"],
                "balance_only": d.get("balance_only", False),
            }
            for d in band["_drivers"][:3]
        ],
        "breach_risk": band["trough_breach_risk"],
    }


# --- #3 coverage census ------------------------------------------------------


def _coverage(
    db_path: str,
    as_of: str,
    *,
    source_freshness: dict[str, Any],
    accounts: list[dict[str, Any]],
    drift_warnings: list[dict[str, Any]],
    longest_projection: dict[str, Any] | None,
) -> dict[str, Any]:
    """Honest coverage census for the 8:10am meta-question.

    Of everything modeled: how much is autopay (intentionally silent), how much
    needs the user, how much is surfacing to Todoist within the lead window, and
    how much is not modeled at all. Pure read-only composition over the obligation
    roster, the longest projection's already-materialized events, the manual-due
    surface rows, the onboarding queue, and the surfacing board's freshness.
    """

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        roster = list_obligations(conn, status="active", include_instances=False, compact=True)
        roster_by_id = {o["id"]: o for o in roster}
        modeled = len(roster)
        autopay_silent = sum(1 for o in roster if o.get("autopay"))
        manual_attention = modeled - autopay_silent

        # In-window = distinct active obligations with an instance in the longest
        # window. Autopay split joins the roster; obligations not in the roster
        # (status flipped between queries) stay out of the split (edge case).
        events = (longest_projection or {}).get("events") or []
        in_window_ids = {e["obligation_id"] for e in events if e.get("obligation_id")}
        in_window_autopay = sum(
            1 for oid in in_window_ids if roster_by_id.get(oid) and roster_by_id[oid].get("autopay")
        )
        in_window_manual = sum(
            1 for oid in in_window_ids if roster_by_id.get(oid) and not roster_by_id[oid].get("autopay")
        )

        # Surfaced = the manual, unpaid, due-within-lead-days rows that actually
        # reach Todoist (autopay is intentionally excluded - that is the point).
        try:
            surfaced_rows = _manual_obligation_due_rows(conn, dt.date.fromisoformat(as_of))
        except (sqlite3.OperationalError, ValueError):
            surfaced_rows = []
        surfaced_count = len(surfaced_rows)

        # Not-yet-modeled = onboarding candidates still awaiting a human decision.
        onboarding_counts: dict[str, int] = {}
        try:
            placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
            for row in conn.execute(
                f"SELECT status, COUNT(*) AS n FROM charge_onboarding_candidates "
                f"WHERE status IN ({placeholders}) GROUP BY status",
                tuple(ACTIVE_STATUSES),
            ).fetchall():
                onboarding_counts[row["status"]] = row["n"]
        except sqlite3.OperationalError:
            onboarding_counts = {}

        board_health = _board_health(
            conn,
            source_freshness=source_freshness,
            drift_warnings=drift_warnings,
        )
    finally:
        conn.close()

    try:
        writes_enabled = bool(get_finance_config().get("todoist_write_enabled"))
    except Exception:
        writes_enabled = True

    return {
        "modeled_obligations": modeled,
        "autopay_silent": autopay_silent,
        "manual_attention": manual_attention,
        "in_window_obligations": len(in_window_ids),
        "in_window_autopay": in_window_autopay,
        "in_window_manual": in_window_manual,
        "surfaced_count": surfaced_count,
        "surfaced_lead_days": MANUAL_DUE_LEAD_DAYS,
        "surfaced_writes_enabled": writes_enabled,
        "onboarding_active": sum(onboarding_counts.values()),
        "onboarding_by_status": {s: onboarding_counts.get(s, 0) for s in ACTIVE_STATUSES},
        "board_health": board_health,
    }


def _board_health(
    conn: sqlite3.Connection,
    *,
    source_freshness: dict[str, Any],
    drift_warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Freshness of the surfacing board, so the counts are paired with "is the
    board even current?". All read-only, reusing the grounded freshness/drift."""

    last_surfaced_at = None
    try:
        row = conn.execute("SELECT MAX(last_seen) AS m FROM todoist_emissions").fetchone()
        last_surfaced_at = row["m"] if row else None
    except sqlite3.OperationalError:
        last_surfaced_at = None

    # Open drift over the $200 invariant blocks a clean board (invariant #2).
    open_big_drift = sum(
        1 for d in drift_warnings if abs(float(d.get("cash_flow_impact") or 0.0)) > 200.0
    )

    sf = (source_freshness or {}).get("simplefin") or {}
    return {
        # Cannot prove a clean board without a successful emission; a quiet board
        # is not a clean one. No run-level "ok" status exists in the emissions
        # ledger, so this keys off emission presence + no open >$200 drift.
        "managed_clean": bool(last_surfaced_at) and open_big_drift == 0,
        "last_surfaced_at": last_surfaced_at,
        "last_sync_at": sf.get("last_finished_at"),
        "apple_card_stale": _apple_card_stale(conn),
    }


def _apple_card_stale(conn: sqlite3.Connection) -> bool:
    """Whether the Apple Card cycle has had no covering paste (design #4).

    The real paste-cycle freshness signal: stale when the current open statement
    cycle has no covering card-spend paste (the latest card_import_runs row is
    older than a cycle), measured against the statement cycle rather than the 36h
    SimpleFIN sync clock. Replaces the slice-2 balance-snapshot-age proxy; the
    Apple Card has no live feed, so a fresh balance never meant fresh spend."""

    from .card_import import apple_card_paste_freshness

    try:
        return apple_card_paste_freshness(conn)["status"] == "stale"
    except sqlite3.OperationalError:
        return False


def _relative_time(iso: Any) -> str:
    """Human, deterministic relative age (e.g. "2h ago", "3 days ago")."""

    if not iso:
        return "never"
    try:
        ts = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    age_h = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600
    if age_h < 1:
        return "just now"
    if age_h < 48:
        return f"{int(round(age_h))}h ago"
    return f"{int(age_h // 24)} days ago"


def _status_color(digest: dict[str, Any]) -> str:
    severities = {g["severity"] for g in digest["guardrails"] if not g.get("advisory")}
    if "critical" in severities or "high" in severities:
        return "RED"
    if any((c["lowest_balance"] is not None and c["lowest_balance"] < 0) for c in digest["cash_flow"]):
        return "RED"
    # #7: a trough whose downside band crosses zero is RED even when the point
    # estimate clears - the estimated bills before the low point could push it
    # negative (mirrors the lowest_balance < 0 gate above).
    if any((c.get("trough_low_estimate") is not None and c["trough_low_estimate"] < 0) for c in digest["cash_flow"]):
        return "RED"
    if "medium" in severities:
        return "YELLOW"
    # #7: the point estimate clears the floor, but the estimated outflows before
    # the trough could drop it below the $2,500 cash floor (or to zero) - cap at
    # YELLOW until those amounts are confirmed.
    ts = digest.get("trough_sensitivity")
    if ts and ts.get("breach_risk"):
        return "YELLOW"
    # A confident GREEN is unwarranted when material recurring CHECKING debits are
    # known but not yet in the projection (e.g. a car payment): the runway is
    # provably incomplete, so cap at YELLOW until they are modeled.
    if digest.get("recurring_checking_monthly", 0) >= 100:
        return "YELLOW"
    # A material ESTIMATED bill (e.g. a variable card statement payment) means the
    # runway hinges on a guess for one of the largest outflows; the real amount has
    # run well above the estimate, so confident GREEN is unwarranted - cap at YELLOW.
    if digest.get("estimated_material"):
        return "YELLOW"
    return "GREEN"


def _account_label(account: dict[str, Any]) -> str:
    """Account name, with org appended when the name alone is uninformative
    (e.g. "Owner" -> "Owner [Apple Card (Updated Monthly)]")."""

    name = account.get("name") or "account"
    org = account.get("org")
    if org and org.split() and org.split()[0].lower() not in name.lower():
        return f"{name} [{org}]"
    return name


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"
