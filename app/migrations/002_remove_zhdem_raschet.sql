-- ---------------------------------------------------------------------------
-- Migration 002: historical no-op (kept to preserve the migration chain).
--
-- This step originally trimmed one seeded stage from the pipeline. With the
-- current generic seed there is nothing to remove, so it only advances the
-- schema version.
-- ---------------------------------------------------------------------------

UPDATE app_meta SET value = '2' WHERE key = 'schema_version';
