# Handoff — UF AIS Financial Management System (sandbox rebuild)

**For:** whoever picks this up next, human or agent.
**Read this first.** It is written to be self-contained: you should not need the
prior conversation.

Last verified: all 188 tests passing; app runs; 892 real transactions loaded.

---

## 1. What this is, and what it is not

`C:\Users\durpy_7vdh2wz\ais-fmd-sandbox` is a **rebuild** of the treasury app at
`C:\Users\durpy_7vdh2wz\ais-fmd-app`. It is a **sandbox**: it runs against a
local SQLite file with no network access.

**The original repo has never been modified.** It is still at commit `6cb308c`
with the same two modified files it had at the start. The sandbox is a separate
git repo with **no remote configured**, so `git push` has nowhere to go.

Nothing here has been deployed. The production code path (Supabase) has never
executed — see §6.

### Running it

```bash
cd C:\Users\durpy_7vdh2wz\ais-fmd-sandbox
.venv/Scripts/python -m streamlit run app.py     # http://localhost:8501
.venv/Scripts/python -m pytest                    # 188 tests
```

Useful scripts:

| Script | Purpose |
| --- | --- |
| `scripts/load_real_statement.py <csv> --into-sandbox` | Load a real statement, replacing sandbox data |
| `scripts/test_real_statement.py <csv>` | Parse-only diagnostic, no writes |
| `scripts/verify_later_modules.py` | E2E check of locking/reimbursements/planner on a DB copy |
| `scripts/profile_hotpath.py` | Times the work every page rerun does |

### The safety model — do not weaken this

Three independent layers:

1. **`openai` and `supabase` are not installed** in the sandbox venv. Verify:
   `.venv/Scripts/python -c "import importlib.util as u; print(u.find_spec('openai'), u.find_spec('supabase'))"` → both `None`.
2. **Fails closed.** `AIS_FMD_ENV` must equal exactly `production`. Unset, empty,
   `prod`, `1`, `true` all resolve to sandbox. A stray `OPENAI_API_KEY` still
   spends nothing — `settings.llm_enabled()` returns `False` in sandbox mode.
3. **Separate directory, own git repo, no remote.**

`tests/test_sandbox_safety.py` asserts all of it. If those tests fail, the
sandbox is no longer safe to hand to anyone.

---

## 2. Verified current state

```
transactions     892   (real Wells Fargo data, Jul 2024 – Jul 2026)
terms              9
budgets           84
uploaded_files     1
balances           0   <-- reconciliation has nothing to check
merchants          0   <-- merchant memory is dormant
receipts           0
reimbursements     0
accounts        ['Wells Fargo']  <-- no Venmo data at all
```

Code: **13,949 lines** — 10,718 app (3,148 of it views), 2,191 tests.

Categorization on the real data: **562 of 892 by rule (63%)**, 330 residual.
Of those 330, **210 map to 97 distinct merchants** — mapping those once would
clear them permanently.

---

## 3. Architecture

```
ais_fmd/
  settings.py            env detection + sandbox guard (fails closed)
  auth.py                roles: MEMBER < OFFICER < TREASURER < ADMIN
  config/categories.py   committees, purposes, accounts — ONE source of truth
  data/
    backend.py           interface both backends implement
    sqlite_backend.py    sandbox (855 lines — see §5, getting large)
    supabase_backend.py  production — NEVER EXECUTED
    repositories.py      the single cache layer
    schema_postgres.sql  production DDL + atomic import RPC
    seed.py              demo data generator
  domain/                business logic — NOTHING here imports streamlit
    money, terms, budgets, dedupe, reconcile, quality, dues,
    alerts, report, scenarios, reimbursements, receipts, assistant
    categorize/          predicates, merchants, llm, pipeline
    parsers/             venmo, wells_fargo
  ui/                    theme (Plotly template), charts, shell
  views/                 13 Streamlit pages — layout and wiring only
tests/                   188 tests
```

