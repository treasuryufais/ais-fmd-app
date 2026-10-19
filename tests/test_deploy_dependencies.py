"""
The Pipfile (what Streamlit Community Cloud installs) must match
requirements-production.txt (what a production install is documented as).

Cloud picks the Pipfile over requirements.txt, so a package added to the
requirements files but not here would be missing only in the deployed app --
an import error on first load in production, invisible to every local test.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_ONLY = {"pytest"}


def _requirements(path: Path) -> dict[str, str]:
    """name -> specifier, following `-r` includes."""
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r"):
            found.update(_requirements(path.parent / line[2:].strip()))
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", line)
        assert match, f"unparseable requirement line in {path.name}: {raw!r}"
        found[match.group(1).lower()] = match.group(2).replace(" ", "")
    return found


def _pipfile() -> dict[str, str]:
    data = tomllib.loads((ROOT / "Pipfile").read_text(encoding="utf-8"))
    return {name.lower(): str(spec).replace(" ", "") for name, spec in data["packages"].items()}


def test_pipfile_matches_production_requirements():
    expected = {
        name: spec
        for name, spec in _requirements(ROOT / "requirements-production.txt").items()
        if name not in TEST_ONLY
    }
    assert _pipfile() == expected, (
        "Pipfile and requirements-production.txt disagree -- Streamlit Cloud "
        "installs from the Pipfile, so update both together."
    )


def test_pipfile_includes_the_production_only_packages():
    """The whole point of the file: Cloud gets what the sandbox deliberately omits."""
    assert {"supabase", "authlib"} <= set(_pipfile())
    assert "supabase" not in _requirements(ROOT / "requirements.txt")
