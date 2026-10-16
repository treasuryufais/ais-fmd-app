"""
A one-committee printable report, for a VP to bring to a meeting.

Self-contained HTML (no external files, no scripts) that opens in any browser and
prints to PDF. It has the same figures as the My Committee page, for one
committee and one term.

Scoping is done *inside* `build_for_committee`, not left to the caller: it takes
the whole ledger and filters to the committee's budget lines itself, so a caller
that forgot to filter cannot leak another committee into the file. Every piece of
text that came from data is HTML-escaped, because a bank description is free text.

No Streamlit here, so it is testable on its own.
"""

from __future__ import annotations

import html
from datetime import datetime

import pandas as pd

from ..config import vp_committees
from ..config.categories import committee_name
from . import budgets as budget_domain
from . import vp_metrics
from .money import format_currency
from .terms import attach_semester

LARGEST_CHARGES = 10
TOP_PURPOSES = 10


def _esc(value: object) -> str:
    return html.escape("" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value))


def _table(
    headers: list[str],
    rows: list[list[str]],
    numeric: set[str] = frozenset(),
    nowrap: set[str] = frozenset(),
) -> str:
    """`numeric` columns are right-aligned (header too); `nowrap` ones never break across lines."""
    if not rows:
        return '<p class="muted">Nothing to report.</p>'

    def css_class(header: str) -> str:
        return " ".join(
            name for name, on in (("num", header in numeric), ("nowrap", header in nowrap)) if on
        )

    head = "".join(f'<th class="{css_class(h)}">{_esc(h)}</th>' for h in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{css_class(header)}">{_esc(cell)}</td>'
            for header, cell in zip(headers, row)
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _pct(value: object) -> str:
    return "—" if value is None or (isinstance(value, float) and pd.isna(value)) else f"{value:.1f}%"


_STATUS_LABEL = {
    "over": "Over budget",
    "approaching": "Approaching budget",
    "on track": "On track",
}


def build_for_committee(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    committee_id: int,
    semester: str,
    *,
    generated: datetime,
    data_note: str = "",
    sandbox: bool = False,
    as_of: pd.Timestamp | None = None,
) -> str:
    """The report for the committee that owns `committee_id`, for `semester`."""
    line_ids = vp_committees.budget_ids_for(committee_id)
    if not line_ids:
        raise ValueError("A committee report needs a committee.")
    names = [committee_name(cid) for cid in line_ids]
    title = vp_committees.title_for(committee_id)

    mine = (
        df_transactions
        if df_transactions.empty
        else df_transactions[df_transactions["budget_category"].isin(line_ids)]
    )

    summary = budget_domain.budget_vs_actual(df_transactions, df_budgets, df_terms, semester)
    lines = summary[summary["Committee_Name"].isin(names)]
    position = vp_metrics.rollup(summary, names)
    status = vp_metrics.status_for(position["percent"], position["spent"])
    elapsed = vp_metrics.term_elapsed_percent(df_terms, semester, as_of=as_of)
    pace = vp_metrics.pace_sentence(position, elapsed, semester)

    scoped = attach_semester(mine, df_terms)
    scoped = scoped[scoped["Semester"] == semester] if not scoped.empty else scoped
    expense_split = budget_domain.categorize_flow(scoped, budget_domain.EXPENSE)
    income_split = budget_domain.categorize_flow(scoped, budget_domain.INCOME)
    history = vp_metrics.historical(mine, df_budgets, df_terms, names)

    metrics = "".join(
        f'<div class="metric"><span class="label">{label}</span><span class="value">{value}</span></div>'
        for label, value in (
            ("Budget", format_currency(position["budget"])),
            ("Spent", format_currency(position["spent"])),
            ("Remaining", format_currency(position["remaining"])),
            ("Used", "—" if position["percent"] is None else f"{position['percent']:.0f}%"),
        )
    )

    line_rows = [
        [row["Committee_Name"], format_currency(row["Budget"]), format_currency(row["Spent"]),
         format_currency(row["Remaining"]), _pct(row["% Spent"]), row["Status"]]
        for _, row in lines.iterrows()
    ]

    purpose_rows = [
        [row["Category"], format_currency(row["Amount"])]
        for _, row in expense_split.head(TOP_PURPOSES).iterrows()
    ]
    received_rows = [
        [row["Category"], format_currency(row["Amount"])] for _, row in income_split.iterrows()
    ]

    expenses = scoped[scoped["amount"] < 0] if not scoped.empty else scoped
    largest = expenses.sort_values("amount").head(LARGEST_CHARGES) if not expenses.empty else expenses
    largest_rows = [
        [f"{pd.Timestamp(row['transaction_date']):%Y-%m-%d}", format_currency(row["amount"]),
         "—" if pd.isna(row["purpose"]) else row["purpose"],
         ("" if pd.isna(row["details"]) else str(row["details"]))[:90]]
        for _, row in largest.iterrows()
    ]

    history_rows = [
        [row["Semester"], format_currency(row["Budget"]), format_currency(row["Spent"]), _pct(row["% Spent"])]
        for _, row in history.iterrows()
    ]

    banner = (
        '<div class="sandbox">SANDBOX DATA &mdash; made-up figures, not a real report.</div>'
        if sandbox
        else ""
    )
    pace_html = f"<p>{_esc(pace)}</p>" if pace else ""
    covers = ", ".join(names)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<title>{_esc(title)} — {_esc(semester)} committee report</title>
<style>
  :root {{ --ink:#16202a; --muted:#5f6f7a; --rule:#d8e0e5; --accent:#0b6f9a; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:Georgia,'Times New Roman',serif; color:var(--ink);
         max-width:46rem; margin:0 auto; padding:2.5rem 1.5rem 4rem; line-height:1.55; }}
  header {{ border-bottom:2px solid var(--ink); padding-bottom:.8rem; margin-bottom:1.5rem; }}
  h1 {{ font-size:1.8rem; margin:0 0 .2rem; letter-spacing:-.01em; }}
  h2 {{ font-size:1.05rem; margin:1.8rem 0 .5rem; padding-bottom:.25rem; border-bottom:1px solid var(--rule); }}
  .eyebrow {{ font-family:system-ui,sans-serif; font-size:.72rem; letter-spacing:.14em;
              text-transform:uppercase; color:var(--accent); margin-bottom:.5rem; }}
  .sandbox {{ font-family:system-ui,sans-serif; font-size:.75rem; letter-spacing:.06em;
              background:#fff4d6; border:1px solid #e6c866; padding:.4rem .7rem; margin-bottom:1rem; }}
  .metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:1px; background:var(--rule);
              border:1px solid var(--rule); margin:1.2rem 0; }}
  .metric {{ background:#fff; padding:.8rem 1rem; }}
  .metric .label {{ display:block; font-family:system-ui,sans-serif; font-size:.68rem;
                    text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }}
  .metric .value {{ display:block; font-size:1.25rem; margin-top:.2rem; font-variant-numeric:tabular-nums; }}
  .status {{ font-family:system-ui,sans-serif; font-size:.8rem; font-weight:600; }}
  table {{ width:100%; border-collapse:collapse; font-family:system-ui,sans-serif;
           font-size:.8rem; margin:.4rem 0 .8rem; }}
  th {{ text-align:left; font-size:.65rem; text-transform:uppercase; letter-spacing:.07em;
        color:var(--muted); border-bottom:1px solid var(--ink); padding:.4rem .45rem; }}
  td {{ padding:.4rem .45rem; border-bottom:1px solid var(--rule); vertical-align:top; }}
  th.num, td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  th.nowrap, td.nowrap {{ white-space:nowrap; }}
  .muted {{ color:var(--muted); font-style:italic; }}
  footer {{ margin-top:2rem; padding-top:.8rem; border-top:1px solid var(--rule);
            font-family:system-ui,sans-serif; font-size:.72rem; color:var(--muted); }}
  @media print {{
    body {{ padding:0; max-width:none; }}
    h2 {{ page-break-after:avoid; }}
    table, .metrics {{ page-break-inside:avoid; }}
  }}
</style></head><body>
{banner}<header>
  <div class="eyebrow">UF AIS &mdash; Committee report</div>
  <h1>{_esc(title)}</h1>
  <div>{_esc(semester)} &middot; <span class="status">{_esc(_STATUS_LABEL.get(status, status.title()))}</span></div>
  <div class="muted">Generated {generated:%B} {generated.day}, {generated.year}. {_esc(data_note)}</div>
</header>

<div class="metrics">{metrics}</div>
{pace_html}

<h2>By budget line</h2>
{_table(["Budget line", "Budget", "Spent", "Remaining", "% spent", "Status"], line_rows,
        {"Budget", "Spent", "Remaining", "% spent"})}

<h2>Spending by purpose</h2>
{_table(["Purpose", "Amount"], purpose_rows, {"Amount"})}

<h2>Money received</h2>
{_table(["Source", "Amount"], received_rows, {"Amount"})}

<h2>Largest charges</h2>
{_table(["Date", "Amount", "Purpose", "Details"], largest_rows, {"Amount"}, {"Date"})}

<h2>Across semesters</h2>
{_table(["Semester", "Budget", "Spent", "% spent"], history_rows, {"Budget", "Spent", "% spent"})}

<footer>
  Covers only this committee's budget lines: {_esc(covers)}. Figures are drawn from
  recorded transactions; confirm against bank statements before external use.
</footer>
</body></html>"""
