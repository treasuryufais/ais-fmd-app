-- ===========================================================================
-- Migration 014 -- committee plans and budget requests. NOT YET RUN.
-- Treasurer decision.
--
-- Two tables behind the VP "My Plan" page (`views/MyPlan.py`):
--
--   committee_plan_lines  a VP's budget plan: one line per category (a purpose,
--                         e.g. Food & Drink) and a planned amount. An estimate
--                         only: it never books, moves or reserves money.
--   budget_requests       one request per committee and semester: an amount and
--                         a reason, for the treasurer. The treasurer still sets
--                         every budget in `committeebudgets`.
--
-- Both are keyed by the semester's NAME ("Spring 2027"), not a TermID, because a
-- VP plans for next term before the treasurer has created it.
--
-- Until this runs, My Plan says plans cannot be saved yet and shows only the
-- suggestion from past terms (the backend reports the tables missing and the page
-- checks before drawing the editors). Nothing else in the app depends on it.
--
-- Locked down the way 008 locks every table: RLS on, no policies, only
-- service_role. No foreign key to `committees`, which is empty on live (see 013).
-- Authorization stays application-level: the backend refuses a write for any
-- committee but the caller's own.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.committee_plan_lines (
    line_id      integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    committee_id integer NOT NULL,
    semester     text NOT NULL,
    purpose      text NOT NULL,
    amount       numeric(12, 2) NOT NULL CHECK (amount > 0),
    note         text CHECK (note IS NULL OR char_length(note) <= 200),
    updated_by   text,
    updated_at   timestamptz DEFAULT now(),
    UNIQUE (committee_id, semester, purpose)
);

CREATE TABLE IF NOT EXISTS public.budget_requests (
    request_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    committee_id integer NOT NULL,
    semester     text NOT NULL,
    amount       numeric(12, 2) NOT NULL CHECK (amount > 0),
    reason       text NOT NULL CHECK (char_length(reason) BETWEEN 10 AND 500),
    requested_by text NOT NULL,
    requested_at timestamptz DEFAULT now(),
    UNIQUE (committee_id, semester)
);

ALTER TABLE public.committee_plan_lines ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.committee_plan_lines FROM anon, authenticated;
GRANT ALL ON public.committee_plan_lines TO service_role;

ALTER TABLE public.budget_requests ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.budget_requests FROM anon, authenticated;
GRANT ALL ON public.budget_requests TO service_role;

COMMIT;
