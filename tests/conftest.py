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
