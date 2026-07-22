-- Migration 001: full initial OpsCenter schema.
-- Tables, indexes, FTS5 (external content) + sync triggers, seed of the
-- default pipeline stages, and application metadata (app_meta).

-- ---------------------------------------------------------------------------
-- Application metadata / schema versioning
-- ---------------------------------------------------------------------------
CREATE TABLE app_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Pipeline stages
-- ---------------------------------------------------------------------------
CREATE TABLE stages (
  id             INTEGER PRIMARY KEY,
  name           TEXT    NOT NULL,
  position       INTEGER NOT NULL,
  threshold_days INTEGER NOT NULL DEFAULT 5,
  is_terminal    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX idx_stages_position ON stages(position);

-- ---------------------------------------------------------------------------
-- Items
-- ---------------------------------------------------------------------------
CREATE TABLE deals (
  id               INTEGER PRIMARY KEY,
  title            TEXT NOT NULL,
  company          TEXT,
  partner          TEXT,
  rate             REAL,
  jurisdiction     TEXT,
  waiting_on       TEXT,
  description      TEXT,
  stage_id         INTEGER NOT NULL REFERENCES stages(id),
  stage_entered_at TEXT NOT NULL,
  last_activity_at TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  closed_at        TEXT,
  drive_folder_url TEXT
);
CREATE INDEX idx_deals_stage ON deals(stage_id);

-- ---------------------------------------------------------------------------
-- Notes
-- ---------------------------------------------------------------------------
CREATE TABLE notes (
  id                INTEGER PRIMARY KEY,
  body              TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL DEFAULT 'inbox'
                    CHECK (status IN ('inbox','attached','archived','deferred')),
  deal_id           INTEGER REFERENCES deals(id),
  note_type         TEXT CHECK (note_type IN ('status','task','agreement','reminder')),
  created_at        TEXT NOT NULL,
  suggested_deal_id INTEGER REFERENCES deals(id),
  llm_confidence    REAL,
  llm_status        TEXT NOT NULL DEFAULT 'none'
                    CHECK (llm_status IN ('none','suggested','confirmed','rejected')),
  -- Attachment invariant: deal_id is set if and only if status='attached'.
  CHECK ((status = 'attached') = (deal_id IS NOT NULL))
);
CREATE INDEX idx_notes_status_created ON notes(status, created_at DESC);
CREATE INDEX idx_notes_deal ON notes(deal_id, created_at);

-- ---------------------------------------------------------------------------
-- Attachments
-- ---------------------------------------------------------------------------
CREATE TABLE attachments (
  id            INTEGER PRIMARY KEY,
  note_id       INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  original_name TEXT NOT NULL,
  stored_name   TEXT NOT NULL UNIQUE,
  mime_type     TEXT NOT NULL DEFAULT 'application/octet-stream',
  size_bytes    INTEGER NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_attachments_note ON attachments(note_id);

-- ---------------------------------------------------------------------------
-- FTS5 (external content) + sync triggers
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE notes_fts USING fts5(
  body, content='notes', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE deals_fts USING fts5(
  title, company, partner, jurisdiction, waiting_on, description,
  content='deals', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);

-- notes: three sync triggers
CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', old.id, old.body);
  INSERT INTO notes_fts(rowid, body) VALUES (new.id, new.body);
END;

-- deals: three sync triggers
CREATE TRIGGER deals_ai AFTER INSERT ON deals BEGIN
  INSERT INTO deals_fts(rowid, title, company, partner, jurisdiction, waiting_on, description)
  VALUES (new.id, new.title, new.company, new.partner, new.jurisdiction, new.waiting_on, new.description);
END;
CREATE TRIGGER deals_ad AFTER DELETE ON deals BEGIN
  INSERT INTO deals_fts(deals_fts, rowid, title, company, partner, jurisdiction, waiting_on, description)
  VALUES ('delete', old.id, old.title, old.company, old.partner, old.jurisdiction, old.waiting_on, old.description);
END;
CREATE TRIGGER deals_au AFTER UPDATE ON deals BEGIN
  INSERT INTO deals_fts(deals_fts, rowid, title, company, partner, jurisdiction, waiting_on, description)
  VALUES ('delete', old.id, old.title, old.company, old.partner, old.jurisdiction, old.waiting_on, old.description);
  INSERT INTO deals_fts(rowid, title, company, partner, jurisdiction, waiting_on, description)
  VALUES (new.id, new.title, new.company, new.partner, new.jurisdiction, new.waiting_on, new.description);
END;

-- ---------------------------------------------------------------------------
-- Seed: default generic pipeline (6 stages, in order), threshold_days=5,
-- only "Done" is terminal.
-- ---------------------------------------------------------------------------
INSERT INTO stages (name, position, threshold_days, is_terminal) VALUES
  ('Backlog',      1,  5, 0),
  ('To Do',        2,  5, 0),
  ('In Progress',  3,  5, 0),
  ('Review',       4,  5, 0),
  ('Blocked',      5,  5, 0),
  ('Done',         6,  5, 1);

-- ---------------------------------------------------------------------------
-- app_meta: schema version + first-migration timestamps.
-- ---------------------------------------------------------------------------
INSERT INTO app_meta (key, value) VALUES
  ('schema_version', '1'),
  ('last_triage_at', strftime('%Y-%m-%dT%H:%M:%S', 'now')),
  ('first_run_at',   strftime('%Y-%m-%dT%H:%M:%S', 'now'));
