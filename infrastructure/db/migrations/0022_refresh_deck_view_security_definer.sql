-- Migration 0022: refresh_deck_view() → SECURITY DEFINER
--
-- Observed 2026-11-20 in dev: the knotify-refresh-deck-view Lambda has been
-- failing every 15 minutes (EventBridge schedule) AND on every profile-
-- completion PATCH trigger with:
--
--   InsufficientPrivilege: permission denied for materialized view deck_view
--     CONTEXT: SQL function "refresh_deck_view" statement 1
--
-- Chain of ownership on dev Supabase:
--   - deck_view (materialized view)       — owner: postgres
--   - refresh_deck_view()                  — owner: postgres, SECURITY INVOKER
--   - aurora_refresh (Lambda role)         — has EXECUTE on the function (via
--                                           the default PUBLIC grant)
--
-- Postgres requires OWNERSHIP of a materialized view to run REFRESH
-- MATERIALIZED VIEW — there is no REFRESH privilege to grant. The original
-- migration 0009 shipped the function as SECURITY INVOKER, which means the
-- REFRESH inside the function body runs with the caller's privileges. The
-- aurora_refresh Lambda role is not the owner of deck_view, so the inner
-- REFRESH is rejected even though the function call itself is authorised.
--
-- On the previous Aurora deployment the call chain happened to run under a
-- role that inherited the matview's ownership privileges; the phase-14
-- Supabase port broke that assumption, and we only noticed when a new user
-- signed up and never appeared in the deck.
--
-- Fix: flip the function to SECURITY DEFINER. The function owner (postgres)
-- also owns the matview, so the REFRESH will run with the owner's rights
-- regardless of which role calls the function. Narrow the EXECUTE grant at
-- the same time — SECURITY DEFINER functions should not be callable from
-- PUBLIC; only the aurora_refresh role (used by the Lambda) needs to invoke
-- this.
--
-- Safety: set an explicit search_path so the SECURITY DEFINER body cannot
-- be hijacked by a schema-shadowing attack on the caller's search_path.
-- The function body only touches deck_view by unqualified name; pinning
-- search_path to `public, pg_catalog` resolves it unambiguously to the
-- single matview we deployed.

CREATE OR REPLACE FUNCTION refresh_deck_view()
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
    REFRESH MATERIALIZED VIEW CONCURRENTLY deck_view;
$$;

REVOKE EXECUTE ON FUNCTION refresh_deck_view() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION refresh_deck_view() TO aurora_refresh;