**Invariants enforced by tests** (`tests/test_ui_conventions.py`):
- nothing under `domain/` imports Streamlit
- every view calls `auth.require(...)`
- no view imports a backend directly (must go through `repositories`)
- no view renders currency through a raw `st.caption`/`st.write` (see §7, trap 1)

---

## 4. What was fixed (18 findings from the original app)

Condensed. Each has a regression test named for it in `tests/test_regressions.py`.

| ID | Fix |
| --- | --- |
| F1 | Supabase client was `@st.cache_resource` — process-global, so auth leaked between users. Now per-session token. |
| F2 | Open signup + one shared treasury password → role-based access (`auth.py`) |
| F3 | Dedupe keyed on `(details, date)` only, silently dropping real same-day repeats. Now includes amount + occurrence ordinal, DB-enforced. |
| F4 | Upload wasn't atomic. Now one transaction. |
| F5 | Statement parsing was positional and unvalidated; parse failures became **$0.00 rows**. Now validated + rejected loudly. |
| F6 | Editor compared `int` to `"8 - Meeting Food"` → every visible row rewritten on every save. Now typed comparison + batched write. |
| F7 | Purpose→committee map contradicts the on-screen reference table. **Preserved deliberately — needs a decision, see §8.** |
| F8 | LLM failures silently returned `[]`, indistinguishable from "no match". Now typed outcomes. |
| F9 | Unbounded LLM prompt. Now batched with a token budget. |
| F10 | Assistant router tested `"committee"` first and returned the roster, so its own headline example never reached the spending branch. Rebuilt on scored tools. |
| F11 | Rising expenses rendered green. `delta_color="inverse"`. |
| F12 | `% spent` divided by zero → `inf` destroyed chart axes. |
| F13 | `account` written as `'Wells'`, documented as `'Wells Fargo'`. Canonicalised. |
| F14 | Metric deltas rendered as bare floats. |
| F15 | Dead + broken `fetch_term_budget_usage`. Deleted. |
| F16 | Dedupe crashed on an empty table (fresh DB). |
| F17 | `inplace` fillna on a chained slice — a no-op under pandas 3 CoW. |
| F18 | Money as float64 end-to-end. Now `Decimal` at boundaries. |

Also fixed: caching was double-layered so the 5-minute TTL never fired;
`get_semester` was O(rows × terms) per rerun.

---

## 5. Modules built

| ID | Module | State |
| --- | --- | --- |
| M1 | Reconciliation | Built. **Dormant on real data — 0 statement balances recorded.** |
| M2 | Audit trail | Built, working. Every write is audited. |
| M3 | Review queue | Built, working. |
| M4 | Merchant memory | Built. **Dormant on real data — 0 merchant rules.** |
| M5 | Burn-rate projection | Built (Dashboard section). |
| M6 | Alerts | Built. Compute only — no delivery (email/Slack). |
| M7 | Dues | Built, working. $9,065 / 246 payments on real data. |
| M8 | Reimbursements | Built, E2E verified. Matched a real transaction at 96% confidence. |
| M9 | Receipts | Built. Path-traversal + type guards tested. No OCR (needs network). |
| M10 | Period locking | Built, enforced in the data layer. E2E verified. |
| M11 | Officer portal | Built. **`Identity.committee_id` is never assigned anywhere — scoping always falls back to "choose any".** |
| M12 | Board pack | Built. Self-contained HTML, prints to PDF. |
| M13 | Data quality | Built, working. |
| M14 | Scenario planner | Built. Fall 2025: break-even is 156 members at $36.79. |
| M15 | Assistant | **Half built.** Deterministic tool routing works and is tested. The LLM tool-calling path was designed but never wired. |
| M16 | Runbook | Built, reads live state. |
| M17 | Multi-account | Not built. |

---

## 6. Known gaps and defects

Ordered by how much they should worry you.

