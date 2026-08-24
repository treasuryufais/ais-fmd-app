"""
AIS event calendar used as a signal for auto-categorization.

Bank details tell you *where* money was spent; the event calendar tells you
*why*. A Publix run on a Tuesday is Meeting Food if there is a GBM the next
day, and Membership food if the only thing on the calendar is a snacks social.

The calendar is used in two places (see treasury_auto_categorize.py):
  1. Every transaction sent to the LLM carries a short summary of the events
     within a day of the purchase.
  2. Rows that neither the LLM nor the deterministic Python overrides could
     categorize fall back to a *strong* same-day/next-day event hint.

It never overrides the deterministic rules — those are higher confidence than
a date match.

Coverage is per semester. Dates outside a listed semester return no hints at
all, so transactions from semesters that have not been entered here are left
alone rather than guessed at. To add a semester: add its date range to
SEMESTER_RANGES and its events to _RAW_EVENTS.

No Streamlit or pandas import — this module is safe to use from scripts.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import NamedTuple

# Hint strength.
#   STRONG — the calendar is the primary evidence for this event's spending and
#            the merchant name alone would not resolve it (GBM food, consulting
#            meetings, formal, a tab at a venue whose name has no bar keyword).
#            Strong hints can fill in an otherwise-uncategorized row.
#   WEAK   — the event happened, but what was spent on it is a guess (a
#            basketball game, study hours). Weak hints are context for the LLM
#            only; they never assign a category on their own.
STRONG = "strong"
WEAK = "weak"

# Local copy of the committee display names. Kept here rather than imported
# from treasury_auto_categorize to avoid a circular import.
_COMMITTEE_NAME = {
    1: "Dues",
    5: "Membership",
    7: "Consulting",
    8: "Meeting Food",
    9: "Marketing",
    10: "Professional Development",
    16: "Passport",
    17: "Refunded",
    18: "Formal",
}

SEMESTER_RANGES = {
    "Spring 2026": ("2026-01-04", "2026-05-02"),
}


class Event(NamedTuple):
    day: date
    title: str
    location: str
    kind: str
    committee: int | None  # committee this event's spending rolls up to
    strength: str


# (iso date, title, location, kind, committee, strength)
#
# committee=None means nobody in the org owns that spending — outside events
# (ISOM mixer, the ISOM banquet, a concert) and graduation. Those still show up
# as LLM context but never drive a fallback assignment.
_RAW_EVENTS = (
    # --- January ---
    ("2026-01-09", "Orientation Tab", "MacDintons", "Social", 5, STRONG),
    ("2026-01-10", "Basketball Game", "O'Dome", "Social", 5, WEAK),
    ("2026-01-12", "Coffee and Pastries", "Hough ground floor", "Social", 5, STRONG),
    ("2026-01-14", "GBM 1 - Welcome to AIS + Postgame", "", "GBM", 8, STRONG),
    ("2026-01-15", "Snacks Social", "Hough", "Social", 5, STRONG),
    ("2026-01-17", "Mystery Tab", "Barcade", "Social", 5, STRONG),
    ("2026-01-21", "GBM 2 - Do you ReMember", "", "GBM", 8, STRONG),
    ("2026-01-21", "Champagne and Shackles", "Conall's Apt", "Social", 5, WEAK),
    ("2026-01-23", "Headshots", "Outside Hough", "Marketing", 9, STRONG),
    ("2026-01-24", "Basketball Game + Going Out", "O'Dome", "Social", 5, WEAK),
    ("2026-01-27", "Bowling", "Reitz", "Social", 5, WEAK),
    ("2026-01-28", "GBM 3", "", "GBM", 8, STRONG),
    ("2026-01-30", "Gymnastics Meet", "O'Dome", "Social", 5, WEAK),
    # --- February ---
    ("2026-02-02", "Study Hours", "", "Social", 5, WEAK),
    ("2026-02-02", "ISOM Mixer", "Press Deck @ Ben Hill Griffin", "External", None, WEAK),
    ("2026-02-04", "GBM 4 - KPMG", "", "GBM", 8, STRONG),
    ("2026-02-05", "Dog Tab", "Salty Dog", "Social", 5, STRONG),
    ("2026-02-06", "Consulting Kickoff", "Jacksonville", "Consulting", 7, STRONG),
    ("2026-02-07", "Hot Dog Hop Pregame", "Stadium house", "Social", 5, STRONG),
    ("2026-02-08", "Superbowl Sunday", "First Magnitude Brewing", "Social", 5, STRONG),
    ("2026-02-09", "Consulting Meeting", "Hough 120", "Consulting", 7, STRONG),
    ("2026-02-11", "GBM 5 - Shark Tank", "", "GBM", 8, STRONG),
    ("2026-02-11", "Valentines Flowers", "", "Social", 5, WEAK),
    ("2026-02-13", "Baseball Game", "", "Social", 5, WEAK),
    ("2026-02-16", "Consulting Meeting", "Hough 120", "Consulting", 7, STRONG),
    ("2026-02-17", "Basketball Game", "O'Dome", "Social", 5, WEAK),
    ("2026-02-18", "GBM 6 - Deloitte GPS", "", "GBM", 8, STRONG),
    ("2026-02-20", "Barcade Tab + Pregame", "Barcade", "Social", 5, STRONG),
    ("2026-02-23", "Consulting Meeting", "Hough 120", "Consulting", 7, STRONG),
    ("2026-02-23", "Study Hours", "Hough 120", "Social", 5, WEAK),
    # --- March ---
    ("2026-03-02", "Consulting Meeting", "Hough 120", "Consulting", 7, STRONG),
    ("2026-03-03", "Basketball Game", "", "Social", 5, WEAK),
    ("2026-03-04", "GBM 6 (second numbering)", "", "GBM", 8, STRONG),
    ("2026-03-06", "Stadiums", "Ben Hill Griffin", "Social", 5, WEAK),
    ("2026-03-07", "Pool Day", "The Retreat", "Social", 5, WEAK),
    ("2026-03-07", "House Party - St. Patty's (NUTRL sponsor)", "Arman's", "Social", 5, STRONG),
    ("2026-03-08", "Gymnastics Meet", "", "Social", 5, WEAK),
    ("2026-03-09", "Consulting Meeting (midpoint)", "Hough 120", "Consulting", 7, STRONG),
    ("2026-03-10", "Pottery Social + Dog Trivia", "Reitz / Salty Dog", "Social", 5, STRONG),
    # Mar 15-21 is Spring Break — intentionally empty.
    ("2026-03-23", "Consulting Meeting", "Hough 120", "Consulting", 7, STRONG),
    ("2026-03-24", "Workshop 1 - Power BI", "", "Workshop", 10, STRONG),
    ("2026-03-25", "GBM 7", "", "GBM", 8, STRONG),
    ("2026-03-29", "Passport Potluck", "Hough 120", "Passport", 16, STRONG),
    ("2026-03-31", "Workshop 2 - Resumes & Interviews 101", "", "Workshop", 10, STRONG),
    # --- April ---
    ("2026-04-02", "Workshop 2 (alt date)", "", "Workshop", 10, STRONG),
    ("2026-04-04", "Passport Pickleball", "Flavet", "Passport", 16, STRONG),
    ("2026-04-05", "Easter", "", "Social", 5, WEAK),
    ("2026-04-06", "Consulting Meeting + Rowdy Karaoke", "Rowdy's", "Consulting", 7, STRONG),
    ("2026-04-07", "Beer Olympics", "Arman's", "Social", 5, STRONG),
    ("2026-04-08", "GBM 8 + Champagne and Shackles", "Conall's", "GBM", 8, STRONG),
    ("2026-04-09", "Formal", "Whoopis", "Formal", 18, STRONG),
    ("2026-04-10", "Beach Day + $1 Shots Tab", "Rowdy's", "Social", 5, STRONG),
    ("2026-04-11", "Orange & Blue Tailgate + Dog Tab", "Grace's house / Salty Dog", "Social", 5, STRONG),
    ("2026-04-12", "Lake Wauburg Passport", "Lake Wauburg", "Passport", 16, STRONG),
    ("2026-04-13", "Consulting Meeting", "Hough 120", "Consulting", 7, STRONG),
    ("2026-04-14", "Chainsmokers SGP", "Flavet Field", "External", None, WEAK),
    ("2026-04-15", "GBM 9 - Goodbyes", "", "GBM", 8, STRONG),
    # Weak on purpose: shares a day with GBM 9, and two conflicting strong hints
    # would cancel each other out. The GBM is the larger, more predictable spend.
    ("2026-04-15", "Workshop 3 - AWS", "Hough 250", "Workshop", 10, WEAK),
    ("2026-04-16", "ISOM Forum Awards Banquet", "", "External", None, WEAK),
    ("2026-04-17", "Consulting Final Presentations", "Jacksonville", "Consulting", 7, STRONG),
    ("2026-04-19", "Passport 4 - Hibachi", "", "Passport", 16, STRONG),
    ("2026-04-20", "Study Hours", "", "Social", 5, WEAK),
    ("2026-04-23", "Bar Golf", "", "Social", 5, STRONG),
    ("2026-04-25", "Redneck Wedding", "Andre's house", "Social", 5, STRONG),
    ("2026-04-27", "Grad Pics", "", "Marketing", 9, STRONG),
    ("2026-04-28", "Open to Close Dog Seniors", "Salty Dog", "Social", 5, STRONG),
    # --- May ---
    ("2026-05-01", "Graduation", "", "Other", None, WEAK),
)


def _iso(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


EVENTS: tuple[Event, ...] = tuple(
    sorted(
        (Event(_iso(d), title, loc, kind, cid, strength) for d, title, loc, kind, cid, strength in _RAW_EVENTS),
        key=lambda e: (e.day, e.title),
    )
)

_BY_DAY: dict[date, list[Event]] = {}
for _event in EVENTS:
    _BY_DAY.setdefault(_event.day, []).append(_event)

_COVERAGE: tuple[tuple[date, date], ...] = tuple(
    (_iso(start), _iso(end)) for start, end in SEMESTER_RANGES.values()
)

# Described from the transaction's point of view: "next day" means the event is
# the day after the purchase (a grocery run for tomorrow's GBM).
_OFFSET_LABEL = {0: "same day", 1: "next day", -1: "previous day"}


# --- Date handling ---

def coerce_date(value) -> date | None:
    """Best-effort conversion of a date, datetime, or common date string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Fall back to the leading ISO date of a timestamp string.
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def is_covered(value) -> bool:
    """True if the date falls inside a semester listed in SEMESTER_RANGES."""
    d = coerce_date(value)
    if d is None:
        return False
    return any(start <= d <= end for start, end in _COVERAGE)


