"""
UF AIS Financial Management System -- entry point.

Run:
    streamlit run app.py

Defaults to sandbox mode: a local SQLite database, no network calls, no real
money. See README.md for the safety guarantees.
"""

from __future__ import annotations

import streamlit as st

from ais_fmd import auth, settings
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
# MVP SCOPE. `Reimbursements` is commented out rather than deleted: the page,
# its domain module, its tests and its SQLite tables all still exist and still
# pass, and the Postgres side is written and waiting in
# migrations/002_deferred_features.sql. Turning it back on means uncommenting
# one line and running that migration. Nothing else imports it -- verified.

PAGES = [
    ("ais_fmd/views/Home.py", "Home", ":material/home:", auth.Role.MEMBER),
    ("ais_fmd/views/Dashboard.py", "Dashboard", ":material/analytics:", auth.Role.MEMBER),
    ("ais_fmd/views/Transactions.py", "Transactions", ":material/table_rows:", auth.Role.MEMBER),
    ("ais_fmd/views/ReviewQueue.py", "Review Queue", ":material/rule:", auth.Role.TREASURER),
    ("ais_fmd/views/Officer.py", "My Committee", ":material/badge:", auth.Role.OFFICER),
    ("ais_fmd/views/Dues.py", "Dues", ":material/groups:", auth.Role.MEMBER),
    # ("ais_fmd/views/Reimbursements.py", "Reimbursements", ":material/receipt_long:", auth.Role.MEMBER),  # MVP: deferred
    ("ais_fmd/views/Reports.py", "Alerts & Reports", ":material/notifications:", auth.Role.MEMBER),
    ("ais_fmd/views/Reconciliation.py", "Reconciliation", ":material/balance:", auth.Role.MEMBER),
    ("ais_fmd/views/Planner.py", "Scenario Planner", ":material/insights:", auth.Role.TREASURER),
    ("ais_fmd/views/Treasury.py", "Treasury", ":material/account_balance:", auth.Role.TREASURER),
    ("ais_fmd/views/DataQuality.py", "Data Quality", ":material/health_and_safety:", auth.Role.TREASURER),
    ("ais_fmd/views/AuditLog.py", "Audit Log", ":material/history:", auth.Role.TREASURER),
    ("ais_fmd/views/Assistant.py", "Assistant", ":material/smart_toy:", auth.Role.MEMBER),
    ("ais_fmd/views/Runbook.py", "Runbook", ":material/menu_book:", auth.Role.MEMBER),
]

identity = auth.current_user()

pages = [
    st.Page(path, title=title, icon=icon, default=(title == "Home"))
    for path, title, icon, required in PAGES
    if identity.can(required)
]

navigation = st.navigation(pages)

# --- Sidebar -----------------------------------------------------------------

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
