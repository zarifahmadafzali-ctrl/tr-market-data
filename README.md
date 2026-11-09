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


## Legacy archive backfill (admin)

Telegram files uploaded **before** GitHub manifest support are not in the
durable index. After a Render disk wipe they look “missing” and `/get` would
queue a new Dukascopy download.

**Do not re-download.** Index the existing channel documents:

1. In the private archive channel, find the three files for the day
   (`ticks.parquet`, `bars_1s.parquet`, `metadata.json`).
2. In a private chat with the bot (admin account):

```text
/backfill US30 2026-11-07
```

3. Forward each of the three documents to the bot.
4. The bot registers each `file_id` into SQLite and the GitHub manifest.
5. Confirm:

```text
/get US30 2026-11-07 tick
```

You should see **Ready** and delivery **(archive)** with **no new job**.

If captions already contain `dukascopy|SYMBOL|YYYY-MM-DD|ticks_parquet`, you can
forward without `/backfill SYMBOL DAY` first.

`/backfill clear` exits context mode.


## License


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

## GitHub archive manifest (durable index)

Telegram stores the files (`file_id`). A **private** GitHub repository stores a
JSON index so Render can wipe local SQLite and still avoid re-downloading.

### Setup

1. Create a **private** empty GitHub repository (example: `your-user/tr-market-archive-index`).
2. Create a fine-grained PAT with **Contents: Read and write** on that repo only.
3. On Render set:

```
ARCHIVE_MANIFEST_ENABLED=true
ARCHIVE_MANIFEST_REPO=OWNER/PRIVATE_REPO
ARCHIVE_MANIFEST_BRANCH=main
ARCHIVE_MANIFEST_PATH=archive_manifest.json
ARCHIVE_MANIFEST_TOKEN=github_pat_...
```

Never commit the token. Minimum scopes only.

### Behaviour

1. After Telegram upload succeeds, the bot merges the artifact into the manifest
   via the GitHub Contents API (handles HTTP 409 by refetch + merge).
2. SQLite marks the artifact **INDEXED** only after the manifest write succeeds.
3. If the manifest write fails, SQLite keeps status **UPLOADED** with `file_id`
   and retries without re-uploading to Telegram.
4. On process start, the worker fetches the manifest and rebuilds SQLite archive
   rows (idempotent; does not clobber local in-flight uploads).
5. `/get` hydrates from the manifest when local SQLite has no INDEXED row, then
   delivers by `file_id`. Downloads run only when the artifact is absent from
   both indexes.

Existing archived Telegram files are **not** backfilled automatically; new
archives after enablement are indexed going forward.

### When GitHub is down

The bot keeps running on local SQLite. Manifest sync is retried later. Temporary
GitHub outages do not by themselves trigger Dukascopy re-downloads.
