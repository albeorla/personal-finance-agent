"""Tests for obligation migration from legacy sources (slice H)."""

import json
import os
import sqlite3

import pytest

from financial_agent.migration import (
    apply_obligation_migration,
    parse_cashflow_md,
    parse_obligations_yaml,
)
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def _yaml(path, items):
    with open(path, "w") as fh:
        json.dump({"working_account_id": "XXXX", "items": items}, fh)
    return str(path)


def test_parse_obligations_yaml_normalizes(tmp_path):
    p = _yaml(tmp_path / "o.yaml", [
        {"date": "2026-07-03", "label": "Partner pay", "amount": 2011.67, "source": "payroll"},
        {"date": "2026-07-31", "label": "Volvo disposition fee", "amount": -700.0, "source": "estimate"},
    ])
    rows = parse_obligations_yaml(p)
    assert rows[0]["direction"] == "inflow" and rows[0]["amount"] == 2011.67
    assert rows[1]["direction"] == "outflow" and rows[1]["amount"] == 700.0
    assert rows[1]["needs_review"] is True  # "estimate" in source


def test_dry_run_writes_nothing(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    p = _yaml(tmp_path / "o.yaml", [{"date": "2026-07-31", "label": "Volvo wear and tear", "amount": -712.0, "source": "verbal"}])
    res = apply_obligation_migration(conn, source="obligations_yaml", path=p, dry_run=True)
    assert res["obligations_to_create"] == 1
    assert res["created_obligations"] == 0
    assert conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0] == 0


def test_dedup_skips_already_modeled(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "income_partner", "name": "Partner pay", "kind": "income",
                    "cadence": "biweekly", "status": "active", "source": "seed"},
        instances=[{"id": "income_partner:2026-07-03", "due_date": "2026-07-03", "amount": 2011.67,
                    "direction": "inflow", "source": "seed"}],
    )
    p = _yaml(tmp_path / "o.yaml", [{"date": "2026-07-03", "label": "Partner pay", "amount": 2011.67, "source": "payroll"}])
    res = apply_obligation_migration(conn, source="obligations_yaml", path=p, dry_run=False)
    assert res["skipped_already_modeled"] == 1
    assert res["created_obligations"] == 0
    assert conn.execute("SELECT COUNT(*) FROM obligations WHERE id LIKE 'migrated_%'").fetchone()[0] == 0


def test_generic_token_does_not_false_dedup_in_migration(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "federal_tax", "name": "Federal tax", "kind": "tax",
                    "cadence": "annual", "status": "active", "source": "seed"},
        instances=[{"id": "federal_tax:2026-04-15", "due_date": "2026-04-15", "amount": -2969.0,
                    "direction": "outflow", "source": "seed"}],
    )
    p = _yaml(tmp_path / "o.yaml", [{"date": "2026-04-16", "label": "State tax payment", "amount": -2969.0, "source": "verbal"}])
    res = apply_obligation_migration(conn, source="obligations_yaml", path=p, dry_run=False)
    assert res["skipped_already_modeled"] == 0  # "State tax" != "Federal tax"
    assert res["created_obligations"] == 1


def test_new_obligation_is_created(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    p = _yaml(tmp_path / "o.yaml", [{"date": "2026-07-31", "label": "Volvo wear and tear", "amount": -712.0, "source": "verbal"}])
    res = apply_obligation_migration(conn, source="obligations_yaml", path=p, dry_run=False)
    assert res["created_obligations"] == 1
    row = conn.execute("SELECT amount, direction, status FROM obligation_instances WHERE obligation_id='migrated_volvo_wear_and_tear'").fetchone()
    assert (row["amount"], row["direction"], row["status"]) == (712.0, "outflow", "expected")


def test_ambiguous_row_becomes_needs_review(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    p = _yaml(tmp_path / "o.yaml", [{"date": "2026-07-31", "label": "Volvo disposition fee (midpoint estimate; range $400-$1000)", "amount": -700.0, "source": "verbal"}])
    res = apply_obligation_migration(conn, source="obligations_yaml", path=p, dry_run=False)
    assert res["needs_review"] == 1
    status = conn.execute("SELECT status FROM obligation_instances WHERE obligation_id LIKE 'migrated_%'").fetchone()[0]
    assert status == "needs_review"


def test_migration_is_idempotent(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    p = _yaml(tmp_path / "o.yaml", [{"date": "2026-07-31", "label": "Volvo wear and tear", "amount": -712.0, "source": "verbal"}])
    apply_obligation_migration(conn, source="obligations_yaml", path=p, dry_run=False)
    second = apply_obligation_migration(conn, source="obligations_yaml", path=p, dry_run=False)
    assert second["skipped_already_modeled"] == 1
    assert second["created_instances"] == 0
    assert conn.execute("SELECT COUNT(*) FROM obligation_instances WHERE obligation_id LIKE 'migrated_%'").fetchone()[0] == 1


def test_migration_log_recorded(tmp_path):
    conn = _db(tmp_path / "m.sqlite")
    p = _yaml(tmp_path / "o.yaml", [{"date": "2026-07-31", "label": "Volvo wear and tear", "amount": -712.0, "source": "verbal"}])
    apply_obligation_migration(conn, source="obligations_yaml", path=p, dry_run=False)
    log = conn.execute("SELECT source_type, parsed, created_obligations FROM obligation_migration_log").fetchone()
    assert log["source_type"] == "obligations_yaml" and log["parsed"] == 1


def test_cashflow_md_rows_are_needs_review(tmp_path):
    md = tmp_path / "cash-flow.md"
    md.write_text(
        "# Finance\n## Obligations Due (window)\n"
        "| Due | Obligation | Amount | Auto | Notes |\n|-----|-----|-----|-----|-----|\n"
        "| Jun 2 | Garbage (Santaguida) | $48.00 | AUTO | |\n"
        "| Jun 4 | Rent check | $3,000.00 | MANUAL | |\n"
    )
    rows = parse_cashflow_md(str(md), base_year=2026)
    assert len(rows) == 2
    assert all(r["needs_review"] for r in rows)
    assert rows[0]["date"] == "2026-06-02" and rows[0]["amount"] == 48.0
    assert rows[0]["direction"] == "outflow"


def test_real_obligations_yaml_parses_if_present():
    path = os.path.expanduser("~/dev/areas/finances/obligations.yaml")
    if not os.path.exists(path):
        pytest.skip("legacy obligations.yaml not present")
    rows = parse_obligations_yaml(path)
    assert len(rows) > 50  # ~71 items
    assert all("date" in r and "amount" in r and r["direction"] in ("inflow", "outflow") for r in rows)
