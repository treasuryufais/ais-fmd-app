"""
Module M7 -- dues and membership revenue.

Dues were already special-cased in the categorizer (the $35 / $52.50 rule), but
nothing ever reported on them. They are the organisation's most predictable
income line and the one a treasurer is most often asked about: how many people
have paid, how much is outstanding, and is collection tracking last year.

Payer identity is recovered from the transfer description. Wells Fargo Zelle
rows read "ZELLE FROM JANE DOE ON 07/08 REF # ...", and Venmo rows are
pipe-joined with the sender in the third field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from .categorize.predicates import DUES_AMOUNTS, DuesSchedule, DuesWindow
from .money import parse_amount
from .terms import attach_semester, date_range_for_semester

DUES_COMMITTEE_ID = 1


def parse_rates(value: object) -> tuple[Decimal, ...]:
    """
    Parse a stored `dues_rates` cell ("35.00,52.50") into decimals.

    Tolerant by design: a malformed cell yields no rates, and the caller then
    falls back to the default rather than raising during a page render.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ()
    rates: list[Decimal] = []
    for part in str(value).replace(";", ",").split(","):
        part = part.strip().lstrip("$")
        if not part:
            continue
        try:
            rates.append(Decimal(part))
        except (ArithmeticError, ValueError):
            continue
    return tuple(sorted(set(rates)))


def format_rates(rates: "tuple[Decimal, ...] | list[Decimal]") -> str:
    """Inverse of `parse_rates`, for writing the column back."""
    return ",".join(f"{Decimal(rate):.2f}" for rate in sorted(set(rates)))


# Memo for `schedule_from_terms`, keyed on the terms table's *contents* rather
# than object identity -- a caller passing a fresh copy of the same data must
# hit, and a caller who edited a rate must miss. Same reasoning, and same
# bounded size, as `terms._INDEX_CACHE`.
_SCHEDULE_CACHE: dict[tuple, DuesSchedule] = {}
_SCHEDULE_CACHE_LIMIT = 32


def _schedule_fingerprint(
    df_terms: pd.DataFrame, default_rates: tuple[Decimal, ...], accept_venmo_net: bool
) -> tuple | None:
    """Hashable summary, or None to disable caching rather than risk a wrong hit."""
    required = ("TermID", "start_date", "end_date", "dues_rates")
    if df_terms is None or not all(column in df_terms.columns for column in required):
        return None
    try:
        rows = tuple(
            (str(term), str(start), str(end), str(rates), str(verified))
            for term, start, end, rates, verified in zip(
                df_terms["TermID"],
                df_terms["start_date"],
                df_terms["end_date"],
                df_terms["dues_rates"],
                df_terms.get("dues_rates_verified", [None] * len(df_terms)),
            )
        )
    except TypeError:
        return None
    return (rows, tuple(str(r) for r in default_rates), accept_venmo_net)


def schedule_from_terms(
    df_terms: pd.DataFrame,
    *,
    default_rates: tuple[Decimal, ...] = DUES_AMOUNTS,
    accept_venmo_net: bool = False,
) -> DuesSchedule:
    """
    Build the categorizer's dues schedule from the terms table.

    Terms with no stored rates are simply absent from the schedule, so they fall
    back to `default_rates` -- which is exactly the pre-existing behaviour.

    Memoized: the dues reporting functions each build one, and rebuilding it per
    call cost ~6 ms on `compare_semesters` alone.
    """
    if df_terms is None or df_terms.empty or "dues_rates" not in df_terms.columns:
        return DuesSchedule(
            default_rates=default_rates, accept_venmo_net=accept_venmo_net
        )

    key = _schedule_fingerprint(df_terms, default_rates, accept_venmo_net)
    if key is not None and key in _SCHEDULE_CACHE:
        return _SCHEDULE_CACHE[key]

    windows: list[DuesWindow] = []
    for row in df_terms.to_dict("records"):
        rates = parse_rates(row.get("dues_rates"))
        if not rates:
            continue
        start = pd.to_datetime(row.get("start_date"), errors="coerce")
        end = pd.to_datetime(row.get("end_date"), errors="coerce")
        if pd.isna(start) or pd.isna(end):
            continue
        verified = row.get("dues_rates_verified")
        windows.append(
            DuesWindow(
                term_id=str(row.get("TermID") or ""),
                semester=str(row.get("Semester") or ""),
                start=start.date(),
                end=end.date(),
                rates=rates,
                verified=bool(0 if verified is None or pd.isna(verified) else int(verified)),
            )
        )

    schedule = DuesSchedule(
        windows,
        default_rates=default_rates,
        accept_venmo_net=accept_venmo_net,
    )
    if key is not None:
        if len(_SCHEDULE_CACHE) >= _SCHEDULE_CACHE_LIMIT:
            _SCHEDULE_CACHE.clear()
        _SCHEDULE_CACHE[key] = schedule
    return schedule


