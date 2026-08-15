# Handoff — UF AIS Financial Management System (sandbox rebuild)

**For:** whoever picks this up next, human or agent.
**Read this first.** Written to be self-contained — you should not need any prior
conversation.

**Last verified 2026-08-14:** 365 tests passing (1 skipped, ~55 s); app runs;
892 real transactions; 590 human ground-truth labels.

This document describes **what is true now**, not what happened when. Where a
decision looks odd, the reason it is that way is given, because in almost every
case it was odd for a reason discovered the hard way.

---

## 1. What this is, and what it is not

`C:\Users\durpy_7vdh2wz\ais-fmd-sandbox` is a **rebuild** of the treasury app at
`C:\Users\durpy_7vdh2wz\ais-fmd-app`. It is a **sandbox**: local SQLite, no
network.

**The original repo has never been modified.** Still at commit `6cb308c` with
the same two modified files it had at the start (`requirements.txt`,
`views/treasury_auto_categorize.py`). The sandbox is a separate git repo with
**no remote**, so `git push` has nowhere to go. The one file touched outside the
sandbox is `ais-fmd-app/.claude/launch.json` — agent tooling config, untracked,
no app code.

**Nothing has been deployed. The production path (Supabase) has never executed.**

### Running it

```bash
cd C:\Users\durpy_7vdh2wz\ais-fmd-sandbox
.venv/Scripts/python -m streamlit run app.py     # http://localhost:8501
.venv/Scripts/python -m pytest                    # 365 tests, ~55 s
```

| Script | Purpose |
| --- | --- |
| `scripts/load_real_statement.py <csv> --into-sandbox` | Load a real statement, replacing sandbox data |
| `scripts/test_real_statement.py <csv>` | Parse-only diagnostic, no writes |
| `scripts/propose_merchants.py` | Bulk merchant mapping: propose → review CSV → `--apply` |
| `scripts/import_ground_truth.py <rendered.txt> --era <era>` | Import human labels from Drive ledgers |
| `scripts/evaluate_categorizer.py [--fit]` | Accuracy per threshold/committee; `--fit` prints learned weights |
| `scripts/profile_hotpath.py` | Times the work every page rerun does |
| `scripts/verify_later_modules.py` | E2E check of locking/reimbursements/planner on a DB copy |

### The safety model — do not weaken this

Three independent layers:

1. **`openai` and `supabase` are not installed** in the sandbox venv. Verify:
   `.venv/Scripts/python -c "import importlib.util as u; print(u.find_spec('openai'), u.find_spec('supabase'))"` → both `None`.
2. **Fails closed.** `AIS_FMD_ENV` must equal `production` (case-insensitive,
   whitespace-trimmed). Unset, empty, `prod`, `1`, `true` all resolve to sandbox.
   A stray `OPENAI_API_KEY` spends nothing — `settings.llm_enabled()` is `False`
   in sandbox mode. One documented override exists,
   `AIS_FMD_ALLOW_LLM_IN_SANDBOX=1`, which still cannot spend anything because
   layer 1 removes the package. That independence is the point.
3. **Separate directory, own git repo, no remote.**

`tests/test_sandbox_safety.py` asserts all of it. **If those tests fail, the
sandbox is no longer safe to hand to anyone.**

---

## 2. Verified current state

```
transactions       892   real Wells Fargo data, 2024-07-09 .. 2026-07-08
  categorized      566   (stored value; the current pipeline resolves more — see §5)
terms                9
committeebudgets    84
labeled_examples   590   human ground truth, era 2022-2023 (Drive ledgers)
merchants            0   <-- merchant memory still dormant
transaction_audit  993
statement_balances   0   <-- reconciliation has nothing to check
receipts             0
reimbursements       0
accounts             ['Wells Fargo']   <-- no Venmo data at all
```

Code: **18,069 lines** — 12,975 app (3,347 of it views), 3,783 tests, 1,311 scripts.

**Pipeline performance on the real data (re-run, not the stored column):**

```
ALL 892 rows : 411 by exact rule, 193 by scoring, 288 for review (59 with a proposal)  → 67.7%
PAST YEAR 398: 220 by exact rule,  97 by scoring,  81 for review (27 with a proposal)  → 79.6%
```

