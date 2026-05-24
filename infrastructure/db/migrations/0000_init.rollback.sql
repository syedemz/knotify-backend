-- Rollback for 0000_init
--
-- Removes the harness sentinel table entirely, returning the database to
-- a clean state (aside from yoyo's own _yoyo_* tracking tables, which
-- yoyo manages separately).

DROP TABLE IF EXISTS _schema_init;
