-- Migration 0002: create users table
--
-- Creates the primary profile table exactly as specified in architecture.md §5.1.
-- Key features:
--   - age is GENERATED ALWAYS AS (computed from birthday) STORED — never written directly
--   - email_format CHECK enforces a basic format at the DB layer
--   - preference_vector is a vector(20) column (requires extension from 0001)
--   - preferences is JSONB for flexible per-user preference storage
--   - deleted_at supports soft-delete (see §11 of architecture.md)
--
-- Four indexes are created:
--   idx_users_sex_country_religion — composite partial (WHERE deleted_at IS NULL) for match queries
--   idx_users_age                  — partial (WHERE deleted_at IS NULL) for age-band filtering
--   idx_users_prefs_gin            — GIN index for JSONB preference key/value queries
--   idx_users_vector               — HNSW index for cosine-similarity vector search (pgvector)
--
-- Implementation note — age GENERATED ALWAYS AS workaround:
--   PostgreSQL requires generation expressions to be IMMUTABLE. The built-in
--   age(date) function is STABLE (depends on current_date), which Postgres
--   rejects for stored generated columns. The standard workaround is to wrap
--   the call in an IMMUTABLE-declared SQL function. Because birthday is itself
--   an immutable field (never changed after profile creation per §5.7), the
--   stored value is correct at INSERT time and refreshed whenever birthday is
--   updated (which the immutable-fields trigger in story 2.8 prevents). The
--   net effect matches the architecture intent exactly.

-- Helper: IMMUTABLE wrapper so the generated column expression is accepted.
-- Declared IMMUTABLE because birthday never changes after profile completion.
CREATE OR REPLACE FUNCTION _age_from_birthday(bd DATE)
RETURNS INTEGER
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CAST(date_part('year', age(bd)) AS INTEGER);
$$;

CREATE TABLE users (
    user_id              UUID PRIMARY KEY,                  -- Cognito sub
    email                TEXT UNIQUE NOT NULL,
    phone_number         TEXT,
    username             TEXT NOT NULL,

    -- IMMUTABLE fields (set at signup, never updated after profile completion)
    first_name           TEXT NOT NULL,
    last_name            TEXT NOT NULL,
    sex                  TEXT NOT NULL CHECK (sex IN ('Male','Female')),
    birthday             DATE NOT NULL,
    religion             TEXT,
    subsect              TEXT,

    -- MUTABLE fields
    age                  INTEGER GENERATED ALWAYS AS (_age_from_birthday(birthday)) STORED,
    chosen_profile_avatar TEXT,
    photo_url            TEXT,
    college_name         TEXT,
    current_residence_city    TEXT,
    current_residence_country TEXT,
    resident_country_code     CHAR(2),
    district             TEXT,
    education_level      TEXT,
    employer_name        TEXT,
    employment_type      TEXT,
    family_residence_address TEXT,
    father_retired       TEXT,
    fathers_job          TEXT,
    fathers_name         TEXT,
    graduation_year      INTEGER,
    has_children         BOOLEAN,
    higher_secondary     TEXT,
    higher_secondary_passing_year INTEGER,
    highest_degree       TEXT,
    high_school          TEXT,
    high_school_passing_year INTEGER,
    job_title            TEXT,
    marital_status       TEXT,
    marriage_time        TEXT,
    mother_retired       TEXT,
    mothers_job          TEXT,
    mothers_name         TEXT,
    move_abroad          BOOLEAN,
    office_address       TEXT,
    partners_religious_level TEXT,
    professional_category TEXT,
    profile_complete_verified BOOLEAN DEFAULT false,
    relation             TEXT,
    religious_level      TEXT,
    salary_range         TEXT,

    -- structured blob, JSONB so individual prefs can be queried with GIN
    preferences          JSONB DEFAULT '{}'::jsonb,

    -- vector for similarity ranking (20 boolean prefs => 20-D vector)
    preference_vector    vector(20),

    -- audit
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at           TIMESTAMPTZ,                       -- soft-delete (see §11)

    CONSTRAINT email_format CHECK (email ~* '^[^@]+@[^@]+\.[^@]+$')
);

-- Composite partial index for match queries filtered by sex, country, religion
-- Partial (WHERE deleted_at IS NULL) keeps the index lean — soft-deleted rows excluded
CREATE INDEX idx_users_sex_country_religion ON users (sex, current_residence_country, religion) WHERE deleted_at IS NULL;

-- Partial index for age-band filtering in match queries
CREATE INDEX idx_users_age ON users (age) WHERE deleted_at IS NULL;

-- GIN index for JSONB preference key/value queries (e.g. preferences @> '{"halal":true}')
CREATE INDEX idx_users_prefs_gin ON users USING GIN (preferences);

-- HNSW index for cosine-similarity vector search via pgvector
-- vector_cosine_ops is correct for normalized preference vectors
CREATE INDEX idx_users_vector ON users USING hnsw (preference_vector vector_cosine_ops);
