"""
UF AIS Financial Management System -- entry point.

Run:
    streamlit run app.py

Defaults to sandbox mode: a local SQLite database, no network calls, no real
money. See README.md for the safety guarantees.
"""

from __future__ import annotations

import streamlit as st

from ais_fmd import auth, nav, settings
from ais_fmd.ui import shell

shell.bootstrap()

# --- Sandbox bootstrap -------------------------------------------------------
# Seed on first run so the app has something to show immediately.

if settings.is_sandbox():
    from ais_fmd.data.sqlite_backend import SqliteBackend

    if "sandbox_ready" not in st.session_state:
        backend = SqliteBackend()
        if backend.fetch_transactions().empty:
            with st.spinner("Preparing the sandbox database…"):
                from ais_fmd.data.seed import seed

                seed(backend, reset=False)
        st.session_state.sandbox_ready = True

# --- Authentication ----------------------------------------------------------
# Sandbox is open and switches roles from the sidebar. Production requires the
# operator password before any page renders.

auth.login_gate()

# --- Navigation --------------------------------------------------------------
#
# PEBBLE CONSOLIDATION: sixteen entries -> eleven. Seven primary pages sit in
# the top pill bar; four admin pages sit under "More". Merged pages are hubs
# (views/*Hub.py) that run the original, unchanged view files one at a time:
#
#   Dues          = Dues + Roster
#   Transactions  = Transactions + Review Queue
#   Assistant     = Assistant + Runbook (as "Help")
#   Alerts & Data Quality = Alerts & Reports + Data Quality
#   Settings      = Audit Log (+ Officer Access, deferred)
#
# MVP SCOPE. Two pages are commented out rather than deleted. In both cases the
# page, its domain module, its tests and its SQLite tables all still exist and
# still pass, and the Postgres side is written and waiting in
# migrations/002_deferred_features.sql. Turning either back on means
# uncommenting one line and running that migration.
#
#   Reimbursements  -- deferred from the start.
#
#   Officer Access  -- deferred 2026-09-08, for the first production term.
#     This is the VP portal's admin page: it maps an email to a role and a
#     committee, which is what Google sign-in looks up in `profiles`. The first
#     term is treasurer-only, so nothing needs those mappings yet -- and leaving
#     the page on would have shipped a feature whose table lives in a migration
#     marked "do not run". It is commented out inside views/SettingsHub.py.
#
#     Note this does NOT disable Google sign-in itself: `auth.login_gate` still
#     offers it whenever an `[auth]` section is configured, and is inert
#     otherwise. It only removes the page for managing who gets in that way.
#
# `Officer.py` ("My Committee") is deliberately a primary page -- it is a
# read-only committee view that a treasurer outranks, and it stays available.

PAGES = {
    "": [
        ("ais_fmd/views/Home.py", "Home", ":material/home:", auth.Role.MEMBER),
        ("ais_fmd/views/Dashboard.py", "Dashboard", ":material/analytics:", auth.Role.MEMBER),
        ("ais_fmd/views/DuesHub.py", "Dues", ":material/groups:", auth.Role.MEMBER),
        ("ais_fmd/views/TransactionsHub.py", "Transactions", ":material/table_rows:", auth.Role.MEMBER),
        ("ais_fmd/views/Planner.py", "Planner", ":material/insights:", auth.Role.TREASURER),
        ("ais_fmd/views/AssistantHub.py", "Assistant", ":material/smart_toy:", auth.Role.MEMBER),
        ("ais_fmd/views/Officer.py", "My Committee", ":material/badge:", auth.Role.OFFICER),
        # ("ais_fmd/views/Reimbursements.py", "Reimbursements", ":material/receipt_long:", auth.Role.MEMBER),  # MVP: deferred
    ],
    "More": [
        ("ais_fmd/views/Reconciliation.py", "Reconciliation", ":material/balance:", auth.Role.MEMBER),
        ("ais_fmd/views/AlertsHub.py", "Alerts & Data Quality", ":material/notifications:", auth.Role.MEMBER),
        ("ais_fmd/views/Treasury.py", "Treasury", ":material/account_balance:", auth.Role.TREASURER),
        ("ais_fmd/views/SettingsHub.py", "Settings", ":material/settings:", auth.Role.TREASURER),
    ],
}

identity = auth.current_user()

# A VP sees a short, committee-scoped navigation instead of the full list; see
# `ais_fmd/nav.py` for why hiding pages is what actually keeps other committees'
# data out of their reach.
pages = {
    section: [
        st.Page(path, title=title, icon=icon, default=(title == "Home"))
        for path, title, icon, required in entries
    ]
    for section, entries in nav.visible_pages(identity, PAGES).items()
}

navigation = st.navigation(pages, position="top")

# --- Sidebar -----------------------------------------------------------------
# Collapsed by default (shell.bootstrap). Sandbox-only controls live here.

auth.role_switcher()

if settings.is_sandbox():
    st.sidebar.markdown("---")
    st.sidebar.markdown("### Sandbox controls")
    if st.sidebar.button("Reset & reseed data", width="stretch"):
        from ais_fmd.data.seed import seed
        from ais_fmd.data import repositories as repo

        with st.spinner("Rebuilding the sandbox database…"):
            summary = seed()
            repo.invalidate()
        st.sidebar.success(f"Reseeded {summary['transactions']} transactions.")
        st.rerun()
    st.sidebar.caption(
        "Wipes and regenerates the local SQLite file. Nothing outside this "
        "folder is touched."
    )

shell.sidebar_footer()

navigation.run()
