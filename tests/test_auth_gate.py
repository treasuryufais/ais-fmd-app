"""
The production login gate.

This is the only thing standing between the internet and the organisation's
financial history, so the tests that matter are the ones asserting it **fails
closed**: no password configured, wrong password, and blank password must all
deny. A gate that opens when it is misconfigured is worse than no gate, because
it looks like security.

The sandbox path is asserted separately -- it must stay open and role-switchable
so the app is still usable with no setup at all.
"""

from __future__ import annotations

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from ais_fmd import auth, settings

PASSWORD = "correct-horse-battery-staple"


def gate_script(secrets: dict | None) -> str:
    """A minimal app that is nothing but the gate, plus a marker after it."""
    return f"""
import streamlit as st
from ais_fmd import auth

auth.login_gate()
st.write("PAST_THE_GATE")
st.write(auth.current_user().email)
"""


def run_gate(monkeypatch, *, production: bool, secrets: dict) -> AppTest:
    monkeypatch.setenv(settings.ENV_VAR, "production" if production else "sandbox")
    app = AppTest.from_string(gate_script(secrets))
    app.secrets.update(secrets)
    return app.run()


# --- Fails closed ------------------------------------------------------------

def test_no_password_configured_denies_everyone(monkeypatch):
    """A missing secret must not read as 'no password required'."""
    app = run_gate(monkeypatch, production=True, secrets={})
    assert not any("PAST_THE_GATE" in str(m.value) for m in app.markdown)
    assert app.error, "expected a configuration error to be shown"
    assert "Not configured" in app.error[0].value


def test_blank_password_configured_denies_everyone(monkeypatch):
    app = run_gate(monkeypatch, production=True, secrets={"treasury": {"password": "   "}})
    assert app.error
    assert "Not configured" in app.error[0].value


def test_gate_blocks_before_any_page_renders(monkeypatch):
    app = run_gate(monkeypatch, production=True, secrets={"treasury": {"password": PASSWORD}})
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" not in rendered


def test_wrong_password_is_rejected(monkeypatch):
    app = run_gate(monkeypatch, production=True, secrets={"treasury": {"password": PASSWORD}})
    app.text_input[0].set_value("not-the-password")
    app.button[0].click().run()
    assert app.error
    assert "Incorrect" in app.error[0].value
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" not in rendered


def test_empty_submission_is_rejected(monkeypatch):
    """Submitting nothing must not match a configured password."""
    app = run_gate(monkeypatch, production=True, secrets={"treasury": {"password": PASSWORD}})
    app.button[0].click().run()
    assert app.error
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" not in rendered


# --- Opens for the right password --------------------------------------------

def test_correct_password_admits(monkeypatch):
    app = run_gate(monkeypatch, production=True, secrets={"treasury": {"password": PASSWORD}})
    app.text_input[0].set_value(PASSWORD).run()
    app.button[0].click().run()
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" in rendered


def test_operator_email_is_used_when_configured(monkeypatch):
    app = run_gate(
        monkeypatch,
        production=True,
        secrets={"treasury": {"password": PASSWORD, "operator_email": "nic@example.edu"}},
    )
    app.text_input[0].set_value(PASSWORD).run()
    app.button[0].click().run()
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "nic@example.edu" in rendered


def test_signed_in_operator_is_admin(monkeypatch):
    monkeypatch.setenv(settings.ENV_VAR, "production")
    identity = auth.Identity(email="x@example.edu", role=auth.Role.ADMIN)
    assert identity.can(auth.Role.TREASURER)
    assert identity.can(auth.Role.ADMIN)


# --- Sandbox stays open ------------------------------------------------------

def test_sandbox_needs_no_password(monkeypatch):
    app = run_gate(monkeypatch, production=False, secrets={})
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" in rendered


def test_sandbox_identity_is_still_fabricated(monkeypatch):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from ais_fmd import auth\n"
        "st.write(auth.current_user().role.label)\n"
    ).run()
    assert "Treasurer" in " ".join(str(m.value) for m in app.markdown)


# --- current_user contract ---------------------------------------------------

def test_current_user_refuses_to_fabricate_in_production(monkeypatch):
    """
    Skipping the gate must raise, not silently mint an identity.

    A default here would mean forgetting one call in app.py hands every visitor
    a working session.
    """
    monkeypatch.setenv(settings.ENV_VAR, "production")
    app = AppTest.from_string(
        "from ais_fmd import auth\n"
        "auth.current_user()\n"
    ).run()
    assert app.exception, "expected current_user() to raise without a gate"


def test_role_switcher_is_absent_in_production(monkeypatch):
    """The client must never be able to choose its own role in production."""
    monkeypatch.setenv(settings.ENV_VAR, "production")
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from ais_fmd import auth\n"
        "auth.set_identity(auth.Identity('x@example.edu', auth.Role.ADMIN))\n"
        "auth.role_switcher()\n"
        "st.write('done')\n"
    ).run()
    assert not app.sidebar.selectbox, "role switcher must not render in production"
