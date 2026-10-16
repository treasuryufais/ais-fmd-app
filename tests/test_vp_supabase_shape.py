"""
The VP pages and reports against data shaped the way Supabase returns it.

This sandbox is cut off from the real Supabase on purpose, so it cannot prove a live
connection. What it can prove is the next best thing: every VP page and the report
read only through `repositories` (where Supabase plugs in), and they behave the
same when the data arrives in Supabase's shapes rather than SQLite's. Those shapes
differ in small ways that break code written only against SQLite:

  * a blank is `None`, not NaN;
  * a whole-dollar amount arrives as an integer;
  * timestamps (`uploaded_at`, flag times) are timezone-aware ISO strings with an
    offset, where SQLite stores a naive string.

`SupabaseShaped` wraps the sandbox backend and reshapes what it returns; the tests
run each page and report both ways and require the same output. A real connection
still has to be tried once against a throwaway Supabase project -- see HANDOFF.md.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
import streamlit as st

from ais_fmd import auth
from ais_fmd.data import repositories
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import committee_report, vp_metrics

from tests.test_views import VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_end_to_end import VP_LINES, demo_db, demo_template  # noqa: F401
from tests.test_vp_portal import OFFICER, run_as


def _iso_utc(series: pd.Series) -> pd.Series:
    """'2026-10-01 12:00:00' -> '2026-10-01T12:00:00+00:00', and a blank -> None."""
    stamps = pd.to_datetime(series, errors="coerce")
    text = stamps.dt.tz_localize("UTC").dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return text.astype(object).where(stamps.notna(), None)


def _blank_as_none(frame: pd.DataFrame, columns) -> pd.DataFrame:
    for column in columns:
        if column in frame.columns:
            frame[column] = frame[column].astype(object).where(frame[column].notna(), None)
    return frame


class SupabaseShaped:
    """The sandbox backend, returning data in the shapes SupabaseBackend returns."""

    def __init__(self, inner: SqliteBackend, *, whole_dollar_amounts: bool = False):
        self._inner = inner
        self._whole = whole_dollar_amounts

    def __getattr__(self, name):  # writes and anything unshaped pass straight through
        return getattr(self._inner, name)

    def fetch_transactions(self) -> pd.DataFrame:
        frame = self._inner.fetch_transactions().copy()
        _blank_as_none(frame, ("purpose", "details", "account", "source_file", "natural_key"))
        # JSON numbers: a whole amount arrives as an int, anything else as a float.
        amounts = [
            int(round(a)) if self._whole or float(a).is_integer() else float(a) for a in frame["amount"]
        ]
        frame["amount"] = pd.Series(amounts, index=frame.index)
        return frame

    def fetch_uploaded_files(self) -> pd.DataFrame:
        frame = self._inner.fetch_uploaded_files().copy()
        if "uploaded_at" in frame.columns:
            frame["uploaded_at"] = _iso_utc(frame["uploaded_at"])
        return frame

    def fetch_flags(self, committee_ids=None) -> pd.DataFrame:
        frame = self._inner.fetch_flags(committee_ids).copy()
        if frame.empty:
            return frame
        for column in ("flagged_at", "resolved_at"):
            if column in frame.columns:
                frame[column] = _iso_utc(frame[column])
        return _blank_as_none(frame, ("resolved_by", "resolution_note"))


@pytest.fixture
def plain(demo_db):
    return SqliteBackend()


def _use(monkeypatch, backend) -> None:
    monkeypatch.setattr(repositories, "backend", lambda: backend)
    st.cache_data.clear()  # nothing read before the swap may be served after it


def _snapshot(app) -> dict:
    """What a person would see: every table, every caption, every section heading."""
    return {
        "tables": [element.value.to_csv() for element in app.dataframe],
        "captions": [caption.value for caption in app.caption],
        "headings": [subheader.value for subheader in app.subheader],
    }


# --- Pages ---------------------------------------------------------------------------


@pytest.mark.parametrize("view", ["Officer.py", "MyTransactions.py"])
@pytest.mark.parametrize("line", VP_LINES)
def test_the_page_looks_the_same_on_supabase_shaped_data(view, line, plain, monkeypatch):
    extra = {"mytxn_semester": "All"} if view == "MyTransactions.py" else {}
    baseline = run_as(VIEWS / view, OFFICER, line, **extra)
    assert_clean(baseline, f"{view} on sandbox data")

    _use(monkeypatch, SupabaseShaped(plain))
    shaped = run_as(VIEWS / view, OFFICER, line, **extra)
    assert_clean(shaped, f"{view} on Supabase-shaped data")
    assert _snapshot(shaped) == _snapshot(baseline)


@pytest.mark.parametrize("view", ["Officer.py", "MyTransactions.py"])
def test_whole_dollar_amounts_do_not_break_the_pages(view, plain, monkeypatch):
    """An all-integer `amount` column arrives as int64 rather than float64."""
    _use(monkeypatch, SupabaseShaped(plain, whole_dollar_amounts=True))
    extra = {"mytxn_semester": "All"} if view == "MyTransactions.py" else {}
    app = run_as(VIEWS / view, OFFICER, 7, **extra)
    assert_clean(app, f"{view} with whole-dollar amounts")


def test_flags_read_back_cleanly_through_timezone_aware_timestamps(plain, monkeypatch):
    _use(monkeypatch, SupabaseShaped(plain))
    vp = run_as(VIEWS / "MyTransactions.py", OFFICER, 9, mytxn_semester="All")  # has a closed flag
    assert_clean(vp, "My Transactions with timezone-aware flag times")
    assert any("Your note" in element.value.columns for element in vp.dataframe)

    treasurer = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    assert_clean(treasurer, "Review Queue with timezone-aware flag times")
    assert any("Recently closed" in block.value for block in treasurer.markdown)


# --- Reports ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", VP_LINES)
@pytest.mark.parametrize("semester", ["Fall 2024", "Fall 2025", "Fall 2026"])
def test_the_report_is_identical_from_supabase_shaped_frames(line, semester, plain):
    shaped = SupabaseShaped(plain)
    kwargs = dict(generated=datetime(2026, 10, 1, 9, 30), sandbox=True)

    def build(source):
        return committee_report.build_for_committee(
            source.fetch_transactions(), source.fetch_budgets(), source.fetch_terms(),
            line, semester, **kwargs,
        )

    assert build(shaped) == build(plain)


def test_the_freshness_line_reads_a_timezone_aware_upload_time(plain):
    shaped = SupabaseShaped(plain)
    uploads = shaped.fetch_uploaded_files()
    assert uploads["uploaded_at"].str.endswith("+00:00").all()  # really in Supabase's shape
    text = vp_metrics.freshness_text(shaped.fetch_transactions(), uploads)
    assert text == vp_metrics.freshness_text(plain.fetch_transactions(), plain.fetch_uploaded_files())
    assert "Statements last uploaded" in text


# --- The data path itself ---------------------------------------------------------------


def test_the_pages_and_report_reach_data_only_through_the_shared_layer():
    """Anything that bypassed `repositories` would silently keep reading SQLite in production."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "ais_fmd"
    files = [
        "views/Officer.py", "views/MyTransactions.py", "ui/flags.py", "ui/committee_charts.py",
        "domain/committee_report.py", "domain/vp_metrics.py", "domain/vp_history.py",
        "domain/exports.py", "domain/flags.py",
    ]
    for name in files:
        source = (root / name).read_text(encoding="utf-8")
        for forbidden in ("sqlite3", "SqliteBackend", "sqlite_backend", "sandbox_data", "supabase_backend"):
            assert forbidden not in source, f"{name} reaches the database directly via {forbidden!r}"


def test_the_supabase_backend_implements_everything_the_vp_pages_call():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "ais_fmd" / "data" / "supabase_backend.py").read_text(
        encoding="utf-8"
    )
    for method in (
        "fetch_committees", "fetch_terms", "fetch_budgets", "fetch_transactions",
        "fetch_uploaded_files", "fetch_reimbursements", "fetch_flags", "create_flag",
        "resolve_flag", "fetch_profile",
    ):
        assert f"def {method}(" in source, f"SupabaseBackend does not implement {method}"


def test_the_stand_in_really_replaces_the_data_layer(plain, monkeypatch):
    """Guards the guard: if the swap silently did nothing, every comparison above would pass for free."""
    before = repositories.load_transactions()
    assert not any(value is None for value in before["purpose"]), "the sandbox should use NaN for blanks"

    _use(monkeypatch, SupabaseShaped(plain))
    frame = repositories.load_transactions()
    assert any(value is None for value in frame["purpose"]), "blanks should arrive as None"
    uploads = repositories.load_uploaded_files()
    assert uploads["uploaded_at"].astype(str).str.endswith("+00:00").all()
