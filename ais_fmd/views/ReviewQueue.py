"""
Module M3 -- categorization review queue.

Replaces "categorize everything, then eyeball a 200-row grid". Uncategorized
transactions are worked one at a time, highest-value first, with the reason the
categorizer could not resolve them shown alongside.

The part that compounds: every correction is offered to merchant memory (M4).
Resolve "SWEETWATER BRANCH INN" once and every future transaction from that
merchant is categorized instantly, for free, forever. The queue shrinks as it
is worked rather than regenerating at the same size each semester.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import (
    committee_dropdown_options,
    committee_name,
    parse_committee_label,
    purpose_dropdown_options,
    purpose_to_committee,
)
from ais_fmd.data import repositories as repo
from ais_fmd.data.backend import TransactionChange
from ais_fmd.domain.categorize.merchants import merchant_key
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.ui import charts, shell

identity = auth.require(auth.Role.TREASURER)

shell.environment_banner()
shell.page_header(
    "Review Queue",
    "Work through what the categorizer could not resolve. Every correction "
    "teaches the merchant table, so the queue shrinks over time.",
)

transactions = repo.load_transactions()
memory = repo.load_merchant_memory()

if transactions.empty:
    shell.empty_state("No transactions yet", "Upload a statement from the Treasury page.")
    st.stop()

pending = transactions[
    transactions["budget_category"].isna() | transactions["purpose"].isna()
].copy()

# --- Overview ----------------------------------------------------------------

resolved = len(transactions) - len(pending)
overview = st.columns(4)
overview[0].metric("Awaiting review", f"{len(pending):,}")
overview[1].metric("Already categorized", f"{resolved:,}")
overview[2].metric(
    "Coverage", f"{(resolved / len(transactions) * 100) if len(transactions) else 0:.1f}%"
)
overview[3].metric("Merchant rules learned", f"{len(memory):,}")

if pending.empty:
    st.success("The queue is empty — every transaction has a committee and a purpose.")
    st.stop()

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

# --- Suggestions -------------------------------------------------------------
# Re-run the pipeline over the pending rows so the queue shows what the current
# rules and merchant memory would do now, which may differ from when they were
# first imported.

records = pending.to_dict("records")
run = categorize_records(records, memory)

pending = pending.reset_index(drop=True)
pending["suggested_committee"] = [item.committee_id for item in run.classifications]
pending["suggested_purpose"] = [item.purpose for item in run.classifications]
pending["match_rule"] = [item.rule for item in run.classifications]
pending["match_source"] = [item.source for item in run.classifications]
pending["confidence"] = [item.confidence for item in run.classifications]
pending["magnitude"] = pending["amount"].abs()

suggested = int((pending["suggested_committee"].notna()).sum())
if suggested:
    st.info(
        f"Current rules and merchant memory can now resolve **{suggested}** of these "
        f"**{len(pending)}** without any model call."
    )

if run.llm.skipped_reason:
    st.caption(f"Model pass: {run.llm.skipped_reason}")
elif run.llm.fully_failed:
    shell.error_state(
        "Every model batch failed",
        "\n".join(run.llm.errors[:3]) or "No detail returned.",
    )
elif run.llm.partially_failed:
    st.warning(
        f"{run.llm.batches_failed} of {run.llm.batches_attempted} model batches failed. "
        f"Partial results are shown."
    )

# --- Queue -------------------------------------------------------------------

sort_choice = st.radio(
    "Work order",
    ["Largest amount first", "Most confident suggestion first", "Oldest first"],
    horizontal=True,
    key="queue_sort",
)

if sort_choice == "Largest amount first":
    pending = pending.sort_values("magnitude", ascending=False)
elif sort_choice == "Most confident suggestion first":
    pending = pending.sort_values(["confidence", "magnitude"], ascending=[False, False])
else:
    pending = pending.sort_values("transaction_date")

pending = pending.reset_index(drop=True)

batch_size = st.slider("How many to show", 5, 50, 12, step=1, key="queue_batch")
batch = pending.head(batch_size)

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

with st.form("review_queue_form"):
    decisions: list[dict] = []

    for position, row in batch.iterrows():
        transaction_id = int(row["transactionid"])
        suggestion = row["suggested_committee"]
        suggested_label = (
            f"{int(suggestion)} - {committee_name(suggestion)}" if pd.notna(suggestion) else ""
        )

        detail_column, choice_column = st.columns([3, 2])

        with detail_column:
            date_text = pd.to_datetime(row["transaction_date"]).strftime("%Y-%m-%d")
            amount_text = f"${abs(row['amount']):,.2f}"
            direction = "in" if row["amount"] > 0 else "out"
            # pd.isna guard: a NaN account is a float and is truthy, so `or` alone
            # would print "nan" rather than falling back to the dash.
            account_text = "—" if pd.isna(row["account"]) else str(row["account"])
            st.markdown(
                shell.money_safe(
                    f"**{amount_text}** {direction} &nbsp;·&nbsp; {date_text} "
                    f"&nbsp;·&nbsp; {account_text}"
                ),
                unsafe_allow_html=True,
            )
            st.caption(str(row["details"])[:220])
            if row["match_rule"]:
                st.caption(
                    f"Suggestion: {row['match_rule']} "
                    f"(confidence {row['confidence']:.0%}, via {row['match_source']})"
                )
            else:
                st.caption("No rule or known merchant matched this one.")

        with choice_column:
            committee_choice = st.selectbox(
                "Committee",
                committee_dropdown_options(),
                index=(
                    committee_dropdown_options().index(suggested_label)
                    if suggested_label in committee_dropdown_options()
                    else 0
                ),
                key=f"queue_committee_{transaction_id}",
                label_visibility="collapsed",
            )
            purpose_options = purpose_dropdown_options()
            suggested_purpose = row["suggested_purpose"] or ""
            purpose_choice = st.selectbox(
                "Purpose",
                purpose_options,
                index=(
                    purpose_options.index(suggested_purpose)
                    if suggested_purpose in purpose_options
                    else 0
                ),
                key=f"queue_purpose_{transaction_id}",
                label_visibility="collapsed",
            )
            remember = st.checkbox(
                "Remember this merchant",
                value=bool(merchant_key(row["details"])),
                disabled=not merchant_key(row["details"]),
                key=f"queue_remember_{transaction_id}",
                help=(
                    "Adds a merchant rule so future transactions from this merchant "
                    "are categorized instantly."
                    if merchant_key(row["details"])
                    else "This description has no stable merchant to remember "
                    "(Venmo transfers are person-specific)."
                ),
            )

        decisions.append(
            {
                "transaction_id": transaction_id,
                "details": row["details"],
                "committee_key": f"queue_committee_{transaction_id}",
                "purpose_key": f"queue_purpose_{transaction_id}",
                "remember_key": f"queue_remember_{transaction_id}",
            }
        )
        st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

    submitted = st.form_submit_button("Apply decisions", type="primary")

if submitted:
    changes: list[TransactionChange] = []
    merchant_rules: list[dict] = []

    for decision in decisions:
        committee_id = parse_committee_label(st.session_state.get(decision["committee_key"]))
        purpose = (st.session_state.get(decision["purpose_key"]) or "").strip() or None

        if committee_id is None and purpose:
            committee_id = purpose_to_committee(purpose)
        if committee_id is None and purpose is None:
            continue  # left blank -- stays in the queue

        changes.append(
            TransactionChange(
                transaction_id=decision["transaction_id"],
                purpose=purpose,
                budget_category=committee_id,
            )
        )

        if st.session_state.get(decision["remember_key"]) and committee_id is not None:
            key = merchant_key(decision["details"])
            if key:
                merchant_rules.append(
                    {
                        "merchant_key": key,
                        "canonical_name": key.title(),
                        "committee_id": committee_id,
                        "purpose": purpose or "",
                        "hit_count": 1,
                        "source": "learned",
                    }
                )

    if not changes:
        st.info("Nothing selected — the queue is unchanged.")
    else:
        result = repo.update_transactions(changes, identity.email)
        if result.error:
            shell.error_state("Could not apply decisions", result.error)
        else:
            message = f"Categorized {result.updated} transaction(s)."
            if merchant_rules:
                merchant_result = repo.upsert_merchants(merchant_rules, identity.email)
                if merchant_result.error:
                    st.warning(f"Transactions saved, but merchant rules failed: {merchant_result.error}")
                else:
                    message += (
                        f" Learned {len(merchant_rules)} merchant rule(s) — "
                        f"these will categorize automatically next time."
                    )
            st.success(message)
            st.rerun()

# --- Confidence distribution -------------------------------------------------

with st.expander("Suggestion confidence across the queue"):
    shell.chart(
        charts.confidence_histogram(
            [value for value in pending["confidence"].tolist() if value > 0]
        ),
        key="queue_confidence",
    )
    st.caption(
        "Rules score 0.85–1.0, merchant memory 0.95, model suggestions carry the "
        "model's own confidence. Zero means nothing matched."
    )
