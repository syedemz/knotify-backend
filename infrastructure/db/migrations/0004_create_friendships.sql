-- Migration 0004: create friendships table
--
-- Creates the friendships table per architecture.md §5.1.
-- Stores one row per friendship pair, canonicalized so the smaller UUID is
-- always user_a. This eliminates duplicates (no (alice, bob) AND (bob, alice)).
--
-- The CHECK (user_a < user_b) constraint is enforced by Postgres at insert/update
-- time, rejecting any row where user_a >= user_b. Application code must
-- canonicalize the pair before inserting:
--   user_a = MIN(id_1, id_2), user_b = MAX(id_1, id_2)
--
-- Columns per §5.1:
--   user_a      — smaller UUID of the pair, FK → users(user_id) ON DELETE CASCADE
--   user_b      — larger UUID of the pair,  FK → users(user_id) ON DELETE CASCADE
--   created_at  — audit timestamp, default NOW()
--   PRIMARY KEY (user_a, user_b) — composite PK, also uniqueness guarantee
--   CHECK (user_a < user_b)      — canonical ordering enforced at DB level
--
-- Index:
--   idx_friendships_b — B-tree on user_b, supports the reverse-direction lookup
--     "SELECT user_a FROM friendships WHERE user_b = :id"
--     (the user_a direction is covered by the composite PK's leading column)

CREATE TABLE friendships (
    user_a       UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    user_b       UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_a, user_b),
    CHECK (user_a < user_b)
);

CREATE INDEX idx_friendships_b ON friendships (user_b);
