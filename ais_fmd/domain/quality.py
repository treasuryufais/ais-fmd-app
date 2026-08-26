"""
Module M13 -- data quality checks.

The original had a "Data Validation" block buried at the bottom of the Database
Tools page that checked two things and printed a warning. These are the same
checks, promoted to a real module, expanded, and returned as structured issues
so the UI can rank them and link to the affected rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from ..config.categories import (
    ACCOUNTS,
    COMMITTEE_BY_ID,
    DISPUTED_PURPOSE_MAPPINGS,
    PURPOSE_TO_COMMITTEE,
    PURPOSES,
    committee_name,
    legacy_account_values,
)
from .categorize.predicates import is_venmo_or_zelle, mentions_dues
from .categorize.scoring import (
    CONFIRMED_CARDS,
    ROSTER_ERA,
    ROSTER_ERA_ENDS,
    card_number,
)
from .dedupe import identity_tuple
from .dues import schedule_from_terms
from .money import parse_amount

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# How many people must send the same unrecognised amount before it reads as a
# rate rather than a coincidence. Two is too low -- a pair of members splitting
# an odd reimbursement would trip it.
DUES_RATE_CHANGE_MIN_PAYERS = 3


@dataclass
class Issue:
    code: str
    severity: str
    title: str
    detail: str
    count: int
    rows: pd.DataFrame | None = None

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 99)


def run_all_checks(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
) -> list[Issue]:
    """Every check, worst first."""
    issues: list[Issue] = []
    for check in (
        check_orphaned_transactions,
        check_orphaned_budgets,
        check_uncategorized,
        check_legacy_account_values,
        check_unknown_purposes,
        check_zero_amounts,
        check_duplicate_candidates,
        check_transactions_outside_terms,
        check_disputed_mappings,
        check_unverified_dues_rates,
        check_possible_dues_rate_change,
        check_card_roster_era,
    ):
        try:
            issue = check(df_transactions, df_budgets, df_terms)
        except Exception as exc:  # noqa: BLE001 - a broken check must not hide the others
            issue = Issue(
                code=check.__name__,
                severity="low",
                title=f"Check '{check.__name__}' could not run",
                detail=f"{type(exc).__name__}: {exc}",
                count=0,
            )
        if issue is not None:
            issues.append(issue)
    return sorted(issues, key=lambda i: (i.rank, -i.count))


def _no_issue() -> None:
    return None


def check_orphaned_transactions(df_transactions, df_budgets, df_terms) -> Issue | None:
    if df_transactions.empty:
        return _no_issue()
    assigned = df_transactions[df_transactions["budget_category"].notna()]
    if assigned.empty:
        return _no_issue()
    known = set(COMMITTEE_BY_ID)
    orphans = assigned[~assigned["budget_category"].astype("Int64").isin(known)]
    if orphans.empty:
        return _no_issue()
    return Issue(
        code="orphaned_transactions",
        severity="critical",
        title="Transactions pointing at a committee that does not exist",
        detail=(
            "These rows reference a committee ID with no matching committee, so "
            "their spending appears in no budget and no chart."
        ),
        count=len(orphans),
        rows=orphans.head(200),
    )


def check_orphaned_budgets(df_transactions, df_budgets, df_terms) -> Issue | None:
    if df_budgets.empty:
        return _no_issue()
    known = set(COMMITTEE_BY_ID)
    orphans = df_budgets[~df_budgets["committeeid"].astype("Int64").isin(known)]
    if orphans.empty:
        return _no_issue()
    return Issue(
        code="orphaned_budgets",
        severity="high",
        title="Budget rows for a committee that does not exist",
        detail="Allocated money that can never be spent against, because the committee is gone.",
        count=len(orphans),
        rows=orphans.head(200),
    )


def check_uncategorized(df_transactions, df_budgets, df_terms) -> Issue | None:
    if df_transactions.empty:
        return _no_issue()
    missing = df_transactions[
        df_transactions["budget_category"].isna() | df_transactions["purpose"].isna()
    ]
    if missing.empty:
        return _no_issue()
    share = len(missing) / len(df_transactions) * 100
    severity = "high" if share > 20 else "medium" if share > 5 else "low"
    return Issue(
        code="uncategorized",
        severity=severity,
        title="Uncategorized transactions",
        detail=(
            f"{share:.1f}% of all transactions are missing a committee or a purpose. "
            f"They are excluded from budget-vs-actual, so committee spend is understated."
        ),
        count=len(missing),
        rows=missing.head(200),
    )


def check_legacy_account_values(df_transactions, df_budgets, df_terms) -> Issue | None:
    """FINDING F13 -- 'Wells' written where 'Wells Fargo' was documented."""
    if df_transactions.empty:
        return _no_issue()
    stale = legacy_account_values(df_transactions["account"].dropna().unique())
    if not stale:
        return _no_issue()
    affected = df_transactions[df_transactions["account"].isin(stale)]
    return Issue(
        code="legacy_account_values",
        severity="medium",
        title="Non-canonical account labels",
        detail=(
            f"Found {', '.join(sorted(repr(v) for v in stale))} where the canonical "
            f"values are {', '.join(ACCOUNTS)}. Filters that match on exact value "
            f"will miss these rows."
        ),
        count=len(affected),
        rows=affected.head(200),
    )


def check_unknown_purposes(df_transactions, df_budgets, df_terms) -> Issue | None:
    if df_transactions.empty:
        return _no_issue()
    known = set(PURPOSES)
    present = df_transactions["purpose"].dropna()
    present = present[present.astype(str).str.strip() != ""]
    unknown = present[~present.isin(known)]
    if unknown.empty:
        return _no_issue()
    values = ", ".join(sorted(set(unknown.astype(str)))[:8])
    return Issue(
        code="unknown_purposes",
        severity="low",
        title="Purposes outside the configured list",
        detail=f"Values not in config.categories.PURPOSES: {values}",
        count=len(unknown),
        rows=df_transactions.loc[unknown.index].head(200),
    )


def check_zero_amounts(df_transactions, df_budgets, df_terms) -> Issue | None:
    """
    FINDING F5's downstream symptom. `numeric_amount` returned 0.0 on any parse
    failure, so a misread column produced real $0.00 rows. If historical data
    contains a cluster of these, that is the fingerprint.
    """
    if df_transactions.empty:
        return _no_issue()
    zeros = df_transactions[df_transactions["amount"] == 0]
    if zeros.empty:
        return _no_issue()
    return Issue(
        code="zero_amounts",
        severity="medium" if len(zeros) > 3 else "low",
        title="Zero-amount transactions",
        detail=(
            "A $0.00 transaction is usually a parse failure rather than a real "
            "event -- the previous parser coerced unreadable amounts to zero."
        ),
        count=len(zeros),
        rows=zeros.head(200),
    )


def check_duplicate_candidates(df_transactions, df_budgets, df_terms) -> Issue | None:
    """Rows sharing an identity, surfaced for human judgement rather than auto-deleted."""
    if df_transactions.empty or len(df_transactions) < 2:
        return _no_issue()
    records = df_transactions.to_dict("records")
    seen: dict[tuple, list[int]] = {}
    for position, record in enumerate(records):
        seen.setdefault(identity_tuple(record), []).append(position)

    repeated = [positions for positions in seen.values() if len(positions) > 1]
    if not repeated:
        return _no_issue()

    flat = [position for group in repeated for position in group]
    return Issue(
        code="duplicate_candidates",
        severity="low",
        title="Transactions sharing a date, amount, description and account",
        detail=(
            "These may be genuine repeat purchases -- the deduplication key now "
            "allows them deliberately. Shown so they can be confirmed, not removed."
        ),
        count=len(flat),
        rows=df_transactions.iloc[flat].head(200),
    )


def check_transactions_outside_terms(df_transactions, df_budgets, df_terms) -> Issue | None:
    from .terms import attach_semester

    if df_transactions.empty or df_terms.empty:
        return _no_issue()
    tagged = attach_semester(df_transactions, df_terms)
    orphans = tagged[tagged["Semester"].isna()]
    if orphans.empty:
        return _no_issue()
    return Issue(
        code="outside_terms",
        severity="medium",
        title="Transactions falling outside every defined term",
        detail=(
            "These rows appear in no semester, so they are invisible on the "
            "dashboard. Usually a missing term definition rather than bad data."
        ),
        count=len(orphans),
        rows=orphans.head(200),
    )


def check_disputed_mappings(df_transactions, df_budgets, df_terms) -> Issue | None:
    """FINDING F7 -- surface the contested purpose-to-committee mappings."""
    if not DISPUTED_PURPOSE_MAPPINGS:
        return _no_issue()
    if df_transactions.empty:
        affected = 0
        rows = None
    else:
        mask = df_transactions["purpose"].isin(DISPUTED_PURPOSE_MAPPINGS.keys())
        matched = df_transactions[mask]
        affected = len(matched)
        rows = matched.head(200)

    lines = [
        f"'{purpose}' → committee {PURPOSE_TO_COMMITTEE[purpose]} "
        f"({committee_name(PURPOSE_TO_COMMITTEE[purpose])}): {note}"
        for purpose, note in DISPUTED_PURPOSE_MAPPINGS.items()
    ]
    return Issue(
        code="disputed_mappings",
        severity="high" if affected else "info",
        title="Purpose-to-committee mappings that contradict the reference table",
        detail=(
            "The original behaviour has been preserved so historical figures do "
            "not shift underneath you. Confirm or correct these in "
            "config/categories.py.\n\n" + "\n\n".join(lines)
        ),
        count=affected,
        rows=rows,
    )


def check_card_roster_era(df_transactions, df_budgets, df_terms) -> Issue | None:
    """
    Card assignments confirmed under an officer cohort that has since turned over.

    Treasury: "The cards might change around with new VPs, I will input their
    numbers later." A card number is only evidence about a committee for as long
    as the same person holds it, and nothing about a reissued card looks
    different in a bank statement -- it keeps voting, just for the wrong
    committee.

    Card 8408 is the one that matters. `rule_consulting` treats it as a
    certainty that ignores every other rule, including a mapping a human
    confirmed, so if that card changed hands its spend is being booked to
    Consulting with no way for any other evidence to correct it.

    Fires on transactions dated after the roster's cohort ended, because that is
    when a stale roster starts doing damage rather than when the calendar says
    a year rolled over.
    """
    if df_transactions is None or df_transactions.empty:
        return _no_issue()
    if "transaction_date" not in df_transactions.columns:
        return _no_issue()

    dates = pd.to_datetime(df_transactions["transaction_date"], errors="coerce")
    after = df_transactions[dates > pd.Timestamp(ROSTER_ERA_ENDS)]
    if after.empty:
        return _no_issue()

    cards = after["details"].map(lambda d: card_number(d) if d is not None else None)
    carded = after[cards.notna()]
    if carded.empty:
        return _no_issue()

    known = set(CONFIRMED_CARDS)
    seen = set(cards.dropna())
    on_known = carded[cards.dropna().isin(known).reindex(carded.index, fill_value=False)]
    on_unknown = carded[~cards.dropna().isin(known).reindex(carded.index, fill_value=False)]

    roster = ", ".join(
        f"{card} -> {committee_name(a.committee_id)} ({a.holder})"
        for card, a in sorted(CONFIRMED_CARDS.items())
    )
    header = (
        f"The card roster was confirmed for the {ROSTER_ERA} officer cohort, "
        f"which ended {ROSTER_ERA_ENDS:%Y-%m-%d}.\n\nCurrent roster: {roster}\n"
    )

    # Two different failures, and which one you have changes what to do about
    # it. Real Fall 2026 data showed the second: not one card in the statement
    # appears in the roster, so the card signal contributes nothing at all --
    # a quieter problem than a wrong assignment, and easier to miss precisely
    # because nothing looks wrong.
    if not on_known.empty:
        return Issue(
            code="card_roster_era",
            severity="medium",
            title="Card roster may predate the current officers",
            detail=(
                header
                + f"\n{len(on_known)} transaction(s) after that date are still being "
                "attributed through it. If a card was reissued to a new VP it now "
                "points at the wrong committee, and nothing in the data looks any "
                "different.\n\n"
                "Card 8408 is the urgent one: it is an exact rule that outranks "
                "every other signal including a confirmed merchant mapping, so a "
                "wrong assignment there cannot be corrected by anything except "
                "changing the roster.\n\n"
                "Confirm the numbers with treasury, then update CONFIRMED_CARDS in "
                "`domain/categorize/scoring.py` and bump ROSTER_ERA."
            ),
            count=len(on_known),
            rows=on_known.head(200),
        )

    return Issue(
        code="card_roster_era",
        severity="medium",
        title="No card in current spending is on the roster",
        detail=(
            header
            + f"\nEvery one of the {len(on_unknown)} card transaction(s) since then "
            f"is on a card nobody has identified: {', '.join(sorted(seen))}. None of "
            "the roster's cards appear at all, which is what a completed handover "
            "looks like from inside the data.\n\n"
            "Nothing is being mis-booked — an unknown card simply contributes no "
            "evidence — but the card signal, normally one of the strongest inputs "
            "to categorisation, is doing nothing. Every purchase on these cards "
            "loses its best clue about which committee it belongs to and lands in "
            "the review queue by default.\n\n"
            "Ask treasury who holds them, then update CONFIRMED_CARDS in "
            "`domain/categorize/scoring.py` and bump ROSTER_ERA."
        ),
        count=len(on_unknown),
        rows=on_unknown.head(200),
    )


def check_unverified_dues_rates(df_transactions, df_budgets, df_terms) -> Issue | None:
    """
    Terms whose dues rates nobody has confirmed.

    Dues rates were a module constant pinned to Fall 2024 and are now per-term
    data, but every term was seeded with a *copy* of that constant. A copy is not
    evidence, and this is the difference between "we know" and "we assumed".
    """
    if df_terms is None or df_terms.empty or "dues_rates_verified" not in df_terms.columns:
        return _no_issue()
    flag = pd.to_numeric(df_terms["dues_rates_verified"], errors="coerce").fillna(0)
    unverified = df_terms[flag.astype(int) == 0]
    if unverified.empty:
        return _no_issue()

    names = ", ".join(str(s) for s in unverified["Semester"].dropna().head(12))
    return Issue(
        code="unverified_dues_rates",
        severity="medium",
        title="Terms with unconfirmed dues rates",
        detail=(
            "Dues are matched on an exact amount, so a term charging a rate the "
            "app does not know about stops categorizing dues entirely -- with no "
            "error, the payments simply fall to the review queue. These terms "
            "carry rates seeded from the old Fall 2024 constant rather than "
            "confirmed against what was actually charged.\n\n"
            f"Unconfirmed: {names}"
        ),
        count=len(unverified),
        rows=unverified.head(200),
    )


def check_possible_dues_rate_change(df_transactions, df_budgets, df_terms) -> Issue | None:
    """
    Repeated incoming transfers at one amount that the dues schedule rejects.

    This is the alarm for the failure the per-term schedule cannot prevent on its
    own: rates change, nobody updates the app, and dues income silently stops
    being recognised. Several people paying the *same* unrecognised amount by
    Zelle or Venmo is what a rate change looks like from inside the data.
    """
    if df_transactions is None or df_transactions.empty:
        return _no_issue()
    needed = {"amount", "details", "transaction_date"}
    if not needed.issubset(df_transactions.columns):
        return _no_issue()

    schedule = schedule_from_terms(df_terms if df_terms is not None else pd.DataFrame())
    frame = df_transactions.copy()
    amounts = [parse_amount(value) for value in frame["amount"]]
    dates = pd.to_datetime(frame["transaction_date"], errors="coerce")
    accounts = (
        frame["account"] if "account" in frame.columns else pd.Series([None] * len(frame))
    )

    candidates: list[tuple[Decimal, int]] = []
    for position, (amount, when, details, account) in enumerate(
        zip(amounts, dates, frame["details"], accounts)
    ):
        if amount is None or amount <= 0:
            continue
        if not is_venmo_or_zelle(details, account):
            continue
        if schedule.matches(amount, None if pd.isna(when) else when.date()):
            continue
        candidates.append((amount, position))

    if not candidates:
        return _no_issue()

    by_amount: dict[Decimal, list[int]] = {}
    for amount, position in candidates:
        by_amount.setdefault(amount, []).append(position)

    # One person sending an odd number is noise; several sending the same odd
    # number is a rate.
    clusters = {
        amount: positions
        for amount, positions in by_amount.items()
        if len(positions) >= DUES_RATE_CHANGE_MIN_PAYERS
    }
    if not clusters:
        return _no_issue()

    ranked = sorted(clusters.items(), key=lambda item: -len(item[1]))

    # A payer who wrote "dues" on the memo has named the payment themselves. At
    # an amount no term on file charged, that is the strongest evidence of a
    # rate change available anywhere in the data -- much stronger than a bare
    # cluster, which could be any repeated non-dues payment. `rule_dues_memo`
    # books these, so they are no longer sitting in the review queue waiting to
    # be noticed; this is what makes sure they still get noticed.
    memoed: dict[Decimal, int] = {}
    for amount, positions in clusters.items():
        named = sum(
            1 for p in positions if mentions_dues(df_transactions.iloc[p].get("details"))
        )
        if named:
            memoed[amount] = named

    lines = []
    for amount, positions in ranked[:8]:
        named = memoed.get(amount, 0)
        suffix = f"  ({named} of them say 'dues' on the memo)" if named else ""
        lines.append(f"{len(positions)} payments of {amount:.2f}{suffix}")

    affected = sorted({p for positions in clusters.values() for p in positions})

    # Treasury: "Execs pay the discounted rate, some people pay less for other
    # reasons ... if it says dues ... mark it under dues." Off-rate dues are
    # therefore routine, not an anomaly, and a HIGH alarm on every discount
    # would train a treasurer to ignore this check entirely -- taking the real
    # signal down with it.
    #
    # The two cases separate cleanly on the memo. A cluster where everyone wrote
    # "dues" is already booked as dues income and is most likely a standing
    # discount. A cluster where nobody did is money arriving at an amount the
    # app cannot explain, which is what an unrecorded rate change looks like.
    all_explained = all(
        memoed.get(amount, 0) == len(positions) for amount, positions in clusters.items()
    )

    if all_explained:
        return Issue(
            code="possible_dues_rate_change",
            severity="low",
            title="Dues paid at amounts not recorded on the term",
            detail=(
                "These payments name themselves as dues but sit at an amount no "
                "term on file charged. They are already counted as dues income, "
                "so nothing is missing from the books.\n\n" + "\n".join(lines) +
                "\n\nWorth a look only because a rate known solely from what "
                "members typed in a memo is a rate nobody has confirmed. If one "
                "of these is a standing rate -- an exec or discounted rate, say "
                "-- add it to the term's rates on Treasury and it will match "
                "directly. The term accepts any number of rates."
            ),
            count=len(affected),
            rows=df_transactions.iloc[affected].head(200),
        )

    return Issue(
        code="possible_dues_rate_change",
        severity="high",
        title="Repeated incoming transfers at an amount dues does not recognise",
        detail=(
            "Multiple people sent the same amount by Venmo or Zelle, the dues "
            "schedule rejected all of them, and their memos do not say dues. "
            "That combination is what an unrecorded rate change looks like from "
            "inside the data. Confirm the term's rates and set them on the term, "
            "or dismiss these as non-dues income.\n\n" + "\n".join(lines)
        ),
        count=len(affected),
        rows=df_transactions.iloc[affected].head(200),
    )


def disputed_mapping_report(df_transactions: pd.DataFrame) -> pd.DataFrame:
    """Per-purpose counts and dollar totals, so F7 can be decided with numbers."""
    columns = ["Purpose", "Currently mapped to", "Transactions", "Total amount"]
    if df_transactions.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for purpose in DISPUTED_PURPOSE_MAPPINGS:
        matched = df_transactions[df_transactions["purpose"] == purpose]
        committee_id = PURPOSE_TO_COMMITTEE[purpose]
        rows.append(
            {
                "Purpose": purpose,
                "Currently mapped to": f"{committee_id} - {committee_name(committee_id)}",
                "Transactions": len(matched),
                "Total amount": float(matched["amount"].abs().sum()) if not matched.empty else 0.0,
            }
        )
    return pd.DataFrame(rows, columns=columns)
