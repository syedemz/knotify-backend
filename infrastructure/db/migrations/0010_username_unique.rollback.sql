-- Rollback for migration 0010: drop the case-insensitive partial unique index
-- on users.username.
--
-- After rolling back, duplicate lower(username) values are permitted again and
-- NULL-username rows are unaffected (they were never in the index).

DROP INDEX IF EXISTS users_username_lower_unique;
