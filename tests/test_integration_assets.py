"""Presence/smoke check for the staged Claude Code integration assets (slice T).

These are drafted in-repo for the user to install into the finances workspace
when ready; this test just guards that they exist and stay well-formed and
consistent with the actual server (entry point + tool names).
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / "claude-integration"


def test_finance_skill_present_and_well_formed():
    skill = (CI / "finance-skill" / "SKILL.md").read_text()
    assert skill.startswith("---") and "name: finance" in skill
    # references real tools from the ritual
    for tool in ("get_daily_digest", "confirm_reconciliation_match", "compare_to_legacy", "run_background_sync"):
        assert tool in skill


def test_instructions_block_present():
    txt = (CI / "finance-instructions.md").read_text().lower()
    assert "source of truth" in txt
    assert "must call a finance mcp tool" in txt or "must use" in txt


def test_mcp_registration_valid_and_matches_entry_point():
    data = json.loads((CI / "mcp-registration.json").read_text())
    entry = data["mcpServers"]["financial-agent"]
    assert entry["command"] == "uv"
    # the script name must match pyproject [project.scripts]
    assert "financial-agent-mcp" in entry["args"]
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'financial-agent-mcp = "financial_agent.server:main"' in pyproject


def test_correction_readback_rule_present():
    # IMP-20260710-1: a corrected/claimed account fact must be re-read live from a
    # finance tool before deleting a reminder, completing a task, or writing memory.
    skill = " ".join((CI / "finance-skill" / "SKILL.md").read_text().lower().split())
    instr = " ".join((CI / "finance-instructions.md").read_text().lower().split())
    for text in (skill, instr):
        assert "re-read that account's live state" in text
        assert "before acting on the correction" in text


def test_install_readme_present():
    txt = (CI / "INSTALL.md").read_text()
    assert "parallel-run" in txt.lower()
    assert "claude mcp add" in txt