### 6.1 Views have zero execution coverage — highest risk
3,148 lines of view code. `test_ui_conventions.py` parses them as text (AST) but
**never runs them**. Every runtime bug this session was found by hand in a
browser: a `NaN.strip()` crash on Transactions, currency rendered as LaTeX on
three separate pages, a Plotly `title=None` printing `"undefined"`, pages
defaulting to an empty term.

`streamlit.testing.v1.AppTest` is available (Streamlit 1.61) and can execute
pages headlessly. This is the single highest-leverage thing left to do.

### 6.2 Per-rerun recompute is O(terms × rows) — measured, not theoretical

From `scripts/profile_hotpath.py` on the real 892 rows:

```
 180.8 ms  historical_budget_vs_actual (all terms)
 106.9 ms  dues.compare_semesters (all)
 237.4 ms  Dashboard sparkline loop (9 terms x 2 series)
  43.4 ms  alerts.evaluate
```

≈ **460 ms of avoidable recompute per Dashboard interaction.** The cause is the
same shape as the original bug that was diagnosed and fixed: `attach_semester`
is O(rows), and it is now called once *per semester* inside loops instead of
once overall. The inner function was fixed; the outer loop reintroduced the cost.

Fix: tag semesters once and pass the tagged frame down, or memoize on
`(data_version, semester)`.

### 6.3 The Venmo parser has never seen a real file
The sandbox contains only Wells Fargo data. The Wells Fargo parser passed 103
tests and then **failed on the first real file** — the real export had a header
row with `DESCRIPTION` and `AMOUNT` in the opposite positions from what the
fixtures assumed, and the original app would have imported all 892 rows as
$0.00. The Venmo parser is in exactly that pre-contact state.

### 6.4 Supabase backend: 7 of 25 interface methods unimplemented
Missing: `fetch_reimbursements`, `create_reimbursement`, `decide_reimbursement`,
`link_reimbursement_to_transaction`, `fetch_receipts`, `store_receipt`,
`set_term_lock`. The base class returns clear "not implemented for this backend"
errors rather than failing obscurely.

`schema_postgres.sql` also lacks DDL for `reimbursements`, `receipts`, and the
`terms.locked` columns.

**Nothing in the production path has ever run against a real Postgres.**

### 6.5 Smaller items
- `sqlite_backend.py` is 855 lines and becoming a god object. `Treasury.py` is
  467 lines doing upload + budgets + terms + export + locking.
- `Dashboard.py:169` reaches into `charts._height` (private).
- M6 alerts compute but do not deliver.
- No `natural_key` backfill script — production dedupe cannot work without one.
- Receipt OCR absent by design (needs network/model).

---

## 7. Traps — read before editing

**Trap 1: Streamlit reads `$...$` as LaTeX.** Any sentence with two currency
figures silently renders the text between them as an equation. This was fixed
**four separate times** by hand before a test was added. Always use
`shell.say(...)` / `shell.notify(...)`, never raw `st.caption`/`st.write`, for
text containing money. `tests/test_ui_conventions.py` enforces this.

**Trap 2: `NaN` is truthy and `NaN != NaN`.**
`value or ""` does **not** fall back on a pandas NaN — it returns the NaN, and
`.strip()` then crashes. And `before == after` reports an unchanged null as
changed. Both bit real code here. Use `pd.isna()`.

**Trap 3: Streamlit does not hot-reload imported package modules.** Editing
anything under `ais_fmd/` requires a server restart. Editing only the page file
does not.

**Trap 4: views are `exec()`'d by `st.Page`, so relative imports fail.** Views
must use absolute imports (`from ais_fmd import auth`).

**Trap 5: `INSERT OR IGNORE` swallows foreign-key violations, not just duplicate
keys.** That silently dropped rows while reporting success. The code now uses a
targeted `ON CONFLICT (natural_key) ... DO NOTHING`.

**Trap 6: fixtures that encode your assumptions prove nothing.** The parser had
full green tests and still failed on the first real file. Test against real
artifacts.

---

## 8. Decisions needed from a human

These are not bugs to fix; they change financial meaning.

