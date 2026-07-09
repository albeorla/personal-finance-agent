"""Charge-onboarding discovery, review queue, and decision recording.

This module turns observed transaction history into reviewable charge-pattern
candidates. It is deterministic on purpose: the same copied database always
produces the same candidates, so a background scan and an interactive review can
be reasoned about and regression-tested without an LLM in the loop.

Boundary: candidates are NOT cash-flow truth. They live in their own table
(``charge_onboarding_candidates``) and never write ``obligation_instances``.
Cash-flow projection only reads obligation instances, so an unapplied candidate
cannot move the forecast. Promoting a candidate into a canonical obligation is a
separate, guarded action (``apply_charge_onboarding_candidate``).

State model (see CLAUDE_CODE_HANDOFF.md):

    discovered -> proposed -> in_review -> accepted -> applied

with alternate states ``rejected``, ``deferred``, ``needs_more_evidence``,
``merged``, ``split``, and ``parked`` (auto-triaged out of the active walk; see
``classify_candidate_disposition``). A background re-scan never regresses a state
that a human moved, so a rejected or deferred candidate is not silently revived.
"""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median as _stat_median
from statistics import pstdev
from typing import Any

from .obligations import apply_obligation_instances
from .schema import ensure_app_schema


# --- status vocabulary -----------------------------------------------------

# Statuses that still want a human decision and are walked by the active queue.
ACTIVE_STATUSES: tuple[str, ...] = ("discovered", "proposed", "in_review")

# Auto-triage status: a real charge that is not a schedulable bill (variable
# discretionary, internal transfer, loan/investment review). Kept and listed via
# ``include_resolved=True``, re-scannable, but pulled out of the one-at-a-time
# active walk. Reversible by a human ``reset`` back to ``proposed``.
PARKED_STATUS = "parked"

# Statuses produced by a human/system decision. A re-scan refreshes their
# evidence but must not reset them back to ``proposed``. ``parked`` is included so
# an auto-parked candidate is never silently revived by a later scan.
DECIDED_STATUSES: frozenset[str] = frozenset(
    {"in_review", "accepted", "applied", "rejected", "deferred", "needs_more_evidence", "merged", "split", PARKED_STATUS}
)

# Statuses a fresh, never-reviewed candidate can be in.
FRESH_STATUSES: frozenset[str] = frozenset({"discovered", "proposed"})

# Review decisions and the status they move a candidate to. ``accept`` marks a
# candidate ready to apply; the actual write happens through
# ``apply_charge_onboarding_candidate`` (a separate guarded action), not here.
DECISION_ACTIONS: dict[str, str] = {
    "defer": "deferred",
    "reject": "rejected",
    "park": PARKED_STATUS,
    "needs_more_evidence": "needs_more_evidence",
    "in_review": "in_review",
    "accept": "accepted",
    "reset": "proposed",
}

# ``apply`` is not a review decision: it is the guarded write tool
# ``apply_charge_onboarding_candidate``. Route callers there instead.
APPLY_VIA_TOOL: frozenset[str] = frozenset({"apply"})

# Restructuring decisions that belong to a later slice. Rejected here on purpose.
DEFERRED_TO_LATER_SLICE: frozenset[str] = frozenset({"edit", "merge", "split"})

# Candidate type -> canonical obligation kind when a candidate is applied.
CANDIDATE_TYPE_TO_OBLIGATION_KIND: dict[str, str] = {
    "card_statement_input": "card_spend_input",
    "direct_checking_outflow": "bill",
    "inflow": "inflow",
    "variable_spend": "variable_spend",
    "internal_transfer": "transfer",
    "review_only": "review",
}

# Fixed step (days) for evenly-spaced cadences when generating dated instances.
FIXED_CADENCE_INTERVAL_DAYS: dict[str, int] = {"weekly": 7, "biweekly": 14, "quarterly": 91}

# Cadence labels that imply a real schedule. Proposing one requires at least
# MIN_CONSISTENT_INTERVALS observed intervals that agree with the median's
# bucket, so two consecutive-day gas fills never become a "weekly" candidate.
SCHEDULABLE_CADENCES: frozenset[str] = frozenset({"weekly", "biweekly", "monthly", "quarterly"})
MIN_CONSISTENT_INTERVALS = 3

# ~30.44 days per month (365.25 / 12); used to translate an evidence span into
# months of *elapsed* coverage rather than distinct calendar months touched.
DAYS_PER_MONTH = 30.44

# Charges closer together than this are a burst (multiple gas fills, a night
# out), not a weekly schedule, so their median interval never earns a cadence.
MIN_WEEKLY_INTERVAL_DAYS = 4

DEFAULT_APPLY_HORIZON_DAYS = 180


# --- account / merchant classification -------------------------------------

CLASS_TO_TREATMENT: dict[str, str] = {
    "checking": "direct_checking",
    "savings": "direct_checking",
    "card": "card_statement_input",
    "loan": "review_only",
    "investment": "review_only",
    "other": "review_only",
}

# Usage-driven merchants whose amounts move with consumption/season. Single
# tokens only, matched against the merchant slug and description text.
USAGE_KEYWORDS: frozenset[str] = frozenset(
    {
        "energy", "electric", "eversource", "gault", "aquarion", "gas", "water",
        "heating", "fuel", "oil", "propane", "utility", "kwh", "power", "pseg",
        "coned", "grid", "hvac",
    }
)

# Merchant slugs that look like internal money movement or debt payment rather
# than a third-party merchant charge. Flagged as internal and deprioritized.
INTERNAL_TRANSFER_TOKENS: frozenset[str] = frozenset({"transfer", "autopay"})
INTERNAL_TRANSFER_SUBSTRINGS: tuple[str, ...] = (
    "credit_card", "card_payment", "online_transfer", "payment_thank",
)

# How much each candidate type counts toward review priority. Real recurring
# obligations (clean bills) outrank variable discretionary spend and internal
# transfers, so the one-at-a-time review walk surfaces what matters first.
CANDIDATE_TYPE_PRIORITY_WEIGHT: dict[str, float] = {
    "direct_checking_outflow": 1.0,
    "card_statement_input": 0.9,
    "inflow": 0.7,
    "variable_spend": 0.3,
    "review_only": 0.2,
    "internal_transfer": 0.1,
}

# Generic finance/location words that must not, on their own, link a candidate
# to an existing obligation. Keeps "...Auto Finan..." from matching an autopay.
_OBLIGATION_MATCH_STOPWORDS: frozenset[str] = frozenset(
    {
        "energy", "electric", "card", "cards", "spend", "estimate", "estimates",
        "monthly", "payment", "payments", "autopay", "statement", "transfer",
        "online", "checking", "savings", "credit", "deposit", "greenwich", "town",
        "income", "subscription", "reimbursement", "reimbursements", "account",
        "bank", "plus", "premier",
    }
)

# Card org -> the canonical statement-payment obligation its charges roll into.
ORG_TO_STATEMENT_TARGET: tuple[tuple[str, str], ...] = (
    ("american express", "amex_statement_payment"),
    ("apple card", "apple_card_statement_payment"),
    ("chase", "chase_card_statement_payment"),
)


