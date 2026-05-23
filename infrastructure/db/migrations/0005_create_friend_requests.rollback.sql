-- Rollback for 0005_create_friend_requests
--
-- Drops the friend_requests table. The partial indexes idx_fr_to_pending and
-- idx_fr_from_pending are dropped automatically by Postgres when the table
-- is dropped.

DROP TABLE IF EXISTS friend_requests;
