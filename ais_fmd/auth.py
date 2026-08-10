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
    if SESSION_KEY not in st.session_state:
        st.session_state[SESSION_KEY] = _default_identity()
    return st.session_state[SESSION_KEY]


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
