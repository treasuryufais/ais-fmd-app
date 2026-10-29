"""
A VP's budget plan: how much they mean to spend on each kind of spending this
semester, and what they think next semester needs.

The plan is a basic budget sheet -- one line per category (the app's purposes:
Food & Drink, Social Events, ...) with a planned amount. What has been spent in
each category fills in from the ledger, so the VP sees Planned / Spent / Left
without typing anything else. Two things are saved per committee and semester:

  * plan lines -- a category and an amount. A plan is a VP's own estimate;
    nothing here books, moves or reserves money.
  * one budget request for an upcoming semester -- an amount and a reason. The
    treasurer still sets every budget; a request is a message, like a flag.

This module holds the rules and the arithmetic only: no Streamlit, no database,
so it is tested on its own and shared by every backend.
"""

from __future__ import annotations

import math
import re

import pandas as pd

from ..config.categories import PURPOSES

MAX_NOTE_LENGTH = 200
MAX_AMOUNT = 100_000.0
MIN_REASON_LENGTH = 10
MAX_REASON_LENGTH = 500

# Left unplanned, as a share of the budget, below which the plan is "tight".
TIGHT_SHARE = 0.10

# The columns a page edits.
LINE_FIELDS = ("purpose", "amount", "note")

# A VP may type a category of their own instead of picking one from the list.
MAX_CATEGORY_LENGTH = 40

# Category a past charge with no purpose (or a retired one) is counted under.
FALLBACK_PURPOSE = "Misc."

# Slices in the "where the plan goes" donut before the rest fold into "Other".
MAX_SLICES = 6

_SEMESTER_RE = re.compile(r"^\s*(Fall|Spring|Summer|Winter)\s+(\d{4})\s*$", re.IGNORECASE)


# --- Semesters -----------------------------------------------------------------


def season_of(semester: str | None) -> str | None:
    """'Fall 2026' -> 'Fall'. None for anything that is not 'Season YYYY'."""
    match = _SEMESTER_RE.match(semester or "")
    return match.group(1).title() if match else None


def next_semester_name(semester: str | None) -> str | None:
    """
    The semester after this one, by name. The org runs Fall and Spring terms, so
    Fall -> next Spring and Spring -> Fall; a Summer or Winter term is followed by
    the next of those two. Works when the next term has not been set up yet,
    which is exactly when a VP wants to plan for it.
    """
    match = _SEMESTER_RE.match(semester or "")
    if not match:
        return None
    season, year = match.group(1).title(), int(match.group(2))
    if season == "Fall":
        return f"Spring {year + 1}"
    if season == "Winter":
        return f"Spring {year}"
    return f"Fall {year}"


# --- Plan lines ----------------------------------------------------------------


def _blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def custom_categories(saved: pd.DataFrame | None) -> list[str]:
    """The VP's own category names in a saved plan -- anything not a standard purpose."""
    if saved is None or saved.empty:
        return []
    return sorted({str(p) for p in saved["purpose"].dropna() if str(p) not in PURPOSES})


def clean_lines(rows: pd.DataFrame | list[dict]) -> tuple[list[dict], list[str]]:
    """
    (lines ready to save, problems) from what a VP typed.

    A row left entirely blank is dropped (the editor's empty last row). Any other
    row needs one category and an amount above zero. The category is one of the
    app's purposes or a name the VP typed; the page's category cell can hold a
    list (it is a pick-or-type box), so a list of one is unwrapped and a list of
    several is refused. A typed name that is really a standard one ("food &
    drink") becomes the standard one. Each category appears once -- it is a
    budget sheet, so two "Food & Drink" lines would be one line the VP meant to
    add up.
    """
    standard = {name.lower(): name for name in PURPOSES}
    records = rows.to_dict("records") if isinstance(rows, pd.DataFrame) else list(rows)
    lines: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for position, row in enumerate(records, start=1):
        values = {field: row.get(field) for field in LINE_FIELDS}
        if all(_blank(value) or (isinstance(value, (list, tuple)) and not value) for value in values.values()):
            continue

        picked = values["purpose"]
        if isinstance(picked, (list, tuple)) or hasattr(picked, "tolist"):
            picked = [p for p in list(picked) if not _blank(p)]
            if len(picked) > 1:
                problems.append(f"Row {position}: one category per line ({', '.join(map(str, picked))}).")
                continue
            picked = picked[0] if picked else None
        purpose = None if _blank(picked) else " ".join(str(picked).split())
        if purpose is None:
            problems.append(f"Row {position}: pick a category.")
            continue
        if len(purpose) > MAX_CATEGORY_LENGTH:
            problems.append(f"Row {position}: keep the category name under {MAX_CATEGORY_LENGTH} characters.")
            continue
        purpose = standard.get(purpose.lower(), purpose)
        if purpose.lower() in seen:
            problems.append(f"{purpose} is listed twice. Put it on one line.")
            continue

        try:
            amount = float(values["amount"])
        except (TypeError, ValueError):
            amount = float("nan")
        if math.isnan(amount) or amount <= 0:
            problems.append(f"{purpose}: enter a planned amount above $0.")
            continue
        if amount > MAX_AMOUNT:
            problems.append(f"{purpose}: that amount looks too large.")
            continue

        note = "" if _blank(values["note"]) else str(values["note"]).strip()[:MAX_NOTE_LENGTH]
        seen.add(purpose.lower())
        lines.append({"purpose": purpose, "amount": round(amount, 2), "note": note or None})
    return lines, problems