**8.1 — Disputed purpose→committee mappings (F7).**
`map_purpose_to_budget_id` books `"Professional Development"` to committee **7
(Consulting)** and `"Food & Drink"` to **5 (Membership)**, while the reference
table shown to treasurers says 7 is Consulting and 10 is Professional
Development. The original behaviour was preserved so historical figures would
not shift. Data Quality shows how many transactions each affects (43 on the real
data). Decide, then edit `PURPOSE_TO_COMMITTEE` in `config/categories.py` and
remove the entry from `DISPUTED_PURPOSE_MAPPINGS`.

**8.2 — Deduplication semantics.**
Current rule is *"do we already have this many of it"*, not *"does this row
exist"*. So two genuine identical same-day purchases both import, and
re-uploading a statement imports nothing. The trade-off: a **new** statement
containing one further identical same-day purchase is flagged as a duplicate. It
errs toward never double-importing. Confirm that is the right bias for your books.

---

## 9. Suggested next session — prioritized

Ordered by value per unit of work. **P1 and P2 are the ones I would insist on.**

### P1 — Headless view tests (`AppTest`)
Highest leverage remaining. One test per page that runs it and asserts no
exception, plus a few that drive widgets. Would have caught every runtime bug
found by hand this session. Start with `Transactions`, `Dashboard`, `Treasury` —
the three with real interaction logic.

### P2 — Kill the O(terms × rows) recompute
≈460 ms per Dashboard interaction, measured. Tag semesters once, pass down, or
memoize on `(data_version, semester)`. Re-run `scripts/profile_hotpath.py` to
confirm. Add a test that asserts `attach_semester` is called once per render.

### P3 — Wake up merchant memory on the real data
Currently 0 rules, so M4's advantage is dormant. 97 distinct merchants cover 210
of the 330 residual rows. Either work the Review Queue with "Remember this
merchant" ticked, or write a bulk-mapping screen. Expect residual to fall from
330 to roughly 120.

### P4 — Test the Venmo parser against a real export
Known unknown with a known failure mode (§6.3). Ask for a Venmo CSV, run
`scripts/test_real_statement.py`, and add fixtures from its real shape.

### P5 — Record statement balances so reconciliation runs
0 balances today, so M1 is dormant on real data. Add opening/closing balances
for a few periods from the real statements and confirm the ledger agrees. This
is also the check that catches a statement that was never imported.

### P6 — Assign `Identity.committee_id`
M11's scoping never actually restricts, because the field is never set. Either
wire it to a profile, or add a sandbox control to set it — otherwise the officer
portal's core claim is untested.

### P7 — Finish the production path
Only worth starting when a throwaway Supabase project is available:
implement the 7 missing methods, add DDL for reimbursements/receipts/locking,
write the `natural_key` backfill, then canonicalise `Wells` → `Wells Fargo`.
Do **not** set `AIS_FMD_ENV=production` before all of that.

### P8 — Wire the assistant's LLM tool-calling path
M15 is half built. The tools exist and are tested; the model-chooses-a-tool layer
is not wired. Cheap now that the tools are stable, and it is the shape that keeps
token cost near zero.

### P9 — Split the two large files
`sqlite_backend.py` (855) into reads / writes / reimbursements+receipts.
`Treasury.py` (467) into one module per tab.

### P10 — Small cleanups
Alerts delivery (email); `charts._height` privacy leak in `Dashboard.py:169`;
receipt OCR if a model ever becomes available.

---

## 10. Fast orientation for a new agent

Read in this order:
1. `ais_fmd/settings.py` — the safety model, ~90 lines
2. `ais_fmd/config/categories.py` — the domain vocabulary
3. `ais_fmd/domain/categorize/predicates.py` — the business rules that decide
   where money is booked
4. `ais_fmd/domain/parsers/wells_fargo.py` — the module with the most scar
   tissue; its docstring explains three real failures
5. `tests/test_real_layouts.py` — what a real statement actually looks like

Then run `pytest` and `scripts/profile_hotpath.py` before changing anything.
