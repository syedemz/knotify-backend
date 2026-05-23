-- Rollback for 0002_create_users
--
-- Drops the users table and the _age_from_birthday helper function.
-- All four indexes (idx_users_sex_country_religion, idx_users_age,
-- idx_users_prefs_gin, idx_users_vector) are dropped automatically by
-- Postgres when the table is dropped — no need to drop them individually.
--
-- Tables that depend on users via foreign keys (siblings, friendships,
-- friend_requests, bookmarks, blocks — created in stories 2.4–2.6) must be
-- rolled back before this migration, which yoyo enforces by rolling back in
-- reverse order.

DROP TABLE IF EXISTS users;
DROP FUNCTION IF EXISTS _age_from_birthday(DATE);
