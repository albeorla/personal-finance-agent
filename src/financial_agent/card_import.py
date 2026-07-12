"""Card-spend paste-import (design #4).

The Apple Card has no live transaction feed: SimpleFIN refreshes only the
balance ("Updated Monthly"), so individual charges never land in
``transactions``. That makes card spend invisible to projections and lets a
balance-only account look "fresh" even when nothing has been supplied in months.

This module turns a pasted monthly Apple Card download (CSV or statement text)
into real ``transactions`` rows under a distinct source
(``source='apple_card_paste'``), deduped against prior pastes via a deterministic
synthetic id, fuzzy-matched to the right account. Once written, the existing
onboarding scanner picks the rows up automatically (they carry a non-empty
``payee``) and the statement-estimate rollup grounds the statement-payment
instance. When the paste carries a statement total, ``import_card_statement_for_db``
also promotes that total onto the Apple Card statement instance (a protected,
observed amount the rollup never clobbers) and records a sticky manual balance.

Every real import writes one ``card_import_runs`` row; ``apple_card_paste_freshness``
reads the latest of those against the statement cycle so the daily digest can nag
when a cycle has had no paste. Default ``dry_run=True``: parse + preview, write
only on an explicit confirm.

Pure-function core: ``detect_format`` / ``parse_*`` / ``synthetic_txn_id`` do no
I/O; ``import_card_statement_for_db`` owns the single sqlite connection (the
caller owns commit/rollback, matching ``set_manual_balance``).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
import sqlite3
import uuid
from typing import Any

from .manual_balance import _MATCH_FLOOR, _TIE_BAND, _score, set_manual_balance
from .schema import ensure_app_schema

PASTE_SOURCE = "apple_card_paste"
# The operating checking account is manual-sourced (no reliable live feed), so its
# activity is pasted in as a CSV. Its transactions land under this source, kept
# distinct from the card-paste lane and the SimpleFIN feed.
CHECKING_SOURCE = "checking_paste"
_CHASE_ACTIVITY_HEADER = "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #"

# Card org -> the canonical statement-payment obligation its charges roll into.
# Mirrors onboarding.ORG_TO_STATEMENT_TARGET for the Apple Card only (this module
# is Apple-first; generic_csv is best-effort and does not promote a statement).
APPLE_STATEMENT_OBLIGATION_ID = "apple_card_statement_payment"

# Amount source that marks an observed, confirmed statement total. Listed in
# statements.PROTECTED_AMOUNT_SOURCES so recompute_statement_estimates skips it.
STATEMENT_AMOUNT_SOURCE = "statement_amount"

# Apple writes purchases as positive amounts; flip to negative to match the
# outflow convention (onboarding treats amount > 0 as inflow). Payments and
# Daily Cash / credits stay positive (inflows) and are excluded from cycle spend.
_APPLE_CSV_HEADER = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)"
)
_INFLOW_TYPES = {"payment", "daily cash", "credit"}

# A ~monthly statement cycle. When the most recent covered statement close is
# older than this, a new cycle has closed with no covering paste -> stale. This
# is the chosen approximation of the design's "current open cycle past the last
# covered close" without coupling the freshness signal to modeled obligations.
CYCLE_STALE_DAYS = 35

# Apple statement-line shape: MM/DD/YYYY  <desc...>  $-1,234.56
_STATEMENT_LINE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+\$?(-?[\d,]+\.\d{2})$")
_STATEMENT_TOTAL = re.compile(
    r"(?:new balance|total balance)\D*\$?(-?[\d,]+\.\d{2})", re.IGNORECASE
)
_STATEMENT_CLOSE = re.compile(
    r"statement closing date\D*(\d{2}/\d{2}/\d{4})", re.IGNORECASE
)


# --- format detection / parsing (pure) -------------------------------------


def detect_format(text: str) -> str:
    """Classify a pasted blob as apple_csv | apple_statement | generic_csv | unknown."""

    if not text or not text.strip():
        return "unknown"
    head = text.lstrip().splitlines()[0].strip()
    norm = head.replace(" ", "").lower()
    if norm == _APPLE_CSV_HEADER.replace(" ", "").lower():
        return "apple_csv"
    if "transactiondate" in norm and "amount" in norm:
        return "apple_csv"
    if _STATEMENT_LINE.match(head) or _STATEMENT_TOTAL.search(text) or _STATEMENT_CLOSE.search(text):
        # Statement text: dated money lines and/or a balance/close marker.
        if any(_STATEMENT_LINE.match(line.strip()) for line in text.splitlines()):
            return "apple_statement"
    if "," in head and re.search(r"\bamount\b", head, re.IGNORECASE):
        return "generic_csv"
    return "unknown"


def parse_apple_csv(text: str) -> dict[str, Any]:
    """Parse an Apple Card CSV export into normalized txn dicts.

    Purchases are flipped negative (outflow); ``Type`` in {Payment, Daily Cash,
    Credit} stay positive (inflow) and are tagged ``inflow=True``. Rows whose
    amount or date cannot be parsed are counted in ``skipped``, never written.
    """

    reader = csv.DictReader(io.StringIO(text.lstrip()))
    txns: list[dict[str, Any]] = []
    skipped = 0
    for row in reader:
        raw_date = (row.get("Transaction Date") or "").strip()
        merchant = (row.get("Merchant") or row.get("Description") or "").strip()
        raw_amount = (row.get("Amount (USD)") or row.get("Amount") or "").strip()
        txn_type = (row.get("Type") or "").strip().lower()
        iso = _to_iso_date(raw_date)
        amount = _parse_money(raw_amount)
        if iso is None or amount is None:
            skipped += 1
            continue
        is_inflow = txn_type in _INFLOW_TYPES
        # Apple purchases are positive; outflows must be negative. Inflows
        # (payments / Daily Cash / credits) keep their positive sign.
        signed = abs(amount) if is_inflow else -abs(amount)
        txns.append(
            {
                "transacted_date": iso,
                "merchant": merchant or (row.get("Description") or "").strip(),
                "description": (row.get("Description") or merchant or "").strip(),
                "amount": round(signed, 2),
                "inflow": is_inflow,
                "type": txn_type or None,
            }
        )
    return {"txns": txns, "skipped": skipped, "statement_total": None, "statement_close_date": None}


def parse_apple_statement(text: str) -> dict[str, Any]:
    """Parse Apple Card statement text into normalized txn dicts.

    Pulls ``statement_total`` from a New/Total Balance line and
    ``statement_close_date`` from a "Statement closing date" line when present, so
    the caller can omit them. Statement money lines are treated as purchases
    (outflow) unless explicitly negative in the source.
    """

    txns: list[dict[str, Any]] = []
    skipped = 0
    for raw in text.splitlines():
        line = raw.strip()
        m = _STATEMENT_LINE.match(line)
        if not m:
            continue
        iso = _to_iso_date(m.group(1))
        amount = _parse_money(m.group(3))
        if iso is None or amount is None:
            skipped += 1
            continue
        merchant = m.group(2).strip()
        # Negative source amount = a credit/payment (inflow); a bare purchase
        # line is positive in the source and becomes an outflow.
        is_inflow = amount < 0
        signed = abs(amount) if is_inflow else -abs(amount)
        txns.append(
            {
                "transacted_date": iso,
                "merchant": merchant,
                "description": merchant,
                "amount": round(signed, 2),
                "inflow": is_inflow,
                "type": None,
            }
        )

    total_m = _STATEMENT_TOTAL.search(text)
    close_m = _STATEMENT_CLOSE.search(text)
    statement_total = _parse_money(total_m.group(1)) if total_m else None
    statement_close = _to_iso_date(close_m.group(1)) if close_m else None
    return {
        "txns": txns,
        "skipped": skipped,
        "statement_total": round(abs(statement_total), 2) if statement_total is not None else None,
        "statement_close_date": statement_close,
    }


def parse_generic_csv(text: str) -> dict[str, Any]:
    """Best-effort date/desc/amount mapping for non-Apple card exports.

    Other cards (Amex/Citi) land here later; v1 keeps it minimal and never
    promotes a statement total. Sign comes from a signed Amount column when
    present, else from Debit (outflow) / Credit (inflow) columns.
    """

    reader = csv.DictReader(io.StringIO(text.lstrip()))
    fields = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
    date_col = _first_match(fields, ("transaction date", "date", "posted date"))
    # A single signed Amount column wins (our real Chase export). Otherwise fall
    # back to separate Debit/Credit columns with the checking sign convention:
    # a Debit is money out (negative), a Credit is money in (positive). Never
    # treat a Debit as a signed amount - that inverts every outflow.
    amount_col = _first_match(fields, ("amount (usd)", "amount"))
    debit_col = _first_match(fields, ("debit",))
    credit_col = _first_match(fields, ("credit",))
    desc_col = _first_match(fields, ("merchant", "description", "payee", "name"))
    txns: list[dict[str, Any]] = []
    skipped = 0
    if date_col is None or (amount_col is None and debit_col is None and credit_col is None):
        return {"txns": [], "skipped": 0, "statement_total": None, "statement_close_date": None}
    for row in reader:
        iso = _to_iso_date((row.get(date_col) or "").strip())
        if amount_col is not None:
            amount = _parse_money((row.get(amount_col) or "").strip())
        else:
            debit = _parse_money((row.get(debit_col) or "").strip()) if debit_col else None
            credit = _parse_money((row.get(credit_col) or "").strip()) if credit_col else None
            if debit is None and credit is None:
                amount = None
            elif debit and credit:
                # Ambiguous: a row is a debit OR a credit, never both non-zero.
                # Skip loudly (counted in `skipped`) rather than silently net a
                # wrong sign via credit - debit.
                amount = None
            else:
                amount = (abs(credit) if credit is not None else 0.0) - (abs(debit) if debit is not None else 0.0)
        if iso is None or amount is None:
            skipped += 1
            continue
        merchant = (row.get(desc_col) or "").strip() if desc_col else ""
        txns.append(
            {
                "transacted_date": iso,
                "merchant": merchant,
                "description": merchant,
                "amount": round(amount, 2),
                "inflow": amount > 0,
                "type": None,
            }
        )
    return {"txns": txns, "skipped": skipped, "statement_total": None, "statement_close_date": None}


def parse_text(text: str, fmt: str | None = None) -> dict[str, Any]:
    """Dispatch to the right parser for the detected (or given) format."""

    fmt = fmt or detect_format(text)
    if fmt == "apple_csv":
        return {"format": fmt, **parse_apple_csv(text)}
    if fmt == "apple_statement":
        return {"format": fmt, **parse_apple_statement(text)}
    if fmt == "generic_csv":
        return {"format": fmt, **parse_generic_csv(text)}
    return {"format": "unknown", "txns": [], "skipped": 0, "statement_total": None, "statement_close_date": None}


def synthetic_txn_id(
    account_id: str,
    transacted_date: str,
    amount: float,
    merchant: str,
    ordinal: int,
    prefix: str = "applecard",
) -> str:
    """Deterministic id for a pasted txn (the source supplies none).

    ``id = {prefix}:sha1(account_id|transacted_date|signed_amount|merchant_slug|ordinal)``.
    ``ordinal`` disambiguates genuine same-day / same-merchant / same-amount
    charges within one paste. Re-pasting reproduces the same ids, so the upsert is
    idempotent and overlap is absorbed. ``prefix`` namespaces the id per source
    (``applecard`` for card pastes, ``checking`` for checking activity).
    """

    payload = "|".join(
        [
            str(account_id),
            str(transacted_date),
            f"{float(amount):.2f}",
            _merchant_slug(merchant),
            str(ordinal),
        ]
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def assign_synthetic_ids(
    account_id: str, txns: list[dict[str, Any]], prefix: str = "applecard"
) -> list[dict[str, Any]]:
    """Stamp each parsed txn with its synthetic id, ordinal-disambiguated.

    The ordinal counts prior identical (date, signed amount, merchant slug) rows
    in input order, so two genuinely distinct same-day/same-amount charges get
    distinct ids while a re-paste of the same blob reproduces them exactly.
    """

    seen: dict[tuple[str, str, str], int] = {}
    out: list[dict[str, Any]] = []
    for txn in txns:
        key = (txn["transacted_date"], f"{txn['amount']:.2f}", _merchant_slug(txn["merchant"]))
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1
        out.append({**txn, "id": synthetic_txn_id(account_id, txn["transacted_date"], txn["amount"], txn["merchant"], ordinal, prefix), "ordinal": ordinal})
    return out


# --- DB-facing import ------------------------------------------------------


def import_card_statement_for_db(
    conn: sqlite3.Connection,
    *,
    text: str,
    account_query: str = "Apple Card",
    as_of_date: str,
    statement_close_date: str | None = None,
    statement_total: float | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Parse a pasted card statement and (unless dry-run) write it into the DB.

    Steps: detect format -> parse -> resolve account (fuzzy, unambiguous) ->
    synthetic-id + dedup against existing ``transactions`` -> when committing,
    upsert the rows, record a ``card_import_runs`` row, and (when a total +
    close-date are known) promote the Apple Card statement instance to the
    observed total and record a sticky manual balance. The caller owns
    commit/rollback.
    """

    ensure_app_schema(conn)
    now = _now()
    warnings: list[str] = []

    parsed = parse_text(text)
    fmt = parsed["format"]
    if fmt == "unknown":
        return {
            "status": "unparsed",
            "dry_run": dry_run,
            "format": "unknown",
            "message": "Could not detect a supported format.",
            "example_header": _APPLE_CSV_HEADER,
            "preview": "import_card_statement: unparsed - paste an Apple Card CSV or statement text.",
        }

    # Statement parsing can recover the total/close date the caller omitted.
    statement_total = statement_total if statement_total is not None else parsed.get("statement_total")
    statement_close_date = statement_close_date or parsed.get("statement_close_date")

    account = _resolve_account(conn, account_query)
    if account["status"] != "ok":
        return {
            "status": account["status"],
            "dry_run": dry_run,
            "format": fmt,
            "candidates": account.get("candidates"),
            "message": account.get("message"),
            "preview": f"import_card_statement: {account['status']} - {account.get('message','')}",
        }

    account_id = account["account_id"]
    txns = assign_synthetic_ids(account_id, parsed["txns"])

    # Dedup against transactions already stored (any prior paste reproduces ids).
    new_rows: list[dict[str, Any]] = []
    duplicate = 0
    for txn in txns:
        exists = conn.execute("SELECT 1 FROM transactions WHERE id = ?", (txn["id"],)).fetchone()
        if exists:
            duplicate += 1
        else:
            new_rows.append(txn)

    spend_rows = [t for t in txns if not t["inflow"]]
    inflow_rows = [t for t in txns if t["inflow"]]
    cycle_spend_total = round(sum(t["amount"] for t in spend_rows), 2)  # negative (outflow)

    if statement_total is None:
        warnings.append("partial - no statement total; running the spend rollup only (no statement promotion).")
    if parsed["skipped"]:
        warnings.append(f"{parsed['skipped']} row(s) skipped (unparsable date/amount/currency).")

    promotion = _plan_promotion(conn, account, statement_total, statement_close_date, as_of_date)

    result: dict[str, Any] = {
        "status": "preview" if dry_run else "ok",
        "dry_run": dry_run,
        "format": fmt,
        "account": {
            "account_id": account_id,
            "account_name": account["account_name"],
            "org": account["org"],
            "score": account["score"],
        },
        "rows_parsed": len(txns),
        "spend_rows": len(spend_rows),
        "payment_credit_rows": len(inflow_rows),
        "skipped_rows": parsed["skipped"],
        "new": len(new_rows),
        "duplicate": duplicate,
        "cycle_spend_total": cycle_spend_total,
        "statement_total": round(abs(statement_total), 2) if statement_total is not None else None,
        "statement_close_date": statement_close_date,
        "promotion": promotion,
        "warnings": warnings,
        "card_import_run_id": None,
    }

    if dry_run:
        result["preview"] = _render_preview(result)
        return result

    # --- commit path (caller owns the transaction) ---
    for txn in new_rows:
        _insert_paste_transaction(conn, account_id, txn, now)

    run_id = f"cardimport:{account_id}:{now}:{uuid.uuid4().hex[:8]}"
    conn.execute(
        """
        INSERT INTO card_import_runs (
            id, account_id, imported_at, statement_close_date, txn_count,
            total_spend, source_format, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, account_id, now, statement_close_date, len(txns), cycle_spend_total, fmt, None),
    )
    result["card_import_run_id"] = run_id

    if promotion and promotion.get("action") == "promote":
        # Sticky manual balance (liability stays negative) + observed statement
        # instance (a protected amount the rollup will never overwrite).
        bal = set_manual_balance(
            conn,
            account_query,
            -abs(float(statement_total)),
            as_of_date,
            note=f"Apple Card statement total from paste (close {statement_close_date}).",
        )
        result["manual_balance"] = bal
        conn.execute(
            """
            UPDATE obligation_instances
            SET amount = ?, amount_status = 'observed', amount_source = ?,
                amount_observed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (round(abs(float(statement_total)), 2), STATEMENT_AMOUNT_SOURCE, now, now, promotion["instance_id"]),
        )
        promotion["applied"] = True

    result["preview"] = _render_preview(result)
    return result


