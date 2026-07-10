"""Tests for the non-deterministic adversarial review phase.

The reviewer itself is a spawned ``claude`` subprocess, so every test here drives
the code through the injectable ``runner`` seam (or calls the pure helpers
directly). No test invokes the real CLI: the gate is pinned OFF by the autouse
fixture in conftest, and the integration tests force the step on with a fake
runner. The properties locked down: the default background sequence is unchanged
when the gate is off; a forced-on run lands the step in the exact slot and runs
the injected runner (never a subprocess); the two producers never resolve each
other's rows; envelope parsing is defensive; the runner fails open; and the CLI
entry point is safe with the gate off.
"""

from __future__ import annotations

import json
import sqlite3

import financial_agent.adversarial as adversarial
from financial_agent.adversarial import (
    _claude_runner,
    _normalize_findings,
    _parse_claude_output,
    main,
    run_adversarial_review,
)
from financial_agent.background import get_background_run, run_background_sync
from financial_agent.schema import ensure_app_schema
from financial_agent.verification import SEVERITY_ERROR, SEVERITY_WARN, list_verification_findings

# Reuse the deterministic background-suite fixtures: same DB seed, same expected
# default sequence, so the "gate off => byte-for-byte unchanged" property is
# asserted against the real source of truth.
from test_background import _EXPECTED_SEQUENCE, _db
from test_verification import _clean_db


def test_default_advisor_model_is_fable():
    assert adversarial._default_model() == "fable"


def _seed_candidate(conn, *, candidate_id="cand-1", merchant="acme"):
    """Insert one active recurring-charge candidate so _gather_targets is non-empty."""

    conn.execute(
        """
        INSERT INTO charge_onboarding_candidates (
            id, merchant_key, display_name, direction, status, candidate_type,
            cash_flow_treatment, confidence, priority_score, evidence_count,
            evidence_summary_json, missing_evidence_json, first_evidence_date,
            last_evidence_date, notes, created_at, updated_at
        ) VALUES (?, ?, ?, 'outflow', 'discovered', 'subscription', 'discretionary',
                  'low', 9.0, 3, NULL, NULL, '2026-01-01', '2026-06-01', NULL,
                  '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z')
        """,
        (candidate_id, merchant, merchant.title()),
    )
    conn.commit()


def _seed_open_finding(conn, *, source, check_id, evidence_json, severity=SEVERITY_ERROR):
    conn.execute(
        """
        INSERT INTO verification_findings (
            id, run_id, check_id, severity, title, detail, evidence_json,
            as_of_date, status, source, created_at
        ) VALUES (?, NULL, ?, ?, 'seeded', 'seeded', ?, '2026-06-30', 'open', ?, '2026-06-01T00:00:00Z')
        """,
        (f"seed_{source}_{check_id}", check_id, severity, evidence_json, source),
    )
    conn.commit()


def _minimal_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    return conn


def _high_finding_runner(*, targets, model):
    return {
        "ok": True,
        "error": None,
        "findings": [
            {
                "area": "recurring_candidate",
                "subject": "acme",
                "severity": "high",
                "title": "Classification looks wrong",
                "why": "Evidence count is low for a confident classification.",
                "what_to_check": "Confirm the merchant truly recurs monthly.",
            }
        ],
        "reviewed_count": 1,
        "model": model,
    }


def _clean_runner(*, targets, model):
    return {"ok": True, "error": None, "findings": [], "reviewed_count": 1, "model": model}


def _two_concerns_same_subject_runner(*, targets, model):
    # Two DISTINCT concerns about the same (area, subject), different severities.
    return {
        "ok": True,
        "error": None,
        "findings": [
            {
                "area": "recurring_candidate",
                "subject": "acme",
                "severity": "high",
                "title": "Classification looks wrong",
                "why": "Evidence count is low.",
                "what_to_check": "Confirm it recurs.",
            },
            {
                "area": "recurring_candidate",
                "subject": "acme",
                "severity": "low",
                "title": "Display name differs from merchant key",
                "why": "Cosmetic mismatch.",
                "what_to_check": "Normalize the label.",
            },
        ],
        "reviewed_count": 1,
        "model": model,
    }


