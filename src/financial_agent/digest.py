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
from .reconciliation import list_reconciliation_review_items
from .status import default_db_path, get_finance_status


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
             "lowest_balance": p["lowest_balance"], "lowest_balance_date": p["lowest_balance_date"]}
            for p in projections
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
        },
    }
    digest["status_color"] = _status_color(digest)
    return digest


def render_digest_markdown(digest: dict[str, Any]) -> str:
    bal = digest["balances"]
    lines: list[str] = []
    lines.append(f"# Finance Daily Digest - {digest['as_of_date']}")
    lines.append("")
    lines.append(f"Cash runway (modeled bills only): {digest['status_color']}")
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


def _status_color(digest: dict[str, Any]) -> str:
    severities = {g["severity"] for g in digest["guardrails"] if not g.get("advisory")}
    if "critical" in severities or "high" in severities:
        return "RED"
    if any((c["lowest_balance"] is not None and c["lowest_balance"] < 0) for c in digest["cash_flow"]):
        return "RED"
    if "medium" in severities:
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
