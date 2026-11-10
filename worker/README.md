# TR Market Data — Cloudflare Worker (Phase 2)

Always-on Telegram **webhook**. Downloads run on GitHub Actions (`engine.yml`).

## What it does

1. Receives Telegram updates at `POST /telegram`
2. Admin check (`TELEGRAM_ADMIN_IDS`)
3. `/get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both]`
4. If **single day** is in GitHub `archive_manifest.json` → `sendDocument(file_id)` now
5. Else write `queue/{id}.json` on the private index repo and `workflow_dispatch` the engine

## Secrets (Wrangler)

```bash
cd worker
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET   # random string; also used in setWebhook
npx wrangler secret put TELEGRAM_ADMIN_IDS       # comma-separated numeric ids
npx wrangler secret put ARCHIVE_MANIFEST_REPO    # owner/tr-market-archive-index
npx wrangler secret put ARCHIVE_MANIFEST_TOKEN   # PAT contents:write on index repo
npx wrangler secret put ENGINE_DISPATCH_REPO     # zarifahmadafzali-ctrl/tr-market-data
npx wrangler secret put ENGINE_DISPATCH_TOKEN    # PAT actions:write on code repo (or fine-grained)
npx wrangler deploy
```

Optional vars: `ARCHIVE_MANIFEST_PATH`, `ARCHIVE_MANIFEST_BRANCH`, `ENGINE_WORKFLOW_ID`.

## Deploy webhook (do this only when ready)

**Stop Render polling first** (suspend the Render service or change start command).  
Two consumers → `Conflict: terminated by other getUpdates request`.

```text
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<worker-name>.workers.dev/telegram&secret_token=<TELEGRAM_WEBHOOK_SECRET>
```

Health: `GET https://<worker-name>.workers.dev/health`

## Local pure tests

```bash
node --test worker/test/worker_logic.test.mjs
```

## Phase 3 (later)

Disable `bot.web` Telegram polling permanently once the Worker is confirmed live.
