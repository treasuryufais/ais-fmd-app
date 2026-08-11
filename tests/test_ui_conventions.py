"""
Conventions the views must follow, enforced by scanning the source.

This exists because one bug class kept recurring by hand: Streamlit reads
`$...$` as LaTeX math delimiters, so any sentence containing two currency
figures silently renders the text between them as an equation. It was fixed
three separate times -- on the Reconciliation page, then the Planner, then the
Officer page -- because nothing stopped it coming back.

`shell.say` and `shell.notify` escape the dollar signs. These tests fail if a
view prints money through a raw Streamlit text call instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

VIEWS = Path(__file__).resolve().parent.parent / "ais_fmd" / "views"

# Streamlit calls that render markdown, and so interpret $...$ as maths.
MARKDOWN_CALLS = {"caption", "write", "markdown", "success", "warning", "info", "error"}

# Anything that produces a formatted money string.
MONEY_MARKERS = ("format_currency", ":,.2f", "format_delta")


def view_files() -> list[Path]:
    return sorted(p for p in VIEWS.glob("*.py") if p.name != "__init__.py")


def _renders_money(node: ast.Call) -> bool:
    """Does this call embed a formatted currency value in its text?"""
    source = ast.unparse(node)
    return any(marker in source for marker in MONEY_MARKERS)


def _is_streamlit_markdown_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in MARKDOWN_CALLS:
        return False
    # st.caption(...) / st.markdown(...)
    return isinstance(func.value, ast.Name) and func.value.id == "st"


@pytest.mark.parametrize("path", view_files(), ids=lambda p: p.name)
def test_money_is_never_printed_through_a_raw_streamlit_text_call(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_streamlit_markdown_call(node)
        and _renders_money(node)
    ]
    assert not offenders, (
        f"{path.name} renders currency through a raw st.* text call at line(s) "
        f"{offenders}. Use shell.say(...) or shell.notify(...) so the dollar "
        f"signs are escaped, or Streamlit will read them as LaTeX."
    )


def test_money_safe_escapes_dollar_signs():
    from ais_fmd.ui.shell import money_safe

    assert money_safe("$1,234.56 and $7.00") == r"\$1,234.56 and \$7.00"
    assert money_safe("no money here") == "no money here"


def test_every_view_declares_an_access_role():
    """
    A page that forgets `auth.require` would be reachable by anyone.

    Enforced by scanning rather than by review, because the failure is silent.
    """
    for path in view_files():
        source = path.read_text(encoding="utf-8")
        assert "auth.require(" in source or "auth.current_user(" in source, (
            f"{path.name} does not declare an access role. Every page must call "
            f"auth.require(...) so authorisation cannot be skipped by omission."
        )


def test_views_do_not_import_backends_directly():
    """
    Views must go through the repository layer, which owns caching and
    invalidation. A direct backend import would bypass both.
    """
    for path in view_files():
        source = path.read_text(encoding="utf-8")
        assert "sqlite_backend" not in source, f"{path.name} imports a backend directly"
        assert "supabase_backend" not in source, f"{path.name} imports a backend directly"


def test_domain_never_imports_streamlit():
    """
    The rule that keeps the business logic testable without a browser.
    """
    domain = Path(__file__).resolve().parent.parent / "ais_fmd" / "domain"
    offenders = [
        path.relative_to(domain).as_posix()
        for path in domain.rglob("*.py")
        if "import streamlit" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"domain modules importing streamlit: {offenders}"