def import_checking_activity_for_db(
    conn: sqlite3.Connection,
    *,
    text: str,
    account_query: str = "checking",
    as_of_date: str,
    balance: float | None = None,
    dry_run: bool = True,
    confirmed_source_hash: str | None = None,
) -> dict[str, Any]:
    """Parse pasted checking-account activity (CSV) and (unless dry-run) write it.

    The operating checking account is manual-sourced, so activity is pasted in as
    a CSV. This reuses the generic CSV parser, resolves the account (fuzzy,
    unambiguous), stamps deterministic synthetic ids (``checking:`` prefix) so a
    re-paste is idempotent, and upserts the new rows as ``transactions``
    (``source='checking_paste'``). When ``balance`` is given, a sticky manual
    balance snapshot is recorded for the same account. The caller owns
    commit/rollback, so the transaction rows and the balance snapshot land in one
    db transaction.
    """

    ensure_app_schema(conn)
    now = _now()
    warnings: list[str] = []

    is_chase_activity = bool(text.splitlines()) and text.splitlines()[0] == _CHASE_ACTIVITY_HEADER
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if is_chase_activity else None
    if is_chase_activity:
        reader = csv.DictReader(io.StringIO(text))
        txns = []
        row_errors = []
        total_rows = 0
        for row_number, row in enumerate(reader, start=2):
            total_rows += 1
            iso = _to_iso_date((row.get("Posting Date") or "").strip())
            amount = _parse_money((row.get("Amount") or "").strip())
            if iso is None or amount is None:
                row_errors.append(
                    {
                        "row_number": row_number,
                        "error_code": "invalid_date" if iso is None else "invalid_amount",
                    }
                )
                continue
            description = (row.get("Description") or "").strip()
            txns.append(
                {
                    "transacted_date": iso,
                    "merchant": description,
                    "description": description,
                    "amount": round(amount, 2),
                    "inflow": amount > 0,
                    "type": None,
                }
            )
        parsed = {"txns": txns, "skipped": len(row_errors)}
    else:
        parsed = parse_generic_csv(text)
        row_errors = []
        total_rows = len(parsed["txns"]) + parsed["skipped"]
    if not parsed["txns"]:
        return {
            "status": "unparsed",
            "dry_run": dry_run,
            "message": "Could not parse a date/amount CSV. Expect a header with date and amount columns.",
            "skipped_rows": parsed["skipped"],
            "preview": "import_checking_activity: unparsed - paste a CSV with date and amount columns.",
        }

    account = _resolve_account(conn, account_query)
    if account["status"] != "ok":
        return {
            "status": account["status"],
            "dry_run": dry_run,
            "candidates": account.get("candidates"),
            "message": account.get("message"),
            "preview": f"import_checking_activity: {account['status']} - {account.get('message','')}",
        }

    account_id = account["account_id"]
    txns = assign_synthetic_ids(account_id, parsed["txns"], prefix="checking")

    new_rows: list[dict[str, Any]] = []
    duplicate = 0
    for txn in txns:
        exists = conn.execute("SELECT 1 FROM transactions WHERE id = ?", (txn["id"],)).fetchone()
        if exists:
            duplicate += 1
        else:
            new_rows.append(txn)

    inflow_rows = [t for t in txns if t["inflow"]]
    outflow_rows = [t for t in txns if not t["inflow"]]
    if parsed["skipped"]:
        warnings.append(f"{parsed['skipped']} row(s) skipped (unparsable date/amount).")

    result: dict[str, Any] = {
        "status": "preview" if dry_run else "ok",
        "dry_run": dry_run,
        "account": {
            "account_id": account_id,
            "account_name": account["account_name"],
            "org": account["org"],
            "score": account["score"],
        },
        "rows_parsed": len(txns),
        "inflow_rows": len(inflow_rows),
        "outflow_rows": len(outflow_rows),
        "skipped_rows": parsed["skipped"],
        "new": len(new_rows),
        "duplicate": duplicate,
        "balance": round(float(balance), 2) if balance is not None else None,
        "balance_snapshot": None,
        "warnings": warnings,
    }

    if is_chase_activity:
        result.update(
            {
                "format": "chase_activity",
                "account": {"status": "matched"},
                "total_rows": total_rows,
                "parsed_rows": len(txns),
                "row_error_count": len(row_errors),
                "row_errors": row_errors,
                "source_hash": source_hash,
            }
        )

        if not dry_run and confirmed_source_hash != source_hash:
            result["status"] = "confirmation_required"
            result["preview"] = "Import refused: confirm the matching preview source hash."
            return result

    if dry_run:
        result["preview"] = (
            "Chase activity preview. Confirm this source hash to apply."
            if is_chase_activity
            else _render_checking_preview(result)
        )
        return result

    # --- commit path (caller owns the transaction) ---
    for txn in new_rows:
        _insert_paste_transaction(conn, account_id, txn, now, source=CHECKING_SOURCE)

    if is_chase_activity:
        conn.execute(
            """
            INSERT OR IGNORE INTO checking_import_runs (
                source_hash, account_id, imported_at, total_rows, parsed_rows,
                new_count, duplicate_count, skipped_rows, row_error_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_hash,
                account_id,
                now,
                total_rows,
                len(txns),
                len(new_rows),
                duplicate,
                parsed["skipped"],
                len(row_errors),
            ),
        )

    if balance is not None:
        result["balance_snapshot"] = set_manual_balance(
            conn,
            account_query,
            float(balance),
            as_of_date,
            note="Checking balance snapshot from activity paste.",
        )

    result["preview"] = "Chase activity imported." if is_chase_activity else _render_checking_preview(result)
    return result


def _render_checking_preview(result: dict[str, Any]) -> str:
    tag = "DRY RUN" if result["dry_run"] else "APPLIED"
    acct = result.get("account") or {}
    lines = [
        f"import_checking_activity ({tag}) - {acct.get('account_name', '?')}",
        f"rows parsed: {result['rows_parsed']}   deposits: {result['inflow_rows']}   "
        f"withdrawals: {result['outflow_rows']}",
        f"new: {result['new']}   duplicate (already imported): {result['duplicate']}",
        f"balance snapshot: {_money(result['balance']) if result['balance'] is not None else 'n/a'}",
    ]
    for w in result.get("warnings", []):
        lines.append(f"note: {w}")
    lines.append("Re-run with dry_run=false to write." if result["dry_run"] else "Written.")
    return "\n".join(lines)


def apple_card_paste_freshness(
    conn: sqlite3.Connection,
    *,
    account_query: str = "Apple Card",
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Freshness of the Apple Card paste, measured against the statement cycle.

    Returns ``status`` of:
      - ``unknown`` - no Apple Card account, or no import has ever run.
      - ``fresh``   - the latest import covers the currently-open cycle.
      - ``stale``   - the last covered statement close is older than one cycle
        (``CYCLE_STALE_DAYS``), i.e. a new cycle has closed with no covering paste.

    This is deliberately decoupled from the 36h SimpleFIN sync clock (a
    balance-only account looks "fresh" to that clock even with no card data).
    """

    ensure_app_schema(conn)
    today = (now or dt.datetime.now(dt.timezone.utc)).date()

    account = _resolve_account(conn, account_query)
    if account["status"] != "ok":
        return {"status": "unknown", "reason": "no Apple Card account matched", "account_id": None}

    row = conn.execute(
        """
        SELECT id, imported_at, statement_close_date
        FROM card_import_runs
        WHERE account_id = ? AND error IS NULL
        ORDER BY imported_at DESC, id DESC
        LIMIT 1
        """,
        (account["account_id"],),
    ).fetchone()
    if row is None:
        return {
            "status": "unknown",
            "reason": "no card paste has ever been imported",
            "account_id": account["account_id"],
            "account_name": account["account_name"],
        }

    covered = row["statement_close_date"] or (row["imported_at"][:10] if row["imported_at"] else None)
    age_days = None
    status = "fresh"
    if covered:
        try:
            age_days = (today - dt.date.fromisoformat(covered[:10])).days
        except ValueError:
            age_days = None
        if age_days is not None and age_days > CYCLE_STALE_DAYS:
            status = "stale"
    return {
        "status": status,
        "account_id": account["account_id"],
        "account_name": account["account_name"],
        "last_import_at": row["imported_at"],
        "last_covered_close": row["statement_close_date"],
        "age_days": age_days,
    }


# --- helpers ---------------------------------------------------------------


def _resolve_account(conn: sqlite3.Connection, account_query: str) -> dict[str, Any]:
    """Fuzzy-resolve account_query against accounts.name/org (manual_balance rules).

    Requires an unambiguous match: below ``_MATCH_FLOOR`` -> not_found; a tie
    within ``_TIE_BAND`` -> ambiguous (writes nothing). A pasted Citi/Amex export
    therefore cannot silently land on the Apple Card account.
    """

    rows = conn.execute("SELECT id, name, org FROM accounts").fetchall()
    if not rows:
        return {"status": "not_found", "message": "no accounts in database"}
    scored = sorted(
        (
            {
                "account_id": r["id"] if isinstance(r, sqlite3.Row) else r[0],
                "account_name": r["name"] if isinstance(r, sqlite3.Row) else r[1],
                "org": r["org"] if isinstance(r, sqlite3.Row) else r[2],
                "score": _score(
                    account_query,
                    r["name"] if isinstance(r, sqlite3.Row) else r[1],
                    r["org"] if isinstance(r, sqlite3.Row) else r[2],
                ),
            }
            for r in rows
        ),
        key=lambda c: c["score"],
        reverse=True,
    )
    top = scored[0]
    if top["score"] < _MATCH_FLOOR:
        return {"status": "not_found", "message": f"no account matched {account_query!r}"}
    contenders = [c for c in scored if top["score"] - c["score"] <= _TIE_BAND]
    if len(contenders) > 1:
        return {"status": "ambiguous", "candidates": contenders, "message": "multiple accounts matched; refine account_query"}
    return {"status": "ok", **top}


def _plan_promotion(
    conn: sqlite3.Connection,
    account: dict[str, Any],
    statement_total: float | None,
    statement_close_date: str | None,
    as_of_date: str,
) -> dict[str, Any] | None:
    """Decide whether/how to promote the statement total onto a statement instance.

    Promotion requires a total. The target instance is the Apple Card
    statement-payment instance whose ``statement_close_date`` matches the import
    (exact cycle); failing an exact match, the earliest unprotected estimated
    statement instance on/after ``as_of_date`` (the next open cycle). An already
    observed/protected instance reports ``skipped_protected``.
    """

    if statement_total is None:
        return None

    obligation_id = APPLE_STATEMENT_OBLIGATION_ID
    inst = None
    if statement_close_date:
        inst = conn.execute(
            """
            SELECT id, amount, amount_status, amount_source, due_date, statement_close_date
            FROM obligation_instances
            WHERE obligation_id = ? AND statement_close_date = ?
            ORDER BY due_date LIMIT 1
            """,
            (obligation_id, statement_close_date),
        ).fetchone()
    if inst is None:
        inst = conn.execute(
            """
            SELECT id, amount, amount_status, amount_source, due_date, statement_close_date
            FROM obligation_instances
            WHERE obligation_id = ? AND statement_close_date IS NOT NULL
              AND amount_status = 'estimated' AND due_date >= ?
            ORDER BY due_date LIMIT 1
            """,
            (obligation_id, as_of_date),
        ).fetchone()

    if inst is None:
        return {"action": "no_instance", "message": f"no statement instance found for {obligation_id}"}

    from .statements import PROTECTED_AMOUNT_SOURCES

    if inst["amount_status"] != "estimated" or (inst["amount_source"] or "") in PROTECTED_AMOUNT_SOURCES:
        return {
            "action": "skipped_protected",
            "instance_id": inst["id"],
            "statement_close_date": inst["statement_close_date"],
            "current_amount": round(float(inst["amount"]), 2),
            "message": "statement already confirmed/observed; not overwritten",
        }

    return {
        "action": "promote",
        "instance_id": inst["id"],
        "statement_close_date": inst["statement_close_date"],
        "from_amount": round(float(inst["amount"]), 2),
        "to_amount": round(abs(float(statement_total)), 2),
        "applied": False,
    }


def _insert_paste_transaction(
    conn: sqlite3.Connection, account_id: str, txn: dict[str, Any], now: str, source: str = PASTE_SOURCE
) -> None:
    """Insert one pasted txn as a transactions row (ISO dates, source=paste).

    New rows only: dedup already filtered out ids that exist, and a re-paste
    reproduces the same id so this path never double-inserts. ``source`` tags the
    lane (``apple_card_paste`` for cards, ``checking_paste`` for checking).
    """

    posted = f"{txn['transacted_date']}T00:00:00"
    conn.execute(
        """
        INSERT INTO transactions (
            id, account_id, posted, transacted_at, amount, payee, description,
            pending, source, first_seen_at, last_seen_at, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        """,
        (
            txn["id"], account_id, posted, posted, txn["amount"],
            txn["merchant"], txn["description"], source, now, now, now,
        ),
    )


def _render_preview(result: dict[str, Any]) -> str:
    tag = "DRY RUN" if result["dry_run"] else "APPLIED"
    acct = result.get("account") or {}
    lines = [
        f"import_card_statement ({tag}) - {acct.get('account_name', '?')}",
        f"format: {result['format']}   rows parsed: {result['rows_parsed']}   "
        f"spend rows: {result['spend_rows']}   payments/credits: {result['payment_credit_rows']}",
        f"new: {result['new']}   duplicate (already imported): {result['duplicate']}",
        f"cycle spend total: {_money(result['cycle_spend_total'])}   "
        f"statement_total (parsed): {_money(result['statement_total']) if result['statement_total'] is not None else 'n/a'}",
        f"account match: \"{acct.get('account_name','?')}\" ({acct.get('org','')})  score {acct.get('score')}",
    ]
    promo = result.get("promotion")
    if promo and promo.get("action") == "promote":
        verb = "set" if not result["dry_run"] else "would set"
        lines.append(
            f"statement instance: {promo.get('statement_close_date')} estimated "
            f"{_money(promo['from_amount'])} -> {verb} observed {_money(promo['to_amount'])}"
        )
    elif promo and promo.get("action") == "skipped_protected":
        lines.append(f"statement instance: {promo.get('statement_close_date')} already confirmed - skipped_protected")
    elif promo and promo.get("action") == "no_instance":
        lines.append(f"statement instance: none found for {APPLE_STATEMENT_OBLIGATION_ID}")
    for w in result.get("warnings", []):
        lines.append(f"note: {w}")
    lines.append("Re-run with dry_run=false to write." if result["dry_run"] else "Written.")
    return "\n".join(lines)


def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _merchant_slug(merchant: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (merchant or "").lower())


def _first_match(fields: dict[str, str], names: tuple[str, ...]) -> str | None:
    for n in names:
        if n in fields:
            return fields[n]
    return None


def _parse_money(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = raw.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _to_iso_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")