# "ZELLE FROM JANE DOE ON 07/08 REF # ..."  /  "ZELLE FROM JANE DOE REF # ..."
_ZELLE_PAYER = re.compile(
    r"zelle\s+from\s+(.+?)\s+(?:on\s+\d{1,2}/\d{1,2}|ref\s*#)",
    re.I,
)
_WHITESPACE = re.compile(r"\s+")


def extract_payer(details: object) -> str | None:
    """Best-effort payer name from a transfer description."""
    if details is None:
        return None
    text = _WHITESPACE.sub(" ", str(details)).strip()

    match = _ZELLE_PAYER.search(text)
    if match:
        name = match.group(1).strip(" .,-")
        return name.title() if name else None

    # Venmo: "txn id | note | from | to"
    if "|" in text:
        parts = [part.strip() for part in text.split("|")]
        if len(parts) >= 3 and parts[2]:
            return parts[2].title()

    return None


def dues_transactions(
    df_transactions: pd.DataFrame,
    schedule: DuesSchedule | None = None,
) -> pd.DataFrame:
    """
    Every transaction that looks like a dues payment.

    Uses the stored committee assignment where present, and falls back to the
    amount signature so payments that have not been categorized yet are still
    counted -- otherwise the collection figure would understate reality purely
    because someone has not worked the review queue.

    That fallback has to read rates from the same schedule the categorizer uses.
    When rates were a module constant this was invisible; now that they vary by
    term, a report pinned to one term's rates would disagree with the ledger it
    is reporting on.
    """
    if df_transactions.empty:
        return df_transactions.assign(payer=pd.Series(dtype="object"))

    frame = df_transactions.copy()
    parsed = [parse_amount(value) for value in frame["amount"]]
    frame["_amount"] = parsed

    if schedule is None:
        matches = [
            amount is not None and amount > 0 and any(amount == d for d in DUES_AMOUNTS)
            for amount in parsed
        ]
    else:
        dates = pd.to_datetime(frame["transaction_date"], errors="coerce")
        matches = [
            amount is not None
            and amount > 0
            and schedule.matches(amount, None if pd.isna(when) else when.date())
            for amount, when in zip(parsed, dates)
        ]

    by_committee = frame["budget_category"] == DUES_COMMITTEE_ID
    by_signature = pd.Series(matches, index=frame.index)

    dues = frame[by_committee | by_signature].copy()
    if dues.empty:
        return dues.assign(payer=pd.Series(dtype="object"))

    dues["payer"] = dues["details"].map(extract_payer)
    dues["rate"] = [float(a) if a is not None else 0.0 for a in dues["_amount"]]
    return dues.drop(columns=["_amount"])


@dataclass
class DuesSummary:
    semester: str
    total_collected: float
    payment_count: int
    unique_payers: int
    rates: dict[float, int]
    first_payment: pd.Timestamp | None
    last_payment: pd.Timestamp | None

    @property
    def average_payment(self) -> float:
        return self.total_collected / self.payment_count if self.payment_count else 0.0


def summarize(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
) -> DuesSummary:
    dues = dues_transactions(df_transactions, schedule_from_terms(df_terms))
    if dues.empty:
        return DuesSummary(semester, 0.0, 0, 0, {}, None, None)
    return _summarize_tagged(attach_semester(dues, df_terms), semester)