def semester_for(value) -> str | None:
    d = coerce_date(value)
    if d is None:
        return None
    for name, (start, end) in SEMESTER_RANGES.items():
        if _iso(start) <= d <= _iso(end):
            return name
    return None


def _offsets(window_days: int) -> list[int]:
    """Day offsets ordered by how much weight they carry.

    0 first (spent at the event), then +1 (bought the day before, e.g. a
    grocery run for tomorrow's GBM), then -1 (charge landed a day late).
    """
    order = [0]
    for step in range(1, max(window_days, 0) + 1):
        order.extend((step, -step))
    return order


# --- Lookups ---

def events_near(value, window_days: int = 1) -> list[tuple[Event, int]]:
    """Events within window_days of the given date, closest and forward first.

    Returns (event, offset) where offset is event_day - transaction_day.
    """
    d = coerce_date(value)
    if d is None:
        return []
    found: list[tuple[Event, int]] = []
    for offset in _offsets(window_days):
        for event in _BY_DAY.get(d + timedelta(days=offset), ()):
            found.append((event, offset))
    return found


def event_context(value, window_days: int = 1) -> str:
    """One-line summary of nearby events, for the LLM prompt.

    Example: "same day: GBM 4 - KPMG [Meeting Food]; next day: Study Hours [Membership?]"

    A committee in brackets is where that event's spending belongs. A trailing
    "?" marks a weak association — the event happened, but what was spent on it
    is a guess, so it is only good enough to break a tie.

    Returns "none" when nothing is scheduled, and "outside calendar" for dates
    from semesters that have not been entered.
    """
    d = coerce_date(value)
    if d is None:
        return "unknown date"
    if not is_covered(d):
        return "outside calendar"

    parts = []
    for event, offset in events_near(d, window_days):
        when = _OFFSET_LABEL.get(offset, f"{offset:+d} days")
        where = f" @ {event.location}" if event.location else ""
        if event.committee is None:
            committee = "no committee"
        else:
            committee = _COMMITTEE_NAME.get(event.committee, str(event.committee))
            if event.strength == WEAK:
                committee += "?"
        parts.append(f"{when}: {event.title}{where} [{committee}]")
    return "; ".join(parts) if parts else "none"


