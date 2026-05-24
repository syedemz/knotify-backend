-- Rollback for 0003_create_siblings
--
-- Drops the siblings table. The idx_siblings_user index is dropped
-- automatically by Postgres when the table is dropped.

DROP TABLE IF EXISTS siblings;