DEFAULT_OPTIONS: dict[str, Any] = {
    "min_evidence": 2,
    "include_inflows": False,
    "link_existing_obligations": True,
    # Auto-triage of newly discovered candidates (see classify_candidate_disposition).
    # Modes: off (no disposition computed), shadow (disposition recorded on the
    # candidate but no status changes), park_only (act on the park bucket only),
    # enforce (act on park + auto_reject). Defaults to shadow on first ship so the
    # would-reject list can be eyeballed before any candidate leaves the walk.
    "auto_triage": {"mode": "shadow", "reject_floor": 75.0},
}

# Cadences treated as "regular" by the auto-triage classifier. A regular cadence
# is a safety signal: a steadily-recurring charge is never auto-rejected even when
# its amounts vary (utility/metered service) -- it is parked, not dismissed.
REGULAR_CADENCES: frozenset[str] = frozenset({"weekly", "biweekly", "monthly", "quarterly"})

# Auto-triage dispositions.
DISPOSITION_SURFACE = "surface"
DISPOSITION_PARK = "park"
DISPOSITION_AUTO_REJECT = "auto_reject"


def classify_candidate_disposition(
    candidate: dict[str, Any], *, reject_floor: float = 75.0, statement_absorbed: bool = False
) -> dict[str, Any]:
    """Map a built candidate to surface / park / auto_reject from signals in hand.

    The detector already computes cadence regularity, amount variability (cv),
    confidence, candidate type, and a modeled-impact ``priority_score``. This is
    the judgment step that decides which of those candidates is worth a human's
    one-at-a-time attention (``surface``), which is real spend but not a
    schedulable bill (``park``), and which is structural noise (``auto_reject``).

    Three safety backstops guarantee a real recurring bill is NEVER auto-rejected:
      1. Confidence floor: only ``low``/``very_low`` candidates are reject-eligible;
         any ``medium``/``high`` candidate surfaces.
      2. Regularity override: a regular cadence is always parked (never rejected),
         even when amounts swing (metered utilities).
      3. Magnitude guard: a candidate whose modeled monthly impact
         (``priority_score``) exceeds ``reject_floor`` is parked, never rejected.

    Pure function of the candidate dict; no I/O. Returns
    ``{"disposition": str, "reasons": [str, ...]}``.
    """

    schedule = candidate.get("proposed_schedule_policy") or {}
    amount_policy = candidate.get("proposed_amount_policy") or {}
    cadence = schedule.get("cadence") or "unknown"
    cv = float(amount_policy.get("cv") or 0.0)
    n = int(candidate.get("evidence_count") or 0)
    months_covered = int(schedule.get("months_covered") or 0)
    confidence = candidate.get("confidence") or "very_low"
    candidate_type = candidate.get("candidate_type") or "review_only"
    priority_score = float(candidate.get("priority_score") or 0.0)

    regular = cadence in REGULAR_CADENCES
    stable = cv <= 0.20  # matches the existing needs_review cutoff (_amount_policy)

    base_reasons = [
        f"cadence {cadence} ({'regular' if regular else 'irregular'})",
        f"amounts {'stable' if stable else 'vary widely'} (cv {cv:.2f})",
        f"{n} occurrence{'s' if n != 1 else ''} over {months_covered} month(s)",
        f"{confidence} confidence",
        f"${priority_score:.0f}/mo modeled impact",
    ]

    def _result(disposition: str, reason: str) -> dict[str, Any]:
        return {"disposition": disposition, "reasons": base_reasons + [reason]}

    def _reject_or_guard(reason: str) -> dict[str, Any]:
        # Backstop 3 (magnitude guard): a material recurring charge is parked,
        # never auto-dismissed, even when every other signal says "noise".
        if priority_score > reject_floor:
            return _result(
                DISPOSITION_PARK,
                f"magnitude guard: ${priority_score:.0f}/mo exceeds ${reject_floor:.0f} "
                f"floor, parked not rejected ({reason})",
            )
        return _result(DISPOSITION_AUTO_REJECT, f"auto-dismissed: {reason}")

    # Rule 1: one-off / single-burst -- not enough history to be a schedule.
    if n < 2 or (months_covered < 2 and not regular):
        return _reject_or_guard("too few occurrences / single-burst, not a recurring pattern")

    # Rule 1b: card spend already absorbed by a modeled statement-payment
    # obligation -- the statement carries this cash flow, so no triage needed.
    if statement_absorbed and candidate.get("cash_flow_treatment") == "card_statement_input":
        return _result(
            DISPOSITION_PARK,
            "card spend already absorbed by a modeled statement-payment obligation",
        )

    # Rule 2: internal transfer / debt payment -- modeled elsewhere, park.
    if candidate_type == "internal_transfer":
        return _result(DISPOSITION_PARK, "internal transfer or debt payment, modeled elsewhere")

    # Rule 3: irregular variable discretionary spend at low confidence -- the bulk
    # of the noise (gas, dining, retail). Backstop 1 (confidence floor) lives in
    # the ``confidence in {low, very_low}`` clause: medium/high never rejects here.
    if candidate_type == "variable_spend" and not regular and confidence in {"low", "very_low"}:
        return _reject_or_guard("irregular variable discretionary spend at low confidence")

    # Rule 4 (regularity override, backstop 2): a regular cadence with variable
    # amounts (metered utility) is parked, never rejected.
    if candidate_type == "variable_spend" and regular:
        return _result(
            DISPOSITION_PARK,
            "regular cadence with variable amounts (utility/metered), parked not rejected",
        )

    # Rule 5: loan / investment review item -- park (not a checking bill).
    if candidate_type == "review_only":
        return _result(DISPOSITION_PARK, "loan/investment review item, not a scheduled checking bill")

    # Rule 6: plausible recurring obligation (clean bill, card statement input,
    # inflow, or medium/high confidence) -- keep in the active walk.
    return _result(DISPOSITION_SURFACE, "plausible recurring obligation; kept in the active queue")


def normalize_merchant_key(payee: str | None) -> str:
    """Slugify a payee into a stable merchant key.

    Payees in the copied database are already cleanly normalized, so a slug of
    the payee is a deterministic, low-surprise grouping key.
    """

    slug = re.sub(r"[^a-z0-9]+", "_", (payee or "").strip().lower())
    return slug.strip("_")


def account_class(account: dict[str, Any]) -> str:
    """Infer an account class from name/org because ``kind`` is empty in copy."""

    name = (account.get("name") or "").lower()
    org = (account.get("org") or "").lower()
    if "ckg" in name or "checking" in name:
        return "checking"
    if "saving" in name:
        return "savings"
    if "loan" in name:
        return "loan"
    if "schwab" in org or "pcra" in name or "trust" in name:
        return "investment"
    if (
        "american express" in org
        or "apple card" in org
        or "visa" in name
        or "mastercard" in name
        or "card" in name
    ):
        return "card"
    return "other"


def _statement_target_for_org(org: str | None) -> str | None:
    org_lower = (org or "").lower()
    for needle, target in ORG_TO_STATEMENT_TARGET:
        if needle in org_lower:
            return target
    return None


# --- scanning --------------------------------------------------------------


