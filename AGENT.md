# AGENT.md — UF AIS Financial Management Dashboard

## What This Is

A Streamlit-based financial dashboard and treasury management portal for the **University of Florida Association for Information Systems (AIS)**. It tracks transactions, committee budgets, and spending across academic semesters, and provides an AI-powered financial Q&A interface.

Two audiences:
- **General members** — view dashboards, analytics, and export data (read-only)
- **Treasury officers** — upload Excel bank statements, manage budgets/terms, and edit transactions (password-protected)

---

## Tech Stack

| Layer | Tool | Notes |
|---|---|---|
| UI framework | Streamlit 1.44.1 | Wide mode, dark theme (#191414, #1DB9FF) |
| Charts | Plotly 6.0.1 | Interactive, all within Streamlit |
| Database | Supabase (PostgreSQL) | Anon key for reads, service role key for writes |
| AI / LLM | Google Gemini 2.0 Flash | Free tier: 15 RPM, 1M TPM |
| LLM wrapper | LangChain + `ChatGoogleGenerativeAI` | Context injection, no persistent memory |
| Data | Pandas 2.2.3 | All transforms in-memory |
| Excel parsing | openpyxl + xlrd | .xlsx and legacy .xls |

---

## How to Run

```bash
pip install -r requirements.txt
streamlit run app.py
# App at http://localhost:8501
```

Secrets go in `.streamlit/secrets.toml` (never committed):

```toml
[supabase]
url = "..."
key = "..."           # anon key
service_key = "..."   # service role key (admin writes)

[treasury]
password = "..."      # treasury portal gate

[google]
api_key = "..."       # Gemini API key
```

---

## File Map

```
app.py                          # Entry point: auth + navigation
utils.py                        # Supabase client, data loaders, caching
components.py                   # Reusable UI widgets (animated title, etc.)
views/
  Homepage.py                   # Welcome screen
  Financial_Dashboard.py        # Main analytics (filters, metrics, charts)
  AI_Assistant.py               # Gemini LLM chat interface
  Transaction_Editor.py         # Inline bulk editor for transactions
  Treasury_Management.py        # Admin portal (upload, budgets, terms)
  treasury_auto_categorize.py   # Rule engine for auto-categorizing transactions
  treasury_event_calendar.py    # Semester event schedule used as a categorization signal
  treasury_parse_utils.py       # Excel parsing helpers
  AIS_Financial_Dashboard.py    # Deprecated — do not use
.streamlit/config.toml          # Dark theme config
assets/AIS_logo.png             # Logo
```

---

## Database Schema

**`transactions`** — core table
- `transactionid`, `transaction_date`, `amount` (positive=income, negative=expense)
- `details` (raw description), `purpose`, `account` ("Venmo" or "Wells Fargo")
- `budget_category` → FK to `committees.CommitteeID`

**`committees`** — committee definitions
- `CommitteeID`, `Committee_Name`, `Committee_Type`

**`committeebudgets`** — budget per committee per term
- `committeebudgetid`, `termid` (FK), `committeeid` (FK), `budget_amount`

**`terms`** — academic semesters
- `TermID`, `Semester`, `start_date`, `end_date`

---

## Key Architecture Decisions

**Caching:** All Supabase queries are cached with a 5-minute TTL via `@st.cache_data`. Transactions are fetched in 1,000-row batches to bypass API limits. Cache is manually cleared on logout and after certain admin writes.

**Auth:** Supabase email/password auth. Session stored in `st.session_state` under a hash of the user's email to prevent cross-user data leakage. Logout clears both user state and data caches.

**Two Supabase clients:** `get_supabase()` uses the anon key (read-safe, respects RLS). `get_admin()` uses the service role key (bypasses RLS, used only in Treasury Management for writes).

**Auto-categorization (treasury_auto_categorize.py):** Rule-based engine applied to uploaded transactions. Rules run in priority order; first match wins. Rules include amount-based (dues = $35 or $52.50), keyword-based (bar names, food merchants), and day-of-week heuristics (Tue/Wed food = GBM meeting food). Three passes, in order: an LLM pass for fuzzy merchant matching, deterministic Python overrides that always beat the LLM, then an event-calendar fallback that only fills rows still blank.

**Event calendar (treasury_event_calendar.py):** The semester event schedule (GBMs, socials, consulting meetings, formal) as structured data, keyed by date. It answers what the merchant name can't — a Publix run is Meeting Food if a GBM is that day or the next, Membership if the only nearby event is a social. Used two ways: every transaction sent to the LLM carries a `Nearby_Events` summary, and rows neither pass could categorize fall back to a *strong* nearby event. Events are marked `STRONG` (calendar is the primary evidence — GBMs, consulting meetings, formal, venue tabs) or `WEAK` (event happened but the spend is a guess — basketball games, study hours); weak events are LLM context only and never assign on their own. Guard rails: fallback touches expenses (`amount < 0`) only, never overrides a deterministic rule, abstains when a day's strong events disagree, and requires a food/grocery merchant before assigning Meeting Food. Dates outside the semesters in `SEMESTER_RANGES` return no hints at all, so uncovered terms are left alone rather than guessed at. **To add a semester:** add its range to `SEMESTER_RANGES` and its events to `_RAW_EVENTS`. Currently loaded: Spring 2026 only.

**AI Assistant:** Gemini 2.0 Flash receives a natural language question plus a structured data context (filtered transaction data, schema description). Temperature is 0. No tool calling — just prompt engineering with injected context. History is stored in `st.session_state.ai_messages` only for the current session.

---

## Common Patterns

- All pages use `st.session_state` for transient UI state; database state flows through utils.py loaders.
- `Financial_Dashboard.py` is the most complex file — semester filtering drives all downstream metrics and charts.
- When editing transactions, changes are staged locally and saved in batch via a single "Save Changes" button.
- Treasury portal is gated behind a password check at the top of `Treasury_Management.py`, not Supabase auth.

---

## What to Watch Out For

- **`AIS_Financial_Dashboard.py`** is deprecated — don't touch it or reference it.
- Supabase free tier has row limits and rate limits; the pagination batch logic in `utils.py` is load-bearing.
- The treasury password is a simple shared secret stored in secrets.toml — it's not per-user auth.
- Gemini free tier (15 RPM) can throttle under heavy AI Assistant use; errors are caught and displayed gracefully.
- `budget_category` in `transactions` stores a `CommitteeID` integer, not a name — always join to `committees` before displaying.
- The event calendar only covers semesters listed in `SEMESTER_RANGES`. Uploading a term that isn't entered there is not an error — those rows just fall back to merchant and weekday rules, and `nearby_events` reads "outside calendar".
