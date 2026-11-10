-- Rollback 0020: restore Supabase's default anon/authenticated/service_role
-- grants on knotify tables.
--
-- WARNING: running this rollback RE-EXPOSES every table listed below to the
-- PostgREST API served at https://<project>.supabase.co/rest/v1/*, meaning
-- anyone with the publicly-distributed anon API key can read (and INSERT,
-- UPDATE, DELETE, TRUNCATE) all rows. The only table protected by RLS is
-- `users`, and even there anon can still INSERT (the INSERT policy's
-- WITH CHECK is `true`). Do NOT roll this back on a live environment
-- without replacing it with a different, explicit protection.
--
-- This rollback exists only so yoyo's down-migration semantics are
-- consistent across all migrations — not because you ever want to run it
-- in production.

-- Restore default privileges first so new tables created after rollback
-- also pick up the default grants again.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT ALL ON FUNCTIONS TO anon, authenticated, service_role;

GRANT ALL PRIVILEGES ON TABLE
    users,
    siblings,
    friendships,
    friend_requests,
    bookmarks,
    blocks,
    refresh_log
  TO anon, authenticated, service_role;

GRANT ALL PRIVILEGES ON TABLE deck_view TO anon, authenticated, service_role;

GRANT ALL PRIVILEGES ON TABLE
    _yoyo_migration,
    _yoyo_log,
    _yoyo_version,
    yoyo_lock,
    _schema_init
  TO anon, authenticated, service_role;
