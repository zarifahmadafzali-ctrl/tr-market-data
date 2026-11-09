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
| `db/` | SQLite catalog (symbols, days, status, Telegram file_ids) |
| `storage/` | Local + Telegram channel archive |
| `scheduler/` | Timed download jobs |
| `data/` | Local file workspace (gitignored payloads) |

## Stack (pilot)

- Python 3.11+
- Telegram Bot API
- Dukascopy historical ticks
- Parquet (compressed) for bulk files
- SQLite for catalog (Postgres later if needed)
- Private Telegram channel as durable file storage

## Principle

**Bot server ≠ durable file storage.**  
Downloaded datasets must survive bot restarts. Local `data/` is a cache. After every successful day the bot uploads the Parquet files to a private Telegram channel and stores the `file_id` in SQLite. On `/get` the bot prefers the archived `file_id` so delivery works even when the Render disk is empty.

## Quick start (local)

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — set TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_IDS, TELEGRAM_STORAGE_CHANNEL_ID

# Pilot: download one US30 day of ticks
python -m scripts.pilot_us30_day --date 2024-06-12
```

## Environment

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_IDS=1675868882
TELEGRAM_STORAGE_CHANNEL_ID=-100xxxxxxxxxx   # private channel, bot is admin
SQLITE_PATH=db/tr_market_data.sqlite
DATA_ROOT=data
BOT_PUBLIC_ACCESS=false
```

## Telegram channel storage

1. Create a private channel (or use an existing one).
2. Add the bot as administrator with **Post Messages**.
3. Get the channel ID (forward a message to @userinfobot → looks like `-100…`).
4. Set `TELEGRAM_STORAGE_CHANNEL_ID` in the environment.
5. After each successful download the bot posts:
   - `SYMBOL | YYYY-MM-DD | tick`
   - `SYMBOL | YYYY-MM-DD | 1s`
   - `SYMBOL | YYYY-MM-DD | meta`
6. `/get` then re-sends those files by `file_id` (no local disk required).

## Phase 2A — multi-day jobs

```bash
python -m scripts.run_range_job --symbol US30 --start 2024-06-10 --end 2024-06-12
```

- Skips days with tick+bars SUCCESS
- Hour-level resume for incomplete days
- Job states: QUEUED, RUNNING, PAUSED, COMPLETED, FAILED, CANCELLED
- Restart recovery: RUNNING → QUEUED

## Phase 2B — Telegram bot (admin-only)

Start (local):

```bash
python -m bot.app          # bot only
python -m bot.worker       # worker only
```

Or combined (Render):

```bash
python -m bot.web
```

Commands: `/start` `/status` `/symbols` `/get` `/jobs` `/cancel` `/help`

## Render (Free Web Service)

**Build Command**
```
pip install -r requirements.txt
```

**Start Command**
```
python -m bot.web
```

Health check: `https://<your-service>.onrender.com/health`

Notes:
- Disk is ephemeral — that is why channel archive exists.
- Prefer a longer uptime check interval (5–10 min) to avoid extra load.
- Run **one** instance only (Telegram allows a single `getUpdates` consumer).

## License

Private project — all rights reserved.
