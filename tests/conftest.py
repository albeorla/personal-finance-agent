"""Shared test setup.

The operating/working checking account is identified by a name hint pulled from
config (``WORKING_ACCOUNT_HINT``) rather than a hardcoded number in source. The
fixtures in this suite use a synthetic last-4 (``4321``); this autouse fixture
injects that hint and points the config loader at a nonexistent .env so tests
never read the developer's real finances .env.
"""

from __future__ import annotations

import pytest

WORKING_ACCOUNT_HINT = "4321"


@pytest.fixture(autouse=True)
def _working_account_hint(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCE_AGENT_ENV", str(tmp_path / "nonexistent.env"))
    monkeypatch.setenv("WORKING_ACCOUNT_HINT", WORKING_ACCOUNT_HINT)
    yield


@pytest.fixture(autouse=True)
def _adversarial_gate_off(monkeypatch):
    """Pin the non-deterministic adversarial gate OFF for the whole suite.

    The gate is env-driven (``FINANCE_AGENT_ADVERSARIAL`` truthy AND the
    ``claude`` CLI on PATH). If a developer exports the flag to enable the daily
    feature and then runs the suite, the default background sequence would gain
    an ``adversarial_review`` event and a real ``claude`` subprocess could be
    spawned mid-test. Clearing the env here makes the deterministic default
    independent of the developer's shell; tests opt in explicitly via
    ``options["adversarial"]`` with an injected fake runner.
    """

    monkeypatch.delenv("FINANCE_AGENT_ADVERSARIAL", raising=False)
    monkeypatch.delenv("FINANCE_AGENT_ADVERSARIAL_MODEL", raising=False)
    monkeypatch.delenv("FINANCE_AGENT_ADVERSARIAL_TIMEOUT", raising=False)
    yield
