"""
My Plan -- a VP's basic budget planner.

One line per kind of spending (Food & Drink, Social Events, ...) with a planned
amount. What has been spent in each fills in from the ledger, so the sheet reads
Planned / Spent / Left, with a bar chart of plan against spending and a donut of
where the plan goes. Past spending can fill the amounts in, and the VP edits
from there.

This semester: does the plan fit the budget, and how is each category tracking?
Next semester: what the same season has cost before, a plan sheet compared with
last time, and a budget request for the treasurer.

For the committee VPs only: the page is in `nav.VP_PAGES` and not in the
treasurer's navigation. The committee comes from the signed-in profile, never
from a widget, so there is nothing a VP could change to see another committee's
plan. A plan is an estimate -- nothing here books, moves or reserves money --
and a request is a message, like a flag.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config import vp_committees
from ais_fmd.config.categories import PURPOSES, committee_name
from ais_fmd.data import repositories as repo
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.domain import committee_plan as plan
from ais_fmd.domain import vp_metrics
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import date_range_for_semester, default_semester, ordered_semesters
from ais_fmd.ui import plan_charts, shell

identity = auth.require(auth.Role.OFFICER)

shell.environment_banner()
shell.page_header("My Plan", "A simple budget for your committee: plan each kind of spending, then track it.")

_FLASH = "my_plan_flash"
_MONEY = st.column_config.NumberColumn(format="$%.2f")
flash = st.session_state.pop(_FLASH, None)
if flash:
    shell.notify("success", flash)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)
current = default_semester(bundle.transactions, bundle.terms)
if not semesters or current is None:
    shell.empty_state("No terms defined yet", "The treasurer sets terms up on the Treasury page.")
    st.stop()
upcoming = plan.next_semester_name(current)

# --- Which committee ----------------------------------------------------------

if identity.committee_id is None:
    shell.empty_state(
        "No committee assigned to your account",
        " Ask the treasurer to add your email under Officer Access.",
    )
    st.stop()

committee_id = identity.committee_id
line_ids = vp_committees.budget_ids_for(committee_id)
line_names = [committee_name(cid) for cid in line_ids]
title = vp_committees.title_for(committee_id)

# The page is only in a VP's navigation, but the role check keeps it honest: a
# read-only identity is also refused by the write layer.
can_edit = identity.committee_scoped
own = (int(committee_id),)

charges = bundle.transactions
charges = charges[charges["budget_category"].isin(line_ids)] if not charges.empty else charges


def _load(loader, *args) -> pd.DataFrame | None:
    """The saved rows, or None when this deployment cannot store plans yet."""
    try:
        return loader(*args)
    except Exception:  # noqa: BLE001 - a missing table is a deployment state, not an error
        return None


def _position(semester: str) -> dict:
    summary = budget_domain.budget_vs_actual(bundle.transactions, bundle.budgets, bundle.terms, semester)
    return vp_metrics.rollup(summary, line_names)


def _spent_in(semester: str) -> pd.DataFrame:
    window = date_range_for_semester(bundle.terms, semester)
    if window is None:
        return pd.DataFrame(columns=["Purpose", "Amount"])
    return plan.spending_by_purpose(charges, window[0], window[1])


def _save(semester: str, lines: list[dict], message: str) -> None:
    result = repo.replace_plan_lines(committee_id, semester, lines, own, identity.email)
    if result.error:
        shell.notify("warning", result.error)
        return
    st.session_state[_FLASH] = message
    st.rerun()


def _plan_editor(semester: str, saved: pd.DataFrame, sheet: pd.DataFrame, compare: str, key: str) -> None:
    """
    The budget sheet: Category and Planned are typed; `compare` (this term's
    Spent, or last time's spending for next term) and Left fill in.
    """
    notes = dict(zip(saved["purpose"], saved["note"])) if not saved.empty else {}
    frame = pd.DataFrame({
        "purpose": sheet["Category"],
        "amount": sheet["Planned"].where(sheet["Planned"] > 0),
        "compare": sheet[compare],
        "left": sheet["Planned"] - sheet[compare] if compare == "Spent" else None,
        "note": ["" if pd.isna(notes.get(c)) else str(notes.get(c)) for c in sheet["Category"]],
    })
    # An empty plan gives empty columns pandas types as numbers; the editor needs
    # text for the text columns and numbers for the money ones.
    frame = frame.astype({"purpose": "object", "note": "object", "amount": "float64", "compare": "float64"})
    # The VP's own category names saved earlier come back as choices too.
    options = [*PURPOSES, *plan.custom_categories(saved)]
    config = {
        "purpose": st.column_config.TextColumn("Category"),
        "amount": st.column_config.NumberColumn(
            "Planned", min_value=0.0, max_value=plan.MAX_AMOUNT, step=10.0, format="$%.2f"
        ),
        "compare": st.column_config.NumberColumn(compare, format="$%.2f", disabled=True),
        "left": st.column_config.NumberColumn("Left", format="$%.2f", disabled=True),
        "note": st.column_config.TextColumn("Note", max_chars=plan.MAX_NOTE_LENGTH),
    }
    order = ["purpose", "amount", "compare", *(["left"] if compare == "Spent" else []), "note"]

    if not can_edit:
        shell.dataframe(frame, column_config=config, column_order=order)
        return

    # Pick-or-type: the only table cell Streamlit lets a user both choose from a
    # list and type a new value into is the multiselect one, so Category is a
    # one-item list here. `clean_lines` unwraps it and refuses a second pick.
    frame["purpose"] = pd.Series(
        [[c] if isinstance(c, str) and c else [] for c in frame["purpose"]], index=frame.index, dtype="object"
    )
    config["purpose"] = st.column_config.MultiselectColumn(
        "Category", options=options, accept_new_options=True,
        help="Pick one from the list, or type your own and press Enter.",
    )

    frame["remove"] = False
    config["remove"] = st.column_config.CheckboxColumn("Remove", default=False)
    edited = st.data_editor(
        frame, column_config=config, column_order=["remove", *order],
        num_rows="dynamic", hide_index=True, width="stretch", key=f"{key}_editor",
    )
    st.caption(
        "Add a category in the bottom row: pick one from the list, or type your own and press "
        "Enter. Tick Remove (or clear Planned) to drop one. Then Save plan."
    )
    if compare == "Spent" and plan.custom_categories(saved):
        st.caption("Bank charges are only ever tagged with the listed categories, so your own ones show $0 spent.")
    if st.button("Save plan", key=f"{key}_save", type="primary"):
        kept, removed = plan.drop_removed(edited)
        # A row with no planned amount is a category the VP isn't planning (for
        # example one that only has spending): it isn't saved, and isn't an error.
        amounts = pd.to_numeric(kept["amount"], errors="coerce").fillna(0)
        lines, problems = plan.clean_lines(kept[amounts > 0])
        if problems:
            for problem in problems:
                shell.notify("warning", problem)
            return
        total = sum(line["amount"] for line in lines)
        _save(semester, lines, f"Saved {len(lines)} categor{'y' if len(lines) == 1 else 'ies'} "
                               f"({format_currency(total)}) for {semester}.")


def _charts(sheet: pd.DataFrame, compare: str, label: str, key: str) -> None:
    if sheet.empty:
        return
    bars, ring = st.columns([3, 2], gap="medium")
    with bars:
        st.markdown(f"**Planned vs {label.lower()}**")
        shell.chart(plan_charts.plan_vs_actual(sheet, actual=compare, actual_label=label), key=f"{key}_bars")
    with ring:
        st.markdown("**Where the plan goes**")
        shell.chart(plan_charts.plan_donut(plan.donut_slices(sheet)), key=f"{key}_donut")


def _from_history(semester: str, saved: pd.DataFrame, bases: dict[str, str], key: str) -> None:
    """Fill the planned amounts in from what a past semester actually spent."""
    if not can_edit or not bases:
        return
    with st.expander("Fill in from past spending"):
        basis = st.radio("Based on", list(bases), key=f"{key}_basis", horizontal=True)
        past = bases[basis]
        by_purpose = _spent_in(past)
        if by_purpose.empty:
            st.caption(f"{title} has no spending recorded in {past}, so there's nothing to copy.")
            return
        st.caption(
            f"What {title} spent in {past}, by category, rounded up to $5. Using it sets those "
            f"categories' planned amounts and keeps the rest of your plan."
        )
        # Leave it empty for every category, or pick just the ones to fill in.
        chosen = st.multiselect(
            "Only these categories", list(by_purpose["Purpose"]), key=f"{key}_pick",
            placeholder="All categories",
        )
        picked = by_purpose[by_purpose["Purpose"].isin(chosen)] if chosen else by_purpose
        new_lines = plan.lines_from_history(picked)
        shell.dataframe(
            pd.DataFrame({
                "Category": picked["Purpose"].values,
                "Spent then": picked["Amount"].values,
                "Plan": [plan.round_up(float(amount)) for amount in picked["Amount"]],
            }),
            column_config={"Spent then": _MONEY, "Plan": _MONEY},
        )
        what = (
            "all categories" if not chosen
            else chosen[0] if len(chosen) == 1
            else f"{len(chosen)} categories"
        )
        total = sum(line["amount"] for line in new_lines)
        if st.button(f"Use for {what} ({format_currency(total)})", key=f"{key}_use"):
            existing, _ = plan.clean_lines(saved) if not saved.empty else ([], [])
            lines = plan.merge_lines(existing, new_lines)
            _save(semester, lines, f"Filled in {what} from {past}. Change any you like.")

st.markdown(f"**Committee**  \n{title}")
if can_edit:
    st.caption("Your plan is your own estimate. It doesn't move or reserve any money.")
else:
    st.caption(f"Read-only: only {title}'s VP edits this plan.")

# A switch rather than tabs: tabs jump back to the first one after every save.
views = {"this": f"This semester · {current}", "next": f"Next semester · {upcoming}"}
view = st.radio(
    "Plan for", list(views), format_func=views.get, horizontal=True,
    key="plan_view", label_visibility="collapsed",
)

# Earlier semesters a plan can be copied from: the last one, and the same season a year before.
earlier = [name for name in semesters if name != current and semesters.index(name) < semesters.index(current)]
last_semester = earlier[-1] if earlier else None

# --- This semester ---------------------------------------------------------------

if view == "this":
    saved = _load(repo.load_plan_lines, own, current)
    if saved is None:
        shell.notify("info", "Plans can't be saved on this site yet. The treasurer has to switch the feature on first.")
        st.stop()

    sheet = plan.budget_sheet(saved, _spent_in(current))
    totals = plan.sheet_totals(sheet, _position(current)["budget"])
    shell.metric_row(
        [
            {"label": "Budget", "value": totals["budget"]},
            {"label": "Planned", "value": totals["planned"]},
            {"label": "Spent so far", "value": totals["spent"]},
            {"label": "Not yet planned", "value": totals["unplanned"]},
        ]
    )
    if totals["status"] == "over":
        shell.notify(
            "warning",
            f"The plan is {format_currency(-totals['unplanned'])} more than {title}'s budget. "
            f"Trim it, or talk to the treasurer.",
        )
    elif totals["status"] == "tight":
        shell.say(f"Tight: {format_currency(totals['unplanned'])} of the budget is left unplanned.", caption=True)
    elif totals["status"] == "fits":
        shell.say(f"The plan fits, with {format_currency(totals['unplanned'])} left unplanned.", caption=True)
    else:
        shell.say(f"{title} has no budget for {current}.", caption=True)

    _plan_editor(current, saved, sheet, "Spent", "plan_now")
    _charts(sheet, "Spent", "Spent", "plan_now")
    same_season = next((n for n in reversed(earlier) if plan.season_of(n) == plan.season_of(current)), None)
    _from_history(
        current, saved,
        {f"Last semester ({n})" if n == last_semester else f"Last {plan.season_of(n)} ({n})": n
         for n in dict.fromkeys(filter(None, [last_semester, same_season]))},
        "fill_now",
    )

# --- Next semester ---------------------------------------------------------------

else:
    if upcoming is None:
        shell.empty_state("Can't tell which semester comes next", f"'{current}' isn't named like 'Fall 2026'.")
        st.stop()

    season = plan.season_of(upcoming)
    spent_by, budget_by = {}, {}
    for name in semesters:
        if plan.season_of(name) == season:
            figures = _position(name)
            spent_by[name], budget_by[name] = figures["spent"], figures["budget"]
    history = plan.season_history(semesters, spent_by, budget_by, upcoming)
    guess = plan.suggestion(history)
    last_same = history["Term"].iloc[-1] if not history.empty else None

    st.markdown(f"#### What past {season} terms cost")
    estimate = 0.0
    if guess is None:
        st.caption(f"No earlier {season} term to go on, so there's no suggestion.")
    else:
        shell.dataframe(history, column_config={"Budget": _MONEY, "Spent": _MONEY})
        shell.say(
            f"A typical {season} cost {format_currency(guess['typical'])} "
            f"(lowest {format_currency(guess['low'])}, highest {format_currency(guess['high'])}, "
            f"across {guess['terms']} term(s)).",
            caption=True,
        )
        percent = st.slider(
            f"{upcoming} compared with a typical {season}", min_value=50, max_value=200, value=100,
            step=5, format="%d%%", key="plan_scale",
            help="100% means about the same as usual. Move it if you expect a bigger or smaller term.",
        )
        estimate = plan.scaled_estimate(guess["typical"], percent)
        shell.say(f"Rough estimate: {format_currency(estimate)}.", caption=True)

    saved = _load(repo.load_plan_lines, own, upcoming)
    if saved is None:
        shell.notify("info", "Plans can't be saved on this site yet. The treasurer has to switch the feature on first.")
        st.stop()

    st.markdown(f"#### Plan for {upcoming}")
    compare_label = f"Last {season}" if last_same else "Last time"
    reference = _spent_in(last_same) if last_same else pd.DataFrame(columns=["Purpose", "Amount"])
    sheet = plan.budget_sheet(saved, reference).rename(columns={"Spent": compare_label})
    planned_next = float(sheet["Planned"].sum()) if not sheet.empty else 0.0
    if planned_next:
        shell.say(f"Planned so far: {format_currency(planned_next)}.", caption=True)
    _plan_editor(upcoming, saved, sheet, compare_label, "plan_next")
    _charts(sheet, compare_label, compare_label, "plan_next")
    _from_history(
        upcoming, saved,
        {f"Last semester ({n})" if n == current else f"Last {season} ({n})": n
         for n in dict.fromkeys(filter(None, [current, last_same]))},
        "fill_next",
    )

    st.markdown("#### Budget request")
    requests = _load(repo.load_budget_requests, own, upcoming)
    if requests is None:
        st.caption("Budget requests can't be sent from this site yet.")
    else:
        sent = None if requests.empty else requests.iloc[0]
        if sent is not None:
            when = pd.to_datetime(sent["requested_at"], errors="coerce")
            shell.say(
                f"Sent {'' if pd.isna(when) else when.strftime('%b %d, %Y') + ' '}"
                f"for {format_currency(float(sent['amount']))}: {sent['reason']}",
                caption=True,
            )
        if can_edit:
            st.caption("The treasurer sets every budget. This tells them what you'd like and why.")
            amount = st.number_input(
                "Amount", min_value=0.0, max_value=plan.MAX_AMOUNT,
                value=float(sent["amount"]) if sent is not None else plan.default_request(planned_next, estimate),
                step=10.0, format="%.2f", key="plan_request_amount",
            )
            reason = st.text_area(
                "What it's for", value="" if sent is None else str(sent["reason"]),
                max_chars=plan.MAX_REASON_LENGTH, key="plan_request_reason",
            )
            label = "Update request" if sent is not None else "Send to the treasurer"
            if st.button(label, key="plan_request_send"):
                value, text, problem = plan.clean_request(amount, reason)
                if problem:
                    shell.notify("warning", problem)
                else:
                    result = repo.save_budget_request(committee_id, upcoming, value, text, own, identity.email)
                    if result.error:
                        shell.notify("warning", result.error)
                    else:
                        st.session_state[_FLASH] = f"Budget request for {upcoming} sent to the treasurer."
                        st.rerun()
        elif sent is None:
            st.caption(f"{title} hasn't sent a request for {upcoming}.")
