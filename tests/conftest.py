"""
Shared test configuration.

Two guarantees enforced for every test:
  * sandbox mode, so nothing can reach a real service
  * an isolated database path, so tests never touch the sandbox data the user
    is browsing in the app
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_sandbox(monkeypatch, tmp_path):
    monkeypatch.delenv("AIS_FMD_ENV", raising=False)
    monkeypatch.delenv("AIS_FMD_ALLOW_LLM_IN_SANDBOX", raising=False)
    monkeypatch.setenv("AIS_FMD_SANDBOX_DB", str(tmp_path / "isolated.db"))
    yield
