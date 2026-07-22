-- ---------------------------------------------------------------------------
-- Migration 004: LLM triage of the Inbox (Gemini).
-- Additive operations only (ALTER/CREATE/INSERT/UPDATE); no existing
-- table/row is recreated or touched.
-- ---------------------------------------------------------------------------

-- LLM-suggested note type (same enum as note_type). NULL on existing rows is
-- allowed: the CHECK passes on NULL.
ALTER TABLE notes ADD COLUMN suggested_note_type TEXT
  CHECK (suggested_note_type IN ('status','task','agreement','reminder'));

-- LLM-generated draft text for the note.
ALTER TABLE notes ADD COLUMN llm_draft TEXT;

-- Gemini call log: metadata only, no prompt/response content.
CREATE TABLE llm_calls (
  id            INTEGER PRIMARY KEY,
  created_at    TEXT    NOT NULL,              -- UTC ISO YYYY-MM-DDTHH:MM:SS
  model         TEXT    NOT NULL,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  duration_ms   INTEGER NOT NULL DEFAULT 0,
  status        TEXT    NOT NULL CHECK (status IN ('success','error')),
  purpose       TEXT    NOT NULL DEFAULT 'parse_note'
);
CREATE INDEX idx_llm_calls_created ON llm_calls(created_at);

-- Default LLM confidence threshold (string form of DEFAULT_CONFIDENCE_THRESHOLD).
INSERT INTO app_meta (key, value) VALUES
  ('llm_confidence_threshold', '0.7');

UPDATE app_meta SET value = '4' WHERE key = 'schema_version';
