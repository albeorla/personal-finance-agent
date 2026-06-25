"""Parallel-run parity report (cutover slice R).

The engine for the parallel-run: parse a legacy ``cash-flow.md`` (the user
supplies a fresh one from their own daily ritual) and diff it against the new
daily digest, so the cutover decision rests on a precise list of where the two
systems disagree - not on eyeballing two outputs.

Read-only. It never runs the legacy ritual or writes any legacy file; it only
reads the markdown the caller points it at. Obligation parsing reuses
``migration.parse_cashflow_md``.
"""

from __future__ import annotations

import datetime as dt
import re
from datetime import date
from typing import Any

from .digest import build_daily_digest
from .migration import _amount_close, _label_tokens, parse_cashflow_md
from .status import default_db_path


DATE_WINDOW_DAYS = 7
DATE_DRIFT_WINDOW_DAYS = 31
MATERIAL_AMOUNT_DELTA = 50.0


def compare_to_legacy(
    *,
    legacy_cashflow_md_path: str,
    db_path: str | None = None,
    as_of_date: str | None = None,
    base_year: int = 2026,
) -> dict[str, Any]:
    """Diff the legacy cash-flow.md against the new daily digest."""

    legacy = parse_cashflow_md(legacy_cashflow_md_path, base_year=base_year)
    digest = build_daily_digest(db_path or str(default_db_path()), as_of_date=as_of_date)
    as_of = digest["as_of_date"]
    new_obligations = [
        {"due_date": o["due_date"], "name": o["obligation_name"], "amount": o["amount"], "direction": o["direction"]}
        for o in digest["upcoming_obligations"]
    ]

    # Window alignment: the legacy file covers its own (often stale) window. Only
    # diff the OVERLAP - obligations on/after as_of and on/before the legacy
    # window end - so a 3-week-old file does not produce structurally guaranteed
    # "missing"/"extra"/working-cash noise.
    legacy_updated = _parse_legacy_updated(legacy_cashflow_md_path)
    horizon = (legacy_updated + dt.timedelta(days=35)).isoformat() if legacy_updated else None
    stale_days = (dt.date.fromisoformat(as_of) - legacy_updated).days if legacy_updated else None
    is_stale = stale_days is not None and stale_days > 7

    def _in_window(d: str) -> bool:
        return d >= as_of and (horizon is None or d <= horizon)

    legacy_cmp = [r for r in legacy if _in_window(r["date"])]
    new_cmp = [o for o in new_obligations if horizon is None or o["due_date"] <= horizon]

    matched, changed, missing, extra = _diff(legacy_cmp, new_cmp)
    working_cash = _compare_working_cash(legacy_cashflow_md_path, digest["balances"].get("working_cash"))

    # Parity discrepancies are ADVISORY: the legacy cash-flow.md is hand-maintained
    # and often structured differently (a combined paycheck vs two split inflows,
    # renamed/abbreviated bills, summarized rows), so a "missing"/"extra" is a
    # prompt for human eyes, not a proven system error. Hence medium/low, not high.
    discrepancies = (
        [{"kind": "missing_in_new", "severity": "medium", **m} for m in missing]
        + [{"kind": c["kind"], "severity": _change_severity(c), **c} for c in changed]
        + [{"kind": "extra_in_new", "severity": "low", **e} for e in extra]
    )
    by_severity: dict[str, int] = {}
    for d in discrepancies:
        by_severity[d["severity"]] = by_severity.get(d["severity"], 0) + 1

    staleness_warning = None
    if is_stale:
        staleness_warning = (
            f"Legacy file is {stale_days} days old (Updated {legacy_updated.isoformat()}); "
            f"comparison restricted to the overlapping window [{as_of} .. {horizon}]. "
            "Regenerate cash-flow.md the same day for a clean parity check - the working-cash delta below reflects the time gap."
        )

    return {
        "as_of_date": as_of,
        "legacy_path": legacy_cashflow_md_path,
        "legacy_updated": legacy_updated.isoformat() if legacy_updated else None,
        "legacy_stale_days": stale_days,
        "comparison_window": {"start": as_of, "end": horizon},
        "staleness_warning": staleness_warning,
        "summary": {
            "legacy_obligations_in_window": len(legacy_cmp),
            "new_obligations_in_window": len(new_cmp),
            "matched": len(matched),
            "changed": len(changed),
            "missing_in_new": len(missing),
            "extra_in_new": len(extra),
        },
        "by_severity": by_severity,
        "in_parity": len(missing) == 0 and len(changed) == 0,
        "matched": matched,
        "discrepancies": discrepancies,
        "working_cash": working_cash,
    }