The stored `budget_category` predates the scoring engine. Re-running
categorization would resolve more; nothing has done so in bulk.

---

## 3. Architecture

```
ais_fmd/
  settings.py            env detection + sandbox guard (fails closed)
  auth.py                roles: MEMBER < OFFICER < TREASURER < ADMIN
  config/categories.py   committees, purposes, accounts — ONE source of truth
  data/
    backend.py           interface both backends implement
    sqlite_backend.py    sandbox
    supabase_backend.py  production — NEVER EXECUTED
    repositories.py      the single cache layer
    schema_postgres.sql  production DDL + atomic import RPC
    seed.py              demo data generator
  domain/                business logic — NOTHING here imports streamlit
    money, terms, budgets, dedupe, reconcile, quality, dues,
    alerts, report, scenarios, reimbursements, receipts, assistant
    categorize/          predicates, merchants, scoring, learning, context, bulk, llm, pipeline
    parsers/             venmo, wells_fargo, ledger
  ui/                    theme (Plotly template), charts, shell
  views/                 15 Streamlit pages — layout and wiring only
tests/                   365 tests
```

**Invariants enforced by tests** (`tests/test_ui_conventions.py`):
- nothing under `domain/` imports Streamlit
- every view calls `auth.require(...)`
- no view imports a backend directly (must go through `repositories`)
- no view renders currency through a raw `st.caption`/`st.write` (see §9 trap 1)

---

## 4. The categorization pipeline — read this before touching it

This is where nearly all recent work went. **Order is load-bearing.**

```
1. EXACT RULES      certainties: refund/formal/consulting(card 8408)/dues
2. MERCHANT MEMORY  a human's explicit decision about this exact merchant
3. SCORING          weighted evidence; confidence gate at 0.75
4. THE MODEL        only the residual (never runs in sandbox)
   ↓ below threshold or no signal
   REVIEW QUEUE     with the proposal and its reasoning attached
```

### Why the tiers sit where they do

**Exact rules first.** The spec is emphatic: "If Details contains `card 8408`,
categorize as Committee ID 7 immediately. Ignore all remaining rules." Merchant
memory used to run ahead of all rules, which let a learned mapping override
that. Real data made it concrete: two Publix purchases are on the consulting
card, so the obvious bulk confirmation `publix → Meeting Food` would silently
have re-booked them. Guarded by
`test_exact_rules_outrank_merchant_memory`.

**Merchant memory short-circuits, it does not vote.** Tried as a weighted signal
first; a confirmed mapping and a full meeting-food reading scored close enough
to flag each other, which sent rows a treasurer had already ruled on back into
their queue.

**Scoring replaced a first-match-wins rule chain** (`domain/categorize/scoring.py`).
A rule chain encodes every tie-break as list order and cannot express "two
signals disagree, so I am unsure" — it returns whichever rule was listed first,
at full confidence. Scoring lets signals vote, and **confidence falls when they
conflict**, so contested rows route to a human instead of being booked on a
coin-flip.

  * `confidence = evidence_strength × dominance`
  * `dominance = 0.5 + 0.5 × (top − runner_up) / top` — margin-relative. The
    share form (`top / (top + runner_up)`) was tried first and was too punishing:
    a three-signal winner with one weak dissenter fell below threshold.
  * `AUTO_APPLY_THRESHOLD = 0.75`. **Still a guess** — see §6 for calibrating it
    from data.

**Cardholder default is a heuristic, below meeting food.** Treasury: *"sometimes
people's cards were used to buy things for other committees"*, and meeting food
specifically has been bought on the wrong card due to card-issuance history. So
`rule_membership_card` (cards 8313/5718) sits **below** `rule_meeting_food`.
Getting this backwards silently re-booked 9 correct Meeting Food rows worth
$868.71. Guarded by `test_meeting_food_wins_over_officer_card_default`.

### Confirmed card roster

From treasury's own *"Categorization Architecture"* Google Doc — the document
this categorizer was originally specced from:

| Card | Holder | Committee | Strength |
| --- | --- | --- | --- |
| 8408 | Salena | Consulting (7) | **certainty** — exact rule, spec says it wins outright |
| 8313 | Annalee | Membership (5) | default — heuristic tier |
| 5718 | Grant | Membership (5) | default — heuristic tier |
| 3568 | Trent | **President (4)** | default — note: *not* Membership, despite 13 Macdintons visits |

