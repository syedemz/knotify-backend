-- Rollback for 0004_create_friendships
--
-- Drops the friendships table. The idx_friendships_b index is dropped
-- automatically by Postgres when the table is dropped.

DROP TABLE IF EXISTS friendships;