# --- gating / injected-runner path (the determinism guarantee) -------------


def test_default_run_has_no_adversarial_step(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    result = run_background_sync(conn, as_of_date="2026-06-30")

    assert "adversarial_review" not in result["result_summary"]
    run = get_background_run(conn, result["run_id"])
    events = [e["event_type"] for e in run["events"]]
    assert events == _EXPECTED_SEQUENCE
    assert "adversarial_review" not in events


def test_forced_on_inserts_step_in_exact_slot_with_injected_runner(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    calls: list[dict] = []

    def fake(*, targets, model):
        calls.append({"targets": targets, "model": model})
        return _clean_runner(targets=targets, model=model)

    result = run_background_sync(
        conn,
        as_of_date="2026-06-30",
        options={"adversarial": {"enabled": True, "runner": fake}},
    )

    run = get_background_run(conn, result["run_id"])
    events = [e["event_type"] for e in run["events"]]
    assert "adversarial_review" in events
    # Lands between 'verify' and 'surface_due_items'.
    assert events.index("verify") + 1 == events.index("adversarial_review")
    assert events.index("adversarial_review") + 1 == events.index("surface_due_items")
    # The injected fake is what ran (no real subprocess).
    assert len(calls) == 1
    assert result["result_summary"]["adversarial_review"]["available"] is True


def test_forced_on_runner_that_raises_fails_open(tmp_path):
    conn = _db(tmp_path / "b.sqlite")

    def boom(*, targets, model):
        raise RuntimeError("reviewer exploded")

    result = run_background_sync(
        conn,
        as_of_date="2026-06-30",
        options={"adversarial": {"enabled": True, "runner": boom}},
    )

    # The run still finishes; the broken reviewer is recorded as unavailable.
    assert result["status"] == "succeeded_with_warnings"
    summary = result["result_summary"]["adversarial_review"]
    assert summary["available"] is False
    assert summary["skipped"]


# --- persistence + cross-source isolation ----------------------------------


def test_high_finding_persists_as_error_and_isolates_deterministic(tmp_path):
    conn = _minimal_db(tmp_path / "m.sqlite")
    _seed_candidate(conn)
    _seed_open_finding(
        conn,
        source="deterministic",
        check_id="det_check",
        evidence_json='{"k": "v"}',
        severity=SEVERITY_ERROR,
    )

    result = run_adversarial_review(
        conn, as_of_date="2026-06-30", runner=_high_finding_runner
    )
    conn.commit()

    assert result["available"] is True
    assert result["findings_total"] == 1

    adv = list_verification_findings(conn, source="adversarial", status="open")
    assert len(adv) == 1
    assert adv[0]["check_id"] == "adversarial:recurring_candidate"
    assert adv[0]["severity"] == SEVERITY_ERROR  # reviewer 'high' -> 'error'
    assert adv[0]["evidence"] == {"area": "recurring_candidate", "subject": "acme"}

    # The deterministic-open row is untouched by the adversarial reconciler.
    det = list_verification_findings(conn, source="deterministic", status="open")
    assert len(det) == 1
    assert det[0]["check_id"] == "det_check"


def test_distinct_concerns_same_subject_persist_separately(tmp_path):
    conn = _minimal_db(tmp_path / "m.sqlite")
    _seed_candidate(conn)

    # Two concerns about the same subject must NOT collapse, and the high-severity
    # one must not be masked by the warn.
    result = run_adversarial_review(
        conn, as_of_date="2026-06-30", runner=_two_concerns_same_subject_runner
    )
    conn.commit()
    assert result["findings_total"] == 2

    rows = list_verification_findings(conn, source="adversarial", status="open")
    assert len(rows) == 2
    sevs = sorted(r["severity"] for r in rows)
    assert sevs == [SEVERITY_ERROR, SEVERITY_WARN]  # both preserved, neither masked

    # Re-running the same two concerns is stable: no duplicate rows.
    run_adversarial_review(
        conn, as_of_date="2026-07-01", runner=_two_concerns_same_subject_runner
    )
    conn.commit()
    assert len(list_verification_findings(conn, source="adversarial", status="open")) == 2


def test_clean_review_resolves_only_adversarial_rows(tmp_path):
    conn = _minimal_db(tmp_path / "m.sqlite")
    _seed_candidate(conn)
    _seed_open_finding(
        conn,
        source="adversarial",
        check_id="adversarial:recurring_candidate",
        evidence_json='{"area": "recurring_candidate", "subject": "stale"}',
        severity=SEVERITY_WARN,
    )
    _seed_open_finding(
        conn,
        source="deterministic",
        check_id="det_check",
        evidence_json='{"k": "v"}',
        severity=SEVERITY_ERROR,
    )

    run_adversarial_review(conn, as_of_date="2026-06-30", runner=_clean_runner)
    conn.commit()

    # Stale adversarial row is resolved; deterministic row is left open.
    assert list_verification_findings(conn, source="adversarial", status="open") == []
    det = list_verification_findings(conn, source="deterministic", status="open")
    assert len(det) == 1


# --- defensive envelope parsing --------------------------------------------


def test_parse_claude_output_envelope_shapes():
    # 1. result carries a JSON string.
    body = '{"findings": [{"area": "a"}], "reviewed_count": 2}'
    assert _parse_claude_output(json.dumps({"result": body})) == {
        "findings": [{"area": "a"}],
        "reviewed_count": 2,
    }
    # 2. structured_output carries the object directly.
    assert _parse_claude_output(
        '{"structured_output": {"findings": [], "reviewed_count": 0}}'
    ) == {"findings": [], "reviewed_count": 0}
    # 3. the envelope itself is the finding object.
    assert _parse_claude_output('{"findings": [], "reviewed_count": 0}') == {
        "findings": [],
        "reviewed_count": 0,
    }
    # 4. garbage / unrecognized shapes return None.
    assert _parse_claude_output("not json") is None
    assert _parse_claude_output('{"result": "just prose, no json"}') is None
    assert _parse_claude_output("") is None


# --- runner fail-open paths -------------------------------------------------


def test_runner_unavailable_when_binary_missing(monkeypatch):
    monkeypatch.setattr(adversarial.shutil, "which", lambda _name: None)
    out = _claude_runner(targets={"recurring_candidates": [{"x": 1}]}, model="sonnet")
    assert out["ok"] is False
    assert out["error"]


def test_runner_fails_open_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(adversarial.shutil, "which", lambda _name: "/usr/bin/claude")

    class _Proc:
        returncode = 2
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(adversarial.subprocess, "run", lambda *a, **k: _Proc())
    out = _claude_runner(targets={"recurring_candidates": [{"x": 1}]}, model="sonnet")
    assert out["ok"] is False
    assert "exited 2" in out["error"]


def test_runner_fails_open_on_file_not_found(monkeypatch):
    monkeypatch.setattr(adversarial.shutil, "which", lambda _name: "/usr/bin/claude")

    def _raise(*a, **k):
        raise FileNotFoundError("no claude")

    monkeypatch.setattr(adversarial.subprocess, "run", _raise)
    out = _claude_runner(targets={"recurring_candidates": [{"x": 1}]}, model="sonnet")
    assert out["ok"] is False
    assert out["error"]


def test_runner_child_env_has_gate_flag_stripped(monkeypatch):
    monkeypatch.setattr(adversarial.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setenv("FINANCE_AGENT_ADVERSARIAL", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    captured = {}

    class _Proc:
        returncode = 0
        stdout = '{"result": {"findings": [], "reviewed_count": 0}}'
        stderr = ""

    def _run(*a, **k):
        captured["env"] = k["env"]
        return _Proc()

    monkeypatch.setattr(adversarial.subprocess, "run", _run)
    _claude_runner(targets={"recurring_candidates": [{"x": 1}]}, model="sonnet")
    # No gate flag in the child -> its Stop hook short-circuits, no recursion.
    assert "FINANCE_AGENT_ADVERSARIAL" not in captured["env"]
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert captured["env"].get("FINANCE_AGENT_ADVERSARIAL_CHILD") == "1"


# --- normalization details --------------------------------------------------


def test_normalize_maps_severity_and_keeps_identity():
    findings = _normalize_findings(
        [
            {"area": "trough_driver", "subject": "rent", "severity": "high",
             "title": "t", "why": "w", "what_to_check": "c"},
            {"area": "estimated_obligation", "subject": "tax", "severity": "low",
             "title": "t2", "why": "w2", "what_to_check": "c2"},
            "not a dict",
        ]
    )
    assert [f["severity"] for f in findings] == [SEVERITY_ERROR, SEVERITY_WARN]
    assert findings[0]["check_id"] == "adversarial:trough_driver"
    assert findings[0]["evidence"] == {"area": "trough_driver", "subject": "rent"}


# --- command builder: exact argv, OAuth env, no --bare ---------------------


def test_real_runner_builds_expected_argv(monkeypatch):
    monkeypatch.setattr(adversarial.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    captured = {}

    class _Proc:
        returncode = 0
        stdout = '{"result": {"findings": [], "reviewed_count": 0}}'
        stderr = ""

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return _Proc()

    monkeypatch.setattr(adversarial.subprocess, "run", _run)
    out = _claude_runner(targets={"recurring_candidates": [{"x": 1}]}, model="sonnet")
    assert out["ok"] is True

    cmd = captured["cmd"]
    assert cmd[0] == "claude"
    assert cmd[1] == "-p"
    # --bare would skip OAuth and force an API key: it must never be present.
    assert "--bare" not in cmd
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    # Subscription/OAuth auth is forced: no API key reaches the child.
    assert "ANTHROPIC_API_KEY" not in captured["env"]


# --- digest block: read-only, no spawn -------------------------------------


def test_digest_block_reads_persisted_rows_without_spawning(tmp_path, monkeypatch):
    from financial_agent.digest import build_daily_digest

    db = str(tmp_path / "digest.sqlite")
    conn = _clean_db(db)
    conn.execute(
        "CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, "
        "accounts_seen INT, transactions_inserted INT, transactions_updated INT, error TEXT)"
    )
    conn.execute(
        "INSERT INTO sync_runs (started_at,finished_at,mode,accounts_seen,transactions_inserted,"
        "transactions_updated,error) VALUES ('2026-06-20T09:58:00+00:00','2026-06-20T10:00:00+00:00','i',1,0,0,NULL)"
    )
    _seed_open_finding(
        conn,
        source="adversarial",
        check_id="adversarial:recurring_candidate",
        evidence_json='{"area": "recurring_candidate", "subject": "acme"}',
        severity=SEVERITY_WARN,
    )
    conn.commit()
    conn.close()

    # Any attempt to spawn the reviewer during a digest read is a bug; make it loud.
    def _boom(*a, **k):
        raise AssertionError("digest must never spawn the reviewer")

    monkeypatch.setattr(adversarial, "_claude_runner", _boom)

    digest = build_daily_digest(db, as_of_date="2026-06-20")
    block = digest["adversarial_review"]
    assert block["advisory"] is True
    assert block["findings_total"] == 1
    assert block["ok"] is False
    assert block["findings"][0]["source"] == "adversarial"


# --- CLI entry point safe with the gate off --------------------------------


def test_main_disabled_prints_and_returns_zero(capsys, monkeypatch):
    # Gate is already cleared by the autouse fixture; force claude "missing" too
    # so the test is independent of whether the CLI is installed.
    monkeypatch.setattr(adversarial.shutil, "which", lambda _name: None)
    rc = main(["--as-of", "2026-06-30"])
    assert rc == 0
    assert "disabled" in capsys.readouterr().out
