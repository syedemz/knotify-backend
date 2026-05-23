-- Migration 0000: harness sentinel
--
-- This migration exists solely to validate that the yoyo tooling, the
-- local docker-compose container, and the psycopg2 driver are all wired
-- together correctly. It creates a lightweight housekeeping table that
-- carries no application semantics.
--
-- Real application migrations start at 0001 (story 2.3: enable extensions
-- and create the users table). Do not add application schema here.

CREATE TABLE IF NOT EXISTS _schema_init (
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    note       TEXT        NOT NULL DEFAULT 'harness sentinel'
);

INSERT INTO _schema_init (note) VALUES ('0000_init applied');
