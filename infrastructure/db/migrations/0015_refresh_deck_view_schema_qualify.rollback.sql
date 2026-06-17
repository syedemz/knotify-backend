-- Rollback of migration 0015: restore unqualified deck_view reference
--
-- Reverts to the unqualified body that was in force between migrations 0009
-- and 0015. SECURITY DEFINER stays (from 0014). After rollback the function
-- will once again fail with "relation deck_view does not exist" because the
-- search_path locked in by 0014 excludes public.

CREATE OR REPLACE FUNCTION refresh_deck_view()
RETURNS void
LANGUAGE sql
SECURITY DEFINER
AS $$
    REFRESH MATERIALIZED VIEW CONCURRENTLY deck_view;
$$;
