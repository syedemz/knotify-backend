-- Rollback for migration 0021: restore the whole-table UNIQUE constraint
--                               and drop the partial unique index.
--
-- NOTE: rolling back may fail if the live table contains multiple rows
-- with the same (from_user_id, to_user_id, status) tuple for a non-pending
-- status (which the forward migration allows). A clean rollback requires
-- de-duping those rows first.

DROP INDEX IF EXISTS friend_requests_one_pending_per_direction;

ALTER TABLE friend_requests
    ADD CONSTRAINT friend_requests_from_user_id_to_user_id_status_key
    UNIQUE (from_user_id, to_user_id, status);
