"""
Module M20 -- sponsorship goal tracking.

Storage for this landed in commit d710cc0 (a `sponsorship_goal` column on
`terms`, plus `data.repositories.set_term_sponsorship_goal` to write it). This
module is the read side: given a term's transactions, how much sponsorship
money has actually come in, and how does that compare to the goal.

FINDING (M20, read this before changing the committee logic). Committee 11
("Sponsorship / Donation" in `config.categories.COMMITTEES`) looked like the
obvious home for this and is where the first version of this module matched.
It is wrong: nothing in this database, ever, assigns a transaction to
committee 11 -- checked across all 11 real sponsorship deposits in the seed
data, spanning Fall 2024 through Fall 2026. Every one of them is booked
against committee 6 (Corporate Relations) instead. That is not a documented
committee decision either -- it traces to a bare `(6, "Sponsorship /
Donation")` hint in `data/seed.py`'s fake-data generator, with no comment
explaining the choice.

So this module intentionally does NOT filter on committee/`budget_category`
at all. It matches on `purpose == "Sponsorship / Donation"` instead, which is
the one label that is actually applied consistently regardless of which
committee ID the money happens to be filed under. This is a deliberate,
narrow choice: it only changes how *this dashboard number* is computed. It
does not recategorize, move, or touch a single transaction -- whether
sponsorship income should actually be filed under committee 6 or committee 11
going forward is a real process question for the team, left open on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .money import parse_amount
from .terms import attach_semester, term_id_for_semester

SPONSORSHIP_PURPOSE = "Sponsorship / Donation"


def sponsorship_transactions(df_transactions: pd.DataFrame) -> pd.DataFrame:
    """
    Every transaction booked as sponsorship/donation income.

    Matched on `purpose`, not committee (see the module docstring), and
    scoped to positive amounts only -- a refund or correction should not
    count as money raised, the same reasoning `dues.dues_transactions`
    applies to dues. Unlike dues, there is no amount-signature fallback:
    sponsorship amounts vary too much to recognise by value, so an
    uncategorized sponsorship transaction (no purpose set yet) will not be
    counted here until someone works the review queue. That is a real
    limitation, not an oversight -- flagged here so it is not mistaken for
    one.
    """
    if df_transactions.empty or "purpose" not in df_transactions.columns:
        return df_transactions.iloc[0:0]

    parsed = [parse_amount(value) for value in df_transactions["amount"]]
    positive = pd.Series(
        [amount is not None and amount > 0 for amount in parsed],
        index=df_transactions.index,
    )
    by_purpose = df_transactions["purpose"] == SPONSORSHIP_PURPOSE
    return df_transactions[by_purpose & positive]


def goal_for_term(df_terms: pd.DataFrame, term_id: str | None) -> float | None:
    """
    The stored sponsorship goal for a term, or None if unset or the term is
    unknown.

    None means "no goal set", distinct from a goal of $0 -- same nullable
    convention `set_term_sponsorship_goal` uses when writing it.
    """
    if (
        term_id is None
        or df_terms is None
        or df_terms.empty
        or "sponsorship_goal" not in df_terms.columns
    ):
        return None
    match = df_terms[df_terms["TermID"] == term_id]
    if match.empty:
        return None
    value = match.iloc[0]["sponsorship_goal"]
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return float(value)


@dataclass
class SponsorshipSummary:
    semester: str
    raised: float
    goal: float | None
    payment_count: int

    @property
    def percent_of_goal(self) -> float | None:
        """None when there is no goal to divide by, rather than raising or returning inf."""
        if not self.goal:
            return None
        return (self.raised / self.goal) * 100

    @property
    def remaining(self) -> float | None:
        """How much is left to reach the goal, floored at 0. None when no goal is set."""
        if self.goal is None:
            return None
        return max(self.goal - self.raised, 0.0)


def _summarize_tagged(tagged: pd.DataFrame, df_terms: pd.DataFrame, semester: str) -> SponsorshipSummary:
    """`summarize` over an already-derived, already-tagged sponsorship frame."""
    term_id = term_id_for_semester(df_terms, semester)
    goal = goal_for_term(df_terms, term_id)

    if tagged.empty:
        return SponsorshipSummary(semester, 0.0, goal, 0)

    scoped = tagged[tagged["Semester"] == semester]
    if scoped.empty:
        return SponsorshipSummary(semester, 0.0, goal, 0)

    parsed = [parse_amount(value) for value in scoped["amount"]]
    raised = float(sum(amount for amount in parsed if amount is not None))
    return SponsorshipSummary(semester, raised, goal, int(len(scoped)))


def summarize(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
) -> SponsorshipSummary:
    """Sponsorship raised vs. goal for one semester."""
    sponsorship = sponsorship_transactions(df_transactions)
    tagged = attach_semester(sponsorship, df_terms) if not sponsorship.empty else sponsorship
    return _summarize_tagged(tagged, df_terms, semester)


def compare_semesters(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semesters: list[str],
) -> pd.DataFrame:
    """
    One row per semester: goal, raised, % of goal, payment count.

    For "pull up past semester goals with all the data on them" -- a history
    view, not just the currently-selected term. A semester with no goal set
    shows blank Goal/% columns rather than 0%, so "no target" is visibly
    different from "target of $0" here too, same as everywhere else this
    nullable convention shows up.
    """
    sponsorship = sponsorship_transactions(df_transactions)
    tagged = attach_semester(sponsorship, df_terms) if not sponsorship.empty else sponsorship

    rows = []
    for semester in semesters:
        summary = _summarize_tagged(tagged, df_terms, semester)
        rows.append(
            {
                "Semester": semester,
                "Goal": summary.goal,
                "Raised": summary.raised,
                "% of Goal": summary.percent_of_goal,
                "Payments": summary.payment_count,
            }
        )
    return pd.DataFrame(rows, columns=["Semester", "Goal", "Raised", "% of Goal", "Payments"])


def transactions_for_semester(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
) -> pd.DataFrame:
    """
    The individual payments behind one semester's total on `compare_semesters`.

    The aggregate row answers "how much" -- this answers "from whom". The raw
    bank description alone never carries a company name (e.g. "DEPOSIT
    CORPORATE SPONSORSHIP REF91827"), so it cannot be parsed into a sponsor.
    `Sponsor` is a separate, manually-entered field (`transactions.sponsor_name`,
    written via `data.repositories.set_transaction_sponsor_name`) for exactly
    that reason -- someone has to actually type in who it was.

    `transactionid` is included so a caller can let the sponsor name be
    edited in place; drop the column before displaying if that is not needed.
    """
    columns = ["transactionid", "Date", "Amount", "Details", "Sponsor"]
    sponsorship = sponsorship_transactions(df_transactions)
    if sponsorship.empty:
        return pd.DataFrame(columns=columns)

    tagged = attach_semester(sponsorship, df_terms)
    scoped = tagged[tagged["Semester"] == semester]
    if scoped.empty:
        return pd.DataFrame(columns=columns)

    out = scoped.copy()
    if "sponsor_name" not in out.columns:
        out["sponsor_name"] = None
    out = out[["transactionid", "transaction_date", "amount", "details", "sponsor_name"]].rename(
        columns={
            "transaction_date": "Date",
            "amount": "Amount",
            "details": "Details",
            "sponsor_name": "Sponsor",
        }
    )
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    out["Sponsor"] = out["Sponsor"].fillna("")
    return out.sort_values("Date").reset_index(drop=True)[columns]
