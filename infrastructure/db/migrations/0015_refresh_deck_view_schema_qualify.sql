-- Migration 0015: schema-qualify deck_view in refresh_deck_view()
--
-- Problem
-- -------
-- Migration 0014 pinned refresh_deck_view()'s search_path to
-- `pg_catalog, pg_temp` as standard SECURITY DEFINER hardening. The
-- function body references `deck_view` unqualified, which lives in the
-- `public` schema. With `public` excluded from search_path, Postgres
-- rejects the refresh with:
--
--   ERROR: relation "deck_view" does not exist
--   CONTEXT: SQL function "refresh_deck_view" statement 1
--
-- Surfaced by the phase-7 live probe immediately after 0014 deployed.
--
-- Fix
-- ---
-- CREATE OR REPLACE FUNCTION with the body schema-qualified as
-- `public.deck_view`. The function no longer depends on search_path at
-- all — the locked-down `pg_catalog, pg_temp` setting from 0014 stays
-- in place as defense-in-depth, but is functionally irrelevant. This
-- is the textbook pattern for SECURITY DEFINER functions: qualify every
-- object reference and let search_path be inert.
--
-- SECURITY DEFINER must be re-declared here because CREATE OR REPLACE
-- FUNCTION replaces the function definition wholesale, including the
-- SECURITY clause. The SET search_path clause from 0014 persists across
-- CREATE OR REPLACE (it's attached to the function name, not the body),
-- so it does not need to be re-declared.

CREATE OR REPLACE FUNCTION refresh_deck_view()
RETURNS void
LANGUAGE sql
SECURITY DEFINER
AS $$
    REFRESH MATERIALIZED VIEW CONCURRENTLY public.deck_view;
$$;