Seven other cards appear in the real data with **no documented owner**
(0153, 7757, 9309, 3444, 7193, 1113, 5535). History suggests 0153 is strongly
Meeting Food, but that is inferred from the categorizer's own past output, not
verified. **Asking treasury who holds these is the cheapest available win.**

### 4.1 Per-term dues rates

`rule_dues` matched an exact amount against `DUES_AMOUNTS`, a module constant
pinned to Fall 2024. Because the match is exact and the handbook shows the rate
changing nearly every term, the first term after a change stopped categorizing
dues **entirely and silently** — no error, the payments just fell to the queue.

Rates are now data on the term:

```
terms.dues_rates           "35.00,52.50"  -- comma-separated, NULL = fall back
terms.dues_rates_verified  0/1            -- 0 until a human confirms
```

* `predicates.DuesSchedule` — date → rates in force; pure Python, no pandas.
* `dues.schedule_from_terms(df_terms)` — builds one from the terms table.
* `categorize_records(..., dues=schedule)` / `categorize_frame(..., dues=)`.
* Passing nothing falls back to `DUES_AMOUNTS`, so **every existing caller is
  unchanged.** Verified byte-for-byte on all 892 real rows:
  `scripts/verify_dues_schedule.py` reports 0 classifications changed.

Two guards were added because the mechanism alone cannot prevent the failure —
somebody still has to notice a rate changed:

* `quality.check_unverified_dues_rates` — terms whose rates nobody confirmed.
  Fires on all 9 terms today, by design: they hold a *copy of an assumption*.
* `quality.check_possible_dues_rate_change` — three or more people sending the
  same amount the schedule rejects. That is what a rate change looks like from
  inside the data.

