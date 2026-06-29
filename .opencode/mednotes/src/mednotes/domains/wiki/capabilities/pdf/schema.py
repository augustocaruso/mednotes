"""SQLite schema for the PDF library."""
from __future__ import annotations

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  pdf_sha256 TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  filename TEXT NOT NULL,
  title_guess TEXT NOT NULL DEFAULT '',
  page_count INTEGER NOT NULL,
  has_text_layer INTEGER NOT NULL DEFAULT 0,
  ocr_status TEXT NOT NULL CHECK (ocr_status IN ('not_needed','needed','blocked','partial','complete','failed')),
  ocr_error_code TEXT NOT NULL DEFAULT '',
  ocr_retry_eligible INTEGER NOT NULL DEFAULT 0 CHECK (ocr_retry_eligible IN (0, 1)),
  source_mtime_ns INTEGER NOT NULL,
  source_size_bytes INTEGER NOT NULL,
  indexed_at TEXT NOT NULL,
  removed_at TEXT
);

CREATE TABLE IF NOT EXISTS pages (
  pdf_sha256 TEXT NOT NULL REFERENCES documents(pdf_sha256) ON DELETE CASCADE,
  page_number INTEGER NOT NULL,
  text_source TEXT NOT NULL CHECK (text_source IN ('digital','ocr','none')),
  text TEXT NOT NULL DEFAULT '',
  ocr_confidence REAL,
  ocr_status TEXT NOT NULL CHECK (ocr_status IN ('not_needed','needed','blocked','partial','complete','failed')),
  ocr_error_code TEXT NOT NULL DEFAULT '',
  ocr_retry_eligible INTEGER NOT NULL DEFAULT 0 CHECK (ocr_retry_eligible IN (0, 1)),
  section_path_guess TEXT NOT NULL DEFAULT '[]',
  page_thumbnail_path TEXT NOT NULL DEFAULT '',
  width_px INTEGER NOT NULL DEFAULT 0,
  height_px INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (pdf_sha256, page_number)
);

CREATE TABLE IF NOT EXISTS figures (
  figure_uid TEXT PRIMARY KEY,
  pdf_sha256 TEXT NOT NULL REFERENCES documents(pdf_sha256) ON DELETE CASCADE,
  page_number INTEGER NOT NULL,
  bbox_json TEXT NOT NULL DEFAULT '[]',
  crop_path TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  image_sha256 TEXT NOT NULL DEFAULT '',
  figure_id TEXT NOT NULL DEFAULT '',
  display_label TEXT NOT NULL DEFAULT '',
  caption TEXT NOT NULL DEFAULT '',
  caption_bbox_json TEXT NOT NULL DEFAULT '[]',
  visual_quality_score REAL NOT NULL DEFAULT 0,
  evidence_level TEXT NOT NULL CHECK (evidence_level IN ('caption_and_mentions','caption_only','mentions_only','page_context_only','visual_only')),
  is_low_confidence INTEGER NOT NULL DEFAULT 1,
  conflict_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mentions (
  mention_uid TEXT PRIMARY KEY,
  pdf_sha256 TEXT NOT NULL REFERENCES documents(pdf_sha256) ON DELETE CASCADE,
  figure_uid TEXT REFERENCES figures(figure_uid) ON DELETE SET NULL,
  page_number INTEGER NOT NULL,
  figure_id TEXT NOT NULL DEFAULT '',
  display_label TEXT NOT NULL DEFAULT '',
  sentence TEXT NOT NULL DEFAULT '',
  paragraph TEXT NOT NULL DEFAULT '',
  section_path_guess TEXT NOT NULL DEFAULT '[]',
  offset_start INTEGER NOT NULL DEFAULT 0,
  offset_end INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anchor_cache (
  cache_key TEXT NOT NULL,
  note_sha256 TEXT NOT NULL,
  anchor_id TEXT NOT NULL,
  note_path TEXT NOT NULL DEFAULT '',
  section_path_json TEXT NOT NULL DEFAULT '[]',
  concept TEXT NOT NULL,
  visual_type TEXT NOT NULL,
  search_queries_json TEXT NOT NULL DEFAULT '[]',
  provider TEXT NOT NULL DEFAULT '',
  model_id TEXT NOT NULL DEFAULT '',
  preferred_language TEXT NOT NULL DEFAULT '',
  max_anchors INTEGER NOT NULL DEFAULT 0,
  prompt_version TEXT NOT NULL DEFAULT 'pdf-library-anchors-v1',
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (cache_key, anchor_id)
);

CREATE TABLE IF NOT EXISTS search_receipts (
  receipt_uid TEXT PRIMARY KEY,
  query_text TEXT NOT NULL DEFAULT '',
  note_path TEXT NOT NULL DEFAULT '',
  anchor_id TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT 'local',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embedding_runs (
  run_uid TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  dimensions INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'reserved',
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
  pdf_sha256 UNINDEXED,
  page_number UNINDEXED,
  title_guess,
  section_path_guess,
  text,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS figure_fts USING fts5(
  figure_uid UNINDEXED,
  pdf_sha256 UNINDEXED,
  figure_id,
  display_label,
  caption,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS mention_fts USING fts5(
  mention_uid UNINDEXED,
  pdf_sha256 UNINDEXED,
  figure_id,
  sentence,
  paragraph,
  section_path_guess,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(path);
CREATE INDEX IF NOT EXISTS idx_pages_pdf_page ON pages(pdf_sha256, page_number);
CREATE INDEX IF NOT EXISTS idx_figures_pdf_page ON figures(pdf_sha256, page_number);
CREATE INDEX IF NOT EXISTS idx_figures_figure_id ON figures(pdf_sha256, figure_id);
CREATE INDEX IF NOT EXISTS idx_mentions_figure_id ON mentions(pdf_sha256, figure_id);
CREATE INDEX IF NOT EXISTS idx_anchor_cache_note ON anchor_cache(note_sha256);
"""
