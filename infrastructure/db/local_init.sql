-- Local-development-only: set the app_user role password to match
-- docker-compose conventions and the test_rls.py fixture.
--
-- This file is intentionally OUTSIDE infrastructure/db/migrations/ so yoyo
-- never picks it up — the cluster-side migrator Lambda (phase 3 story 3.7)
-- must NOT apply this. The Aurora dev/prod app_user password is generated
-- at random by the migrator Lambda post-yoyo and stored in Secrets Manager
-- (secret name: knotify-<env>-app-user-credential).
--
-- Usage (after docker compose up -d && yoyo apply):
--
--   psql -h localhost -U knotify -d knotify -f infrastructure/db/local_init.sql
--
-- A future Makefile target (phase 3 story 3.5) will bundle this into
-- `make db-up`.

ALTER ROLE app_user WITH PASSWORD 'app_user';
