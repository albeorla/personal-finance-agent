"""Spending analytics (slice W).

Read-only reports over the copied transaction history: a deterministic,
rules-based merchant/category normalizer (no LLM) and ``summarize_spending``,
which aggregates outflows by category, merchant, or month with totals, counts,
a month-over-month trend, and provenance (the transaction ids behind each
bucket). Transfers and income are excluded by default so "spending" means actual
discretionary/operating outflow, not card payments or payroll.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

# First matching rule wins; substrings are matched against lowercased payee+description.
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("Income", ["payroll", "direct deposit", "intellibridge", "town of greenwich", "salary", "pay transfer"]),
    # Transfers / debt servicing - NOT discretionary spend. Includes paper checks
    # (usually rent/bills), credit-card payments, and auto-loan payments.
    ("Transfers", ["online payment", "card payment", "autopay", "thank you", "ach", "zelle", "venmo", "transfer",
                   "payment to", "credit card", "applecard", "gsbank", "check #", "auto finan", "car fin", "loan pmt",
                   "loan disbursement", "disbursement", "loan proceeds"]),
    ("Taxes", ["state of ct", "dept of revenue", "department of revenue", "irs", "franchise tax", "tax payment"]),
    ("Fees", ["interest charge", "interest:", "interest", "late fee", "annual fee", "membership fee", "renewal membership",
              "cash advance fee", "finance charge", "service fee", "atm fee", "foreign transaction"]),
    ("Insurance", ["safeco", "geico", "allstate", "progressive", "state farm", "insurance"]),
    ("Utilities", ["eversource", "national grid", "water dept", "electric", "natural gas", "comcast", "xfinity", "verizon", "optimum", "aquarion", "gault energy"]),
    ("Software", ["openai", "anthropic", "claude.ai", "google cloud", "cursor", "vercel", "github", "perplexity", "midjourney", "elevenlabs", "notion", "zoho", "superhuman", "paddle", "ynab"]),
    ("BNPL", ["affirm", "afterpay", "klarna", "pay in 4", "paypal pay", "sezzle"]),
    ("Subscriptions", ["spotify", "netflix", "plex", "new york times", "nyt", "apple.com/bill", "hulu", "disney", "youtube", "icloud", "google one", "kindle", "paramount", "amazon prime", "peloton", "ring services"]),
    ("Pets", ["vca", "vetsource", "chewy", "animal hospital", "veterinary"]),
    ("Groceries", ["whole foods", "stop & shop", "stop and shop", "trader joe", "costco", "shoprite", "wegmans", "supermarket", "citarella"]),
    ("Dining", ["restaurant", "coffee", "starbucks", "dunkin", "pizza", "grill", "cafe", "doordash", "uber eats", "grubhub", "tavern", "wendy", "cava", "7-eleven", "geoff"]),
    ("Transport", ["shell", "exxon", "mobil", "gulf", "uber", "lyft", "mta", "metro-north", "parking", "gas station", "sunoco", "citgo", "ford", "audi", "metropolis", "passny"]),
    ("Shopping", ["amazon", "target", "walmart", "best buy", "apple store", "home depot", "etsy", "marine layer", "babolat", "alex mill", "mother denim"]),
    ("Health", ["pharmacy", "cvs", "walgreens", "anthem", "dental", "medical", "doctor", "psycholog", "psychiatry", "therapy", "mychart", "plushcare", "flyte medical", "morning spa"]),
]


def categorize(payee: str | None, description: str | None = "") -> str:
    text = f"{payee or ''} {description or ''}".lower()
    for category, needles in CATEGORY_RULES:
        if any(n in text for n in needles):
            return category
    return "Other"


def normalize_merchant(payee: str | None) -> str:
    if not payee:
        return "Unknown"
    cleaned = re.sub(r"#?\d{2,}", "", payee)          # store / location numbers
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -*#")
    return cleaned.title() if cleaned else "Unknown"


def summarize_spending(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    group_by: str = "category",
    exclude_transfers: bool = True,
    top_n: int = 10,
    sample_ids: int = 5,
) -> dict[str, Any]:
    """Aggregate outflows over a date range. Read-only."""

    if group_by not in {"category", "merchant", "month"}:
        raise ValueError("group_by must be one of: category, merchant, month")

    rows = conn.execute(
        "SELECT id, posted, amount, payee, description FROM transactions "
        "WHERE amount < 0 AND posted IS NOT NULL AND substr(posted,1,10) BETWEEN ? AND ? "
        "ORDER BY posted",
        (start_date, end_date),
    ).fetchall()

    buckets: dict[str, dict[str, Any]] = {}
    by_month: dict[str, float] = {}
    total = 0.0
    excluded = {"Transfers": 0, "Income": 0}

    for r in rows:
        category = categorize(r["payee"], r["description"])
        spend = abs(float(r["amount"]))
        if exclude_transfers and category in ("Transfers", "Income"):
            excluded[category] += 1
            continue

        if group_by == "category":
            key = category
        elif group_by == "merchant":
            key = normalize_merchant(r["payee"])
        else:
            key = (r["posted"] or "")[:7]  # YYYY-MM

        b = buckets.setdefault(key, {"key": key, "total": 0.0, "count": 0, "txn_ids": []})
        b["total"] = round(b["total"] + spend, 2)
        b["count"] += 1
        if len(b["txn_ids"]) < sample_ids:
            b["txn_ids"].append(r["id"])

        month = (r["posted"] or "")[:7]
        by_month[month] = round(by_month.get(month, 0.0) + spend, 2)
        total = round(total + spend, 2)

    ranked = sorted(buckets.values(), key=lambda b: b["total"], reverse=True)
    months_sorted = sorted(by_month.items())
    trend = [{"month": m, "total": t} for m, t in months_sorted]

    return {
        "start_date": start_date,
        "end_date": end_date,
        "group_by": group_by,
        "exclude_transfers": exclude_transfers,
        "total_spending": total,
        "transaction_count": sum(b["count"] for b in buckets.values()),
        "buckets": ranked[:top_n],
        "by_month": trend,
        "month_over_month_change": (round(trend[-1]["total"] - trend[-2]["total"], 2) if len(trend) >= 2 else None),
        "excluded_counts": excluded,
    }


def list_transactions(
    conn: sqlite3.Connection,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    query: str | None = None,
    min_amount: float | None = None,
    account_id: str | None = None,
    include_pending: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """List individual transactions, newest first. Read-only.

    Fills the gap where only aggregate ``summarize_spending`` existed: this is how
    you quote an EXACT charge amount for reconciliation or "what was that $X?".
    Filters (all optional): ``start_date``/``end_date`` (YYYY-MM-DD, inclusive, on
    the posted date), ``query`` (case-insensitive substring over payee+description),
    ``min_amount`` (absolute-value floor), ``account_id``, and ``include_pending``.
    ``limit`` is capped at 500; ``truncated`` flags when more rows matched.
    """

    limit = max(1, min(int(limit), 500))
    where = ["1=1"]
    params: list[Any] = []
    if start_date:
        where.append("substr(t.posted,1,10) >= ?")
        params.append(start_date)
    if end_date:
        where.append("substr(t.posted,1,10) <= ?")
        params.append(end_date)
    if account_id:
        where.append("t.account_id = ?")
        params.append(account_id)
    if min_amount is not None:
        where.append("ABS(t.amount) >= ?")
        params.append(abs(float(min_amount)))
    if query:
        where.append("LOWER(COALESCE(t.payee,'') || ' ' || COALESCE(t.description,'')) LIKE ?")
        params.append(f"%{query.lower()}%")
    if not include_pending:
        where.append("t.pending = 0")

    sql = (
        "SELECT t.id, t.account_id, a.name AS account_name, t.posted, t.amount, "
        "t.payee, t.description, t.pending, t.source "
        "FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY t.posted DESC, t.id DESC LIMIT ?"
    )
    try:
        rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    except sqlite3.OperationalError:
        # No transaction history table yet (never synced): return an empty,
        # well-formed result rather than raising.
        return {"count": 0, "truncated": False, "limit": limit, "transactions": []}

    truncated = len(rows) > limit
    rows = rows[:limit]
    txns = [
        {
            "id": r["id"],
            "account_id": r["account_id"],
            "account_name": r["account_name"],
            "posted": r["posted"],
            "amount": round(float(r["amount"]), 2),
            "payee": r["payee"],
            "description": r["description"],
            "pending": bool(r["pending"]),
            "source": r["source"],
        }
        for r in rows
    ]
    return {"count": len(txns), "truncated": truncated, "limit": limit, "transactions": txns}


def render_spending_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Spending {report['start_date']} -> {report['end_date']} (by {report['group_by']})",
        "",
        f"Total spending: ${_money(report['total_spending'])} across {report['transaction_count']} transactions"
        + ("  (transfers + income excluded)" if report["exclude_transfers"] else ""),
        "",
        "## Top buckets",
    ]
    for b in report["buckets"]:
        lines.append(f"- {b['key']}: ${_money(b['total'])} ({b['count']} txns)")
    if not report["buckets"]:
        lines.append("- none")
    lines.append("")
    lines.append("## Monthly trend")
    for t in report["by_month"]:
        lines.append(f"- {t['month']}: ${_money(t['total'])}")
    if report.get("month_over_month_change") is not None:
        lines.append(f"\nLatest month-over-month change: ${_money(report['month_over_month_change'])}")
    return "\n".join(lines)


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"
