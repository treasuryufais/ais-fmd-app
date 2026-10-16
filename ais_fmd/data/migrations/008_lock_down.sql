-- ===========================================================================
-- Migration 008 -- lock the database down to the secret (service) key.
--
-- RUN AFTER 001-007, AND ONLY TOGETHER WITH THE APP CHANGE THAT MAKES EVERY
-- READ USE THE SECRET KEY (supabase_backend.py, 2026-10-15). An app that still
-- reads with the publishable key will see an empty database after this runs:
-- RLS answers a blocked read with zero rows, not an error.
--
-- WHY. The app talks to Supabase with its own keys regardless of who is using
-- it (see the note on `profiles` in 002), so no individual ever has a Postgres
-- session and `auth.uid()` is always NULL. Row-level security cannot express
-- "this VP sees their committee" here -- authorization is application-level,
-- in `auth.require(...)`. What the database CAN do is refuse everyone except
-- the server-side secret key, and that is all this migration does:
--
--   * enables RLS on every table in `public`, with no permissive policy left,
--     so only the secret key (which bypasses RLS by design) gets in
--   * drops every existing policy on those tables. The live project had
--     "anon can SELECT every transaction" and "anyone can write uploaded_files"
--   * revokes table, sequence and function privileges from anon and
--     authenticated, and EXECUTE from PUBLIC, so the two write RPCs
--     (import_statement, apply_transaction_edits) stop being callable through
--     /rest/v1/rpc with the publishable key
--   * changes default privileges so tables created later are not exposed
--     either, even before someone remembers to enable RLS on them
--   * pins `search_path` on has_role, the one definer function 003 did not
--     rewrite (Supabase linter: function_search_path_mutable)
--
-- Found by the Supabase security advisor during the 2026-10-15 rehearsal on a
-- throwaway project restored from the live backup.
--
-- THE ORIGINAL STREAMLIT APP WILL STOP WORKING after this, if it reads with
-- the anon/publishable key. Run this when switching over, not before.
--
-- Idempotent: safe to re-run.
--
--   Undo (re-opens the database to the publishable key -- don't, unless the
--   old app must come back):
--     restore the policies from the 2026-10-15 backup's schema, then
--     GRANT SELECT ON ALL TABLES IN SCHEMA public TO anon, authenticated;
-- ===========================================================================

BEGIN;

-- --- Every table in public: RLS on, no policies ------------------------------

DO $$
DECLARE
    t   record;
    pol record;
BEGIN
    FOR t IN
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tablename);

        FOR pol IN
            SELECT policyname FROM pg_policies
             WHERE schemaname = 'public' AND tablename = t.tablename
        LOOP
            EXECUTE format('DROP POLICY %I ON public.%I', pol.policyname, t.tablename);
        END LOOP;
    END LOOP;
END;
$$;

-- --- Privileges: nothing for anon / authenticated ----------------------------

REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;

GRANT ALL ON ALL TABLES    IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;

-- Tables, sequences and functions created later by `postgres` (which is who
-- runs migrations) start out closed too.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated;

-- --- has_role: pin search_path ------------------------------------------------
--
-- Not called by this app. It references a `user_roles` table that does not
-- exist, so it is left in place rather than dropped in case the original app
-- names it -- but it no longer resolves names against a caller's search_path.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public' AND p.proname = 'has_role'
    ) THEN
        ALTER FUNCTION public.has_role(text) SET search_path = public, pg_temp;
    END IF;
END;
$$;

COMMIT;