def render_parity_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"# Parallel-Run Parity - {report['as_of_date']}",
        "",
    ]
    lines += [
        "_Advisory: the legacy cash-flow.md is hand-maintained and may combine paychecks, rename, or summarize"
        " differently, so 'missing'/'extra' items are prompts to eyeball - not proven system errors._",
        "",
    ]
    if report.get("staleness_warning"):
        lines += [f"_WARNING: {report['staleness_warning']}_", ""]
    lines += [
        f"In parity: {'YES' if report['in_parity'] else 'NO'}  (window {report['comparison_window']['start']} .. {report['comparison_window']['end']})",
        f"Legacy obligations (in window): {s['legacy_obligations_in_window']} | New: {s['new_obligations_in_window']} | "
        f"Matched: {s['matched']} | Changed: {s['changed']} | Missing in new: {s['missing_in_new']} | Extra in new: {s['extra_in_new']}",
        "",
    ]
    wc = report["working_cash"]
    lines.append("## Working Cash")
    lines.append(f"Legacy XXXX: ${_money(wc.get('legacy_working'))} | New working cash: ${_money(wc.get('new_working_cash'))} | Delta: ${_money(wc.get('delta'))}")
    lines.append("")

    lines.append(f"## Discrepancies ({len(report['discrepancies'])})")
    for d in report["discrepancies"]:
        if d["kind"] == "missing_in_new":
            lines.append(f"- [{d['severity']}] MISSING in new: {d['legacy']['date']} {d['legacy']['label'][:50]} (${_money(d['legacy']['amount'])})")
        elif d["kind"] == "extra_in_new":
            lines.append(f"- [{d['severity']}] EXTRA in new: {d['new']['due_date']} {d['new']['name'][:50]} (${_money(d['new']['amount'])})")
        else:
            lines.append(
                f"- [{d['severity']}] {d['kind'].upper()}: {d['new']['name'][:40]} "
                f"legacy ${_money(d['legacy']['amount'])}@{d['legacy']['date']} vs new ${_money(d['new']['amount'])}@{d['new']['due_date']} "
                f"(amountdelta ${_money(d.get('amount_delta'))}, datedelta {d.get('date_delta_days')}d)"
            )
    if not report["discrepancies"]:
        lines.append("- none")
    return "\n".join(lines)


# --- internals -------------------------------------------------------------


def _diff(legacy: list[dict[str, Any]], new_obligations: list[dict[str, Any]]):
    claimed: set[int] = set()
    matched: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for lr in legacy:
        l_date = date.fromisoformat(lr["date"])
        best_i: int | None = None
        best_kind: str | None = None
        for i, do in enumerate(new_obligations):
            if i in claimed or lr["direction"] != do["direction"]:
                continue
            if not (_label_tokens(lr["label"]) & _label_tokens(do["name"])):
                continue
            delta_days = abs((l_date - date.fromisoformat(do["due_date"])).days)
            amt_close = _amount_close(lr["amount"], do["amount"])
            if delta_days <= DATE_WINDOW_DAYS and amt_close:
                best_i, best_kind = i, "matched"
                break
            if delta_days <= DATE_WINDOW_DAYS and not amt_close and best_kind is None:
                best_i, best_kind = i, "amount_changed"
            elif amt_close and delta_days <= DATE_DRIFT_WINDOW_DAYS and best_kind is None:
                best_i, best_kind = i, "date_changed"

        if best_i is None:
            missing.append({"legacy": lr})
            continue
        claimed.add(best_i)
        do = new_obligations[best_i]
        if best_kind == "matched":
            matched.append({"legacy": lr, "new": do})
        else:
            changed.append({
                "legacy": lr, "new": do, "kind": best_kind,
                "amount_delta": round(do["amount"] - lr["amount"], 2),
                "date_delta_days": (date.fromisoformat(do["due_date"]) - l_date).days,
            })

    # Rename-tolerant second pass: a legacy row with no token overlap (e.g.
    # "NYTimes" vs "New York Times") still matches an unclaimed new obligation on
    # amount + date + direction, so the SAME bill is not double-reported as both
    # MISSING and EXTRA.
    still_missing: list[dict[str, Any]] = []
    for m in missing:
        lr = m["legacy"]
        l_date = date.fromisoformat(lr["date"])
        # EXACT amount (to the cent) + a tight date window, and only when the
        # match is UNIQUE - otherwise two unrelated same-amount bills would be
        # false-bound, hiding a genuinely missing one.
        hits = [
            i for i, do in enumerate(new_obligations)
            if i not in claimed and lr["direction"] == do["direction"]
            and abs(abs(lr["amount"]) - abs(do["amount"])) < 0.01
            and abs((l_date - date.fromisoformat(do["due_date"])).days) <= 2
        ]
        if len(hits) == 1:
            claimed.add(hits[0])
            matched.append({"legacy": lr, "new": new_obligations[hits[0]], "matched_by": "amount+date (renamed)"})
        else:
            still_missing.append(m)

    extra = [{"new": do} for i, do in enumerate(new_obligations) if i not in claimed]
    return matched, changed, still_missing, extra


def _change_severity(change: dict[str, Any]) -> str:
    if change["kind"] == "amount_changed":
        return "medium" if abs(change.get("amount_delta", 0)) >= MATERIAL_AMOUNT_DELTA else "low"
    return "low"  # date_changed


def _parse_legacy_updated(md_path: str) -> dt.date | None:
    try:
        with open(md_path) as fh:
            for line in fh:
                m = re.search(r"Updated:\s*(\d{4}-\d{2}-\d{2})", line)
                if m:
                    return dt.date.fromisoformat(m.group(1))
    except OSError:
        return None
    return None


def _compare_working_cash(md_path: str, new_working_cash: float | None) -> dict[str, Any]:
    legacy_val = None
    try:
        with open(md_path) as fh:
            for line in fh:
                if "XXXX" in line:
                    amounts = re.findall(r"\$([\d,]+\.?\d*)", line)
                    if amounts:
                        legacy_val = float(amounts[-1].replace(",", ""))  # 'avail' is the last $ on the balances line
                        break
    except OSError:
        legacy_val = None
    delta = None
    if legacy_val is not None and new_working_cash is not None:
        delta = round(new_working_cash - legacy_val, 2)
    return {"legacy_working": legacy_val, "new_working_cash": new_working_cash, "delta": delta}


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"
