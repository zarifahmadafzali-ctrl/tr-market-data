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
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, provider, day)
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
