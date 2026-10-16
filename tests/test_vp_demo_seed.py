"""
The VP demo seed, run more than once.

The script used to refuse a second run, so adding a committee to it meant wiping
the sandbox. It now adds only what is missing (a newly listed committee, a new
profile) and never repeats what is already there. These tests hold both halves:
a second run changes nothing, and a committee that is missing gets added alone.

Everything here is the invented sandbox data; all emails are `@sandbox.local`.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from ais_fmd.config import vp_committees
from ais_fmd.data.sqlite_backend import SqliteBackend

# Fixtures and helpers shared with the other VP tests.
from tests.test_views import seeded_db  # noqa: F401
from tests.test_vp_end_to_end import DEMO, PROFILES, _load_demo_script  # noqa: F401

OPERATIONS = 6
OPERATIONS_EMAIL = "vp.operations@sandbox.local"


@pytest.fixture
def fresh_demo(tmp_path, seeded_db, monkeypatch):  # noqa: F811
    """A private copy of the base seed with the demo seed run once."""
    target = tmp_path / "demo.db"
    shutil.copy(seeded_db, target)
    monkeypatch.setenv("AIS_FMD_SANDBOX_DB", str(target))
    assert _load_demo_script().main() == 0
    return target


def run_demo() -> int:
    return _load_demo_script().main()


def counts_by_line(path) -> dict:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT budget_category, COUNT(*) FROM transactions "
            "WHERE source_file = ? GROUP BY budget_category",
            (DEMO.FILE_NAME,),
        ).fetchall()
    return {int(line): count for line, count in rows}


def test_operations_is_listed_and_has_a_sign_in():
    assert OPERATIONS in [vp.budget_ids[0] for vp in vp_committees.VP_COMMITTEES]
    profile = next(p for p in PROFILES if p[0] == OPERATIONS_EMAIL)
    assert profile[1:3] == ("officer", OPERATIONS)


def test_the_first_run_gives_operations_spending_and_a_profile(fresh_demo):
    assert counts_by_line(fresh_demo).get(OPERATIONS, 0) > 0
    assert SqliteBackend().fetch_profile(OPERATIONS_EMAIL)["committee_id"] == OPERATIONS


def test_a_second_run_changes_nothing(fresh_demo):
    before = counts_by_line(fresh_demo)
    assert run_demo() == 0
    assert counts_by_line(fresh_demo) == before


def test_a_committee_added_later_is_filled_in_without_touching_the_others(fresh_demo):
    """The case that matters: a sandbox seeded before the committee existed."""
    before = counts_by_line(fresh_demo)
    with sqlite3.connect(fresh_demo) as connection:
        connection.execute(
            "DELETE FROM transactions WHERE source_file = ? AND budget_category = ?",
            (DEMO.FILE_NAME, OPERATIONS),
        )
        connection.execute("DELETE FROM profiles WHERE email = ?", (OPERATIONS_EMAIL,))
        connection.commit()
    assert OPERATIONS not in counts_by_line(fresh_demo)
    assert SqliteBackend().fetch_profile(OPERATIONS_EMAIL) is None

    assert run_demo() == 0

    after = counts_by_line(fresh_demo)
    assert after.get(OPERATIONS, 0) > 0                      # it is back
    assert {line: n for line, n in after.items() if line != OPERATIONS} == {
        line: n for line, n in before.items() if line != OPERATIONS
    }                                                                  # nothing else moved
    assert SqliteBackend().fetch_profile(OPERATIONS_EMAIL) is not None


def test_a_missing_profile_alone_is_restored(fresh_demo):
    before = counts_by_line(fresh_demo)
    with sqlite3.connect(fresh_demo) as connection:
        connection.execute("DELETE FROM profiles WHERE email = ?", (OPERATIONS_EMAIL,))
        connection.commit()
    assert run_demo() == 0
    assert SqliteBackend().fetch_profile(OPERATIONS_EMAIL) is not None
    assert counts_by_line(fresh_demo) == before
