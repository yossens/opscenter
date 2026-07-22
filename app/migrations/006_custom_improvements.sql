-- ---------------------------------------------------------------------------
-- Migration 006: usability and functionality improvements.
--   (a) FK-safe rebuild of notes: +is_pinned, +ocr_text, note_type/
--       suggested_note_type include 'info', both existing CHECKs preserved.
--   (b) notes_fts → two-column external-content FTS5 (body, ocr_text) with
--       native triggers notes_ai/notes_ad/notes_au.
--   (c) schema_version → '6'.
--
-- PRAGMA ordering is critical (see Design → Migration mechanics, points 2-3):
--   PRAGMA foreign_keys=OFF must be the FIRST statement (in autocommit, before
--   BEGIN) — inside a transaction it is a no-op, and DROP TABLE notes would then
--   cascade-delete attachments. PRAGMA foreign_keys=ON goes last, after COMMIT.
-- ---------------------------------------------------------------------------

PRAGMA foreign_keys=OFF;

BEGIN;

-- (a) Tear down the old notes FTS infrastructure before rebuilding the table.
DROP TRIGGER IF EXISTS notes_ai;
DROP TRIGGER IF EXISTS notes_ad;
DROP TRIGGER IF EXISTS notes_au;
DROP TABLE IF EXISTS notes_fts;

-- (b) Rebuild notes: new schema + explicit column-list data copy.
CREATE TABLE notes_new (
  id                INTEGER PRIMARY KEY,
  body              TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL DEFAULT 'inbox'
                    CHECK (status IN ('inbox','attached','archived','deferred')),
  deal_id           INTEGER REFERENCES deals(id),
  note_type         TEXT CHECK (note_type IN ('status','task','agreement','reminder','info')),
  created_at        TEXT NOT NULL,
  suggested_deal_id INTEGER REFERENCES deals(id),
  llm_confidence    REAL,
  llm_status        TEXT NOT NULL DEFAULT 'none'
                    CHECK (llm_status IN ('none','suggested','confirmed','rejected')),
  suggested_note_type TEXT CHECK (suggested_note_type IN ('status','task','agreement','reminder','info')),
  llm_draft         TEXT,
  is_pinned         INTEGER NOT NULL DEFAULT 0 CHECK (is_pinned IN (0, 1)),
  ocr_text          TEXT,
  -- The attachment invariant is preserved unchanged.
  CHECK ((status = 'attached') = (deal_id IS NOT NULL))
);

INSERT INTO notes_new
  (id, body, status, deal_id, note_type, created_at,
   suggested_deal_id, llm_confidence, llm_status, suggested_note_type, llm_draft)
SELECT
   id, body, status, deal_id, note_type, created_at,
   suggested_deal_id, llm_confidence, llm_status, suggested_note_type, llm_draft
FROM notes;

DROP TABLE notes;
ALTER TABLE notes_new RENAME TO notes;

-- (c) Recreate the notes indexes (they were bound to the old table).
CREATE INDEX idx_notes_status_created ON notes(status, created_at DESC);
CREATE INDEX idx_notes_deal ON notes(deal_id, created_at);

-- (d) Two-column external-content FTS5 + native triggers.
CREATE VIRTUAL TABLE notes_fts USING fts5(
  body, ocr_text, content='notes', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, body, ocr_text) VALUES (new.id, new.body, new.ocr_text);
END;
CREATE TRIGGER notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, body, ocr_text) VALUES ('delete', old.id, old.body, old.ocr_text);
END;
CREATE TRIGGER notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, body, ocr_text) VALUES ('delete', old.id, old.body, old.ocr_text);
  INSERT INTO notes_fts(rowid, body, ocr_text) VALUES (new.id, new.body, new.ocr_text);
END;
INSERT INTO notes_fts(notes_fts) VALUES('rebuild');

-- (e) Schema version.
UPDATE app_meta SET value = '6' WHERE key = 'schema_version';

PRAGMA foreign_key_check;

COMMIT;

PRAGMA foreign_keys=ON;
