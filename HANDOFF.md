# Handoff — UF AIS Financial Management System (sandbox rebuild)

**For:** whoever picks this up next, human or agent.
**Read this first.** Written to be self-contained — you should not need any prior
conversation.

**Last verified 2026-08-26:** 562 tests passing (2 skipped); app runs from
`C:\Users\durpy\ais-fmd-app` (see the correction to §1 below — the paths and
two-repo model that section originally described do not match this machine).

**Read this before trusting anything in §2, §5, §6, or most of §7:** the
sandbox database **no longer contains** the 892-row historical dataset (2024-07
.. 2026-07) or the 590 ground-truth labels those sections describe. Checked by
direct query 2026-08-26 — the live sandbox now holds:

```
transactions        156   real Wells Fargo, 2026-07-01 .. 2026-08-21 (the Fall 2026 dues drive)
terms                  3   FA26, SP26, SU26
committeebudgets      28
merchants               1
labeled_examples        6
members               149   real roster, term FA26 -- see §12.1
profiles                0
transaction_audit    201
```

**What happened:** `scripts/load_real_statement.py --into-sandbox` calls
`backend.reset()` before loading — documented behavior ("replacing sandbox
data"), used correctly this session to load a real Fall 2026 statement
(`C:\Users\durpy\Downloads\Checking.csv`, not committed — see §12.1). Nobody
in this session backed up the 892-row database first, because nobody realized
until writing this note that no other copy of that dataset was known to exist.
**I do not know where the source file for the 892-row / 590-label dataset
is, or whether it still exists anywhere.** It was loaded by a session before
this conversation's history. `C:\Users\durpy\Downloads\` currently has several
un-triaged real files that might be relevant and might not —
`VenmoStatement_January_2026.csv`, `VenmoStatement_February_2026.csv`,
`Checking2.csv`, `Checking2 (1).csv`, `Jan1-Mar23 Transactions.csv`,
`2025-04-15_transaction_download.csv`, `2026-03-31_transaction_download.csv`,
`categorized_transactions (2).csv`, `categorized_transactions (4).csv` — none
of these have been opened or identified. **If the 892-row baseline matters
going forward** (it's what §5/§6's whole learning-loop evaluation is built on),
sorting out whether one of these is it, or whether it's simply gone, is
probably the single highest-value thing to do before trusting any evaluation
number in this document.

§2, §5, §6, and most of §7 below are left as they were: they document the
**pipeline's logic and reasoning**, which is unaffected and still accurate. Only
the specific numbers (892 rows, 590 labels, 67.7% coverage, 41.9% precision,
etc.) describe data that is not currently loaded. Each is written to be
reproducible — the scripts they name will just report different figures until
that dataset is back, if it ever is.

**§12 is new** (roster reconciliation + the VP portal's Google sign-in) and is
the most likely thing an agent picking this up needs to read first if the user
mentions either. Three dependency floors were also raised this session and are
load-bearing, not cosmetic — `streamlit>=1.49`, `pandas>=2.3`, `openpyxl>=3.1.5`
in `requirements.txt` — installing older pins reintroduces real bugs (§12.3).

**Treasury answered the open questions on 2026-08-24** (§4.1–4.3, §7,
`docs/treasury-questions.md`). Three of the changes move money — reimbursement
routing, per-term dues rates, and Venmo net-of-fee matching — and they **have
been verified against the 156-row Fall 2026 statement** (§12.1's numbers), but
**not against the 892-row historical set**, for the reason above: that dataset
isn't loaded here to verify against.

This document describes **what is true now**, not what happened when. Where a
decision looks odd, the reason it is that way is given, because in almost every
case it was odd for a reason discovered the hard way.

---

## 1. What this is, and what it is not

**Corrected 2026-08-26 — read this before the rest of the section.** Everything
below the line describes a two-repo model (`...ais-fmd-sandbox` rebuilding
`...ais-fmd-app`, under a `durpy_7vdh2wz` user profile) that **does not match
this machine**. This whole session worked in a single repo at
`C:\Users\durpy\ais-fmd-app`, branch `sandbox/mvp-rebuild`, user `durpy`. I
don't know when or why the split closed — whether the rebuild was merged into
the original repo at some point before this session's history, whether this is
a different machine entirely, or something else — and I'm not going to guess
at history I can't verify. What's below is kept for the *reasoning* (still
apparently accurate: this is still a sandbox, still local SQLite, still no
network), with the concrete paths and commands corrected to what this session
actually used.

**Running it, in this repo:**

```bash
cd C:\Users\durpy\ais-fmd-app
.venv/Scripts/python -m streamlit run app.py     # http://localhost:8501
.venv/Scripts/python -m pytest                    # 562 passing, 2 skipped, ~90s (2026-08-26)
```

The sandbox database is `sandbox_data/ais_fmd_sandbox.db` inside this same
repo (gitignored), not a separate repository. See the top of this document for
what's actually in it right now — it is **not** the 892-row dataset the
sections below reference.

**Original text, for the reasoning even though the paths are stale:**

`C:\Users\durpy_7vdh2wz\ais-fmd-sandbox` is a **rebuild** of the treasury app at
`C:\Users\durpy_7vdh2wz\ais-fmd-app`. It is a **sandbox**: local SQLite, no
network.

**The original repo has never been modified.** Still at commit `6cb308c` with
the same two modified files it had at the start (`requirements.txt`,
`views/treasury_auto_categorize.py`). The sandbox is a separate git repo with
**no remote**, so `git push` has nowhere to go. The one file touched outside the
sandbox is `ais-fmd-app/.claude/launch.json` — agent tooling config, untracked,
no app code.

**Nothing has been deployed. The production path (Supabase) has never executed
against a live database** — the code path has, as of 2026-08-26, been fully
implemented and reviewed (§12.2), just never run against real Postgres.

| Script | Purpose |
| --- | --- |
| `scripts/load_real_statement.py <csv> --into-sandbox` | Load a real statement, replacing sandbox data |
| `scripts/test_real_statement.py <csv>` | Parse-only diagnostic, no writes |
| `scripts/propose_merchants.py` | Bulk merchant mapping: propose → review CSV → `--apply` |
| `scripts/import_ground_truth.py <rendered.txt> --era <era>` | Import human labels from Drive ledgers |
| `scripts/evaluate_categorizer.py [--fit]` | Accuracy per threshold/committee; `--fit` prints learned weights |
| `scripts/profile_hotpath.py` | Times the work every page rerun does |
| `scripts/verify_later_modules.py` | E2E check of locking/reimbursements/planner on a DB copy |
| `scripts/verify_dues_schedule.py` | Proves per-term dues rates are behaviour-preserving on real data |
| `scripts/spot_check.py [--apply CSV\|--report]` | Measures accuracy on auto-applied rows: sample → review CSV → `--apply` |
| `scripts/apply_treasury_rates.py [--apply]` | Writes treasury's confirmed dues rates onto the terms rows; dry-run reports how many rows change classification |

### The safety model — do not weaken this

Three independent layers, designed so no single one has to hold:

1. **`openai` and `supabase` are not installed** in the sandbox venv, in
   principle. Verify:
   `.venv/Scripts/python -c "import importlib.util as u; print(u.find_spec('openai'), u.find_spec('supabase'))"` → both `None`.
   **On this machine, as of 2026-08-26, this layer is not intact** — both
   packages ARE installed (`supabase==2.15.0`, `openai==3.3.1`), needed this
   session to read the live Postgres schema while writing `supabase_backend.py`
   (§12.2) and to check dependency versions. `test_openai_and_supabase_are_absent_from_the_sandbox_venv`
   knows this is possible and **skips rather than fails** when it finds them —
   by design, so a developer installing them for production work doesn't look
   like a broken safety net. Layers 2 and 3 below are what were actually relied
   on this whole session; they held.
2. **Fails closed.** `AIS_FMD_ENV` must equal `production` (case-insensitive,
   whitespace-trimmed). Unset, empty, `prod`, `1`, `true` all resolve to sandbox.
   A stray `OPENAI_API_KEY` spends nothing — `settings.llm_enabled()` is `False`
   in sandbox mode. One documented override exists,
   `AIS_FMD_ALLOW_LLM_IN_SANDBOX=1`, which still cannot spend anything because
   layer 1, when intact, removes the package. That independence is the point
   — and exactly why layer 1 being absent on this machine did not, on its own,
   put anything at risk this session: nothing here ever set `AIS_FMD_ENV=production`.
3. **Separate directory, own git repo, no remote.** True on whatever machine
   this was originally written on; **not verified for this one** — see the
   correction at the top of §1. What *is* true here: the sandbox database
   (`sandbox_data/`) is gitignored, and nothing this session did set
   `AIS_FMD_ENV=production` or wrote to the live Supabase project (confirmed —
   every write this session went through `SqliteBackend`).

`tests/test_sandbox_safety.py` asserts what it can. **If `test_defaults_to_sandbox_when_unset`,
`test_fails_closed_on_anything_but_exact_token`, or `test_llm_disabled_in_sandbox_even_with_a_key`
fail, stop — layer 2 is broken and that's the one this whole safety model
actually depends on.**

---

## 2. Verified current state

**This snapshot is not what's in the sandbox right now** — see the notice at
the very top of this document, dated 2026-08-26. Kept as-is below because §5,
§6, and part of §7 reason about this specific dataset; treat it as "what was
analyzed when those sections were written," not as today's `SELECT COUNT(*)`.

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

**These predate the 2026-08-24 treasury rulings and have not been re-measured.**
Expect them to move in both directions: the reimbursement change (§4.2) removes
a whole class of automatic assignments, while the memo rules (§4.3) and the
corrected dues rates (§4.1) add some back. Whichever way the net lands, coverage
is now a more honest number than it was — the rows it dropped were being
answered without being asked. Re-run `scripts/evaluate_categorizer.py` on the
real data to replace these.

The stored `budget_category` predates the scoring engine. Re-running
categorization would resolve more; nothing has done so in bulk.

---

## 3. Architecture

```
ais_fmd/
  settings.py            env detection + sandbox guard (fails closed)
  auth.py                roles: MEMBER < OFFICER < TREASURER < ADMIN
                          + Google sign-in for the VP portal (§12.2, 2026-08-26)
  config/categories.py   committees, purposes, accounts — ONE source of truth
  data/
    backend.py           interface both backends implement -- 33 methods, all
                          implemented by both backends as of 2026-08-26 (§12.2)
    sqlite_backend.py    sandbox
    supabase_backend.py  production — code complete, NEVER RUN against real
                          Postgres (§12.2)
    repositories.py      the single cache layer
    schema_postgres.sql  production DDL + atomic import RPC (older; see
                          data/migrations/ for what's actually current)
    migrations/          001 (additive, never run against live Supabase),
                          002 (deferred features + profiles, also never run)
    seed.py              demo data generator
  domain/                business logic — NOTHING here imports streamlit
    money, terms, budgets, dedupe, reconcile, quality, dues,
    alerts, report, scenarios, reimbursements, receipts, assistant,
    roster.py             membership matching + dues reconciliation (§12.1, new)
    categorize/          predicates, merchants, scoring, learning, context, bulk, llm, pipeline
    parsers/             venmo, wells_fargo, ledger
  ui/                    theme (Plotly template), charts, shell
  views/                 17 Streamlit pages — layout and wiring only
                          (Roster.py, OfficerAccess.py new 2026-08-26)
tests/                   562 passing, 2 skipped (2026-08-26)
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
1. EXACT RULES      memo keyword / consulting(card 8408) / dues / reimbursement
2. MERCHANT MEMORY  a human's explicit decision about this exact merchant
3. SCORING          weighted evidence; confidence gate at 0.75
4. THE MODEL        only the residual (never runs in sandbox)
   ↓ below threshold or no signal
   REVIEW QUEUE     with the proposal and its reasoning attached
```

Within tier 1 the order is
`memo → card 8408 → dues → dues-memo → reimbursement`. Memo before dues because
of the \$50/\$65 collision (§4.3); dues-memo *after* dues so the schedule keeps
first refusal (§4.3); reimbursement last because it resolves nothing — it only
labels an outgoing transfer that no other rule could place, so it must not
pre-empt one that could.

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

Seven other cards appear in **the 892-row historical data** (see the top-of-doc
notice — not currently loaded) with **no documented owner** (0153, 7757, 9309,
3444, 7193, 1113, 5535). History suggests 0153 is strongly Meeting Food, but
that is inferred from the categorizer's own past output, not verified.

**The real Fall 2026 statement (§12.1) uses two different, ALSO undocumented
cards: 3466 and 3526.** Neither matches the four confirmed above nor any of the
seven historical ones — zero overlap with any card number this document has
ever recorded, consistent with treasury's own comment that cards move with new
VPs. `quality.check_card_roster_era` fires on exactly this and explains why in
its detail text; see §7 for the current state of asking treasury who holds them.
**Asking treasury who holds these (all of them — old and new) is the cheapest
available win**, unchanged from before, just with a longer list now.

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

Venmo's fee is handled by `DuesSchedule(accept_venmo_net=True)` and is now **on**
— treasury deprecated Venmo going forward and asked for historical rows to be
counted net of fees, which bounds the risk: no new Venmo rows will arrive, so
the window can only ever apply to history that already exists. The three
observed net amounts (24.43 / 29.34 / 39.14) do not fit one rate-plus-fee
formula to the cent, so a bounded 3% window below gross is used rather than a
fabricated exact formula. The switch lives at `dues.ACCEPT_VENMO_NET_OF_FEES`,
on the builder every caller uses; `DuesSchedule` itself stays strict so the
pre-decision behaviour can still be reconstructed for diffing.

### 4.2 Reimbursements belong to the committee they repaid

Treasury: *"Reimbursements should go to the committee they represent. If
someone spends money on a personal card and gets reimbursed then it was a
committee expenditure."*

`rule_refund` booked every negative Venmo/Zelle to committee 17 (Refunded) at
full confidence. 17 is `kind="ledger"`, so those rows sat outside
budget-vs-actual entirely — every dollar a member fronted and was repaid was
invisible to the budget of the committee that actually spent it.

It is now `rule_reimbursement`, and it has **no default committee**:

* the memo is tried first (`rule_memo_committee`, §4.3);
* failing that the row falls through to merchant memory and scoring like any
  other row;
* if nothing resolves it, it goes to the review queue carrying a sentence
  explaining what it is and what is missing, rather than an empty cell.

**This costs automatic coverage on purpose.** The coverage it removes was
manufactured by answering a question nobody had asked. Expect the headline
coverage number to fall and accuracy against the ledger labels to rise — the 18
labeled rows that disagreed with the old behaviour agree with this one.

Committee 17 keeps its meaning for money coming *back*: a merchant return is
still a refund (`bulk.propose`), which is why the rule was renamed and not
deleted.

### 4.3 Memo keywords

Treasury, asked whether a payment memo is enough to book a transfer on:
*"Absolutely look at the memos they will clarify it well."*

`MEMO_COMMITTEE_KEYWORDS` maps memo text to a committee: merch/crewneck/hoodie
→ 13, road trip/St. Augustine → 14, formal/semi-formal → 18. It applies in
**both directions** — incoming it books a member's hoodie payment to Merch,
outgoing it books a formal reimbursement to Formal.

This is safe where a rule keyed on a payer's *name* is not: `merchant_key`
refuses to learn from an incoming transfer because a rule keyed on one member
would mis-categorise everything they ever pay, whereas a memo travels with a
single payment.

**`rule_memo_committee` runs before `rule_dues`, and that order is
load-bearing.** From Fall 2026 dues are \$50/\$65 and treasury warned that formal
payments land near those amounts — *"there are also going to be some formal dues
that might be around those amounts but the actual dues are a very specific
amount"*. The collision is expected rather than hypothetical, and the memo is
the only evidence that can break it.

Two keywords are deliberately **absent**:

* **"headshot"** (6 rows) — Professional Development vs Membership was left open.
* **bare "ticket"** — appears on formal, road trip and event payments alike.

**Dues memos are handled separately, by `rule_dues_memo`, and the placement is
the design.** Treasury asked for a dues keyword *"in addition"* to the amount
rule. In addition means **after**: everything in `MEMO_COMMITTEE_KEYWORDS` runs
ahead of `rule_dues`, so putting "dues" there would shadow the per-term schedule
and strip the term attribution off every dues row carrying the word.
`rule_dues_memo` runs after instead — the schedule gets first refusal, and the
memo only speaks for amounts the schedule turned down. The reason string always
names the amount, because a dues row at a rate no term charged is a fact worth
seeing.

The original objection to this keyword was that it would mask a rate change.
It does not: `check_possible_dues_rate_change` reads **raw amounts against the
schedule**, not `budget_category`, so a booked row is still visible to it. That
check now calls these rows out by name — a payer who writes "dues" at an amount
no term charged is the strongest evidence of a rate change anywhere in the data.

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

### 5.1 Coverage is measured. Accuracy is not.

Read the headline numbers in §2 carefully: 67.7% and 79.6% are **coverage** —
how many rows got an answer. Nothing in this project measures how many of those
answers were *right*, and the two are easy to conflate.

The only accuracy figure that exists is against the 2022-23 ledger labels, and
it is weak: **41.9% precision at the 0.75 gate**, with the harness reporting
that *no* threshold reaches 95% on that set. That is cross-era, so it is not a
verdict on today's accuracy — but the absence of a current-era number is the
point. The app books real money against committees at an unmeasured error rate.

It cannot be measured from the labels that exist, either. Every one came from a
historical ledger or from a row the categorizer *flagged as uncertain*. Both are
biased away from the confident majority, which is exactly the population in
question. `Evaluation.selection_bias_warning()` reports this; reporting a gap
does not close it.

`domain/categorize/spotcheck.py` + `scripts/spot_check.py` close it. Sample →
review CSV → `--apply`, the same propose/confirm discipline as
`propose_merchants.py`. The sample is:

* **stratified by tier and committee with a floor of one per stratum** — a
  uniform draw would spend most of its budget on `rule`/Dues and might never
  touch a rare committee, which is where a systematic misbooking survives
  longest because nobody looks there;
* **deterministic given `--seed`**, ranked by hashing each row's own identity,
  so a review survives a pause, a resume, and new statements landing;
* **largest-amount-first within each stratum**, then random for the rest — the
  big rows are where an error costs most, the random ones are the only part that
  supports an unbiased estimate.

`--report` gives the agreement rate split by tier. `coverage_note()` says out
loud when a sample is too small to be a measurement, because a 10-row check that
reads like a statistic is worse than no check.

**Nobody has run a round yet.** Until someone does, treat every accuracy claim
about this categorizer as unverified.

---

## 6. What the evaluation harness found

Run `python scripts/evaluate_categorizer.py --fit` to reproduce any of these.

**The harness caught a bug in itself first.** It originally measured `score()`
alone and reported "91% of labels produce no signal" — an artefact of dues and
transfers being resolved by exact rules upstream. Measuring one tier of a
four-tier pipeline says nothing about the pipeline.

| Finding | Status |
| --- | --- |
| **`DUES_AMOUNTS` was pinned to Fall 2024 rates** `(35.00, 52.50)`. Rates are now per-term data (`terms.dues_rates`) via `DuesSchedule` — see §4.1. Treasury supplied Fall 2026 (**\$50 / \$65**) and confirmed the previous term at \$35/\$52.50. Earlier terms are still unconfirmed. | **Resolved for the current term** — `dues.CONFIRMED_DUES_RATES`, applied by `scripts/apply_treasury_rates.py` |
| **Venmo dues arrive net of fees** — historical amounts are `24.43`, `29.34`, `39.14` ($25/$30/$40 minus Venmo's cut). Treasury deprecated Venmo going forward and asked for historical rows to be counted net of fees. | **Resolved — on.** `dues.ACCEPT_VENMO_NET_OF_FEES` |
| **Outgoing reimbursements were in the wrong bucket.** `rule_refund` booked every negative Venmo/Zelle to committee 17 (Refunded), `kind="ledger"` and excluded from budget-vs-actual. 18 labeled rows disagreed. | **Resolved — reimbursements now hit the committee they repaid.** See §4.2 |
| **`bar-merchant → Membership` is over-weighted** at 2.5; measured precision on historical labels is 38%. Largest confusion is `Membership → Consulting` (8 rows) — bar/restaurant spend on consulting projects. `salty dog saloon` is settled as **Consulting** across 10 human decisions. Learned weight: 0.00. | Open — recalibrate once current-era labels exist |
| **`mobile deposit → Sponsorship / Donation`** (9 human decisions). Answers the long-open question about the $15,400 of unexplained deposits. | Evidence available, not applied |

---

## 7. Decisions needed from a human

**Most of this section was answered on 2026-08-24** — see
`docs/treasury-questions.md` for what was decided and
`tests/test_treasury_decisions.py` for the assertions. Resolved: per-term dues
rates for the current term (§4.1), Venmo fee handling (§4.1), reimbursement
accounting (§4.2), and memo keywords (§4.3, which covers 7.5 below).

**Still open, in priority order:**

* **7.1** — the disputed purpose mappings. Treasury: *"I'm not sure, keep it as
  an open question."* 89 transactions, Food & Drink the larger exposure.
* **7.2** — deduplication semantics. Never asked.
* **The card roster, now nine undocumented cards, not seven.** Treasury said
  the numbers will follow, and that cards move with new VPs — confirmed since:
  the real Fall 2026 statement uses cards 3466/3526, which match nothing ever
  recorded in this document (§4). Nothing was guessed — see `quality.check_card_roster_era`,
  which reports the sharper version of this risk: the four *documented* cards
  were confirmed for the 2024-2026 cohort and Fall 2026 began a new one.
* **"Headshot" memos** — Professional Development or Membership, 6 rows.
* **7.4** — bulk merchant mappings. Unchanged and still the largest single win.

The originals are kept below, since an answer only means something next to the
question it answered.

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

**7.5 — Memo-keyword rules. RESOLVED 2026-08-24 — see §4.3.** 120 residual rows
are incoming member Zelle payments. `merchant_key` refuses to learn from them by
design (a rule keyed on a member's name would mis-categorise everything they
pay), but the memo often says what the payment was for: 13 rows/$580 say
"hoodie"/"tshirt" (Merch 13), 5 rows/$447 say "road trip" (14). Treasury:
*"Absolutely look at the memos they will clarify it well."* Implemented as
`MEMO_COMMITTEE_KEYWORDS`. Headshots (6 rows) are still excluded — that call was
not made.

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

**8.4 — RESOLVED 2026-08-26, unverified.** `SupabaseBackend` used to be missing
7 of `Backend`'s interface methods (`fetch_reimbursements`,
`create_reimbursement`, `decide_reimbursement`,
`link_reimbursement_to_transaction`, `fetch_receipts`, `store_receipt`,
`set_term_lock`) plus `fetch_labeled_examples`/`insert_labeled_examples`. All
are now implemented, alongside 7 more written the same session for the roster
and VP-portal features (§12). Full detail, including a real bug caught and
fixed before it ever ran, is in §12.2's "Also outstanding" list — not repeated
here to avoid the two sections disagreeing the way they briefly did today.
**The DDL for reimbursements/receipts/term-locking (`migrations/002`) and for
`members`/`profiles` (`migrations/001` and `002`) is written; none of it has
been run against the live database.** That, not the Python code, is what
"nothing in the production path has ever run" now actually refers to.

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

**P2 — ~~Send `docs/treasury-questions.md`~~. Answered 2026-08-24.** Four of
five resolved; the code changes have landed and are asserted in
`tests/test_treasury_decisions.py`. What remains from it: the disputed purpose
mappings (7.1), the seven card numbers, and the headshot call.

**P3 — Run `scripts/apply_treasury_rates.py` against the real database.** The
rates are recorded in `dues.CONFIRMED_DUES_RATES`; this writes them onto the
terms rows. Dry-run first — it reports how many rows change classification in
each direction, and a rate correction can take dues *away* as well as add them.

**Check the Fall 2026 term row exists before anything else.** It began
2026-08-15 at rates the app did not know, so dues for the current term have been
falling silently into the review queue since then. If the term row is missing
entirely, its payments take the default rates and the script says so.

```bash
.venv/Scripts/python scripts/apply_treasury_rates.py
```

**P4 — Run a spot-check round.** The tooling now exists
(`scripts/spot_check.py`, §5.1); nobody has reviewed a sample with it yet.
40 rows is roughly an hour and produces the first accuracy number this project
has ever had for the rows it books without asking.

```bash
.venv/Scripts/python scripts/spot_check.py --size 40   # then review the CSV
```

**P5 — Re-run categorization over stored transactions.** The stored
`budget_category` predates the scoring engine; a re-run resolves more. Needs a
bulk-apply path with the same propose/confirm discipline as
`propose_merchants.py`.

**This is now more urgent than it was, and it moves money.** The reimbursement
ruling (§4.2) means every stored row currently sitting in committee 17 that was
an outgoing transfer is in the wrong bucket, and re-running is what moves them
into the committees whose budgets they belong to. Until it runs, budget-vs-actual
still understates reimbursed spend exactly as before — the rule changed, the
stored data did not.

**P6 — Wire the model pass** for the genuine residual, using
`context.build_prompt_for`. Only worth it once a real API key exists; note the
residual splits into ~54 past-year rows with *no signal at all* (mostly
`MOBILE DEPOSIT : REF NUMBER :...`, which a model cannot help with) and a much
smaller set of genuinely contested rows where reasoning would.

**P7 — Test the Venmo parser against a real export** (§8.3), which also unblocks
the Venmo dues-fee question.

**P8 — Finish the production path** (§8.4, §12.2). The missing methods are now
written (P15 covers testing them). What's left: run the DDL (P14), get a
`natural_key` backfill script written (nothing does this yet, needed for
production dedupe to work at all), and only then consider
`AIS_FMD_ENV=production` — on a throwaway Supabase project first, never the
org's real one, per §8.4.

**P9 — Headless tests for write paths** (§8.2).

**P10 — Split the large files**; alerts delivery; `charts._height` privacy leak.

**P11 — Chase the 7 disputed / 6 unmatched roster payments** (§12.1). Data
problem, not code — the Roster page has both lists ready to export.

**P12 — Register the Google OAuth app + add `[auth]` to production secrets**
(§12.2). Two external steps, ~15 minutes total, blocking everything else about
the VP portal. Nothing else on this list moves until this exists — same
shape as P2 was for the treasury questions.

**P13 — Once P12 exists: manually verify a real Google sign-in**, both a
`profiles` hit and a miss. This is the one part of §12.2 that could not be
tested from here.

**P14 — Run migration 001, then 002's `profiles` table, against live
Supabase** (§12.2, §8.4). Prerequisite for P12/P13 to mean anything in
production rather than just in this sandbox.

**P15 — Verify `SupabaseBackend` against a throwaway Supabase project**
(§8.4/§12.2, code done 2026-08-26). All 17 previously-missing methods are
written; none have run against a real Postgres instance. Do this before P14
touches the org's actual database — it's exactly the kind of bug a throwaway
project is for catching first.

---

## 11. Fast orientation for a new agent

Read in this order:

1. **This whole document**, top to bottom — the notice at the very top about
   the missing 892-row dataset changes how several later sections should be
   read.
2. `ais_fmd/settings.py` — the safety model, ~110 lines (§1's corrected
   "safety model" note explains what's actually intact on this machine)
3. `ais_fmd/config/categories.py` — the domain vocabulary
4. `ais_fmd/domain/categorize/pipeline.py` — the tier order and why it is that order
5. `ais_fmd/domain/categorize/scoring.py` — how confidence is actually computed
6. `ais_fmd/domain/categorize/predicates.py` — the exact rules
7. `ais_fmd/domain/parsers/wells_fargo.py` — the module with the most scar
   tissue; its docstring explains three real failures
8. `tests/test_real_layouts.py` — what a real statement actually looks like
9. `tests/test_views.py` — how to run a page headlessly; extend `use_db` /
   `run_view` to cover a new page

**If the user mentions the roster, dues reconciliation, VP access, or Google
sign-in:** skip ahead to §12 before doing anything else — it's self-contained
and covers all four.

Then run, before changing anything:

```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python scripts/profile_hotpath.py
```

`evaluate_categorizer.py --fit` is deliberately not in that list any more — it
reasons about the 892-row/590-label dataset, which per the top-of-document
notice is not currently loaded. Running it will not error, but the numbers it
prints describe whatever 156-row Fall 2026 statement is actually in the
sandbox, not what §5/§6 discuss.

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

---

## 12. Roster reconciliation and the VP portal (2026-08-26)

Two features landed in one session, on top of the 2026-08-24 treasury rulings
(§4.1–4.3). Both are code-complete and tested against real data; both have
concrete, external, human-only steps left before either is actually usable in
production. Read this section before starting either one.

### 12.1 Roster & dues reconciliation (module M21)

`ais_fmd/domain/roster.py` + `ais_fmd/views/Roster.py`. Answers "who still
owes dues" against an uploaded membership list, not just "how much came in."

**Why matching is its own module, not a string comparison.** Three properties
of the real Fall 2026 data make `payer == member` wrong often enough to be
useless: Wells Fargo emits names in both orders in the same file
("SCHUCK JOHN" and "CAMERYN WEITZ"); people pay dues on each other's behalf and
say so in the memo ("NICOLAS SANDERS ... JAYME RUDDS DUES"); and 64 of 149
members on the Fall 2026 form go by a preferred name ("Katherine" → "Kate")
that the bank data uses freely. `normalize_name` handles order and noise words;
matching searches the memo *before* the payer, because a memo naming someone
else is the payer's own instruction about who the money is for.

**Verified against real data, loaded into this sandbox's `members` table**
(term `FA26`, from `AIS Fall 2026 Membership Form (Responses).xlsx`, not
committed — has 149 real names, emails, UFIDs):

```
149 on the roster, 142 paid, 7 outstanding, 95% collection, $7,040 credited
6 payments ($315) name nobody on the roster at all
7 members certify on the form they paid; no matching payment exists for them
```

Cross-checked: the 7 unpaid and the 6 unmatched-payment payers are **confirmed
different people** (no name/token overlap in either direction) — two separate
problems, not one seen from two sides. See the conversation this was built in
for the full breakdown; not worth re-deriving, the numbers won't move until
someone acts on them.

**`Reconciliation.suggestions()`** finds near-misses a strict match refuses —
"ZACKARY FLORENDO" against a roster reading "Zack Florendo" — and shows them to
a treasurer to confirm, never auto-credits. Confirming (`add_member_alias`,
wired to a button in the Roster page's "Likely matches" tab) teaches the
roster that spelling permanently, so the next statement matches it directly.
Two real ones were confirmed this session: Zack Florendo, Jayme Rudd.

**Outstanding:**

* **The 7 disputed and 6 unmatched people need a human, not code.** Chase list
  is on the Roster page ("Says they paid" / "Unmatched payments" tabs,
  downloadable as CSV).
* **`members`'s Postgres DDL was out of date; fixed this session.**
  `migrations/001_extend_live_schema.sql` already had the table (written
  before the roster module existed) but was missing `alt_keys`,
  `preferred_name`, and `claims_paid` — added now, since migration 001 has
  never been run against live Supabase, so there was no live schema to
  reconcile against. Verify this stays true before trusting the file: if 001
  has since been applied, these three columns need `ALTER TABLE`, not a
  `CREATE TABLE IF NOT EXISTS` that will silently no-op against an existing
  table missing them.
* **No bulk CSV import for the VP↔committee mapping (§12.2)** — manual add/edit
  only. Fine for ~15 people, would want `roster.parse_roster`-style bulk import
  if the org is larger.

### 12.2 VP portal: Google sign-in (module M22)

The actual VP-facing "my committee's budget" page already existed
(`ais_fmd/views/Officer.py`, "My Committee") before this session — budget vs.
actual, spending detail, reimbursements, all scoped to one committee. **That
was never the gap.** The gap was access control: production had exactly one
shared password, handing out full `Role.ADMIN` to whoever had it. There was no
way for an individual VP to sign in and see only their own committee.

**What was built:** `st.login()` / `st.user` — Streamlit's own native OIDC
support (Authlib, built into Streamlit ≥1.42, confirmed present in the
installed 1.62), configured against Google directly. **Not Supabase Auth** —
the app talks to Supabase with its own service/anon key regardless of who's
using it, so a Supabase Auth session would buy nothing here. This is a
deliberate pivot from what `migrations/002_deferred_features.sql` originally
assumed (see the note at the top of that file's `profiles` table, revised
2026-08-26): `profiles` is now keyed by **email**, not `auth.users.id`.

`ais_fmd/auth.py::login_gate()`: in production, tries Google sign-in first
(only if `[auth]` is configured in secrets — completely inert otherwise, the
existing password flow is byte-for-byte unchanged when Google isn't set up). A
signed-in Google identity is looked up by email in `profiles`; a hit becomes
that person's real `Identity` (role + committee); a miss is **refused, not
downgraded** — falls through to the password form rather than silently
granting MEMBER access, because a typo in `profiles` should look like an
error, not like reduced access. The treasurer's own password login is
completely unaffected either way.

`ais_fmd/views/OfficerAccess.py` (new, TREASURER-gated, "Officer Access" in
nav): add/edit/remove a VP's `email → role, committee` assignment. This is
where the committee-mapping data goes once you have it.

**What's tested:** the resolution logic (`auth._identity_for_google_user`,
pure, dependency-injected — `tests/test_auth_gate.py`) and the `profiles`
storage layer (`tests/test_officer_access.py`), 30 tests total. **What is
*not* tested, and cannot be from here:** the actual `st.login()`/`st.user` OIDC
round-trip. `AppTest` has no hook to fake an OIDC session, and there's no real
Google Cloud client to test against yet. This is the one piece of this feature
genuinely unverified — everything downstream of "a real email came back from
Google" is covered; getting a real email back from Google is not.

**Outstanding — nothing further to build until a human does two things
outside this repo:**

1. **Register a Google OAuth app** in Google Cloud Console, restricted to
   `@ufl.edu`. Free, ~10 minutes. Produces a client ID and secret.
2. **Add an `[auth]` section to the production `secrets.toml`:**
   ```toml
   [auth]
   redirect_uri = "https://<your-deployed-url>/oauth2callback"
   cookie_secret = "<a long random string>"
   client_id = "<from step 1>"
   client_secret = "<from step 1>"
   server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
   ```

Once both exist, **manually verify the round trip before trusting it**: sign
in with a real `@ufl.edu` Google account that has (and separately, one that
does *not* have) a `profiles` row, and confirm the second case is refused
cleanly rather than falling through to something unexpected. Nobody has done
this yet because nobody has a real Google Cloud app to test against.

**Also outstanding, lower urgency:**

* `Authlib>=1.3.2` is in `requirements-production.txt`, correctly absent from
  the sandbox venv's normal install — matches the existing pattern for
  `supabase`/`openai`.
* ~~`SupabaseBackend` does not implement...~~ **Done 2026-08-26.** All 17
  previously-missing methods are implemented — the original 7 from §8.4
  (reimbursements, receipts, term locking, labeled examples) plus the 7 new
  roster/profile ones from this session. `SupabaseBackend` now overrides every
  method `Backend` declares; nothing falls through to a default any more.
  **Genuinely untested, though** — this repo has zero test coverage for
  `supabase_backend.py`, old methods or new, consistent with the "only start
  with a throwaway Supabase project" guidance already in §8.4. Verify against
  one before trusting any of it. One real bug was caught and fixed just from
  reading the code carefully, without running it: three methods
  (`decide_reimbursement`, `link_reimbursement_to_transaction`,
  `set_term_lock`) originally set a timestamp column to the literal string
  `"now()"` — a REST write inserts that as data, not SQL, so Postgres would
  have rejected it as an invalid timestamp on every real call. Fixed with an
  actual Python-side ISO timestamp (`_now()`). That the bug was only caught by
  inspection, not by any test, is itself the argument for testing this against
  a real project before it carries real traffic.
* `profiles`'s DDL exists (migration 002, revised this session for
  email-keying — see the note at the top of that table's definition) but,
  like all of migration 002, has not been run against the live database. Run
  002's `profiles` table (only that part — leave reimbursements/receipts/RLS
  deferred as documented at the top of the file) once migration 001 is in and
  before Google sign-in can work against production.
* RLS remains **inapplicable, not just deferred**, under this architecture —
  see the long note in `migrations/002_deferred_features.sql`. `auth.uid()`
  will always be NULL because individual VPs never open their own Postgres
  session; authorization has to stay application-level
  (`auth.require(...)`), same as it already is everywhere else in this app.

### 12.3 Three dependency floors were raised, and they're load-bearing

Discovered getting the app running on this machine, before any of the M21/M22
work started — real bugs, not preference. Full reasoning is in each package's
comment in `requirements.txt`; summarized here so it survives a skim.

* **`streamlit>=1.49`** (was `>=1.40`). Several views pass `width="stretch"`
  to `st.dataframe`/`st.plotly_chart`. On 1.44 (what this machine had) that
  raises `TypeError` at render time — every view test failed while the domain
  test suite stayed green, which looks exactly like "the app is broken" rather
  than "the floor is too low." `st.login()`/`st.user` (§12.2) also need a
  recent Streamlit; 1.62 is what this session verified against.
* **`pandas>=2.3`** (was `>=2.2`). 2.2.3 against the numpy version this venv
  has makes `pd.Timedelta(days=n)` itself emit a `DeprecationWarning` that says
  it will become a hard error — fires on the Review Queue's period filter.
  Nothing to do with app logic; upgrading to 3.0.5 cleared it and also cleared
  an unrelated `FutureWarning` in `domain/budgets.py`'s `fillna` call.
* **`openpyxl>=3.1.5`** (was `>=3.1`). Pandas 3 refuses to read `.xlsx` files
  with anything older — silently, as an `ImportError` the first time any code
  tries `pd.read_excel`. Would have broken both statement uploads and the
  roster's membership-form upload (§12.1) the first time either got real use.

**If a fresh environment behaves differently than this document describes**,
check these three versions before assuming the code changed. `pip install` from
`requirements.txt` as committed should get all three automatically; the risk is
an environment that had older pins installed before this session and never
upgraded them.
