-- Catalog only. Bulk market data lives in Parquet files under data/.
CREATE TABLE IF NOT EXISTS symbols (
  symbol TEXT PRIMARY KEY,
  dukascopy_id TEXT NOT NULL,
  description TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS download_days (
  symbol TEXT NOT NULL,
  day TEXT NOT NULL,              -- YYYY-MM-DD (UTC calendar day for catalog)
  status TEXT NOT NULL,            -- pending|running|success|expected_empty|failed
  ticks INTEGER DEFAULT 0,
  bars_1s INTEGER DEFAULT 0,
  raw_path TEXT,
  parquet_path TEXT,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, day)
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
