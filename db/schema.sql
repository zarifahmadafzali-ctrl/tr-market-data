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
