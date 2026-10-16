"""
"Projected burn" on My Committee is shown for Membership only.

It is a straight-line pace estimate, so it is switched on committee by committee
(`vp_committees.PROJECTED_BURN_LINE_IDS`). These tests pin who gets it, and that
everyone else still gets the rest of the page -- including the semester chart.
"""

from __future__ import annotations

import pytest

from ais_fmd.config import vp_committees
from ais_fmd.config.categories import committee_name

# Fixtures and helpers shared with the other VP tests.
from tests.test_flags import OFFICER  # noqa: F401
from tests.test_views import VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import run_as  # noqa: F401

from ais_fmd import auth

MEMBERSHIP = 5
OTHERS = [4, 7, 9, 10]  # President, Consulting, Marketing, Professional Development


def burn_shown(app) -> bool:
    return any("Projected burn" in str(block.value) for block in app.markdown)


def headings(app) -> list[str]:
    return [str(header.value) for header in app.subheader]


# --- The setting (pure) --------------------------------------------------------


def test_only_membership_is_switched_on():
    assert vp_committees.shows_projected_burn((MEMBERSHIP,))
    for line in OTHERS:
        assert not vp_committees.shows_projected_burn((line,)), committee_name(line)


def test_a_committee_that_owns_several_lines_gets_it_if_one_of_them_is_membership():
    assert vp_committees.shows_projected_burn((7, MEMBERSHIP))
    assert not vp_committees.shows_projected_burn((7, 9))


def test_no_lines_means_no_projected_burn():
    assert not vp_committees.shows_projected_burn(())


# --- The page ------------------------------------------------------------------


def test_the_membership_vp_sees_projected_burn(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", OFFICER, MEMBERSHIP)
    assert_clean(app, "My Committee as the Membership VP")
    assert burn_shown(app)


@pytest.mark.parametrize("line", OTHERS)
def test_everyone_else_does_not(line, seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", OFFICER, line)
    assert_clean(app, f"My Committee as {committee_name(line)}")
    assert not burn_shown(app)
    # The rest of the section is untouched: the semester chart is still there.
    assert "Across semesters" in headings(app)


def test_the_treasurer_viewing_membership_sees_it_but_not_other_committees(seeded_db, use_db):
    use_db(seeded_db)
    assert burn_shown(run_as(VIEWS / "Officer.py", auth.Role.TREASURER, MEMBERSHIP))
    assert not burn_shown(run_as(VIEWS / "Officer.py", auth.Role.TREASURER, 7))
