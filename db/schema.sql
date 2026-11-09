-- Catalog only. Bulk market data lives in Parquet files under data/
-- and is also archived to a private Telegram channel via file_id.

CREATE TABLE IF NOT EXISTS symbols (
  symbol TEXT PRIMARY KEY,
  dukascopy_id TEXT NOT NULL,
  description TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS download_days (
  symbol TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'dukascopy',
  day TEXT NOT NULL,
  status TEXT NOT NULL,
  tick_status TEXT,
  bars_1s_status TEXT,
  tick_count INTEGER DEFAULT 0,
  bars_1s_count INTEGER DEFAULT 0,
  successful_hours INTEGER DEFAULT 0,
  empty_hours INTEGER DEFAULT 0,
  failed_hours INTEGER DEFAULT 0,
  tick_file_path TEXT,
  bars_1s_file_path TEXT,
  metadata_path TEXT,
  -- Telegram channel file_ids (survive local disk loss)
  tick_file_id TEXT,
  bars_1s_file_id TEXT,
  metadata_file_id TEXT,
  storage_message_ids TEXT,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, provider, day)
);

-- Per-hour status for resume without redownloading successful hours.
CREATE TABLE IF NOT EXISTS download_hours (
  symbol TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'dukascopy',
  day TEXT NOT NULL,
  hour INTEGER NOT NULL,
  status TEXT NOT NULL,
  tick_count INTEGER DEFAULT 0,
  http_status INTEGER,
  attempts INTEGER DEFAULT 0,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, provider, day, hour)
);

-- Multi-day / multi-symbol download jobs (survive process restart).
CREATE TABLE IF NOT EXISTS download_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT 'range',
  symbols_json TEXT NOT NULL,
  start_day TEXT NOT NULL,
  end_day TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'dukascopy',
  status TEXT NOT NULL,
  progress_json TEXT,
  current_symbol TEXT,
  current_day TEXT,
  current_hour INTEGER,
  retry_count INTEGER DEFAULT 0,
  max_retries INTEGER DEFAULT 3,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_range
  ON download_jobs (symbols_json, start_day, end_day, provider)
  WHERE status IN ('QUEUED', 'RUNNING', 'PAUSED');

CREATE TABLE IF NOT EXISTS job_day_results (
  job_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  day TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (job_id, symbol, day),
  FOREIGN KEY (job_id) REFERENCES download_jobs(id)
);

-- Telegram channel archive (metadata only; blobs live on Telegram).
CREATE TABLE IF NOT EXISTS archive_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  package_key TEXT NOT NULL,
  symbol TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'dukascopy',
  day TEXT NOT NULL,
  status TEXT NOT NULL,
  artifact_expected INTEGER NOT NULL DEFAULT 0,
  artifact_indexed INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,
  max_retries INTEGER NOT NULL DEFAULT 8,
  next_attempt_at TEXT,
  last_error TEXT,
  requester_chat_id INTEGER,
  requester_user_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (package_key)
);

CREATE INDEX IF NOT EXISTS idx_archive_jobs_status
  ON archive_jobs (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS archived_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  archive_job_id INTEGER NOT NULL,
  artifact_key TEXT NOT NULL,
  symbol TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'dukascopy',
  day TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  file_name TEXT NOT NULL,
  local_path TEXT,
  size_bytes INTEGER,
  sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  max_retries INTEGER NOT NULL DEFAULT 8,
  next_attempt_at TEXT,
  last_error TEXT,
  telegram_file_id TEXT,
  telegram_file_unique_id TEXT,
  telegram_message_id INTEGER,
  telegram_chat_id INTEGER,
  uploaded_at TEXT,
  indexed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (artifact_key),
  UNIQUE (provider, symbol, day, artifact_type),
  FOREIGN KEY (archive_job_id) REFERENCES archive_jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_archived_artifacts_status
  ON archived_artifacts (status, next_attempt_at);

CREATE INDEX IF NOT EXISTS idx_archived_artifacts_sha
  ON archived_artifacts (sha256);

CREATE TABLE IF NOT EXISTS archive_upload_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id INTEGER NOT NULL,
  attempt_no INTEGER NOT NULL,
  status TEXT NOT NULL,
  local_sha256 TEXT NOT NULL,
  telegram_file_id TEXT,
  telegram_message_id INTEGER,
  error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  FOREIGN KEY (artifact_id) REFERENCES archived_artifacts(id)
);