def _summarize_tagged(tagged: pd.DataFrame, semester: str) -> DuesSummary:
    """
    `summarize` over an already-derived, already-tagged dues frame.

    FINDING (performance). `compare_semesters` called `summarize` once per
    semester, and each call re-derived the dues subset (a `parse_amount` per row
    over the whole table) and re-tagged it. Both are loop-invariant.
    """
    if tagged.empty:
        return DuesSummary(semester, 0.0, 0, 0, {}, None, None)

    scoped = tagged[tagged["Semester"] == semester]
    if scoped.empty:
        return DuesSummary(semester, 0.0, 0, 0, {}, None, None)

    dates = pd.to_datetime(scoped["transaction_date"], errors="coerce")
    rates = scoped["rate"].round(2).value_counts().to_dict()

    return DuesSummary(
        semester=semester,
        total_collected=float(scoped["rate"].sum()),
        payment_count=int(len(scoped)),
        unique_payers=int(scoped["payer"].dropna().nunique()),
        rates={float(k): int(v) for k, v in rates.items()},
        first_payment=dates.min() if dates.notna().any() else None,
        last_payment=dates.max() if dates.notna().any() else None,
    )


def collection_curve(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
) -> pd.DataFrame:
    """
    Cumulative dues collected by day, indexed on days since the term started.

    Indexing on elapsed days rather than calendar date is what makes two
    semesters comparable on one axis.
    """
    columns = ["Day", "Date", "Collected", "Cumulative", "Payments"]
    window = date_range_for_semester(df_terms, semester)
    if window is None:
        return pd.DataFrame(columns=columns)

    dues = dues_transactions(df_transactions, schedule_from_terms(df_terms))
    if dues.empty:
        return pd.DataFrame(columns=columns)

    tagged = attach_semester(dues, df_terms)
    scoped = tagged[tagged["Semester"] == semester].copy()
    if scoped.empty:
        return pd.DataFrame(columns=columns)

    start, _end = window
    scoped["Date"] = pd.to_datetime(scoped["transaction_date"], errors="coerce")
    scoped = scoped.dropna(subset=["Date"])
    scoped["Day"] = (scoped["Date"] - start).dt.days

    daily = (
        scoped.groupby("Day", as_index=False)
        .agg(Collected=("rate", "sum"), Payments=("rate", "size"), Date=("Date", "min"))
        .sort_values("Day")
    )
    daily["Cumulative"] = daily["Collected"].cumsum()
    return daily[columns]


def compare_semesters(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semesters: list[str],
) -> pd.DataFrame:
    """One row per semester, for a side-by-side view."""
    dues = dues_transactions(df_transactions, schedule_from_terms(df_terms))
    tagged = attach_semester(dues, df_terms) if not dues.empty else dues

    rows = []
    for semester in semesters:
        summary = _summarize_tagged(tagged, semester)
        rows.append(
            {
                "Semester": semester,
                "Collected": summary.total_collected,
                "Payments": summary.payment_count,
                "Unique payers": summary.unique_payers,
                "Average": round(summary.average_payment, 2),
            }
        )
    return pd.DataFrame(
        rows, columns=["Semester", "Collected", "Payments", "Unique payers", "Average"]
    )


def payer_roster(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
) -> pd.DataFrame:
    """
    Who paid, how much, and when.

    Multiple payments from one person are aggregated -- a member who pays the
    instalment rate twice should appear once, having paid the full amount.
    """
    columns = ["Payer", "Paid", "Payments", "First payment"]
    dues = dues_transactions(df_transactions, schedule_from_terms(df_terms))
    if dues.empty:
        return pd.DataFrame(columns=columns)

    tagged = attach_semester(dues, df_terms)
    scoped = tagged[tagged["Semester"] == semester].copy()
    scoped = scoped[scoped["payer"].notna()]
    if scoped.empty:
        return pd.DataFrame(columns=columns)

    scoped["Date"] = pd.to_datetime(scoped["transaction_date"], errors="coerce")
    roster = (
        scoped.groupby("payer", as_index=False)
        .agg(Paid=("rate", "sum"), Payments=("rate", "size"), **{"First payment": ("Date", "min")})
        .rename(columns={"payer": "Payer"})
        .sort_values("Paid", ascending=False)
    )
    return roster[columns].reset_index(drop=True)


def outstanding(collected_payers: int, expected_members: int) -> dict:
    """Simple gap analysis against a roster size the treasurer supplies."""
    expected = max(expected_members, 0)
    paid = max(collected_payers, 0)
    return {
        "expected": expected,
        "paid": paid,
        "outstanding": max(expected - paid, 0),
        "rate": (paid / expected * 100) if expected else 0.0,
    }
