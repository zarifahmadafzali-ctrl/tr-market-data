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

