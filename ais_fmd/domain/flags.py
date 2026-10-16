"""
"This isn't ours" -- a VP disputing a charge.

A VP who thinks a charge is booked to the wrong committee flags it with a note.
The flag goes to the treasurer's Review Queue. The VP never changes the booking:
only the treasurer decides which committee money is booked to, so a flag is a
message, not an edit. Resolving or dismissing it is also the treasurer's call.

Pure rules and display helpers only -- no Streamlit, no database -- so they can
be tested on their own and shared by every backend.
"""

from __future__ import annotations

import pandas as pd

OPEN = "open"
RESOLVED = "resolved"
DISMISSED = "dismissed"

# What a treasurer may close a flag as. A flag is created open; never created
# resolved.
CLOSING_STATUSES = (RESOLVED, DISMISSED)

MIN_NOTE_LENGTH = 5
MAX_NOTE_LENGTH = 500

# How each status reads to a VP, who does not think in "dismissed".
_VP_LABELS = {
    OPEN: "With the treasurer",
    RESOLVED: "Resolved",
    DISMISSED: "Treasurer kept it as booked",
}


def clean_note(note: object) -> tuple[str | None, str | None]:
    """
    (cleaned note, None) when usable, or (None, reason) when not.

    A flag with no explanation is no use to the treasurer, who would have to ask
    the VP what they meant, so a note is required. An upper bound keeps one flag
    from becoming an essay (or a place to paste something that does not belong
    in a public-facing log).
    """
    if note is None or (isinstance(note, float) and pd.isna(note)):
        return None, "Say why this charge is not yours."
    text = " ".join(str(note).split())
    if len(text) < MIN_NOTE_LENGTH:
        return None, f"Add a few words on why (at least {MIN_NOTE_LENGTH} characters)."
    if len(text) > MAX_NOTE_LENGTH:
        return None, f"Keep it under {MAX_NOTE_LENGTH} characters (this is {len(text)})."
    return text, None


def vp_status_label(status: object) -> str:
    return _VP_LABELS.get(str(status), str(status))


def status_by_transaction(flags: pd.DataFrame) -> dict[int, str]:
    """
    Latest status per transaction, with an open flag always winning.

    A charge can be flagged, resolved, then flagged again later; the table that
    marks "which of my charges are flagged" should show the live one.
    """
    if flags is None or flags.empty:
        return {}
    ordered = flags.copy()
    ordered["_rank"] = ordered["status"].map(lambda value: 0 if value == OPEN else 1)
    ordered = ordered.sort_values(["_rank", "flag_id"], ascending=[True, False])
    first = ordered.drop_duplicates("transaction_id", keep="first")
    return {int(row["transaction_id"]): str(row["status"]) for _, row in first.iterrows()}


def with_charges(flags: pd.DataFrame, transactions: pd.DataFrame) -> pd.DataFrame:
    """Each flag next to the charge it is about (date, amount, details, booked to)."""
    if flags is None or flags.empty:
        return pd.DataFrame()
    charges = transactions[
        ["transactionid", "transaction_date", "amount", "details", "budget_category", "purpose"]
    ].rename(columns={"transactionid": "transaction_id"})
    return flags.merge(charges, on="transaction_id", how="left")


# --- What a VP is told about flagged charges ------------------------------------
#
# A VP flags a charge, and later the treasurer decides. Without a message the VP
# only finds out by opening a table on another page, and a charge that was moved
# *into* their committee just appears in their totals with no explanation. These
# rules turn flags into a short list of things worth saying on My Committee.

UPDATE_WINDOW_DAYS = 30

PENDING = "pending"
MOVED_OUT = "moved_out"
KEPT = "kept"
CLOSED = "closed"
ARRIVED = "arrived"

HEADLINES = {
    PENDING: "Waiting on the treasurer",
    MOVED_OUT: "Moved out of your committee",
    KEPT: "The treasurer kept it with your committee",
    CLOSED: "Closed by the treasurer; still booked to you",
    ARRIVED: "Moved into your committee",
}

NOTIFICATION_COLUMNS = [
    "when", "kind", "headline", "transaction_date", "amount", "details", "reply",
]


def _moment(series: pd.Series) -> pd.Series:
    """Timestamps from either database, as naive datetimes (so they compare)."""
    return pd.to_datetime(series, errors="coerce", utc=True).dt.tz_localize(None)


def _clean(value: object) -> str:
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def notifications(
    own_flags: pd.DataFrame | None,
    arrivals: pd.DataFrame | None,
    transactions: pd.DataFrame,
    line_ids: tuple[int, ...],
    *,
    today: pd.Timestamp,
    window_days: int = UPDATE_WINDOW_DAYS,
) -> pd.DataFrame:
    """
    Things worth telling a committee about, newest first.

    `own_flags` are flags this committee raised (so the VP already knows the
    charge). Each says where it stands: waiting, moved out, kept, or closed with
    the charge still theirs -- the last two derived from where the charge is
    booked *now*, not from the wording of a note.

    `arrivals` are charges the treasurer moved *into* this committee because
    someone else flagged them. They carry only the charge itself: who flagged
    it, why, and which committee it came from are not passed in, so they cannot
    be shown.

    A waiting flag stays until it is answered. Answered ones drop off after
    `window_days`, so the list says what is new rather than growing forever.
    """
    ids = {int(i) for i in line_ids}
    cutoff = today - pd.Timedelta(days=window_days)
    rows: list[dict] = []

    for _, flag in with_charges(
        own_flags if own_flags is not None else pd.DataFrame(), transactions
    ).iterrows():
        status = str(flag["status"])
        if status == OPEN:
            when = _moment(pd.Series([flag["flagged_at"]])).iloc[0]
            kind = PENDING
        else:
            when = _moment(pd.Series([flag["resolved_at"]])).iloc[0]
            if pd.isna(when) or when < cutoff:
                continue
            if status == DISMISSED:
                kind = KEPT
            else:
                booked = flag["budget_category"]
                still_here = not pd.isna(booked) and int(booked) in ids
                kind = CLOSED if still_here else MOVED_OUT
        rows.append(
            {
                "when": when,
                "kind": kind,
                "transaction_date": flag["transaction_date"],
                "amount": flag["amount"],
                "details": _clean(flag["details"]),
                "reply": _clean(flag.get("resolution_note")),
            }
        )

    if arrivals is not None and not arrivals.empty:
        for _, charge in arrivals.iterrows():
            when = _moment(pd.Series([charge["resolved_at"]])).iloc[0]
            if pd.isna(when) or when < cutoff:
                continue
            rows.append(
                {
                    "when": when,
                    "kind": ARRIVED,
                    "transaction_date": charge["transaction_date"],
                    "amount": charge["amount"],
                    "details": _clean(charge["details"]),
                    "reply": "",
                }
            )

    if not rows:
        return pd.DataFrame(columns=NOTIFICATION_COLUMNS)
    frame = pd.DataFrame(rows)
    frame["headline"] = frame["kind"].map(HEADLINES)
    return frame.sort_values("when", ascending=False).reset_index(drop=True)[NOTIFICATION_COLUMNS]
