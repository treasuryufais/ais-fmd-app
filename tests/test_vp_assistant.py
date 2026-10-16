"""
The Assistant, for a VP.

The Assistant is in a VP's menu (under My Transactions) but is the shared page: it is
not scoped to a committee here. Keeping a VP to their own committee's numbers is the
AI Assistant team's work ("VPs only see their own committee": 0 cross-committee leaks
across conflicting prompts), so this file only records where things stand.

`test_the_assistant_does_not_show_a_vp_other_committees` is a KNOWN GAP, marked
xfail: it fails today because the Assistant answers from every committee. When the
guardrails land it will start passing; at that point delete the marker so it becomes
a real guard.
"""

from __future__ import annotations

import pytest

from tests.test_flags import OFFICER
from tests.test_views import VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import run_as

CONSULTING = 7
OTHER_COMMITTEES = ["Marketing", "Technology", "Meeting Food", "Road Trip", "Formal"]


def ask(question: str):
    app = run_as(VIEWS / "Assistant.py", OFFICER, CONSULTING)
    app.chat_input[0].set_value(question).run()
    return app


def visible_text(app) -> str:
    parts = [str(block.value) for block in app.markdown]
    for table in app.dataframe:
        parts.append(table.value.to_string())
    return "\n".join(parts)


def test_the_assistant_page_opens_for_a_vp_and_answers(seeded_db, use_db):
    use_db(seeded_db)
    app = ask("Which committees are over budget?")
    assert_clean(app, "the Assistant as the Consulting VP")
    assert "over budget" in visible_text(app)


@pytest.mark.xfail(
    reason="Known gap: the Assistant is not scoped to a committee yet. "
    "The AI Assistant team owns the guardrails.",
    strict=False,
)
def test_the_assistant_does_not_show_a_vp_other_committees(seeded_db, use_db):
    use_db(seeded_db)
    text = visible_text(ask("Which committees are over budget?"))
    leaked = [name for name in OTHER_COMMITTEES if name in text]
    assert not leaked, f"a Consulting VP was shown other committees: {leaked}"
