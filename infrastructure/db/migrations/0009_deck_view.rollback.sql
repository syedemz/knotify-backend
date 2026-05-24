-- Rollback 0009: remove refresh_deck_view() function and deck_view materialized view
--
-- Reversal order:
--   1. Drop the function first (it references the view; dropping the view
--      would leave a dangling function body that references a non-existent
--      object — harmless in practice for SQL functions, but drop in dependency
--      order for clarity).
--   2. DROP MATERIALIZED VIEW also removes idx_deck_user automatically.

DROP FUNCTION IF EXISTS refresh_deck_view();
DROP MATERIALIZED VIEW IF EXISTS deck_view;
