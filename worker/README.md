# TR Market Data — Cloudflare Worker (Phase 2)

**Worker name (must match Cloudflare Builds):** `tr-market-data`

Always-on Telegram **webhook**. Downloads run on GitHub Actions (`engine.yml` in this repo).

## What it does

1. Receives Telegram updates at `POST /telegram`
2. Admin check when `TELEGRAM_ADMIN_IDS` is set
3. `/get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both]`
4. If **single day** is in GitHub `archive_manifest.json` → `sendDocument(file_id)` immediately
5. Else write `queue/{id}.json` on the private index repo and `workflow_dispatch` the engine

## Configuration

### Cloudflare Runtime Variables (non-secret)

Set in the dashboard **or** via `[vars]` in `wrangler.toml` (already present for the repos):

| Variable | Expected value |
|----------|----------------|
| `ARCHIVE_MANIFEST_REPO` | `zarifahmadafzali-ctrl/tr-market-archive-index` |
| `ENGINE_DISPATCH_REPO` | `zarifahmadafzali-ctrl/tr-market-data` |
| `ARCHIVE_MANIFEST_BRANCH` | `main` |
| `ARCHIVE_MANIFEST_PATH` | `archive_manifest.json` |
| `ENGINE_WORKFLOW_ID` | `engine.yml` |
| `TELEGRAM_ADMIN_IDS` | Comma-separated numeric Telegram user IDs (set in **dashboard**, not committed) |

### Cloudflare Secrets (sensitive — never in git)

Exact names expected by `src/index.mjs`:

| Secret | Purpose |
|--------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API |
| `TELEGRAM_WEBHOOK_SECRET` | Validates `X-Telegram-Bot-Api-Secret-Token` on webhook requests |
| `ARCHIVE_MANIFEST_TOKEN` | GitHub PAT: `contents:write` on the **index** repo (manifest + `queue/`) |
| `ENGINE_DISPATCH_TOKEN` | GitHub PAT: `actions:write` (or workflow dispatch) on **this** code repo |

```bash
cd worker
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET
npx wrangler secret put ARCHIVE_MANIFEST_TOKEN
npx wrangler secret put ENGINE_DISPATCH_TOKEN
```

If a token was ever printed in a Cloudflare or CI build log, **rotate it** in GitHub and update the Cloudflare secret. Do not paste tokens into chat or commit them.

## Deploy

```bash
cd worker
npx wrangler deploy
```

Local `[vars]` keep repo paths aligned with the dashboard so deploys do not strip those settings.

## Webhook (only after Render polling is stopped)

Two consumers on the same bot token cause `Conflict: terminated by other getUpdates request`. Suspend or stop Render polling **before** setWebhook.

```text
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://tr-market-data.<your-subdomain>.workers.dev/telegram&secret_token=<TELEGRAM_WEBHOOK_SECRET>
```

Health: `GET https://tr-market-data.<your-subdomain>.workers.dev/health`

## Local pure tests

```bash
node --test worker/test/worker_logic.test.mjs
```

## Phase 3 (later)

Disable `bot.web` Telegram polling permanently once the Worker is confirmed live.
