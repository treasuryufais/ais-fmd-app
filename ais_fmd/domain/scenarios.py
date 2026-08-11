"""
Module M14 -- scenario planning.

"If dues go to $40 and Formal is cut 20%, what is the semester's net?" -- the
question that gets asked in the meeting where budgets are set, and that
previously required a spreadsheet nobody kept.

Everything is derived from a baseline semester's actuals, so a scenario is
grounded in what actually happened rather than in guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from . import budgets as budget_domain
from . import dues as dues_domain
from .terms import ordered_semesters, previous_semester


@dataclass
class Baseline:
    """What actually happened in the reference semester."""

    semester: str
    income: float
    expenses: float
    net: float
    dues_collected: float
    dues_payers: int
    dues_rate: float
    non_dues_income: float
    committee_spend: dict[int, float] = field(default_factory=dict)

    @property
    def committee_total(self) -> float:
        return sum(self.committee_spend.values())

    @property
    def other_expenses(self) -> float:
        """Expenses not attributable to a budgeted committee."""
        return max(self.expenses - self.committee_total, 0.0)


def build_baseline(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
) -> Baseline:
    totals = budget_domain.semester_totals(df_transactions, df_terms, semester)
    dues = dues_domain.summarize(df_transactions, df_terms, semester)

    summary = budget_domain.budget_vs_actual(df_transactions, df_budgets, df_terms, semester)
    spend: dict[int, float] = {}
    if not summary.empty:
        by_name = dict(zip(summary["Committee_Name"], summary["Spent"]))
        for committee_id in BUDGETED_COMMITTEE_IDS:
            spend[committee_id] = float(by_name.get(committee_name(committee_id), 0.0))

    # The typical rate actually paid, rather than the advertised rate.
    rate = dues.total_collected / dues.payment_count if dues.payment_count else 35.0

    return Baseline(
        semester=semester,
        income=totals["income"],
        expenses=totals["expenses"],
        net=totals["net"],
        dues_collected=dues.total_collected,
        dues_payers=dues.unique_payers,
        dues_rate=round(rate, 2),
        non_dues_income=max(totals["income"] - dues.total_collected, 0.0),
        committee_spend=spend,
    )


@dataclass
class Scenario:
    dues_rate: float
    expected_members: int
    committee_multipliers: dict[int, float] = field(default_factory=dict)
    other_income_change: float = 0.0
    other_expense_change: float = 0.0


@dataclass
class Projection:
    baseline: Baseline
    scenario: Scenario
    projected_dues: float
    projected_income: float
    projected_expenses: float
    projected_net: float
    committee_projection: dict[int, float] = field(default_factory=dict)

    @property
    def net_change(self) -> float:
        return self.projected_net - self.baseline.net

    @property
    def income_change(self) -> float:
        return self.projected_income - self.baseline.income

    @property
    def expense_change(self) -> float:
        return self.projected_expenses - self.baseline.expenses


def project(baseline: Baseline, scenario: Scenario) -> Projection:
    projected_dues = scenario.dues_rate * max(scenario.expected_members, 0)

    projected_committee = {
        committee_id: spend * scenario.committee_multipliers.get(committee_id, 1.0)
        for committee_id, spend in baseline.committee_spend.items()
    }

    other_expenses = max(baseline.other_expenses + scenario.other_expense_change, 0.0)
    projected_expenses = sum(projected_committee.values()) + other_expenses
    projected_income = max(
        projected_dues + baseline.non_dues_income + scenario.other_income_change, 0.0
    )

    return Projection(
        baseline=baseline,
        scenario=scenario,
        projected_dues=projected_dues,
        projected_income=projected_income,
        projected_expenses=projected_expenses,
        projected_net=projected_income - projected_expenses,
        committee_projection=projected_committee,
    )


def comparison_frame(projection: Projection) -> pd.DataFrame:
    """Baseline against scenario, line by line."""
    baseline, _ = projection.baseline, projection.scenario
    rows = [
        ("Dues income", baseline.dues_collected, projection.projected_dues),
        ("Other income", baseline.non_dues_income, projection.projected_income - projection.projected_dues),
        ("Total income", baseline.income, projection.projected_income),
        ("Committee spend", baseline.committee_total, sum(projection.committee_projection.values())),
        ("Other expenses", baseline.other_expenses, projection.projected_expenses - sum(projection.committee_projection.values())),
        ("Total expenses", baseline.expenses, projection.projected_expenses),
        ("Net", baseline.net, projection.projected_net),
    ]
    frame = pd.DataFrame(rows, columns=["Line", "Baseline", "Scenario"])
    frame["Change"] = frame["Scenario"] - frame["Baseline"]
    return frame


def committee_frame(projection: Projection) -> pd.DataFrame:
    rows = []
    for committee_id, projected in sorted(
        projection.committee_projection.items(), key=lambda kv: -kv[1]
    ):
        actual = projection.baseline.committee_spend.get(committee_id, 0.0)
        if actual == 0 and projected == 0:
            continue
        rows.append(
            {
                "Committee": committee_name(committee_id),
                "Baseline": actual,
                "Scenario": projected,
                "Change": projected - actual,
            }
        )
    return pd.DataFrame(rows, columns=["Committee", "Baseline", "Scenario", "Change"])


def break_even_members(baseline: Baseline, scenario: Scenario) -> int | None:
    """
    How many paying members the scenario needs to break even.

    None when the scenario breaks even regardless (non-dues income already
    covers expenses) or when dues are free and so can never close a gap.
    """
    projection = project(baseline, scenario)
    fixed_income = projection.projected_income - projection.projected_dues
    shortfall = projection.projected_expenses - fixed_income
    if shortfall <= 0:
        return None
    if scenario.dues_rate <= 0:
        return None
    import math

    return int(math.ceil(shortfall / scenario.dues_rate))


def suggest_from_history(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
) -> dict:
    """
    A starting scenario drawn from history rather than defaults.

    Uses the equivalent term a year earlier where one exists -- comparing Fall
    with Fall is far more useful than comparing Fall with the Spring before it.
    """
    baseline = build_baseline(df_transactions, df_budgets, df_terms, semester)
    order = ordered_semesters(df_terms)

    season = semester.split()[0] if semester else ""
    prior_same_season = [
        s for s in order if s.startswith(season) and s != semester and order.index(s) < order.index(semester)
    ]
    reference = prior_same_season[-1] if prior_same_season else previous_semester(df_terms, semester)

    growth = 1.0
    if reference:
        earlier = build_baseline(df_transactions, df_budgets, df_terms, reference)
        if earlier.dues_payers:
            growth = baseline.dues_payers / earlier.dues_payers

    return {
        "dues_rate": baseline.dues_rate or 35.0,
        "expected_members": max(int(round(baseline.dues_payers * growth)), 0),
        "reference": reference,
        "growth": round(growth, 2),
    }
