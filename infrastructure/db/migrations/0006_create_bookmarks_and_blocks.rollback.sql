-- Rollback for 0006_create_bookmarks_and_blocks
--
-- Drops both tables in reverse dependency order (neither table is referenced
-- by any other table, so order between the two does not matter here, but
-- blocks is dropped first for consistency with creation order).
-- The indexes idx_blocks_blocked and idx_bookmarks_target are dropped
-- automatically by Postgres when their tables are dropped.

DROP TABLE IF EXISTS blocks;
DROP TABLE IF EXISTS bookmarks;
