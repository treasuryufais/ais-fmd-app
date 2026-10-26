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


# --- Where configuration comes from ------------------------------------------
#
# LAUNCH BUG, fixed here. Every setting below used to read `os.environ` only.
# That is correct on a host where you can set real environment variables, and
# silently wrong on Streamlit Community Cloud, where you cannot -- there the
# only channel is `.streamlit/secrets.toml`.
#
# Streamlit *does* copy top-level secrets into `os.environ`, which is why this
# looked like it worked. But it does that lazily, on the first access to
# `st.secrets`, and `app.py` asked `is_sandbox()` before anything had touched
# `st.secrets`. So `AIS_FMD_ENV` read empty, the fail-closed default returned
# SANDBOX, and a production deployment would quietly serve generated demo data
# with the login gate skipped entirely. No error, no warning.
#
# Reading both sources explicitly removes the ordering dependency and works
# unchanged on either kind of host. A real environment variable wins, because
# setting one is the more deliberate act; secrets are the fallback.
#
# `st.secrets` is imported inside the function, not at module scope, so this
# module stays importable by the scripts in `scripts/` that run outside a
# Streamlit process -- and so a missing or malformed secrets file is a fallback,
# never a crash.

def _setting(name: str, default: str = "") -> str:
    """One configuration value, from the environment or from Streamlit secrets."""
    value = os.environ.get(name)
    if value:
        return value

    try:
        import streamlit as st

        found = st.secrets.get(name)
    except Exception:  # noqa: BLE001 - no secrets file at all is a valid state
        return default
    return str(found) if found is not None else default


def secret_section(name: str) -> dict:
    """
    A `[section]` from secrets.toml as a plain dict, or empty.

    Sections are the other half of the same bug: Streamlit copies only top-level
    *scalars* into `os.environ`, never the members of a section. So a
    `[supabase]` block in secrets.toml could never satisfy code reading
    `os.environ["SUPABASE_URL"]`, which is exactly what the project's own
    `secrets.toml.example` told people to write.
    """
    try:
        import streamlit as st

        section = st.secrets.get(name, {})
    except Exception:  # noqa: BLE001
        return {}
    try:
        return dict(section)
    except (TypeError, ValueError):
        return {}


class Env(str, Enum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class SandboxViolation(RuntimeError):
    """Raised when sandbox code attempts to reach a real external service."""


ENV_VAR = "AIS_FMD_ENV"
_PRODUCTION_TOKEN = "production"


def get_env() -> Env:
    """Resolve the active environment. Anything but an exact match is sandbox."""
    raw = _setting(ENV_VAR)
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
    override = _setting("AIS_FMD_SANDBOX_DB")
    if override:
        return Path(override)
    return PROJECT_ROOT / "sandbox_data" / "ais_fmd_sandbox.db"


# --- Login gate in sandbox ---------------------------------------------------

def sandbox_login_enabled() -> bool:
    """
    Run the real login gate in sandbox mode, against the SQLite `profiles`.

    Off by default, so the sandbox stays open and role-switchable. On, it is
    the only way to try Google sign-in end to end without a production
    database: the gate, `profiles` lookup and sign-out all run for real.
    """
    return is_sandbox() and _setting("AIS_FMD_SANDBOX_LOGIN").strip() == "1"


# --- LLM configuration -------------------------------------------------------

def llm_enabled() -> bool:
    """
    True only when a real model call is both permitted and configured.

    Sandbox mode always returns False, so `pipeline.categorize()` transparently
    uses the offline heuristic and costs nothing.
    """
    if is_sandbox():
        return _setting("AIS_FMD_ALLOW_LLM_IN_SANDBOX").strip() == "1"
    return bool(_setting("OPENAI_API_KEY").strip())


def llm_model() -> str:
    """Small model by default -- residual classification does not need a large one."""
    return _setting("AIS_FMD_LLM_MODEL", "gpt-4.1-mini")


def llm_batch_size() -> int:
    try:
        return max(1, int(_setting("AIS_FMD_LLM_BATCH_SIZE", "40")))
    except ValueError:
        return 40


def banner_text() -> str:
    if is_sandbox():
        return (
            "SANDBOX — local SQLite database, no network calls, no real money. "
            "Nothing here can reach Supabase or GitHub."
        )
    return "PRODUCTION — writes affect the live database."