def scan_charge_onboarding_candidates(
    conn: sqlite3.Connection,
    *,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scan transaction history and upsert reviewable charge-pattern candidates.

    Idempotent: re-running produces the same candidate ids and refreshes their
    evidence in place. Human decisions are preserved.
    """

    ensure_app_schema(conn)
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    min_evidence = int(opts["min_evidence"])
    include_inflows = bool(opts["include_inflows"])
    triage = {**DEFAULT_OPTIONS["auto_triage"], **(opts.get("auto_triage") or {})}
    triage_mode = str(triage.get("mode", "shadow"))
    reject_floor = float(triage.get("reject_floor", 75.0))

    accounts = {
        row["id"]: {"name": row["name"], "org": row["org"], "kind": row["kind"]}
        for row in conn.execute("SELECT id, name, org, kind FROM accounts").fetchall()
    }

    txn_query = (
        "SELECT id, account_id, posted, transacted_at, amount, payee, description "
        "FROM transactions WHERE payee IS NOT NULL AND payee != '' AND amount != 0"
    )

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    scanned = 0
    for row in conn.execute(txn_query).fetchall():
        posted = row["posted"] or row["transacted_at"]
        if not posted:
            continue
        account = accounts.get(row["account_id"])
        if account is None:
            continue
        scanned += 1
        merchant_key = normalize_merchant_key(row["payee"])
        if not merchant_key:
            continue
        cls = account_class(account)
        direction = "inflow" if row["amount"] > 0 else "outflow"
        groups[(merchant_key, cls, direction)].append(
            {
                "id": row["id"],
                "account_id": row["account_id"],
                "account_name": account["name"],
                "account_org": account["org"],
                "posted": posted,
                "date": posted[:10],
                "amount": float(row["amount"]),
                "payee": row["payee"],
                "description": row["description"] or "",
            }
        )

    existing_obligations = (
        _load_obligation_index(conn) if opts["link_existing_obligations"] else []
    )
    modeled_obligation_ids = {oid for oid, _ in existing_obligations}

    now = _now()
    created = updated = unchanged = skipped = 0
    candidate_ids: list[str] = []
    by_disposition: dict[str, int] = defaultdict(int)
    triage_counts: dict[str, int] = defaultdict(int)
    for (merchant_key, cls, direction), items in sorted(groups.items()):
        if direction == "inflow" and not include_inflows:
            continue
        if len(items) < min_evidence:
            skipped += 1
            continue
        items.sort(key=lambda it: (it["date"], it["id"]))
        candidate = _build_candidate(merchant_key, cls, direction, items, existing_obligations)

        # Auto-triage: stamp the disposition (and its reasons) on the candidate's
        # review policy so it persists deterministically with the rest of the
        # proposal. "off" leaves the candidate untouched.
        disposition = None
        if triage_mode != "off":
            statement_target = (candidate.get("proposed_cash_impact_policy") or {}).get(
                "statement_target_obligation_id"
            )
            disposition = classify_candidate_disposition(
                candidate,
                reject_floor=reject_floor,
                statement_absorbed=bool(statement_target) and statement_target in modeled_obligation_ids,
            )
            candidate["proposed_review_policy"]["auto_disposition"] = disposition["disposition"]
            candidate["proposed_review_policy"]["disposition_reasons"] = disposition["reasons"]
            by_disposition[disposition["disposition"]] += 1

        outcome = _upsert_candidate(conn, candidate, now)
        candidate_ids.append(candidate["id"])
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        else:
            unchanged += 1

        if disposition is not None:
            _route_auto_triage(conn, candidate["id"], disposition, triage_mode, triage_counts)

    by_status: dict[str, int] = defaultdict(int)
    by_treatment: dict[str, int] = defaultdict(int)
    for row in conn.execute(
        "SELECT status, cash_flow_treatment FROM charge_onboarding_candidates"
    ).fetchall():
        by_status[row["status"]] += 1
        by_treatment[row["cash_flow_treatment"] or "unknown"] += 1

    return {
        "scanned_transactions": scanned,
        "groups_considered": len(groups),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skipped_low_evidence": skipped,
        "candidate_ids": candidate_ids,
        "candidates_total": conn.execute(
            "SELECT COUNT(*) FROM charge_onboarding_candidates"
        ).fetchone()[0],
        "by_status": dict(by_status),
        "by_cash_flow_treatment": dict(by_treatment),
        "by_disposition": dict(by_disposition),
        "auto_triage": {
            "mode": triage_mode,
            "reject_floor": reject_floor,
            "parked": triage_counts.get("parked", 0),
            "auto_rejected": triage_counts.get("auto_rejected", 0),
            "revived": triage_counts.get("revived", 0),
        },
        "options": {"min_evidence": min_evidence, "include_inflows": include_inflows},
    }


def _route_auto_triage(
    conn: sqlite3.Connection,
    candidate_id: str,
    disposition: dict[str, Any],
    mode: str,
    counts: dict[str, int],
) -> None:
    """Act on a candidate's disposition without ever destroying it.

    ``shadow`` records the disposition on the candidate (done by the caller) but
    changes no status. ``park_only`` moves the park bucket out of the active walk.
    ``enforce`` additionally auto-rejects structural noise. All moves go through
    ``record_charge_onboarding_decision`` with ``decided_by="auto_classifier"`` so
    they are auditable and reversible by a human ``reset``.

    Only FRESH candidates (never human-decided) are auto-moved. As a maturing-
    pattern safety valve, a candidate that an earlier auto run parked/rejected and
    that now clears the surface bar is revived to ``proposed`` -- but only when the
    prior decision was the classifier's, never a human's.
    """

    if mode not in {"park_only", "enforce"}:
        return

    row = conn.execute(
        "SELECT status, decision_json FROM charge_onboarding_candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return
    status = row["status"]
    disp = disposition["disposition"]
    reasons = disposition["reasons"]

    if status in FRESH_STATUSES:
        if disp == DISPOSITION_PARK:
            record_charge_onboarding_decision(
                conn, candidate_id,
                {"action": "park", "decided_by": "auto_classifier", "reasons": reasons},
            )
            counts["parked"] += 1
        elif disp == DISPOSITION_AUTO_REJECT and mode == "enforce":
            record_charge_onboarding_decision(
                conn, candidate_id,
                {"action": "reject", "decided_by": "auto_classifier", "reasons": reasons},
            )
            counts["auto_rejected"] += 1
        return

    # Maturing-pattern revival: a previously auto-parked/rejected candidate that
    # now surfaces is walked again -- but only if no human ever decided it.
    if status in {PARKED_STATUS, "rejected"} and disp == DISPOSITION_SURFACE:
        prior = _loads(row["decision_json"]) or {}
        if prior.get("decided_by") == "auto_classifier":
            record_charge_onboarding_decision(
                conn, candidate_id,
                {"action": "reset", "decided_by": "auto_classifier", "reasons": reasons},
            )
            counts["revived"] += 1


def _build_candidate(
    merchant_key: str,
    cls: str,
    direction: str,
    items: list[dict[str, Any]],
    existing_obligations: list[tuple[str, str]],
) -> dict[str, Any]:
    amounts = [round(abs(it["amount"]), 2) for it in items]
    n = len(amounts)
    avg = sum(amounts) / n
    med = float(_stat_median(amounts))
    mn, mx = min(amounts), max(amounts)
    stdev = pstdev(amounts) if n > 1 else 0.0
    cv = (stdev / avg) if avg else 0.0

    dates = [date.fromisoformat(it["date"]) for it in items]
    intervals = [(dates[i] - dates[i - 1]).days for i in range(1, n)]
    median_interval = float(_stat_median(intervals)) if intervals else None
    days = [d.day for d in dates]
    span_months = (dates[-1].year - dates[0].year) * 12 + (dates[-1].month - dates[0].month) + 1
    # Months of elapsed evidence, from the actual day span rather than distinct
    # calendar months touched: 4 charges over 5 days that cross a month boundary
    # cover 1 month, not 2.
    months_covered = round((dates[-1] - dates[0]).days / DAYS_PER_MONTH) + 1

    descriptions = [it["description"] for it in items]
    payee_display = _mode([it["payee"] for it in items])
    is_usage = _has_usage_keyword(merchant_key, descriptions)
    is_internal = _looks_internal(merchant_key)

    treatment = "inflow" if direction == "inflow" else CLASS_TO_TREATMENT[cls]
    statement_target = _statement_target_for_org(items[0]["account_org"]) if treatment == "card_statement_input" else None

    cadence = _cadence_label(median_interval, intervals)
    schedule_policy = {
        "cadence": cadence,
        "typical_day_of_month": int(round(_stat_median(days))),
        "day_spread": max(days) - min(days),
        "median_interval_days": round(median_interval, 1) if median_interval is not None else None,
        "occurrences": n,
        "months_covered": months_covered,
        "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(),
        "next_expected_date": (
            (dates[-1] + _days(median_interval)).isoformat() if median_interval is not None else None
        ),
    }

    amount_policy = _amount_policy(
        amounts=amounts, treatment=treatment, is_usage=is_usage,
        avg=avg, med=med, mn=mn, mx=mx, stdev=stdev, cv=cv,
    )

    cash_impact_policy = {
        "cash_flow_treatment": treatment,
        "statement_target_obligation_id": statement_target,
        "evidence_account_ids": sorted({it["account_id"] for it in items}),
        "evidence_account_names": sorted({it["account_name"] for it in items}),
        "affects_checking": treatment in {"direct_checking", "inflow"},
    }

    confidence = _confidence(n=n, months_covered=months_covered, cv=cv, cadence=cadence)
    missing_evidence = _missing_evidence(
        n=n, months_covered=months_covered, cv=cv, treatment=treatment,
        amount_method=amount_policy["method"], cadence=cadence,
    )

    if direction == "inflow":
        candidate_type = "inflow"
    elif is_internal:
        candidate_type = "internal_transfer"
    elif amount_policy["method"] == "needs_review" and not is_usage:
        # Highly variable, non-utility spend (groceries, gas, retail) is real
        # discretionary spend, not a clean recurring obligation to schedule.
        candidate_type = "variable_spend"
    elif treatment == "card_statement_input":
        candidate_type = "card_statement_input"
    elif treatment == "direct_checking":
        candidate_type = "direct_checking_outflow"
    else:
        candidate_type = "review_only"

    monthly_rate = n / max(span_months, 1)
    type_weight = CANDIDATE_TYPE_PRIORITY_WEIGHT.get(candidate_type, 0.5)
    priority_score = round(amount_policy["amount"] * monthly_rate * type_weight, 2)

    review_policy = {
        "needs_review_before_apply": True,
        "auto_applicable": False,
        "suggested_review_after": schedule_policy["next_expected_date"],
        "reasons": _review_reasons(confidence, candidate_type, missing_evidence),
    }

    existing_obligation_id = _match_existing_obligation(merchant_key, existing_obligations)

    notes = _build_notes(
        payee_display, treatment, amount_policy, cadence, n, candidate_type, existing_obligation_id
    )

    evidence_summary = {
        "occurrences": n,
        "amount_min": round(mn, 2),
        "amount_max": round(mx, 2),
        "amount_avg": round(avg, 2),
        "amount_median": round(med, 2),
        "amount_cv": round(cv, 3),
        "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(),
        "months_covered": months_covered,
        "cadence": cadence,
        "sample_amounts": amounts,
        "accounts": sorted({it["account_name"] for it in items}),
        "sample_descriptions": sorted({d for d in descriptions if d})[:3],
    }

    return {
        "id": f"cand:{merchant_key}:{cls}:{direction}",
        "merchant_key": merchant_key,
        "display_name": payee_display,
        "account_class": cls,
        "direction": direction,
        "candidate_type": candidate_type,
        "cash_flow_treatment": treatment,
        "proposed_schedule_policy": schedule_policy,
        "proposed_amount_policy": amount_policy,
        "proposed_cash_impact_policy": cash_impact_policy,
        "proposed_review_policy": review_policy,
        "confidence": confidence,
        "priority_score": priority_score,
        "evidence_count": n,
        "evidence_transaction_ids": [it["id"] for it in items],
        "evidence_summary": evidence_summary,
        "missing_evidence": missing_evidence,
        "notes": notes,
        "existing_obligation_id": existing_obligation_id,
        "first_evidence_date": dates[0].isoformat(),
        "last_evidence_date": dates[-1].isoformat(),
    }


def _amount_policy(
    *, amounts: list[float], treatment: str, is_usage: bool,
    avg: float, med: float, mn: float, mx: float, stdev: float, cv: float,
) -> dict[str, Any]:
    recent = amounts[-3:]
    recent_stable = len(recent) >= 2 and (max(recent) - min(recent)) <= max(0.01, 0.01 * max(recent))

    if treatment == "card_statement_input" and is_usage:
        method = "seasonal_card_spend"
        amount = round(avg, 2)
    elif treatment == "direct_checking" and is_usage and cv > 0.08:
        method = "seasonal_multiplier"
        amount = round(avg, 2)
    elif recent_stable:
        method = "fixed"
        amount = round(recent[-1], 2)
    elif cv <= 0.05:
        method = "fixed"
        amount = round(avg, 2)
    elif cv <= 0.20:
        method = "average"
        amount = round(avg, 2)
    else:
        method = "needs_review"
        amount = round(med, 2)

    policy: dict[str, Any] = {
        "method": method,
        "amount": amount,
        "currency": "USD",
        "base_average": round(avg, 2),
        "median": round(med, 2),
        "min": round(mn, 2),
        "max": round(mx, 2),
        "stdev": round(stdev, 2),
        "cv": round(cv, 3),
        "sample_amounts": amounts,
    }
    if method in {"seasonal_multiplier", "seasonal_card_spend"}:
        # Mirrors the existing Eversource/Gault estimator policy: average
        # observed usage, then apply a seasonal multiplier for peak months.
        policy["summer_multiplier"] = 1.5
        policy["winter_multiplier"] = 1.5
        policy["note"] = "Usage-driven; average observed spend and apply a seasonal multiplier for peak months."
    elif method == "needs_review":
        policy["note"] = "Amounts vary widely; needs an actual bill/statement before a fixed estimate is trustworthy."
    return policy


def _cadence_label(median_interval: float | None, intervals: list[int] | None = None) -> str:
    label = _interval_bucket(median_interval)
    if intervals is not None and label in SCHEDULABLE_CADENCES:
        consistent = sum(1 for iv in intervals if _interval_bucket(float(iv)) == label)
        if consistent < MIN_CONSISTENT_INTERVALS:
            return "unknown"
    return label


def _interval_bucket(median_interval: float | None) -> str:
    if median_interval is None:
        return "unknown"
    if median_interval < MIN_WEEKLY_INTERVAL_DAYS:
        # Sub-weekly gaps are a clustered burst, not a schedulable cadence.
        return "unknown"
    if median_interval <= 10:
        return "weekly"
    if median_interval <= 18:
        return "biweekly"
    if median_interval <= 37:
        return "monthly"
    if median_interval <= 75:
        return "irregular_multiweek"
    if median_interval <= 100:
        return "quarterly"
    return "irregular"


def _confidence(*, n: int, months_covered: int, cv: float, cadence: str) -> str:
    if n <= 1:
        return "very_low"
    regular = cadence in {"weekly", "biweekly", "monthly"}
    if n >= 4 and months_covered >= 4 and regular and cv <= 0.10:
        return "high"
    if n >= 3 and months_covered >= 3 and (regular or cadence == "irregular_multiweek"):
        return "medium"
    return "low"


def _missing_evidence(
    *, n: int, months_covered: int, cv: float, treatment: str, amount_method: str, cadence: str
) -> list[str]:
    missing: list[str] = []
    if n < 4:
        missing.append(f"only {n} matching transactions; more history would raise confidence")
    if amount_method in {"seasonal_multiplier", "seasonal_card_spend"} and months_covered < 12:
        missing.append("less than a full year of history, so the seasonal amount pattern is not fully confirmed")
    if cv > 0.20:
        missing.append("amounts vary widely; confirm an actual bill or statement amount")
    if treatment == "card_statement_input":
        missing.append("confirm which statement cycle each charge lands in before rolling it into statement-payment estimates")
    if cadence in {"irregular", "irregular_multiweek", "unknown"}:
        missing.append("timing is lumpy; the next due date is an estimate, not a known date")
    return missing


def _review_reasons(confidence: str, candidate_type: str, missing_evidence: list[str]) -> list[str]:
    reasons = ["applying a candidate writes canonical obligations, which is a separate guarded action"]
    if confidence in {"low", "very_low"}:
        reasons.append(f"confidence is {confidence}")
    if candidate_type == "card_statement_input":
        reasons.append("card spend feeds statement estimates rather than reducing checking directly")
    if candidate_type == "internal_transfer":
        reasons.append("looks like an internal transfer or debt payment, not a third-party merchant charge")
    if candidate_type == "variable_spend":
        reasons.append("amounts are highly variable discretionary spend, not a clean recurring obligation")
    if missing_evidence:
        reasons.append("evidence gaps remain (see missing_evidence)")
    return reasons


def _build_notes(
    display_name: str, treatment: str, amount_policy: dict[str, Any],
    cadence: str, n: int, candidate_type: str, existing_obligation_id: str | None,
) -> str:
    parts = [
        f"{display_name}: {n} observed charges, {cadence} cadence, "
        f"{amount_policy['method']} amount policy (~${amount_policy['amount']:.2f}).",
        f"Cash-flow treatment: {treatment}.",
    ]
    if candidate_type == "internal_transfer":
        parts.append("Flagged as internal: resembles an internal transfer or debt payment.")
    if candidate_type == "variable_spend":
        parts.append("Flagged as variable spend: amounts swing too much to schedule as a fixed obligation.")
    if existing_obligation_id:
        parts.append(f"Possibly already modeled by obligation '{existing_obligation_id}'; verify before creating a duplicate.")
    return " ".join(parts)


# --- queue + decisions -----------------------------------------------------


def list_charge_onboarding_queue(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    include_resolved: bool = False,
    summary: bool = False,
) -> list[dict[str, Any]]:
    """List candidates ordered by estimated monthly cash impact, descending.

    By default returns only the active queue (statuses that still want a human
    decision). Pass ``status`` to filter exactly, or ``include_resolved=True``
    to see decided/paused candidates too. ``summary=True`` returns compact rows
    (id, merchant, amount, cadence, confidence, status) instead of full detail.
    """

    ensure_app_schema(conn)
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    elif not include_resolved:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        where.append(f"status IN ({placeholders})")
        params.extend(ACTIVE_STATUSES)

    query = "SELECT * FROM charge_onboarding_candidates"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY priority_score DESC, evidence_count DESC, merchant_key ASC"
    # LIMIT -1 = unlimited in SQLite, so offset works without a limit.
    query += " LIMIT ? OFFSET ?"
    params.extend([limit if limit is not None else -1, offset or 0])

    candidates = [_row_to_candidate(row) for row in conn.execute(query, params).fetchall()]
    if summary:
        return [_candidate_summary(c) for c in candidates]
    return candidates


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    """Compact queue row: what a session needs to triage, nothing more."""

    schedule = candidate.get("proposed_schedule_policy") or {}
    amount_policy = candidate.get("proposed_amount_policy") or {}
    return {
        "id": candidate["id"],
        "merchant": candidate["display_name"],
        "amount": amount_policy.get("amount"),
        "cadence": schedule.get("cadence"),
        "confidence": candidate["confidence"],
        "status": candidate["status"],
        "cash_flow_treatment": candidate["cash_flow_treatment"],
        "evidence_count": candidate["evidence_count"],
        "priority_score": candidate["priority_score"],
        "existing_obligation_id": candidate["existing_obligation_id"],
    }


def get_next_charge_onboarding_candidate(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return the highest-priority unresolved candidate, or None if the queue is empty."""

    queue = list_charge_onboarding_queue(conn, limit=1)
    return queue[0] if queue else None


def record_charge_onboarding_decision(
    conn: sqlite3.Connection,
    candidate_id: str,
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Record a review decision against a candidate.

    Supported decisions: ``defer``, ``reject``, ``park``, ``needs_more_evidence``,
    ``in_review``, ``accept``, and ``reset``. ``accept`` only marks a candidate
    ready; the canonical write happens in ``apply_charge_onboarding_candidate``,
    so passing ``apply`` here raises ``ValueError``. Restructuring
    (``merge``/``split``/``edit``) is a separate guarded action and also raises.
    """

    ensure_app_schema(conn)
    # Accept the intuitive shapes, not just {"action": ...}: a bare action string,
    # or a dict that names the action under "decision" (the tool's own param name)
    # instead of "action". This is the most common first-call mistake.
    if isinstance(decision, str):
        decision = {"action": decision}
    decision = decision or {}
    action = decision.get("action") or decision.get("decision")
    row = conn.execute(
        "SELECT status FROM charge_onboarding_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown charge onboarding candidate: {candidate_id}")

    if action in APPLY_VIA_TOOL:
        raise ValueError(
            "use apply_charge_onboarding_candidate(...) to apply an accepted candidate; "
            "'apply' is a guarded write action, not a review decision"
        )
    if action in DEFERRED_TO_LATER_SLICE:
        raise ValueError(
            f"decision action '{action}' (restructuring candidates) is a separate guarded slice; "
            f"supported now: {sorted(DECISION_ACTIONS)}"
        )
    if action not in DECISION_ACTIONS:
        raise ValueError(
            f"unsupported decision action: {action!r}; supported: {sorted(DECISION_ACTIONS)}"
        )

    new_status = DECISION_ACTIONS[action]
    now = _now()
    decision_record = {
        "action": action,
        "notes": decision.get("notes"),
        "decided_at": now,
        "decided_by": decision.get("decided_by", "review"),
    }
    extra = {
        k: v for k, v in decision.items() if k not in {"action", "decision", "notes", "decided_by"}
    }
    if extra:
        decision_record["extra"] = extra

    # A normal decision stamps reviewed_at; reset un-decides the candidate, so it
    # clears reviewed_at back to NULL (as if never reviewed).
    reviewed_at = None if action == "reset" else now
    conn.execute(
        """
        UPDATE charge_onboarding_candidates
        SET status = ?, decision_json = ?, reviewed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_status, json.dumps(decision_record, sort_keys=True), reviewed_at, now, candidate_id),
    )
    return _get_candidate(conn, candidate_id)