def drop_removed(rows: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    (rows to keep, how many were removed) from an edited plan whose "remove"
    column a VP ticks to delete a line. Saving then replaces the whole plan, so a
    removed line is gone. Ticked rows that were blank anyway aren't counted.
    """
    if rows is None or rows.empty or "remove" not in rows.columns:
        return rows, 0
    ticked = rows["remove"].fillna(False).astype(bool)
    removed = rows[ticked]
    real = sum(
        1 for _, row in removed.iterrows() if not all(_blank(row.get(field)) for field in LINE_FIELDS)
    )
    return rows[~ticked].drop(columns="remove"), real


def merge_lines(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """
    `existing` with `incoming` laid over it: a category in both takes the incoming
    amount, and categories only in `existing` are kept. Used when a VP fills the
    plan in from past spending -- it updates the amounts, it doesn't wipe the plan.
    """
    merged = {line["purpose"]: dict(line) for line in existing}
    for line in incoming:
        kept_note = merged.get(line["purpose"], {}).get("note")
        merged[line["purpose"]] = {**line, "note": line.get("note") or kept_note}
    return list(merged.values())


# --- The budget sheet ------------------------------------------------------------


def spending_by_purpose(charges: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """
    Purpose | Amount: money out between `start` and `end` (inclusive), by purpose,
    largest first. Charges with no purpose (or one the app no longer uses) are
    counted under `FALLBACK_PURPOSE`, so every dollar lands in some category.
    """
    columns = ["Purpose", "Amount"]
    if charges is None or charges.empty:
        return pd.DataFrame(columns=columns)
    dates = pd.to_datetime(charges["transaction_date"], errors="coerce")
    amounts = pd.to_numeric(charges["amount"], errors="coerce").fillna(0)
    inside = (dates >= start.normalize()) & (dates <= end.normalize()) & (amounts < 0)
    if not inside.any():
        return pd.DataFrame(columns=columns)
    purposes = charges.loc[inside, "purpose"].where(charges.loc[inside, "purpose"].isin(PURPOSES), FALLBACK_PURPOSE)
    out = (-amounts[inside]).groupby(purposes.values).sum()
    frame = out.rename_axis("Purpose").reset_index(name="Amount")
    return frame.sort_values("Amount", ascending=False, kind="stable").reset_index(drop=True)


def budget_sheet(lines: pd.DataFrame | None, spent: pd.DataFrame | None) -> pd.DataFrame:
    """
    Category | Planned | Spent | Left, one row per category that is planned or
    has spending. A category with spending but no plan still shows (Planned $0),
    so money can't hide by being off the plan. Biggest plan first, then the rest
    by spending.
    """
    planned = (
        {} if lines is None or lines.empty
        else dict(zip(lines["purpose"], pd.to_numeric(lines["amount"], errors="coerce").fillna(0)))
    )
    actual = {} if spent is None or spent.empty else dict(zip(spent["Purpose"], spent["Amount"]))
    categories = list(dict.fromkeys([*planned, *actual]))
    sheet = pd.DataFrame({
        "Category": categories,
        "Planned": [float(planned.get(c, 0.0)) for c in categories],
        "Spent": [float(actual.get(c, 0.0)) for c in categories],
    }, columns=["Category", "Planned", "Spent"])
    sheet["Left"] = sheet["Planned"] - sheet["Spent"]
    return sheet.sort_values(["Planned", "Spent"], ascending=False, kind="stable").reset_index(drop=True)


def sheet_totals(sheet: pd.DataFrame, budget: float) -> dict:
    """
    The headline: budget, planned, spent, and what the plan leaves unplanned.
    Status is "over" when the plan is bigger than the budget, "tight" when less
    than `TIGHT_SHARE` of the budget is left unplanned, "fits" otherwise, and
    "no budget" when the committee has no budget this term.
    """
    planned = float(sheet["Planned"].sum()) if not sheet.empty else 0.0
    spent = float(sheet["Spent"].sum()) if not sheet.empty else 0.0
    unplanned = budget - planned
    if budget <= 0:
        status = "no budget"
    elif unplanned < 0:
        status = "over"
    elif unplanned < budget * TIGHT_SHARE:
        status = "tight"
    else:
        status = "fits"
    return {"budget": budget, "planned": planned, "spent": spent, "unplanned": unplanned, "status": status}


def donut_slices(sheet: pd.DataFrame, max_slices: int = MAX_SLICES) -> pd.DataFrame:
    """
    Category | Planned for the "where the plan goes" donut: the biggest
    `max_slices - 1` categories, the rest folded into "Other" so the ring never
    needs more colours than it has.
    """
    if sheet is None or sheet.empty:
        return pd.DataFrame(columns=["Category", "Planned"])
    planned = sheet[sheet["Planned"] > 0][["Category", "Planned"]]
    if planned.empty:
        return pd.DataFrame(columns=["Category", "Planned"])
    planned = planned.sort_values("Planned", ascending=False, kind="stable").reset_index(drop=True)
    if len(planned) <= max_slices:
        return planned
    head = planned.iloc[: max_slices - 1]
    other = pd.DataFrame({"Category": ["Other"], "Planned": [float(planned.iloc[max_slices - 1:]["Planned"].sum())]})
    return pd.concat([head, other], ignore_index=True)


# --- Filling a plan in from past spending -----------------------------------------


def round_up(amount: float, step: float = 5.0) -> float:
    return float(math.ceil(amount / step) * step) if amount > 0 else 0.0


def lines_from_history(by_purpose: pd.DataFrame) -> list[dict]:
    """Plan lines that repeat past spending, one per category, rounded up to $5."""
    return [
        {"purpose": row.Purpose, "amount": round_up(float(row.Amount)), "note": None}
        for row in by_purpose.itertuples(index=False)
        if round_up(float(row.Amount)) > 0
    ]


# --- Next semester -------------------------------------------------------------


def season_history(
    semesters: list[str],
    spent_by_semester: dict[str, float],
    budget_by_semester: dict[str, float],
    target: str,
) -> pd.DataFrame:
    """
    Term | Budget | Spent for the earlier terms of the same season as `target`
    (Fall plans from past Falls: dues and events make the seasons different).
    Only terms before `target`, and only ones with something recorded.
    """
    season = season_of(target)
    target_match = _SEMESTER_RE.match(target or "")
    target_year = int(target_match.group(2)) if target_match else None
    rows = []
    for name in semesters:
        if season_of(name) != season or name == target:
            continue
        match = _SEMESTER_RE.match(name)
        if target_year is not None and match and int(match.group(2)) >= target_year:
            continue
        spent = float(spent_by_semester.get(name, 0.0) or 0.0)
        budget = float(budget_by_semester.get(name, 0.0) or 0.0)
        if spent == 0 and budget == 0:
            continue
        rows.append({"Term": name, "Budget": budget, "Spent": spent})
    return pd.DataFrame(rows, columns=["Term", "Budget", "Spent"])


def suggestion(history: pd.DataFrame) -> dict | None:
    """
    A range for next term from what the same season has cost before: the lowest,
    the middle (median) and the highest. None with no history to go on.
    """
    if history is None or history.empty:
        return None
    spent = history["Spent"].astype(float)
    # Rounded to cents here, so the page's "typical" and a 100% estimate always agree.
    return {
        "low": round(float(spent.min()), 2),
        "typical": round(float(spent.median()), 2),
        "high": round(float(spent.max()), 2),
        "terms": int(len(spent)),
    }


def scaled_estimate(typical: float, percent: float) -> float:
    """The typical term, scaled by the VP's slider (100 = same as usual)."""
    return round(max(typical, 0.0) * max(percent, 0.0) / 100.0, 2)


def default_request(planned: float, estimate: float) -> float:
    """What to pre-fill a budget request with: the larger of the two, rounded up to $10."""
    largest = max(planned, estimate, 0.0)
    return float(math.ceil(largest / 10.0) * 10) if largest else 0.0


def clean_request(amount: object, reason: object) -> tuple[float | None, str | None, str | None]:
    """(amount, reason, None) when the request can be sent, else (None, None, problem)."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        value = float("nan")
    if math.isnan(value) or value <= 0:
        return None, None, "Ask for an amount above $0."
    if value > MAX_AMOUNT:
        return None, None, "That amount looks too large."
    text = "" if _blank(reason) else str(reason).strip()
    if len(text) < MIN_REASON_LENGTH:
        return None, None, "Say in a sentence what the money is for."
    if len(text) > MAX_REASON_LENGTH:
        return None, None, f"Keep the reason under {MAX_REASON_LENGTH} characters."
    return round(value, 2), text, None
