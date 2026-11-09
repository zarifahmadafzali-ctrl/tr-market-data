# tr-market-data

**Independent** historical market-data downloader and Telegram distribution platform.

This repository is **not** part of Trading Replay.  
If Trading Replay is offline or being updated, this service can keep downloading and serving data.

## Architecture

| Component | Role |
|-----------|------|
| `bot/` | Telegram bot and user commands |
| `downloader/` | Dukascopy historical tick download |
| `processor/` | Tick → 1s / 1m / higher TF bars |
| `db/` | SQLite catalog (symbols, days, status) |
| `storage/` | Local + future cloud object storage |
| `scheduler/` | Timed download jobs |
| `data/` | Local file workspace (gitignored payloads) |

## Stack (pilot)

- Python 3.11+
- Telegram Bot API
- Dukascopy historical ticks
- Parquet (compressed) for bulk files
- SQLite for catalog (Postgres later if needed)

## Principle

**Bot server ≠ durable file storage.**  
Downloaded datasets must survive bot restarts. Start with local `data/` + SQLite; later attach independent object storage without rewriting the domain model.

## Quick start (local)

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env

# Pilot: download one US30 day of ticks (see scripts/)
python -m scripts.pilot_us30_day --date 2024-06-12
```

## Status

Scaffold only. Pilot downloader for US30 is the next executable step.

## License

Private project — all rights reserved.


## Phase 1 status

- Dukascopy historical tick download (ported logic)
- Raw ticks + true 1s mid OHLC Parquet
- SQLite catalog with hour classifications
- Pilot: `python -m scripts.pilot_us30_day --date YYYY-MM-DD`


## Phase 2A — multi-day jobs

```bash
python -m scripts.run_range_job --symbol US30 --start 2024-06-10 --end 2024-06-12
```

- Skips days with tick+bars SUCCESS
- Hour-level resume for incomplete days
- Job states: QUEUED, RUNNING, PAUSED, COMPLETED, FAILED, CANCELLED
- Restart recovery: RUNNING → QUEUED


## Phase 2B — Telegram bot (admin-only)

Environment (`.env`, never commit):

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_IDS=1675868882
SQLITE_PATH=db/tr_market_data.sqlite
DATA_ROOT=data
BOT_PUBLIC_ACCESS=false
```

Start bot (receives Telegram updates):

```bash
python -m bot.app
```

Start worker (processes QUEUED download jobs):

```bash
python -m bot.worker
```

Commands: `/start` `/status` `/symbols` `/get` `/jobs` `/cancel` `/help`

## Render (Free Web Service) — single process

Render **Web Service** requires an open HTTP port. Running only `python -m bot.app`
will poll Telegram but eventually fail with *No open ports detected* / *Timed Out*.

Use the combined entrypoint (health + Telegram bot + download worker):

**Build Command**
```
pip install -r requirements.txt
```

**Start Command**
```
python -m bot.web
```

**Environment**
```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_IDS=1675868882
SQLITE_PATH=db/tr_market_data.sqlite
DATA_ROOT=data
BOT_PUBLIC_ACCESS=false
```

Health check URL: `https://<your-service>.onrender.com/health`

Notes (Render Free):
- Service may **sleep** after inactivity; first Telegram reply can be slow after wake.
- Disk is **ephemeral** — Parquet/SQLite can be lost on redeploy or restart. Plan durable storage later.
- Run **one** instance only (Telegram allows a single `getUpdates` consumer).
- Prefer a **separate** Render account/workspace from Trading Replay so free hours are not shared.

Local equivalent of production start:
```
python -m bot.web
```


## Telegram private channel archive

Durable storage for completed Parquet/JSON artifacts. SQLite stores only
metadata and Telegram `file_id` references — never file bytes.

### Owner setup

1. Create a **private** Telegram channel.
2. Add the bot as **administrator** with **Post messages**.
3. Obtain the numeric channel id (typically `-100…`).
4. Set Render environment variables (never commit secrets):

```
TELEGRAM_ARCHIVE_ENABLED=true
TELEGRAM_ARCHIVE_CHANNEL_ID=-100xxxxxxxxxx
ARCHIVE_MAX_RETRIES=8
ARCHIVE_BACKOFF_BASE_SEC=30
ARCHIVE_BACKOFF_CAP_SEC=3600
ALLOW_DELETE_STAGING=false
```

Keep existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ADMIN_IDS`.

### Behaviour

1. After a day download reaches SUCCESS, archive jobs are enqueued per artifact
   (`ticks.parquet`, `bars_1s.parquet`, `metadata.json` — only files that exist).
2. The worker uploads sequentially to the private channel.
3. On success, SQLite stores `file_id`, `file_unique_id`, `message_id`, `chat_id`
   and marks the artifact **INDEXED**.
4. `/get` prefers INDEXED archive delivery by `file_id` (no disk required).
5. If the day is SUCCESS but archive incomplete, archival continues — **no** re-download.
6. Local disk is **staging**. Render Free disk is ephemeral; do not treat it as durable.
7. `ALLOW_DELETE_STAGING=true` only deletes local files after INDEXED (default false).

### Limits and recovery

- Telegram Bot API document limit ~50MB (enforced with margin).
- Uploads are **at-least-once**: a crash after Telegram accepts a file but before
  SQLite INDEXED may create a duplicate channel message on retry.
- Stale `UPLOADING` rows are recovered on worker start.
- Diagnose with `/status` (archive counts) and SQLite tables `archive_jobs`,
  `archived_artifacts`, `archive_upload_attempts`.

### Reprocess an artifact

Delete the corresponding `archived_artifacts` row (or set status back to PENDING
with cleared telegram fields) and ensure the local staging file exists, then let
the worker retry — or re-download the day if staging was lost.