**The second check found something on the real data.** 59 uncategorized incoming
transfers worth **$1,870** sit at documented handbook rates, concentrated in
Spring 2025 (27 × \$30, 9 × \$50 — exactly the handbook's "\$30/\$50" pair).
They look like dues at an old rate, but that is treasury's call, not an
inference to act on. See `docs/treasury-questions.md` §1.

Venmo's fee is handled by `DuesSchedule(accept_venmo_net=True)` and is **off**.
The three observed net amounts (24.43 / 29.34 / 39.14) do not fit one
rate-plus-fee formula to the cent, so a bounded 3% window below gross is used
rather than a fabricated exact formula.

---

## 5. The learning loop (M19 / M20)

### Why not a neural network

~447 transactions/year across **7 committees in active use**; ~20% need review
≈ **100 new human labels/year.**

| Method | Labels needed | Reachable? |
| --- | --- | --- |
| Logistic regression over named signals | 200–500 | **yes — 590 imported** |
| Gradient-boosted trees | ~1,000+ | years away |
| Neural net, tabular, 7 classes | 10,000+ | never at this volume |

Volume aside: five low-dimensional tabular features favour regression anyway,
and **explainability is a hard requirement** — a treasurer defends a
categorisation to an auditor. Fitted weights can be printed and argued with.

### The pieces

* **`labeled_examples` table** — one store for ground truth *and* the decision
  log. A ledger import is a label with no prediction; a review-queue decision is
  a label that also records `model_committee` / `model_confidence`, so agreement
  rate and overrides fall out of one query. Keyed to be idempotent.
* **`parsers/ledger.py`** — recovers labels from Drive workbooks as the
  connector renders them (markdown tables). Retired committees (`Mixer`) are
  **reported, not silently remapped**.
* **`learning.fit_weights`** — measures how often each named signal agrees with
  the human. Smoothed; signals seen <3 times are reported but not trusted.
* **`learning.evaluate`** — runs the **whole pipeline**, not just scoring.
* **`context.build_prompt_for`** — generates the model briefing from labels:
  committee vocabulary, card roster, settled merchants, contested merchants, and
  the past decisions most similar to the row in question. ~1,000 tokens, capped.

### Three traps designed around

**Era scoping.** The 2022-23 ledger's cards (0319, 6570, 8648, 2949) belong to
graduated officers. Card signals are excluded from cross-era fitting by default
(`include_card_signals=False`); every label carries an `era`.

**Taxonomy drift.** Committee 18 (Formal) post-dates the 2023 labels, so formal
ticket income was booked to Membership then and to Formal now. Counting that as
45 errors would argue for breaking a correct rule. `KNOWN_TAXONOMY_DRIFT` lists
the pairs; the evaluator excludes and reports them separately.

**Selection bias — surfaced, not solved.** If humans only ever label flagged
rows, the set drifts toward hard cases and fitted weights degrade on the easy
majority. `Evaluation.selection_bias_warning()` fires when every queue-sourced
label came from a flagged row. **The fix nobody has done:** spot-check a random
sample of auto-applied rows, stored with `source='spot-check'`.

### Where it stands

590 labels, all `source='ledger'`, all era 2022-2023. **No queue-sourced labels
yet** — the decision log is wired but only fills as the queue is worked. The
fitted weights are informative (§6) but were **deliberately not swapped in**:
the historical era differs in dues rates, cardholders and chart of accounts.

---

## 6. What the evaluation harness found

Run `python scripts/evaluate_categorizer.py --fit` to reproduce any of these.

**The harness caught a bug in itself first.** It originally measured `score()`
alone and reported "91% of labels produce no signal" — an artefact of dues and
transfers being resolved by exact rules upstream. Measuring one tier of a
four-tier pipeline says nothing about the pipeline.

| Finding | Status |
| --- | --- |
| **`DUES_AMOUNTS` was pinned to Fall 2024 rates** `(35.00, 52.50)`. Rates are now per-term data (`terms.dues_rates`) via `DuesSchedule` — see §4.1. **The rates themselves are still unknown and still unverified**; every term carries a seeded copy of the old constant, marked unconfirmed. | **Mechanism fixed; rates still needed from treasury** |
| **Venmo dues arrive net of fees** — historical amounts are `24.43`, `29.34`, `39.14` ($25/$30/$40 minus Venmo's cut). `DuesSchedule(accept_venmo_net=True)` handles it and is **off by default**, because booking net-of-fee income is a treasurer's call. | **Mechanism built, switched off — awaiting decision** |
| **Outgoing reimbursements may be in the wrong bucket.** `rule_refund` books every negative Venmo/Zelle to committee 17 (Refunded), which is `kind="ledger"` and excluded from budget-vs-actual. The 2023 treasurer instead booked a reimbursement to *the committee whose expense it repaid*. 18 labeled rows disagree. If the old way was right, **committee budgets currently understate reimbursed spend.** | **Open — accounting decision** |
| **`bar-merchant → Membership` is over-weighted** at 2.5; measured precision on historical labels is 38%. Largest confusion is `Membership → Consulting` (8 rows) — bar/restaurant spend on consulting projects. `salty dog saloon` is settled as **Consulting** across 10 human decisions. Learned weight: 0.00. | Open — recalibrate once current-era labels exist |
| **`mobile deposit → Sponsorship / Donation`** (9 human decisions). Answers the long-open question about the $15,400 of unexplained deposits. | Evidence available, not applied |

---

## 7. Decisions needed from a human

These change financial meaning. **Do not decide them by inference.**

**7.1 — Disputed purpose→committee mappings (finding F7).**
`PURPOSE_TO_COMMITTEE` books "Professional Development" to committee 7
(Consulting) and "Food & Drink" to 5 (Membership), while the reference table
shown to treasurers says 7 is Consulting and 10 is Professional Development.
Original behaviour preserved so historical figures do not shift. On the real
data it is **89 transactions**, not the 43 stated in an earlier revision:
Professional Development 7 rows / $1,391.98, Food & Drink 82 rows / $7,808.28.
Food & Drink is by far the larger exposure. Decide, then edit
`config/categories.py` and remove the entry from `DISPUTED_PURPOSE_MAPPINGS`.

**7.2 — Deduplication semantics.** The rule is "do we already have this many of
it", not "does this row exist". Two genuine identical same-day purchases both
import; re-uploading a statement imports nothing. Trade-off: a new statement
containing one further identical same-day purchase is flagged as duplicate. It
errs toward never double-importing. Confirm that bias is right.

**7.3 — The three §6 "Open" items** — dues rates per term, Venmo fee handling,
and whether reimbursements should hit the reimbursed committee.

**7.4 — Bulk merchant mappings.** `scripts/propose_merchants.py` groups the
unresolved rows into ~100 decisions; confirming all would resolve ~234 rows
permanently. Only a handful are confident enough to suggest a committee. Largest
open questions: `publix gainesville` (31 rows, −$836, food but off Tue/Wed),
`mobile deposit` (9 rows, +$15,400 — but see §6), Zelle memos saying "headshot"
(6 rows, Prof. Dev. vs Membership both defensible).

**7.5 — Memo-keyword rules.** 120 residual rows are incoming member Zelle
payments. `merchant_key` refuses to learn from them by design (a rule keyed on a
member's name would mis-categorise everything they pay), but the memo often says
what the payment was for: 13 rows/$580 say "hoodie"/"tshirt" (Merch 13), 5
rows/$447 say "road trip" (14). One-line additions to `predicates.py`, but they
book real income.

---

## 8. Known gaps and defects

**8.1 — Merchant memory is empty (0 rules).** The single biggest available
improvement, and it is a data problem, not a code one. See §7.4.

**8.2 — Views have execution coverage; writes do not.**
`tests/test_views.py` runs all 15 views headlessly (`streamlit.testing.v1.AppTest`)
in three scenarios each — populated DB, **empty DB**, and **as MEMBER** — plus
interaction tests for Dashboard, Transactions, Treasury, Planner and the Review
Queue filters. What is *not* covered: nothing drives an upload, budget save,
term lock or reimbursement decision through the widgets.

**8.3 — The Venmo parser has never seen a real file.** The Wells Fargo parser
passed 103 tests and then failed on the first real file — the real export had
`DESCRIPTION` and `AMOUNT` in the opposite positions from the fixtures, and the
original app would have imported all 892 rows as $0.00. The Venmo parser is in
exactly that pre-contact state. This compounds with the Venmo-fee dues issue (§6).

**8.4 — Supabase backend: 7 of 25 interface methods unimplemented.**
Missing: `fetch_reimbursements`, `create_reimbursement`, `decide_reimbursement`,
`link_reimbursement_to_transaction`, `fetch_receipts`, `store_receipt`,
`set_term_lock`, plus the new `fetch_labeled_examples` / `insert_labeled_examples`.
`schema_postgres.sql` also lacks DDL for reimbursements, receipts, term locking
and `labeled_examples`. Nothing in the production path has ever run.

**8.5 — Smaller items.** `sqlite_backend.py` and `Treasury.py` are both large and
doing several jobs. `Dashboard.py:169` reaches into `charts._height` (private).
Alerts compute but do not deliver (no email/Slack). No `natural_key` backfill
script — production dedupe cannot work without one. Receipt OCR absent by design.

---

## 9. Traps — read before editing

**Trap 1: Streamlit reads `$...$` as LaTeX.** Any string with two unescaped `$`
renders the text between them as an equation. This was fixed by hand four times
before tests existed, and once more since — `Runbook.py` had two literal `$0.00`
in one `st.markdown` and silently ate a whole troubleshooting section. Use
`shell.say(...)` / `shell.notify(...)` for text containing money. Both a static
check (`test_ui_conventions.py`) and a **runtime** check (`test_views.py`) now
enforce it; the static one alone missed the literal case. Note `\$` in a
non-raw Python string is a `SyntaxWarning` on 3.13 — use a raw string.

**Trap 2: NaN is truthy and `NaN != NaN`.** `value or ""` does not fall back on
a pandas NaN — it returns the NaN, and `.strip()` then crashes. And
`before == after` reports an unchanged null as changed. Use `pd.isna()`.

**Trap 3: Streamlit does not hot-reload imported package modules.** Editing
anything under `ais_fmd/` requires a server restart. Editing only the page file
does not. This will fool you when verifying in the browser.

**Trap 4: views are `exec()`'d by `st.Page`,** so relative imports fail. Views
must use absolute imports (`from ais_fmd import auth`).

**Trap 5: `INSERT OR IGNORE` swallows foreign-key violations,** not just
duplicate keys — it silently dropped rows while reporting success. Use targeted
`ON CONFLICT (natural_key) DO NOTHING`.

**Trap 6: fixtures that encode your assumptions prove nothing.** The Wells Fargo
parser had full green tests and still failed on the first real file. Test against
real artifacts.

**Trap 7: Streamlit's caches are process-global.** Tests that swap the database
must clear `st.cache_data` **and** `st.cache_resource`, or the cached backend
keeps serving the previous test's file. `tests/test_views.py::use_db` does this
on both sides of every test.

**Trap 8: `conftest.py` has an autouse fixture** pointing `AIS_FMD_SANDBOX_DB` at
a fresh tmp path. To override it, depend on `isolated_sandbox` **explicitly** so
ordering is guaranteed.

**Trap 9: a metric computed after a filter describes the filter, not the
ledger.** The Review Queue's period filter briefly reported 87.9% coverage where
the truth was 63.5%, by counting period-excluded rows as categorized. No
exception — just a wrong number, which render-only tests cannot catch.

---

## 10. Prioritized next steps

**P1 — Work the flagged review queue for a month.** Not code. This is what turns
the learning loop from *built* to *running*: it produces current-era labels,
fills merchant memory, and makes weight-fitting meaningful. Filter the queue to
"Flagged: scored but uncertain" — those rows carry a proposal and its reasoning
and take seconds each.

**P2 — Send `docs/treasury-questions.md`.** One message, five questions,
already drafted with the numbers attached. It unblocks the dues rates (§4.1),
the seven undocumented cards, the reimbursement accounting decision (which is
roughly half of all measured error), the Venmo fee question, and two mapping
calls. Nothing else on this list moves until these are answered.

**P3 — Enter the dues rates once treasury answers**, in Treasury → Terms. This
is data entry, not code: the mechanism landed (§4.1), the rates did not. Until
then Data Quality correctly reports all 9 terms as unconfirmed.

**P4 — Spot-check auto-applied rows** into `labeled_examples` with
`source='spot-check'` so selection bias becomes measurable.

**P5 — Re-run categorization over stored transactions.** The stored
`budget_category` predates the scoring engine; a re-run resolves more. Needs a
bulk-apply path with the same propose/confirm discipline as
`propose_merchants.py`.

**P6 — Wire the model pass** for the genuine residual, using
`context.build_prompt_for`. Only worth it once a real API key exists; note the
residual splits into ~54 past-year rows with *no signal at all* (mostly
`MOBILE DEPOSIT : REF NUMBER :...`, which a model cannot help with) and a much
smaller set of genuinely contested rows where reasoning would.

**P7 — Test the Venmo parser against a real export** (§8.3), which also unblocks
the Venmo dues-fee question.

**P8 — Finish the production path** (§8.4). Only start with a throwaway Supabase
project. Do not set `AIS_FMD_ENV=production` before the missing methods, the DDL,
and a `natural_key` backfill all exist.

**P9 — Headless tests for write paths** (§8.2).

**P10 — Split the large files**; alerts delivery; `charts._height` privacy leak.

---

## 11. Fast orientation for a new agent

Read in this order:

1. `ais_fmd/settings.py` — the safety model, ~110 lines
2. `ais_fmd/config/categories.py` — the domain vocabulary
3. `ais_fmd/domain/categorize/pipeline.py` — the tier order and why it is that order
4. `ais_fmd/domain/categorize/scoring.py` — how confidence is actually computed
5. `ais_fmd/domain/categorize/predicates.py` — the exact rules
6. `ais_fmd/domain/parsers/wells_fargo.py` — the module with the most scar
   tissue; its docstring explains three real failures
7. `tests/test_real_layouts.py` — what a real statement actually looks like
8. `tests/test_views.py` — how to run a page headlessly; extend `use_db` /
   `run_view` to cover a new page

Then run, before changing anything:

```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python scripts/evaluate_categorizer.py --fit
.venv/Scripts/python scripts/profile_hotpath.py
```

### Working style that has paid off here

* **Verify against the real 892 rows, not fixtures.** Every significant bug in
  this project was found that way, and several "obvious" fixes were wrong.
* **Cross-check a refactor against the path it replaces** before trusting it —
  e.g. the rewritten `historical_budget_vs_actual` was diffed against the old
  loop across all 16 committee filters plus empty-input edges.
* **Test on a copy of the DB before writing to it.** `propose_merchants.py` and
  the transaction updates were both proven on a copy first.
* **Separate "the tool proposes" from "the human decides."** Anything that
  changes which committee real money is booked against is a treasurer's call.
