-- Migration 0001: enable PostgreSQL extensions
--
-- Enables three extensions required by the application schema:
--   vector    — pgvector, for preference_vector similarity ranking (story 2.3)
--   pg_trgm   — trigram indexes, for future fuzzy-search support
--   pgcrypto  — cryptographic functions, used by gen_random_uuid() in later tables
--
-- All three are CREATE EXTENSION–only extensions; none requires
-- shared_preload_libraries (confirmed: pgvector on Aurora Postgres 16 and
-- pg_trgm/pgcrypto are extension-only). The local test container is
-- pgvector/pgvector:0.8.2-pg16, which bundles the vector extension.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