# --- apply: candidate -> canonical obligation ------------------------------


def preview_charge_onboarding_apply(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    start_date: date | str | None = None,
    through_date: date | str | None = None,
    horizon_days: int = DEFAULT_APPLY_HORIZON_DAYS,
    obligation_id: str | None = None,
    amount_override: float | None = None,
    cadence_override: str | None = None,
) -> dict[str, Any]:
    """Return the obligation and dated instances that applying would create.

    Read-only: this never writes. It is the guarded preview half of the
    onboarding apply step, so a reviewer can see exactly what would land in the
    cash-flow model before committing to it. Pass amount_override/cadence_override
    to preview a corrected amount or cadence when the detector misread it.
    """

    ensure_app_schema(conn)
    candidate = _require_candidate(conn, candidate_id)
    return _build_apply_plan(
        candidate,
        start_date=start_date,
        through_date=through_date,
        horizon_days=horizon_days,
        obligation_id=obligation_id,
        amount_override=amount_override,
        cadence_override=cadence_override,
    )


def apply_charge_onboarding_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    start_date: date | str | None = None,
    through_date: date | str | None = None,
    horizon_days: int = DEFAULT_APPLY_HORIZON_DAYS,
    obligation_id: str | None = None,
    require_accepted: bool = True,
    amount_override: float | None = None,
    cadence_override: str | None = None,
) -> dict[str, Any]:
    """Promote an accepted candidate into a canonical obligation plus instances.

    This is the only place a candidate becomes cash-flow truth. It is guarded:
    by default the candidate must already be ``accepted`` (record an accept
    decision first). Writing is idempotent because instances use deterministic
    ids, so re-applying the same window updates rather than duplicates. Pass
    amount_override/cadence_override to correct a detector misread in this one
    call instead of rejecting and re-modeling.
    """

    ensure_app_schema(conn)
    candidate = _require_candidate(conn, candidate_id)
    if require_accepted and candidate["status"] != "accepted":
        raise ValueError(
            f"candidate {candidate_id} must be 'accepted' before it can be applied "
            f"(currently '{candidate['status']}'); record an accept decision first, "
            f"or pass require_accepted=False to override"
        )

    plan = _build_apply_plan(
        candidate,
        start_date=start_date,
        through_date=through_date,
        horizon_days=horizon_days,
        obligation_id=obligation_id,
        amount_override=amount_override,
        cadence_override=cadence_override,
    )
    write = apply_obligation_instances(
        conn, obligation=plan["obligation"], instances=plan["instances"]
    )

    now = _now()
    decision_record = {
        "action": "apply",
        "obligation_id": plan["obligation"]["id"],
        "instance_count": len(plan["instances"]),
        "decided_at": now,
    }
    conn.execute(
        """
        UPDATE charge_onboarding_candidates
        SET status = 'applied',
            applied_at = ?,
            existing_obligation_id = ?,
            decision_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (now, plan["obligation"]["id"], json.dumps(decision_record, sort_keys=True), now, candidate_id),
    )

    return {
        "candidate_id": candidate_id,
        "status": "applied",
        "obligation_id": plan["obligation"]["id"],
        "instances_created": write["created"],
        "instances_updated": write["updated"],
        "instances_total": len(plan["instances"]),
        "obligation": plan["obligation"],
        "schedule_summary": plan["schedule_summary"],
        "warnings": plan["warnings"],
    }


def _build_apply_plan(
    candidate: dict[str, Any],
    *,
    start_date: date | str | None,
    through_date: date | str | None,
    horizon_days: int,
    obligation_id: str | None,
    amount_override: float | None = None,
    cadence_override: str | None = None,
) -> dict[str, Any]:
    # A detector mis-classification (wrong cadence, e.g. weekly-should-be-monthly,
    # or a wrong amount) is corrected here in one apply call - the reviewer passes
    # the override instead of reject-then-rescan-then-remodel.
    schedule = {
        **(candidate.get("proposed_schedule_policy") or {}),
        **({"cadence": cadence_override} if cadence_override else {}),
    }
    amount_policy = candidate.get("proposed_amount_policy") or {}
    cash_impact = candidate.get("proposed_cash_impact_policy") or {}
    direction = candidate["direction"]
    treatment = candidate["cash_flow_treatment"]

    anchor = _apply_start_date(candidate, start_date)
    through = _coerce_apply_date(through_date) if through_date else anchor + timedelta(days=horizon_days)
    due_dates = _generate_due_dates(schedule, anchor, through)

    amount = round(
        float(amount_override if amount_override is not None else (amount_policy.get("amount") or 0.0)), 2
    )
    instance_treatment = _instance_cash_flow_treatment(treatment)
    statement_target = cash_impact.get("statement_target_obligation_id")
    resolved_obligation_id = obligation_id or f"onboarded_{candidate['merchant_key']}_{candidate['account_class']}"

    obligation = {
        "id": resolved_obligation_id,
        "name": candidate["display_name"],
        "kind": CANDIDATE_TYPE_TO_OBLIGATION_KIND.get(candidate["candidate_type"], "bill"),
        "cadence": schedule.get("cadence"),
        "status": "active",
        "source": f"charge_onboarding:{candidate['id']}",
    }

    instances: list[dict[str, Any]] = []
    for due in due_dates:
        instances.append(
            {
                "id": f"{resolved_obligation_id}:{due.isoformat()}",
                "due_date": due.isoformat(),
                "amount": amount,
                "direction": direction,
                "status": "expected",
                "source": f"charge_onboarding:{candidate['id']}",
                "confidence": candidate["confidence"],
                "notes": f"Onboarded from charge candidate {candidate['id']} ({amount_policy.get('method')} amount policy).",
                "amount_status": "estimated",
                "amount_source": amount_policy.get("method"),
                "estimation_method": amount_policy.get("method"),
                "estimation_inputs": amount_policy,
                "cash_flow_treatment": instance_treatment,
                "statement_target_obligation_id": (
                    statement_target if instance_treatment == "card_statement_input" else None
                ),
            }
        )

    projects_into_checking = instance_treatment in (None, "direct_checking")
    return {
        "candidate_id": candidate["id"],
        "obligation": obligation,
        "instances": instances,
        "schedule_summary": {
            "cadence": schedule.get("cadence"),
            "start_date": anchor.isoformat(),
            "through_date": through.isoformat(),
            "instance_count": len(instances),
            "amount_each": amount,
            "direction": direction,
            "cash_flow_treatment": instance_treatment,
            "projects_into_checking": projects_into_checking,
            "statement_target_obligation_id": statement_target if instance_treatment == "card_statement_input" else None,
        },
        "warnings": _apply_warnings(candidate, schedule, amount_policy, due_dates),
    }


def _apply_start_date(candidate: dict[str, Any], start_date: date | str | None) -> date:
    if start_date is not None:
        return _coerce_apply_date(start_date)
    schedule = candidate.get("proposed_schedule_policy") or {}
    next_expected = schedule.get("next_expected_date")
    if next_expected:
        return date.fromisoformat(next_expected)
    last = candidate.get("last_evidence_date")
    if last:
        return date.fromisoformat(last)
    raise ValueError(f"cannot infer a start date for candidate {candidate['id']}; pass start_date")


def _generate_due_dates(schedule: dict[str, Any], anchor: date, through: date) -> list[date]:
    if anchor > through:
        return []
    cadence = schedule.get("cadence")
    if cadence == "monthly":
        return _monthly_due_dates(anchor, through, int(schedule.get("typical_day_of_month") or anchor.day))
    interval = _cadence_interval_days(cadence, schedule.get("median_interval_days"))
    if interval is None:
        # Lumpy/unknown cadence: a single best-effort instance at the anchor.
        return [anchor]
    dates: list[date] = []
    current = anchor
    # through_date is exclusive, matching the cash-flow projection window
    # (due_date < end_date_exclusive), so a previewed instance always projects.
    while current < through:
        dates.append(current)
        current = current + timedelta(days=interval)
    return dates


def _monthly_due_dates(anchor: date, through: date, day_of_month: int) -> list[date]:
    dates: list[date] = []
    year, month = anchor.year, anchor.month
    while True:
        last_day = calendar.monthrange(year, month)[1]
        due = date(year, month, min(max(day_of_month, 1), last_day))
        if due >= through:  # through_date is exclusive (matches cash-flow window)
            break
        if due >= anchor:
            dates.append(due)
        month += 1
        if month > 12:
            year += 1
            month = 1
    return dates


def _cadence_interval_days(cadence: str | None, median_interval_days: float | None) -> int | None:
    if cadence in FIXED_CADENCE_INTERVAL_DAYS:
        return FIXED_CADENCE_INTERVAL_DAYS[cadence]
    if median_interval_days:
        return max(1, int(round(median_interval_days)))
    return None


def _instance_cash_flow_treatment(treatment: str | None) -> str | None:
    # None projects as a direct-checking flow (direction carries the sign), which
    # matches how generated income instances behave.
    if treatment == "inflow":
        return None
    if treatment == "direct_checking":
        return "direct_checking"
    if treatment == "card_statement_input":
        return "card_statement_input"
    # investment / loan / other -> not a checking flow; excluded from projection.
    return "review_only"


def _apply_warnings(
    candidate: dict[str, Any],
    schedule: dict[str, Any],
    amount_policy: dict[str, Any],
    due_dates: list[date],
) -> list[str]:
    warnings: list[str] = []
    if amount_policy.get("method") in {"seasonal_card_spend", "seasonal_multiplier", "needs_review", "average"}:
        warnings.append("amount is an estimate; replace with the actual bill or statement amount when known")
    if schedule.get("cadence") in {"irregular", "irregular_multiweek", "quarterly", "unknown"}:
        warnings.append("cadence is lumpy; generated due dates are evenly-spaced estimates, not known dates")
    if candidate["candidate_type"] in {"variable_spend", "internal_transfer", "review_only"}:
        warnings.append(
            f"candidate_type is '{candidate['candidate_type']}'; confirm this should be a scheduled obligation at all"
        )
    if not due_dates:
        warnings.append("no due dates fall in the requested window")
    return warnings


def _require_candidate(conn: sqlite3.Connection, candidate_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM charge_onboarding_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown charge onboarding candidate: {candidate_id}")
    return _row_to_candidate(row)


def _coerce_apply_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# --- persistence helpers ---------------------------------------------------

# Columns refreshed by a re-scan. Status / decision / review timestamps are
# deliberately excluded so a re-scan never overwrites a human decision.
_REFRESHABLE_COLUMNS: tuple[str, ...] = (
    "merchant_key", "display_name", "account_class", "direction", "candidate_type",
    "cash_flow_treatment", "proposed_schedule_policy_json", "proposed_amount_policy_json",
    "proposed_cash_impact_policy_json", "proposed_review_policy_json", "confidence",
    "priority_score", "evidence_count", "evidence_transaction_ids_json",
    "evidence_summary_json", "missing_evidence_json", "notes", "existing_obligation_id",
    "first_evidence_date", "last_evidence_date",
)


def _candidate_columns(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "merchant_key": candidate["merchant_key"],
        "display_name": candidate["display_name"],
        "account_class": candidate["account_class"],
        "direction": candidate["direction"],
        "candidate_type": candidate["candidate_type"],
        "cash_flow_treatment": candidate["cash_flow_treatment"],
        "proposed_schedule_policy_json": _dumps(candidate["proposed_schedule_policy"]),
        "proposed_amount_policy_json": _dumps(candidate["proposed_amount_policy"]),
        "proposed_cash_impact_policy_json": _dumps(candidate["proposed_cash_impact_policy"]),
        "proposed_review_policy_json": _dumps(candidate["proposed_review_policy"]),
        "confidence": candidate["confidence"],
        "priority_score": candidate["priority_score"],
        "evidence_count": candidate["evidence_count"],
        "evidence_transaction_ids_json": _dumps(candidate["evidence_transaction_ids"]),
        "evidence_summary_json": _dumps(candidate["evidence_summary"]),
        "missing_evidence_json": _dumps(candidate["missing_evidence"]),
        "notes": candidate["notes"],
        "existing_obligation_id": candidate["existing_obligation_id"],
        "first_evidence_date": candidate["first_evidence_date"],
        "last_evidence_date": candidate["last_evidence_date"],
    }


def _upsert_candidate(conn: sqlite3.Connection, candidate: dict[str, Any], now: str) -> str:
    cols = _candidate_columns(candidate)
    existing = conn.execute(
        "SELECT * FROM charge_onboarding_candidates WHERE id = ?", (candidate["id"],)
    ).fetchone()

    if existing is None:
        all_cols = {**cols, "id": candidate["id"], "status": "proposed", "created_at": now, "updated_at": now}
        names = ", ".join(all_cols)
        placeholders = ", ".join("?" for _ in all_cols)
        conn.execute(
            f"INSERT INTO charge_onboarding_candidates ({names}) VALUES ({placeholders})",
            tuple(all_cols.values()),
        )
        return "created"

    if all(_columns_equal(existing[name], cols[name]) for name in _REFRESHABLE_COLUMNS):
        return "unchanged"

    # Refresh evidence/proposal in place. Fresh candidates re-assert ``proposed``;
    # decided candidates keep their human status.
    set_status = existing["status"] in FRESH_STATUSES
    assignments = ", ".join(f"{name} = ?" for name in _REFRESHABLE_COLUMNS)
    values: list[Any] = [cols[name] for name in _REFRESHABLE_COLUMNS]
    extra_set = ", updated_at = ?"
    values.append(now)
    if set_status:
        extra_set += ", status = ?"
        values.append("proposed")
    values.append(candidate["id"])
    conn.execute(
        f"UPDATE charge_onboarding_candidates SET {assignments}{extra_set} WHERE id = ?",
        values,
    )
    return "updated"


def _columns_equal(stored: Any, computed: Any) -> bool:
    if isinstance(computed, float) or isinstance(stored, float):
        try:
            return abs(float(stored) - float(computed)) < 1e-9
        except (TypeError, ValueError):
            return False
    return stored == computed


def _row_to_candidate(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "merchant_key": row["merchant_key"],
        "display_name": row["display_name"],
        "account_class": row["account_class"],
        "direction": row["direction"],
        "status": row["status"],
        "candidate_type": row["candidate_type"],
        "cash_flow_treatment": row["cash_flow_treatment"],
        "proposed_schedule_policy": _loads(row["proposed_schedule_policy_json"]),
        "proposed_amount_policy": _loads(row["proposed_amount_policy_json"]),
        "proposed_cash_impact_policy": _loads(row["proposed_cash_impact_policy_json"]),
        "proposed_review_policy": _loads(row["proposed_review_policy_json"]),
        "confidence": row["confidence"],
        "priority_score": row["priority_score"],
        "evidence_count": row["evidence_count"],
        "evidence_transaction_ids": _loads(row["evidence_transaction_ids_json"]) or [],
        "evidence_summary": _loads(row["evidence_summary_json"]),
        "missing_evidence": _loads(row["missing_evidence_json"]) or [],
        "notes": row["notes"],
        "decision": _loads(row["decision_json"]),
        "existing_obligation_id": row["existing_obligation_id"],
        "first_evidence_date": row["first_evidence_date"],
        "last_evidence_date": row["last_evidence_date"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "reviewed_at": row["reviewed_at"],
        "applied_at": row["applied_at"],
    }


def _get_candidate(conn: sqlite3.Connection, candidate_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM charge_onboarding_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    return _row_to_candidate(row)


def _load_obligation_index(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    try:
        rows = conn.execute("SELECT id, name FROM obligations").fetchall()
    except sqlite3.OperationalError:
        return []
    index: list[tuple[str, str]] = []
    for row in rows:
        normalized = f"{row['id']} {normalize_merchant_key(row['name'])}".lower()
        index.append((row["id"], normalized))
    return index


def _match_existing_obligation(merchant_key: str, obligations: list[tuple[str, str]]) -> str | None:
    """Soft-link a candidate to an already-modeled obligation, conservatively.

    Only distinctive tokens (>= 5 chars, not generic finance/location words) are
    allowed to match, so "Volvo ... Auto Finan ..." does not match an "autopay"
    obligation and a Greenwich merchant does not match a Greenwich paycheck.
    """

    tokens = [
        token
        for token in merchant_key.split("_")
        if len(token) >= 5 and token not in _OBLIGATION_MATCH_STOPWORDS
    ]
    if not tokens:
        return None
    for obligation_id, normalized in obligations:
        if any(token in normalized for token in tokens):
            return obligation_id
    return None


def _looks_internal(merchant_key: str) -> bool:
    tokens = set(merchant_key.split("_"))
    if tokens & INTERNAL_TRANSFER_TOKENS:
        return True
    return any(needle in merchant_key for needle in INTERNAL_TRANSFER_SUBSTRINGS)


def _has_usage_keyword(merchant_key: str, descriptions: list[str]) -> bool:
    haystack = (merchant_key + " " + " ".join(descriptions)).lower()
    haystack_tokens = set(re.split(r"[^a-z0-9]+", haystack))
    return bool(haystack_tokens & USAGE_KEYWORDS)


def _mode(values: list[str]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    # Deterministic: highest count, then lexical order.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _days(median_interval: float):
    from datetime import timedelta

    return timedelta(days=int(round(median_interval)))


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def auto_model_high_confidence_recurring(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
    min_evidence: int = 3,
    treatments: tuple[str, ...] = ("direct_checking",),
) -> dict[str, Any]:
    """Apply HIGH-confidence, well-evidenced direct-checking recurring candidates
    as proper obligations so they enter the cash-flow projection.

    This is the "complete the projection" gap-filler: a recurring charge (e.g. a
    car payment) that is unmodeled - or modeled only by a dead obligation with no
    projectable instance - is auto-accepted and applied (forward instances), so
    the runway reflects it. Conservative by design: only confidence='high',
    direct-checking, with >= ``min_evidence`` occurrences; everything else stays
    in the review queue. Skips candidates already modeled by an obligation that
    has a future projectable instance.
    """

    ensure_app_schema(conn)
    as_of = date.fromisoformat(as_of_date) if as_of_date else date.today()
    placeholders = ",".join("?" for _ in treatments)
    rows = conn.execute(
        f"""
        SELECT id, display_name, cash_flow_treatment, confidence, evidence_count,
               existing_obligation_id, status
        FROM charge_onboarding_candidates
        WHERE confidence = 'high'
          AND cash_flow_treatment IN ({placeholders})
          AND evidence_count >= ?
          AND status IN ('proposed', 'in_review', 'needs_more_evidence', 'accepted')
        ORDER BY priority_score DESC
        """,
        (*treatments, min_evidence),
    ).fetchall()

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for r in rows:
        # Never auto-model an internal transfer (e.g. "Online Transfer to
        # Checking") as an obligation - it is moving the user's own money, not a bill.
        if "transfer" in (r["display_name"] or "").lower():
            skipped.append({"candidate_id": r["id"], "reason": "internal transfer, not a bill"})
            continue
        link = r["existing_obligation_id"]
        if link:
            has_future = conn.execute(
                "SELECT 1 FROM obligation_instances WHERE obligation_id = ? "
                "AND status IN ('expected','needs_review','partially_paid') AND due_date >= ? LIMIT 1",
                (link, as_of.isoformat()),
            ).fetchone()
            if has_future:
                skipped.append({"candidate_id": r["id"], "reason": "already modeled with future instances"})
                continue
        try:
            if r["status"] != "accepted":
                record_charge_onboarding_decision(conn, r["id"], {"action": "accept"})
            res = apply_charge_onboarding_candidate(conn, r["id"], start_date=as_of.isoformat())
            applied.append({"candidate_id": r["id"], "merchant": r["display_name"],
                            "obligation_id": res.get("obligation_id"), "instances": res.get("instance_count")})
        except Exception as exc:  # noqa: BLE001 - skip un-appliable candidates, never abort the batch
            skipped.append({"candidate_id": r["id"], "reason": f"{type(exc).__name__}: {exc}"[:140]})

    conn.commit()
    return {"applied_count": len(applied), "applied": applied, "skipped": skipped}
