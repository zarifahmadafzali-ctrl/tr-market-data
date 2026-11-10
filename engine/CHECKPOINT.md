# CHECKPOINT — TR Market Data: Worker + Actions engine

Direction: Cloudflare Worker webhook + GitHub Actions engine + private index repo.
Render polling is NOT extended in this phase.

## Status
- [x] P1a  engine core (`engine/`): queue store, notify, run loop, deadline + continuation
- [x] P1b  CLI `scripts/actions_engine.py` + workflow `.github/workflows/engine.yml`
- [x] P1c  unit tests for queue + run loop (fakes, no network)
- [ ] P1d  owner pilot: secrets on Actions, one day then one week, measure minutes/day
- [x] P2a  Cloudflare Worker (`worker/`): webhook, admin, manifest hit, queue+dispatch
- [x] P2b  Worker pure tests + deploy brief (setWebhook after stopping Render poll)
- [ ] P3   disable Render polling

## Layout
- `engine/github_api.py` — Contents API (stdlib)
- `engine/queue_store.py` — queue/*.json
- `engine/notify.py` — Telegram notify
- `engine/config.py` — env
- `engine/run.py` — claim → day pipeline → archive → pause/redispatch
- `scripts/actions_engine.py` — Actions entry
- `.github/workflows/engine.yml`

## Secrets (Actions)
TELEGRAM_BOT_TOKEN, TELEGRAM_ARCHIVE_CHANNEL_ID, ARCHIVE_MANIFEST_REPO,
ARCHIVE_MANIFEST_TOKEN (contents:write on index repo). Optional TELEGRAM_ADMIN_IDS.
