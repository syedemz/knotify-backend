-- Rollback 0022: refresh_deck_view() back to SECURITY INVOKER
--
-- Restores the function body to the shape created by migration 0009.
-- Restores the default PUBLIC EXECUTE grant. This will re-break the
-- Lambda refresh (see 0022 up migration for the diagnosis), so only roll
-- back if you are swapping to an alternative fix (e.g., transferring
-- ownership of deck_view to aurora_refresh).

CREATE OR REPLACE FUNCTION refresh_deck_view()
RETURNS void
LANGUAGE sql
AS $$
    REFRESH MATERIALIZED VIEW CONCURRENTLY deck_view;
$$;

REVOKE EXECUTE ON FUNCTION refresh_deck_view() FROM aurora_refresh;
GRANT EXECUTE ON FUNCTION refresh_deck_view() TO PUBLIC;
