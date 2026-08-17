"""
Roles and access control.

FINDING F2. The original had open registration -- anyone reaching the URL could
create an account and read the organisation's complete financial history -- and
gated the treasury portal, which grants unrestricted service-role writes, behind
a single shared password compared with `==`. There were no roles, so there was
no way to give a committee chair sight of their own budget without giving them
everything.

The model here is role-based: every page declares the role it needs, and the
check happens in one place. In production `current_user()` would read a Supabase
session and look the role up in a `profiles` table with RLS enforcing it
server-side; the sandbox lets you switch roles from the sidebar so the gating
is actually testable without standing up an auth provider.

The important part is the *shape*: authorisation is a property of the user, not
a secret that gets shared around a committee and never rotated.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import IntEnum

import streamlit as st

from . import settings


class Role(IntEnum):
    """Ordered, so `>=` expresses "at least this much access"."""

    MEMBER = 10
    OFFICER = 20
    TREASURER = 30
    ADMIN = 40

    @property
    def label(self) -> str:
        return {
            Role.MEMBER: "Member",
            Role.OFFICER: "Committee officer",
            Role.TREASURER: "Treasurer",
            Role.ADMIN: "Admin",
        }[self]


ROLE_DESCRIPTIONS = {
    Role.MEMBER: "Read-only dashboards and reports.",
    Role.OFFICER: "Adds their own committee's detail and reimbursement requests.",
    Role.TREASURER: "Uploads statements, edits transactions, sets budgets.",
    Role.ADMIN: "Everything, plus data-quality and audit tooling.",
}

SESSION_KEY = "ais_identity"


@dataclass(frozen=True)
class Identity:
    email: str
    role: Role
    committee_id: int | None = None

    def can(self, required: Role) -> bool:
        return self.role >= required


def _default_identity() -> Identity:
    return Identity(email="treasurer@sandbox.local", role=Role.TREASURER)


def current_user() -> Identity:
    """
    The active identity.

    Sandbox fabricates one so the app is usable with no setup. Production never
    fabricates: it returns whatever `login_gate` put in the session, and if
    nothing is there the caller has skipped the gate -- which is a programming
    error, not a state to paper over with a default.
    """
    if settings.is_sandbox():
        if SESSION_KEY not in st.session_state:
            st.session_state[SESSION_KEY] = _default_identity()
        return st.session_state[SESSION_KEY]

    identity = st.session_state.get(SESSION_KEY)
    if identity is None:
        raise RuntimeError(
            "No authenticated identity. app.py must call auth.login_gate() "
            "before rendering any page."
        )
    return identity


# --- Production login --------------------------------------------------------
#
# One operator, one shared password, matching the pattern the original app
# already used in production (`st.secrets["treasury"]["password"]`) so the same
# secret carries over.
#
# This is deliberately NOT Supabase Auth. With a single trusted operator there
# are no per-user roles to enforce, so a login provider, a profiles table and
# RLS policies would all be machinery serving one account. The `Identity`/`Role`
# shape below is still the real one, so adding genuine multi-user auth later
# means replacing this function -- not rewriting every page.

_PASSWORD_ATTEMPT_KEY = "ais_login_attempts"
MAX_LOGIN_ATTEMPTS = 10


def _configured_password() -> str | None:
    """The operator password from secrets, or None if none is configured."""
    try:
        section = st.secrets.get("treasury", {})
    except Exception:  # noqa: BLE001 - no secrets file at all is a valid state
        return None
    password = str(section.get("password", "") or "").strip()
    return password or None


def _operator_email() -> str:
    try:
        section = st.secrets.get("treasury", {})
        email = str(section.get("operator_email", "") or "").strip()
    except Exception:  # noqa: BLE001
        email = ""
    return email or "treasurer"


def login_gate() -> None:
    """
    Require the operator password before anything renders, in production only.

    Halts the script when not signed in, so a page cannot render for an
    unauthenticated visitor even if it forgets to call `require`.
    """
    if settings.is_sandbox():
        return
    if SESSION_KEY in st.session_state:
        return

    expected = _configured_password()
    if expected is None:
        # Fail closed, loudly. A missing password in production must never mean
        # "let everyone in" -- which is what a falsy default would do.
        st.error(
            "**Not configured.** No treasury password is set, so the app cannot "
            "verify anyone. Add it to the deployment's secrets:\n\n"
            "```toml\n[treasury]\npassword = \"...\"\n```"
        )
        st.stop()

    st.title("UF AIS Financial Management")
    st.caption("Treasury access")

    attempts = st.session_state.get(_PASSWORD_ATTEMPT_KEY, 0)
    if attempts >= MAX_LOGIN_ATTEMPTS:
        st.error("Too many attempts. Reload the page to try again.")
        st.stop()

    with st.form("login"):
        supplied = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")

    if submitted:
        # Constant-time: a plain `==` leaks the length of the shared prefix
        # through timing. Cheap to do correctly.
        if hmac.compare_digest(supplied, expected):
            st.session_state[SESSION_KEY] = Identity(
                email=_operator_email(), role=Role.ADMIN
            )
            st.session_state[_PASSWORD_ATTEMPT_KEY] = 0
            st.rerun()
        else:
            st.session_state[_PASSWORD_ATTEMPT_KEY] = attempts + 1
            st.error("Incorrect password.")

    st.stop()


def sign_out() -> None:
    st.session_state.pop(SESSION_KEY, None)


def set_identity(identity: Identity) -> None:
    st.session_state[SESSION_KEY] = identity


def require(required: Role) -> Identity:
    """
    Gate a page. Renders an explanation and halts when the role is insufficient.

    Because every page calls this, adding a page cannot accidentally skip the
    check the way a copy-pasted password prompt could.
    """
    identity = current_user()
    if identity.can(required):
        return identity

    st.warning(
        f"**{required.label} access required.**\n\n"
        f"You are signed in as **{identity.role.label}**. "
        f"{ROLE_DESCRIPTIONS[required]}"
    )
    if settings.is_sandbox():
        st.caption("Switch roles from the sidebar to explore this page.")
    st.stop()
    raise AssertionError("unreachable")  # pragma: no cover


def role_switcher() -> None:
    """
    Sandbox-only control for exercising the gating.

    Deliberately absent in production -- there, the role comes from the
    authenticated session and cannot be chosen by the client.
    """
    if not settings.is_sandbox():
        return

    identity = current_user()
    st.sidebar.markdown("### Signed in as")
    options = list(Role)
    chosen = st.sidebar.selectbox(
        "Role",
        options,
        index=options.index(identity.role),
        format_func=lambda role: role.label,
        key="ais_role_picker",
        help="Sandbox only. In production this comes from the authenticated session.",
    )
    if chosen != identity.role:
        set_identity(Identity(email=identity.email, role=chosen, committee_id=identity.committee_id))
        st.rerun()

    st.sidebar.caption(ROLE_DESCRIPTIONS[chosen])
