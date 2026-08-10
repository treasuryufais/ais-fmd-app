"""
The model pass -- batched, schema-constrained, and loud about failure.

Three findings are addressed here.

FINDING F8. The original wrapped the entire call in `except Exception: return []`,
so a missing key, a rate limit, a network error and malformed JSON all produced
an empty list that was indistinguishable from "the model matched nothing". The
treasurer saw an uncategorized table with no reason to retry. Failures are now
returned as a typed `LLMOutcome` carrying the error, and the UI surfaces it.

FINDING F9. Every transaction went into one unbounded prompt with no chunking,
so a large statement silently exceeded the context window -- and one malformed
response discarded the results for the whole file. Work is now chunked, and a
failed batch only loses that batch.

FINDING (cost). The prompt encoded seven rules, five of which were then
re-implemented in Python and always overrode the model. Only the residual --
rows no deterministic rule and no merchant memory could resolve -- reaches this
module now, and the prompt only describes the distinction that is genuinely
ambiguous.

In sandbox mode this module never makes a network call. `openai` is not even
installed in the sandbox venv.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ... import settings
from .predicates import (
    COMMITTEE_PURPOSE,
    Classification,
    looks_like_bar,
    looks_like_food_merchant,
    weekday_from_details,
)

# The residual decision is narrow: which committee owns this merchant.
ALLOWED_COMMITTEES = (5, 7, 8, 10, 13, 14, 15)

_SYSTEM_PROMPT = """\
You categorize bank transactions for a university student organization.

Deterministic rules have already been applied upstream. Every transaction you \
are given is one that no rule and no known-merchant mapping could resolve, so \
your only job is to identify what kind of merchant or expense it is.

Choose exactly one committee ID per transaction:
  5  Membership              bars, social venues, member social spending
  7  Consulting              consulting-project costs
  8  Meeting Food            groceries or restaurants bought for a chapter meeting
  10 Professional Development  conferences, training, career events
  13 Merch                   apparel, printing, promotional goods
  14 Road Trip               travel, lodging, transport
  15 Technology              software, hosting, domains, hardware
  0  Unknown                 genuinely cannot tell

Rules of thumb:
  - Weekday_Hint of Tuesday or Wednesday plus a food merchant strongly suggests 8.
  - A bar, brewery or liquor store is 5, never 8.
  - When the merchant is unrecognisable, answer 0 rather than guessing.

Respond with JSON only: {"results":[{"index":<int>,"committee_id":<int>,\
"confidence":<0.0-1.0>,"reason":"<short>"}]}\
"""


@dataclass
class LLMOutcome:
    """What happened, in enough detail for the UI to explain it."""

    classifications: dict[int, Classification] = field(default_factory=dict)
    batches_attempted: int = 0
    batches_failed: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    prompt_chars: int = 0

    @property
    def ran(self) -> bool:
        return self.batches_attempted > 0

    @property
    def partially_failed(self) -> bool:
        return self.batches_failed > 0 and self.batches_failed < self.batches_attempted

    @property
    def fully_failed(self) -> bool:
        return self.batches_attempted > 0 and self.batches_failed == self.batches_attempted


def _render_row(index: int, record: dict) -> str:
    details = str(record.get("details") or "")
    weekday = weekday_from_details(details, record.get("transaction_date"))
    hints = []
    if looks_like_food_merchant(details):
        hints.append("food-merchant-keyword")
    if looks_like_bar(details):
        hints.append("bar-keyword")
    return (
        f'{index}. Details: {details[:180]} | Amount: {record.get("amount")} | '
        f'Account: {record.get("account") or ""} | Weekday_Hint: {weekday} | '
        f'Local_Hints: {",".join(hints) or "none"}'
    )


def _parse_response(payload: str, batch: list[tuple[int, dict]]) -> dict[int, Classification]:
    """Map a model response back onto the original row positions."""
    text = payload.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
        text = text.removeprefix("json").strip()

    data = json.loads(text)
    results = data.get("results", []) if isinstance(data, dict) else []

    positions = {local: original for local, (original, _) in enumerate(batch, start=1)}
    out: dict[int, Classification] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        local_index = item.get("index")
        original_index = positions.get(local_index)
        if original_index is None:
            continue
        try:
            committee_id = int(item.get("committee_id", 0))
        except (TypeError, ValueError):
            continue
        if committee_id not in ALLOWED_COMMITTEES:
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        reason = str(item.get("reason") or "").strip()[:120]
        out[original_index] = Classification(
            committee_id=committee_id,
            purpose=COMMITTEE_PURPOSE.get(committee_id),
            rule=f"Model: {reason}" if reason else "Model",
            confidence=max(0.0, min(1.0, confidence)),
            source="llm",
        )
    return out


def classify_residual(residual: list[tuple[int, dict]]) -> LLMOutcome:
    """
    Classify the rows nothing else could resolve.

    `residual` is a list of (original_index, record) so results can be mapped
    back onto the caller's ordering without relying on positional alignment.
    """
    outcome = LLMOutcome()
    if not residual:
        return outcome

    if not settings.llm_enabled():
        outcome.skipped_reason = (
            "Model pass disabled. In sandbox mode no network call is ever made; "
            "these rows are left uncategorized for the review queue."
        )
        return outcome

    try:
        settings.assert_external_call_allowed("OpenAI categorization request")
    except settings.SandboxViolation as exc:
        outcome.skipped_reason = str(exc)
        return outcome

    try:
        from openai import OpenAI  # imported lazily; absent in the sandbox venv
    except ImportError:
        outcome.skipped_reason = (
            "The 'openai' package is not installed. The sandbox venv omits it "
            "deliberately so no request can be made."
        )
        return outcome

    import os

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    model = settings.llm_model()
    batch_size = settings.llm_batch_size()

    for start in range(0, len(residual), batch_size):
        batch = residual[start : start + batch_size]
        listing = "\n".join(
            _render_row(local, record) for local, (_, record) in enumerate(batch, start=1)
        )
        outcome.batches_attempted += 1
        outcome.prompt_chars += len(listing)
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Transactions:\n{listing}"},
                ],
            )
            content = response.choices[0].message.content or ""
            outcome.classifications.update(_parse_response(content, batch))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            outcome.batches_failed += 1
            outcome.errors.append(f"{type(exc).__name__}: {exc}")

    return outcome
