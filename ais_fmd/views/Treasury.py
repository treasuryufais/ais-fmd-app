"""
Treasury portal: statement upload, budgets, terms.

The original had the Venmo and Checking upload flows as ~130 lines of
near-verbatim duplicate code in two tabs. There is one flow here, parameterised
by which parser to use, so a fix to the upload pipeline lands in both places by
construction.

Fixes visible on this page:
  F3  deduplication keyed on date+amount+details+account with an occurrence
      ordinal, enforced by a unique index rather than a pandas filter
  F4  transactions and the uploaded-files marker commit together or not at all
  F5  statement shape is validated before parsing; unreadable rows are reported
      rather than imported as $0.00
  F7  the committee reference table is generated from config, so it cannot
      contradict the mapping the code applies
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import (
    ACCOUNT_VENMO,
    ACCOUNT_WELLS_FARGO,
    BUDGETED_COMMITTEE_IDS,
    committee_dropdown_options,
    committee_label,
    committee_name,
    committee_reference_rows,
    parse_committee_label,
    purpose_dropdown_options,
)
from ais_fmd.data import repositories as repo
from ais_fmd.domain import dues
from ais_fmd.domain.categorize.pipeline import categorize_frame
from ais_fmd.domain.dedupe import split_new_and_duplicate
from ais_fmd.domain.parsers import venmo, wells_fargo
from ais_fmd.domain.terms import ordered_semesters, validate_semester_name
from ais_fmd.ui import shell

identity = auth.require(auth.Role.TREASURER)

shell.environment_banner()
shell.page_header(
    "Treasury",
    "Upload statements, allocate budgets, and manage academic terms.",
)

SOURCES = {
    "Venmo": {
        "parser": venmo,
        "account": ACCOUNT_VENMO,
        "hint": "Venmo statement export (CSV or Excel).",
        "filename_hint": "venmo",
    },
    "Wells Fargo checking": {
        "parser": wells_fargo,
        "account": ACCOUNT_WELLS_FARGO,
        "hint": "Wells Fargo checking export (CSV or Excel).",
        "filename_hint": "checking",
    },
}

tab_upload, tab_budgets, tab_terms, tab_export = st.tabs(
    ["Upload statement", "Budgets", "Terms", "Export"]
)

# ============================================================================
# UPLOAD
# ============================================================================

with tab_upload:
    with st.expander("Committee ID reference", expanded=False):
        reference = pd.DataFrame(committee_reference_rows(), columns=["ID", "Committee"])
        left, right = st.columns(2)
        midpoint = (len(reference) + 1) // 2
        shell.dataframe(reference.iloc[:midpoint], height=340)
        with right:
            shell.dataframe(reference.iloc[midpoint:], height=340)
        st.caption(
            "Generated from config/categories.py — the same structure the "
            "categorizer uses, so this table cannot drift from the mapping."
        )

    source_name = st.radio(
        "Statement source", list(SOURCES), horizontal=True, key="upload_source"
    )
    source = SOURCES[source_name]
    st.caption(source["hint"])

    uploaded = st.file_uploader(
        f"Upload {source_name} statement",
        type=["csv", "xlsx", "xls"],
        key=f"upload_{source_name}",
    )

    if uploaded is not None:
        file_name = uploaded.name

        if repo.is_file_uploaded(file_name):
            st.warning(
                f"**{file_name}** has already been uploaded. "
                f"Re-uploading is safe — duplicates are rejected by the database — "
                f"but nothing new will be added."
            )

        if source["filename_hint"] not in file_name.lower():
            st.info(
                f"Filename does not contain '{source['filename_hint']}'. "
                f"Proceeding anyway — the parser validates the file's actual shape, "
                f"not its name."
            )

        # --- Parse ----------------------------------------------------------
        try:
            if file_name.lower().endswith(".csv"):
                header = None if source_name == "Wells Fargo checking" else "infer"
                raw = pd.read_csv(uploaded, header=header)
            else:
                header = None if source_name == "Wells Fargo checking" else 0
                raw = pd.read_excel(uploaded, header=header)
        except Exception as exc:  # noqa: BLE001
            raw = None
            shell.error_state("Could not read the file", f"{type(exc).__name__}: {exc}")

        if raw is not None:
            parsed = source["parser"].parse(raw, source_file=file_name)

            for warning in parsed.warnings:
                st.warning(warning)

            if not parsed.ok:
                shell.error_state(
                    "This file was not imported",
                    "\n\n".join(parsed.errors) or "No usable rows found.",
                )
            else:
                st.success(f"Parsed {parsed.row_count:,} transactions from {file_name}.")

                # --- Categorize -------------------------------------------
                memory = repo.load_merchant_memory()
                # Per-term rates, so a statement from a term with its own dues
                # rate is categorized against that rate and not a stale default.
                schedule = dues.schedule_from_terms(repo.load_terms())
                categorized, run = categorize_frame(parsed.rows, memory, dues=schedule)

                counts = run.counts_by_source
                summary_columns = st.columns(4)
                summary_columns[0].metric("From merchant memory", counts["merchant"])
                summary_columns[1].metric("By rule", counts["rule"])
                summary_columns[2].metric("By model", counts["llm"])
                summary_columns[3].metric("Need review", counts["none"])

                st.caption(
                    f"{run.rows_resolved_locally} of {run.total} resolved without any "
                    f"model call. Only {run.rows_sent_to_model} would ever be sent."
                )
                if run.llm.skipped_reason:
                    st.caption(f"Model pass: {run.llm.skipped_reason}")
                elif run.llm.fully_failed:
                    shell.error_state(
                        "Every model batch failed",
                        "\n".join(run.llm.errors[:3]),
                    )

                # --- Dedupe -----------------------------------------------
                existing = repo.load_transactions()
                existing_records = existing.to_dict("records") if not existing.empty else []
                staged = categorized.to_dict("records")
                for record in staged:
                    if isinstance(record.get("transaction_date"), pd.Timestamp):
                        record["transaction_date"] = record["transaction_date"].strftime("%Y-%m-%d")

                dedupe = split_new_and_duplicate(staged, existing_records)

                if dedupe.duplicates:
                    st.info(
                        f"{len(dedupe.duplicates)} row(s) already exist and will be skipped. "
                        f"Genuine same-day repeat purchases are *not* treated as duplicates."
                    )
                    with st.expander("Show skipped duplicates"):
                        shell.dataframe(
                            pd.DataFrame(dedupe.duplicates)[
                                ["transaction_date", "amount", "details"]
                            ].rename(
                                columns={
                                    "transaction_date": "Date",
                                    "amount": "Amount",
                                    "details": "Details",
                                }
                            ),
                            column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
                        )

                if not dedupe.new:
                    st.warning("Every row in this file already exists. Nothing to import.")
                else:
                    st.markdown(f"#### Review {len(dedupe.new)} new transaction(s)")

                    preview = pd.DataFrame(dedupe.new)
                    preview_display = pd.DataFrame(
                        {
                            "Date": preview["transaction_date"],
                            "Amount": preview["amount"],
                            "Details": preview["details"],
                            "Committee": [
                                committee_label(value) for value in preview["budget_category"]
                            ],
                            "Purpose": preview["purpose"].fillna(""),
                            "Matched by": preview["match_rule"].fillna(""),
                        }
                    )

                    edited = st.data_editor(
                        preview_display,
                        key="upload_preview_editor",
                        width="stretch",
                        hide_index=True,
                        disabled=["Date", "Amount", "Details", "Matched by"],
                        column_config={
                            "Amount": st.column_config.NumberColumn(format="$%.2f"),
                            "Details": st.column_config.TextColumn(width="large"),
                            "Committee": st.column_config.SelectboxColumn(
                                options=committee_dropdown_options(), required=False
                            ),
                            "Purpose": st.column_config.SelectboxColumn(
                                options=purpose_dropdown_options(), required=False
                            ),
                            "Matched by": st.column_config.TextColumn(width="medium"),
                        },
                    )

                    if st.button(
                        f"Import {len(dedupe.new)} transaction(s)", type="primary", key="confirm_import"
                    ):
                        final = []
                        for position, record in enumerate(dedupe.new):
                            row = edited.iloc[position]
                            final.append(
                                {
                                    "transaction_date": record["transaction_date"],
                                    "amount": record["amount"],
                                    "details": record["details"],
                                    "account": record["account"],
                                    "budget_category": parse_committee_label(row["Committee"]),
                                    "purpose": (row["Purpose"] or "").strip() or None,
                                    "natural_key": record["natural_key"],
                                }
                            )

                        receipt = repo.insert_transactions(final, file_name, identity.email)
                        if not receipt.ok:
                            shell.error_state("Import failed — nothing was written", receipt.error or "")
                        else:
                            st.success(
                                f"Imported {receipt.inserted} transaction(s) from {file_name}."
                                + (
                                    f" {receipt.duplicates_skipped} rejected as duplicates by the database."
                                    if receipt.duplicates_skipped
                                    else ""
                                )
                            )
                            st.balloons()
                            st.rerun()

    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
    st.markdown("#### Previously uploaded files")
    files = repo.load_uploaded_files()
    if files.empty:
        shell.empty_state("Nothing uploaded yet")
    else:
        shell.dataframe(files.head(25))

# ============================================================================
# BUDGETS
# ============================================================================

with tab_budgets:
    terms = repo.load_terms()
    if terms.empty:
        shell.empty_state("No terms defined", "Add a term first.")
    else:
        term_ids = terms.sort_values("start_date", ascending=False)["TermID"].tolist()
        semester_by_id = dict(zip(terms["TermID"], terms["Semester"]))

        selected_term = st.selectbox(
            "Term",
            term_ids,
            format_func=lambda value: f"{value} — {semester_by_id.get(value, '')}",
            key="budget_term",
        )

        budgets = repo.load_budgets()
        current = {}
        if not budgets.empty:
            scoped = budgets[budgets["termid"] == selected_term]
            current = dict(zip(scoped["committeeid"], scoped["budget_amount"]))

        st.caption(
            "Allocations are upserted per committee. Unlike the original, existing "
            "rows are not deleted first, so there is no window where the term has "
            "no budget and no history is destroyed."
        )

        with st.form("budget_form"):
            inputs: dict[int, float] = {}
            columns = st.columns(3)
            for position, committee_id in enumerate(BUDGETED_COMMITTEE_IDS):
                with columns[position % 3]:
                    inputs[committee_id] = st.number_input(
                        committee_name(committee_id),
                        min_value=0.0,
                        value=float(current.get(committee_id, 0.0) or 0.0),
                        step=50.0,
                        format="%.2f",
                        key=f"budget_{selected_term}_{committee_id}",
                    )
            saved = st.form_submit_button("Save budgets", type="primary")

        if saved:
            result = repo.upsert_budgets(selected_term, inputs, identity.email)
            if result.error:
                shell.error_state("Could not save budgets", result.error)
            else:
                st.success(
                    f"Saved. {result.updated} allocation(s) changed, "
                    f"{result.unchanged} already matched."
                )
                st.rerun()

# ============================================================================
# TERMS
# ============================================================================

with tab_terms:
    terms = repo.load_terms()
    if not terms.empty:
        view = terms.sort_values("start_date", ascending=False).copy()
        view["Status"] = (
            view["locked"].fillna(0).astype(int).map({1: "closed", 0: "open"})
            if "locked" in view.columns
            else "open"
        )
        view["Dues rates"] = (
            view["dues_rates"].fillna("—") if "dues_rates" in view.columns else "—"
        )
        view["Rates confirmed"] = (
            view["dues_rates_verified"].fillna(0).astype(int).map({1: "yes", 0: "no"})
            if "dues_rates_verified" in view.columns
            else "no"
        )
        shell.dataframe(
            view[
                ["TermID", "Semester", "start_date", "end_date", "Status",
                 "Dues rates", "Rates confirmed"]
            ].rename(
                columns={"TermID": "Term ID", "start_date": "Start", "end_date": "End"}
            )
        )

    # --- Per-term dues rates -------------------------------------------------

    st.markdown("#### Dues rates for a term")
    shell.say(
        "Dues are matched on an exact amount. The rate has changed nearly every "
        "term historically, and a term charging a rate the app does not know "
        "about stops categorizing dues altogether — no error, the payments just "
        "land in the review queue. Every term currently carries rates copied "
        "from the old built-in default, marked unconfirmed until someone checks "
        "them against what was actually charged."
    )

    if terms.empty:
        shell.empty_state("No terms yet")
    else:
        ordered_terms = terms.sort_values("start_date", ascending=False)
        semester_names = dict(zip(ordered_terms["TermID"], ordered_terms["Semester"]))
        with st.form("dues_rates_form"):
            rate_columns = st.columns([2, 2, 1])
            with rate_columns[0]:
                rate_term = st.selectbox(
                    "Term",
                    ordered_terms["TermID"].tolist(),
                    format_func=lambda value: f"{value} — {semester_names.get(value, '')}",
                    key="dues_rate_term",
                )
            current = ordered_terms[ordered_terms["TermID"] == rate_term]
            existing = ""
            already_confirmed = False
            if not current.empty:
                raw = current.iloc[0].get("dues_rates")
                existing = "" if raw is None or pd.isna(raw) else str(raw)
                flag = current.iloc[0].get("dues_rates_verified")
                already_confirmed = bool(0 if flag is None or pd.isna(flag) else int(flag))
            with rate_columns[1]:
                entered = st.text_input(
                    "Rates charged (comma separated)",
                    value=existing,
                    placeholder="35.00,52.50",
                    key="dues_rate_values",
                )
            with rate_columns[2]:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                confirmed = st.checkbox(
                    "Confirmed", value=already_confirmed, key="dues_rate_confirmed"
                )
            if st.form_submit_button("Save rates", type="primary"):
                parsed = dues.parse_rates(entered)
                if entered.strip() and not parsed:
                    shell.error_state(
                        "Could not read those rates",
                        "Enter amounts separated by commas, for example 35.00,52.50.",
                    )
                else:
                    result = repo.set_term_dues_rates(
                        rate_term,
                        dues.format_rates(parsed) if parsed else "",
                        confirmed,
                        identity.email,
                    )
                    if result.error:
                        shell.error_state("Could not save the rates", result.error)
                    elif result.unchanged:
                        shell.notify(f"{rate_term} already had those rates.")
                    else:
                        st.success(f"Saved dues rates for {rate_term}.")
                        st.rerun()

    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

    # --- Period locking (M10) -----------------------------------------------

    st.markdown("#### Close or reopen a term")
    st.caption(
        "Closing a term makes its transactions read-only — imports and edits "
        "dated inside it are refused. The check lives in the data layer, so no "
        "page can bypass it. Close a term once it has been reconciled and "
        "reported, so a later edit cannot silently change a published figure."
    )

    if terms.empty:
        shell.empty_state("No terms to close yet")
    else:
        lock_columns = st.columns([2, 1, 2])
        with lock_columns[0]:
            ordered = terms.sort_values("start_date", ascending=False)
            semester_by_term = dict(zip(ordered["TermID"], ordered["Semester"]))
            lock_target = st.selectbox(
                "Term",
                ordered["TermID"].tolist(),
                format_func=lambda value: f"{value} — {semester_by_term.get(value, '')}",
                key="lock_term",
            )

        currently_locked = False
        if "locked" in terms.columns:
            match = terms[terms["TermID"] == lock_target]
            if not match.empty:
                currently_locked = bool(int(match.iloc[0]["locked"] or 0))

        with lock_columns[1]:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            st.markdown(
                shell.pill("closed" if currently_locked else "open",
                           "over" if currently_locked else "on track"),
                unsafe_allow_html=True,
            )

        with lock_columns[2]:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            action = "Reopen term" if currently_locked else "Close term"
            if st.button(action, key="toggle_lock", type="primary"):
                result = repo.set_term_lock(lock_target, not currently_locked, identity.email)
                if result.error:
                    shell.error_state("Could not change the term", result.error)
                else:
                    st.success(
                        f"{lock_target} is now "
                        f"{'open' if currently_locked else 'closed'}."
                    )
                    st.rerun()

    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

    st.markdown("#### Add a term")
    with st.form("term_form"):
        columns = st.columns(2)
        with columns[0]:
            term_id = st.text_input("Term ID", placeholder="FA26")
            semester = st.text_input("Semester name", placeholder="Fall 2026")
        with columns[1]:
            start_date = st.date_input("Start date")
            end_date = st.date_input("End date")
        add_clicked = st.form_submit_button("Add term", type="primary")

    if add_clicked:
        valid, message = validate_semester_name(semester)
        if not term_id.strip():
            shell.error_state("Term ID is required")
        elif not valid:
            shell.error_state("Semester name is not valid", message)
        elif end_date <= start_date:
            shell.error_state("End date must be after the start date")
        else:
            result = repo.insert_term(
                term_id.strip(),
                semester.strip().title(),
                start_date.isoformat(),
                end_date.isoformat(),
                identity.email,
            )
            if result.error:
                shell.error_state("Could not add the term", result.error)
            else:
                st.success(f"Added {term_id.strip()}.")
                st.rerun()

# ============================================================================
# EXPORT
# ============================================================================

with tab_export:
    st.markdown("#### Export data")
    bundle = repo.load_bundle()
    exports = {
        "transactions": bundle.transactions,
        "budgets": bundle.budgets,
        "terms": bundle.terms,
        "committees": bundle.committees,
        "merchants": repo.load_merchants(),
        "audit_log": repo.load_audit(10_000),
    }

    columns = st.columns(3)
    for position, (name, frame) in enumerate(exports.items()):
        with columns[position % 3]:
            st.download_button(
                f"{name} ({len(frame):,})",
                frame.to_csv(index=False),
                file_name=f"{name}.csv",
                mime="text/csv",
                key=f"export_{name}",
                width="stretch",
            )

    st.caption(
        "Downloads are generated in the browser session. In sandbox mode these "
        "come from the local SQLite file."
    )
