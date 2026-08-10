"""
Runtime environment detection and the sandbox safety guard.

The single most important property of this module: **it fails closed.**

`AIS_FMD_ENV` must be set to exactly "production" to leave sandbox mode.
Anything else -- unset, empty, misspelled, garbage -- resolves to SANDBOX.
There is no way to end up talking to a real database by accident.

In sandbox mode:
  * `data.client` refuses to construct a Supabase client at all.
  * `domain.categorize.llm` refuses to make a network call and uses the
    deterministic offline stub instead.
  * Every write goes to a local SQLite file that is created on demand and
    can be deleted without consequence.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path


class Env(str, Enum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class SandboxViolation(RuntimeError):
    """Raised when sandbox code attempts to reach a real external service."""


ENV_VAR = "AIS_FMD_ENV"
_PRODUCTION_TOKEN = "production"


def get_env() -> Env:
    """Resolve the active environment. Anything but an exact match is sandbox."""
    raw = os.environ.get(ENV_VAR, "")
    if raw.strip().lower() == _PRODUCTION_TOKEN:
        return Env.PRODUCTION
    return Env.SANDBOX


def is_sandbox() -> bool:
    return get_env() is Env.SANDBOX


def is_production() -> bool:
    return get_env() is Env.PRODUCTION


def assert_external_call_allowed(what: str) -> None:
    """Gate every outbound network call in the app through this."""
    if is_sandbox():
        raise SandboxViolation(
            f"Blocked in sandbox mode: {what}. "
            f"The sandbox never contacts external services. "
            f"Set {ENV_VAR}=production to enable this (and install the "
            f"'supabase'/'openai' packages, which the sandbox venv omits)."
        )


# --- Paths -------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sandbox_db_path() -> Path:
    """Location of the local SQLite database backing sandbox mode."""
    override = os.environ.get("AIS_FMD_SANDBOX_DB")
    if override:
        return Path(override)
    return PROJECT_ROOT / "sandbox_data" / "ais_fmd_sandbox.db"


# --- LLM configuration -------------------------------------------------------

def llm_enabled() -> bool:
    """
    True only when a real model call is both permitted and configured.

    Sandbox mode always returns False, so `pipeline.categorize()` transparently
    uses the offline heuristic and costs nothing.
    """
    if is_sandbox():
        return os.environ.get("AIS_FMD_ALLOW_LLM_IN_SANDBOX", "").strip() == "1"
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def llm_model() -> str:
    """Small model by default -- residual classification does not need a large one."""
    return os.environ.get("AIS_FMD_LLM_MODEL", "gpt-4.1-mini")


def llm_batch_size() -> int:
    try:
        return max(1, int(os.environ.get("AIS_FMD_LLM_BATCH_SIZE", "40")))
    except ValueError:
        return 40


def banner_text() -> str:
    if is_sandbox():
        return (
            "SANDBOX — local SQLite database, no network calls, no real money. "
            "Nothing here can reach Supabase or GitHub."
        )
    return "PRODUCTION — writes affect the live database."