def strong_hint(value, window_days: int = 1) -> tuple[int | None, str]:
    """The committee a strong nearby event points to, or (None, "").

    Only the closest day that has any strong event is considered. If that day's
    strong events disagree on a committee the hint is dropped rather than
    guessed — the LLM and the treasurer still see all of them via
    event_context().
    """
    d = coerce_date(value)
    if d is None or not is_covered(d):
        return None, ""

    for offset in _offsets(window_days):
        strong = [
            e for e in _BY_DAY.get(d + timedelta(days=offset), ())
            if e.strength == STRONG and e.committee is not None
        ]
        if not strong:
            continue
        committees = {e.committee for e in strong}
        if len(committees) > 1:
            return None, ""
        when = _OFFSET_LABEL.get(offset, f"{offset:+d} days")
        titles = " / ".join(e.title for e in strong)
        return strong[0].committee, f"{titles} ({when})"
    return None, ""


def strong_events_on(value) -> list[Event]:
    """Strong, committee-bearing events on exactly this day (no window)."""
    d = coerce_date(value)
    if d is None or not is_covered(d):
        return []
    return [e for e in _BY_DAY.get(d, ()) if e.strength == STRONG and e.committee is not None]


def gbm_dates() -> tuple[date, ...]:
    """Every GBM on the calendar — the dates that drive Meeting Food."""
    return tuple(e.day for e in EVENTS if e.kind == "GBM")


def coverage_summary() -> str:
    ranges = ", ".join(f"{name} ({start} to {end})" for name, (start, end) in SEMESTER_RANGES.items())
    return f"{len(EVENTS)} events across: {ranges}"
