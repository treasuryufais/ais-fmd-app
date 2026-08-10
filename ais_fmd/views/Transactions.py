"""
Transaction browser and bulk editor.

FINDING F6 is the important one here. The original converted `budget_category`
into a display string ("8 - Meeting Food") for the grid, then parsed the edited
value back to an int before comparing them -- so the change test was
`8 != "8 - Meeting Food"`, always true. Every row on screen got its own UPDATE
round-trip whether or not it had changed, the "No changes were made" branch was
unreachable, and on "All Months" that meant hundreds of sequential requests.

Here the comparison happens between parsed values of the same type, and only
genuinely changed rows are sent -- in one batched call.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import (
    committee_dropdown_options,
    committee_label,
    parse_committee_label,
    purpose_dropdown_options,
)
from ais_fmd.data import repositories as repo
from ais_fmd.data.backend import TransactionChange
from ais_fmd.domain.terms import attach_semester, ordered_semesters
from ais_fmd.ui import shell

identity = auth.require(auth.Role.MEMBER)
can_edit = identity.can(auth.Role.TREASURER)

shell.environment_banner()
shell.page_header(
    "Transactions",
    "Search, filter and correct categorisation. Changes are staged locally and "
    "saved in one batch.",
)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if bundle.transactions.empty:
    shell.empty_state("No transactions yet", "Upload a statement from the Treasury page.")
    st.stop()

# --- Filters -----------------------------------------------------------------

tagged = attach_semester(bundle.transactions, bundle.terms)

filter_columns = st.columns([2, 2, 2, 3])
with filter_columns[0]:
    semester_choice = st.selectbox(
        "Semester", ["All"] + semesters, index=len(semesters), key="txn_semester"
    )
with filter_columns[1]:
    status_choice = st.selectbox(
        "Categorisation", ["All", "Uncategorized", "Categorized"], key="txn_status"
    )
with filter_columns[2]:
    type_choice = st.selectbox("Type", ["All", "Income", "Expense"], key="txn_type")
with filter_columns[3]:
    search = st.text_input("Search details", placeholder="merchant, note, member…", key="txn_search")

view = tagged.copy()
if semester_choice != "All":
    view = view[view["Semester"] == semester_choice]
if status_choice == "Uncategorized":
    view = view[view["budget_category"].isna() | view["purpose"].isna()]
elif status_choice == "Categorized":
    view = view[view["budget_category"].notna() & view["purpose"].notna()]
if type_choice == "Income":
    view = view[view["amount"] > 0]
elif type_choice == "Expense":
    view = view[view["amount"] < 0]
if search.strip():
    view = view[view["details"].str.contains(search.strip(), case=False, na=False, regex=False)]

view = view.sort_values("transaction_date", ascending=False).reset_index(drop=True)

st.caption(f"{len(view):,} of {len(tagged):,} transactions match.")

if view.empty:
    shell.empty_state("Nothing matches these filters", "Try widening the search.")
    st.stop()

# --- Editor ------------------------------------------------------------------

PAGE_SIZE = 200
if len(view) > PAGE_SIZE:
    page_count = (len(view) - 1) // PAGE_SIZE + 1
    page = st.number_input(
        f"Page (showing {PAGE_SIZE} at a time)",
        min_value=1,
        max_value=page_count,
        value=1,
        key="txn_page",
    )
    view = view.iloc[(page - 1) * PAGE_SIZE : page * PAGE_SIZE].reset_index(drop=True)

# The frame the editor renders, and the typed original it is diffed against.
editable = pd.DataFrame(
    {
        "transactionid": view["transactionid"],
        "Date": pd.to_datetime(view["transaction_date"]).dt.date,
        "Amount": view["amount"],
        "Details": view["details"],
        "Committee": [committee_label(value) for value in view["budget_category"]],
        "Purpose": view["purpose"].fillna(""),
        "Account": view["account"].fillna(""),
    }
)

def clean_text(value: object) -> str | None:
    """
    Normalise a cell to a stripped string or None.

    `value or ""` is not safe here: a pandas NaN is a float and is *truthy*, so
    the fallback never fires and `.strip()` raises on it.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


original_by_id = {
    int(row["transactionid"]): (
        parse_committee_label(row["budget_category"]),
        clean_text(row["purpose"]),
    )
    for _, row in view.iterrows()
}

edited = st.data_editor(
    editable,
    key="txn_editor",
    width="stretch",
    hide_index=True,
    disabled=(
        ["transactionid", "Date", "Amount", "Details", "Account"]
        if can_edit
        else list(editable.columns)
    ),
    column_config={
        "transactionid": None,  # carried for identity, not shown
        "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
        "Amount": st.column_config.NumberColumn("Amount", format="$%.2f"),
        "Details": st.column_config.TextColumn("Details", width="large"),
        "Committee": st.column_config.SelectboxColumn(
            "Committee", options=committee_dropdown_options(), required=False
        ),
        "Purpose": st.column_config.SelectboxColumn(
            "Purpose", options=purpose_dropdown_options(), required=False
        ),
        "Account": st.column_config.TextColumn("Account"),
    },
)

if not can_edit:
    st.caption("Read-only. Treasurer access is required to make changes.")
    st.stop()

# --- Diff --------------------------------------------------------------------
#
# F6: both sides are parsed to their real types before comparison, so an
# unchanged row produces no change record.

changes: list[TransactionChange] = []
for _, row in edited.iterrows():
    transaction_id = int(row["transactionid"])
    original = original_by_id.get(transaction_id)
    if original is None:
        continue

    new_committee = parse_committee_label(row["Committee"])
    new_purpose = clean_text(row["Purpose"])
    if (new_committee, new_purpose) != original:
        changes.append(
            TransactionChange(
                transaction_id=transaction_id,
                purpose=new_purpose,
                budget_category=new_committee,
            )
        )

save_column, status_column = st.columns([1, 4])
with save_column:
    save_clicked = st.button(
        f"Save {len(changes)} change{'s' if len(changes) != 1 else ''}",
        type="primary",
        disabled=not changes,
    )
with status_column:
    if changes:
        st.caption(
            f"{len(changes)} row(s) differ from what is stored. "
            f"Unchanged rows are not written."
        )
    else:
        st.caption("No changes staged.")

if save_clicked and changes:
    result = repo.update_transactions(changes, identity.email)
    if result.error:
        shell.error_state("Could not save changes", result.error)
    elif result.failed:
        shell.error_state(
            f"{len(result.failed)} row(s) failed to save",
            f"Transaction IDs: {', '.join(str(i) for i in result.failed[:20])}",
        )
    else:
        st.success(
            f"Saved {result.updated} change(s)."
            + (f" {result.unchanged} row(s) were already up to date." if result.unchanged else "")
        )
        st.rerun()

with st.expander("Why this page no longer rewrites every row"):
    st.markdown(
        """
The previous editor converted the committee into a display string for the grid
(`"8 - Meeting Food"`) but parsed the edited value back to an integer before
comparing. The comparison was therefore `8 != "8 - Meeting Food"` — always true —
so every visible row was written back on every save, one HTTP request each.

Both sides are now parsed to the same type before diffing, and the changed rows
go to the database in a single batched transaction.
"""
    )
