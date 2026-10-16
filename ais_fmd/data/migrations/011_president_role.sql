-- ===========================================================================
-- Migration 011 -- allow the 'president' role in `profiles`. NOT YET RUN.
--
-- The President sees everything the Treasurer sees but cannot change anything
-- (`Role.PRESIDENT` in ais_fmd/auth.py). Migration 002 created `profiles` with a
-- CHECK that only allows member / officer / treasurer / admin, so saving a
-- president row would be rejected until this widens it.
--
-- Written but not applied to any database. Run it with 002, against a throwaway
-- Supabase project first (HANDOFF.md section 8.4). Until it is run, nothing in the
-- app breaks: no profile can carry the role, so nobody can sign in as President.
-- ===========================================================================

BEGIN;

ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_role_check;
ALTER TABLE public.profiles
    ADD CONSTRAINT profiles_role_check
    CHECK (role IN ('member', 'officer', 'treasurer', 'president', 'admin'));

COMMIT;
