-- Rollback of migration 0014: restore SECURITY INVOKER on refresh_deck_view()
--
-- Reverts the function to its original 0009 security mode. Note: after
-- rollback the function will once again fail when invoked by aurora_refresh
-- with "must be owner of materialized view deck_view". Only roll back if the
-- caller invoking refresh_deck_view() is the function owner (master/knotify).

ALTER FUNCTION refresh_deck_view() RESET search_path;
ALTER FUNCTION refresh_deck_view() SECURITY INVOKER;
