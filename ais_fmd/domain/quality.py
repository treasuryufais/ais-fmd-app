"""
Module M13 -- data quality checks.

The original had a "Data Validation" block buried at the bottom of the Database
Tools page that checked two things and printed a warning. These are the same
checks, promoted to a real module, expanded, and returned as structured issues
so the UI can rank them and link to the affected rows.
"""

from __future__ import annotations

from dataclasses import dataclass

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
from .dedupe import identity_tuple

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


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
