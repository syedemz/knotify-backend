-- Migration 0009: deck_view materialized view and refresh_deck_view() function
--
-- Purpose
-- -------
-- Creates the swipe-deck materialized view exactly as specified in
-- architecture.md §5.1.  The view holds a snapshot of users who are eligible
-- to appear in the deck:
--
--   - deleted_at IS NULL        — not soft-deleted
--   - profile_complete_verified = true — profile has been completed and
--                                        verified by the onboarding flow
--
-- Columns projected match §5.1 verbatim so that the Lambda reading the deck
-- can build swipe cards without fetching the full users row.
--
-- idx_deck_user is a UNIQUE index on user_id.  This index serves two roles:
--   1. Enforces uniqueness of user_id in the view (each user appears once).
--   2. Enables REFRESH MATERIALIZED VIEW CONCURRENTLY — Postgres requires at
--      least one unique index on the view to allow non-blocking refresh.
--
-- refresh_deck_view() is a thin SQL wrapper so that any caller (scheduler,
-- trigger, admin Lambda) can refresh the view through a stable function name.
-- CONCURRENTLY means the view remains readable during the refresh.
--
-- Grants:
--   SELECT on deck_view → app_user (Lambda reads from the view under app_user).
--   EXECUTE on refresh_deck_view() is NOT granted to app_user — refresh is an
--   admin/scheduler operation.  Only the master (knotify) can invoke it.

CREATE MATERIALIZED VIEW deck_view AS
SELECT user_id, email, age, chosen_profile_avatar, current_residence_city,
       current_residence_country, first_name, job_title, last_name, photo_url,
       profile_complete_verified, resident_country_code, sex, username
FROM users
WHERE deleted_at IS NULL AND profile_complete_verified = true;

CREATE UNIQUE INDEX idx_deck_user ON deck_view (user_id);

CREATE OR REPLACE FUNCTION refresh_deck_view()
RETURNS void
LANGUAGE sql
AS $$
    REFRESH MATERIALIZED VIEW CONCURRENTLY deck_view;
$$;

-- Lambda reads deck_view as app_user to populate the swipe deck.
-- refresh_deck_view() is intentionally NOT granted to app_user.
GRANT SELECT ON deck_view TO app_user;
