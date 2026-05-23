-- Migration 0006: create bookmarks and blocks tables
--
-- Creates two relationship tables per architecture.md §5.1.
--
-- bookmarks
-- ---------
-- Tracks users that a given user has bookmarked (saved) for later review.
-- Columns per §5.1:
--   user_id            — the user doing the bookmarking, FK → users(user_id) ON DELETE CASCADE
--   bookmarked_user_id — the user being bookmarked,     FK → users(user_id) ON DELETE CASCADE
--   created_at         — audit timestamp, default NOW()
-- Constraints:
--   PRIMARY KEY (user_id, bookmarked_user_id) — composite PK, one bookmark per pair
-- Indexes:
--   idx_bookmarks_target — on bookmarked_user_id; supports "who bookmarked me?" queries
--
-- blocks
-- ------
-- Records unidirectional block relationships.  Enforced bidirectionally in queries
-- (§5.1 note: if A blocks B, neither sees the other in match/search/chat).
-- Columns per §5.1:
--   blocker_id — the user initiating the block, FK → users(user_id) ON DELETE CASCADE
--   blocked_id — the user being blocked,        FK → users(user_id) ON DELETE CASCADE
--   created_at — audit timestamp, default NOW()
-- Constraints:
--   PRIMARY KEY (blocker_id, blocked_id) — composite PK, one block per directed pair
-- Indexes:
--   idx_blocks_blocked — on blocked_id; supports "who has blocked me?" and
--                        bilateral exclusion queries used in match/chat

CREATE TABLE bookmarks (
    user_id              UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    bookmarked_user_id   UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, bookmarked_user_id)
);

CREATE INDEX idx_bookmarks_target ON bookmarks (bookmarked_user_id);

CREATE TABLE blocks (
    blocker_id   UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    blocked_id   UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (blocker_id, blocked_id)
);

CREATE INDEX idx_blocks_blocked ON blocks (blocked_id);
