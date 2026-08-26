"""
Module M22 -- VP portal access: `profiles` storage, and the login resolution
logic it feeds.

The identity-resolution tests live in `test_auth_gate.py` next to the rest of
the login gate; these cover the storage layer underneath it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd.data.sqlite_backend import SqliteBackend


@pytest.fixture
def backend() -> SqliteBackend:
    return SqliteBackend()


def test_upsert_creates_a_new_profile(backend):
    result = backend.upsert_profile("vp@ufl.edu", "officer", 8, "VP Membership", "treasurer")
    assert result.ok and result.updated == 1

    stored = backend.fetch_profile("vp@ufl.edu")
    assert stored["role"] == "officer"
    assert stored["committee_id"] == 8
    assert stored["display_name"] == "VP Membership"


def test_upsert_updates_rather_than_duplicates(backend):
    backend.upsert_profile("vp@ufl.edu", "officer", 8, "Old Name", "treasurer")
    backend.upsert_profile("vp@ufl.edu", "treasurer", None, "New Name", "treasurer")

    all_rows = backend.fetch_profiles()
    assert len(all_rows) == 1, "a second call must update, not insert a second row"
    stored = backend.fetch_profile("vp@ufl.edu")
    assert stored["role"] == "treasurer"
    assert stored["display_name"] == "New Name"
    assert pd.isna(stored["committee_id"]) or stored["committee_id"] is None


def test_lookup_is_case_insensitive():
    """
    Google's identity token capitalises an email however the account was typed.

    A treasurer entering "VP@ufl.edu" and Google asserting "vp@ufl.edu" must
    still be the same person, or a real VP gets refused over a case mismatch.
    """
    backend = SqliteBackend()
    backend.upsert_profile("VP@ufl.edu", "officer", 8, "", "treasurer")
    assert backend.fetch_profile("vp@ufl.edu") is not None
    assert backend.fetch_profile("vp@UFL.EDU") is not None


def test_lookup_of_an_unknown_email_returns_none(backend):
    assert backend.fetch_profile("nobody@ufl.edu") is None


def test_an_unknown_role_is_rejected_before_it_reaches_storage(backend):
    """
    Rejected at the write, not left for the login path to catch.

    `auth._identity_for_google_user` also refuses an unrecognised role string,
    but a bad value should never be stored in the first place -- two
    independent checks are better than relying on the second one alone.
    """
    result = backend.upsert_profile("vp@ufl.edu", "superadmin", None, "", "treasurer")
    assert not result.ok
    assert backend.fetch_profile("vp@ufl.edu") is None


def test_a_blank_email_is_rejected(backend):
    result = backend.upsert_profile("", "officer", 8, "", "treasurer")
    assert not result.ok


def test_remove_profile_deletes_it(backend):
    backend.upsert_profile("vp@ufl.edu", "officer", 8, "", "treasurer")
    result = backend.remove_profile("vp@ufl.edu")
    assert result.ok and result.updated == 1
    assert backend.fetch_profile("vp@ufl.edu") is None


def test_removing_someone_not_present_is_not_an_error(backend):
    result = backend.remove_profile("nobody@ufl.edu")
    assert result.ok
    assert result.updated == 0


def test_fetch_profiles_lists_everyone(backend):
    backend.upsert_profile("a@ufl.edu", "officer", 5, "", "treasurer")
    backend.upsert_profile("b@ufl.edu", "treasurer", None, "", "treasurer")
    rows = backend.fetch_profiles()
    assert set(rows["email"]) == {"a@ufl.edu", "b@ufl.edu"}


def test_base_backend_reports_unsupported_without_crashing():
    """
    The un-overridden defaults answer these calls rather than raising, so a
    backend that has not implemented the VP portal yet -- Supabase, until
    HANDOFF §8.4's missing methods are filled in -- lets a page render "not
    registered" instead of crashing outright.

    Called as plain functions rather than through an instance: `Backend` is
    abstract and constructing any concrete subclass would exercise its
    overrides instead of the defaults this test is actually checking.
    """
    from ais_fmd.data.backend import Backend

    assert Backend.fetch_profile(None, "x@ufl.edu") is None
    assert Backend.fetch_profiles(None).empty
