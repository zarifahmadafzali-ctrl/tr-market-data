# TR Market Data — Cloudflare Worker (Phase 2)

Always-on Telegram webhook. Downloads run on GitHub Actions (`engine.yml`).

## Deploy

```bash
cd worker
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET
npx wrangler secret put TELEGRAM_ADMIN_IDS
npx wrangler secret put ARCHIVE_MANIFEST_REPO
npx wrangler secret put ARCHIVE_MANIFEST_TOKEN
npx wrangler secret put ENGINE_DISPATCH_REPO
npx wrangler secret put ENGINE_DISPATCH_TOKEN
npx wrangler deploy
```

Set webhook (stop Render polling first to avoid getUpdates conflict):

```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<worker>.workers.dev/telegram&secret_token=<SECRET>
```
