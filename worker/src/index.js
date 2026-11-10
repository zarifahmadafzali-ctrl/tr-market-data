/**
 * Cloudflare Worker — Telegram webhook for TR Market Data (Phase 2).
 * Manifest hit → sendDocument; miss → queue JSON + workflow_dispatch.
 */
const TG = "https://api.telegram.org";

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function parseAdmins(raw) {
  const set = new Set();
  for (const p of String(raw || "").split(/[,;]/)) {
    const n = parseInt(p.trim(), 10);
    if (!Number.isNaN(n)) set.add(n);
  }
  return set;
}

async function tg(token, method, body) {
  const res = await fetch(`${TG}/bot${token}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

async function ghGetJson(repo, path, token, branch = "main") {
  const url = `https://api.github.com/repos/${repo}/contents/${path}?ref=${branch}`;
  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "tr-market-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
  if (res.status === 404) return { ok: false, missing: true };
  if (!res.ok) return { ok: false, status: res.status, text: await res.text() };
  const data = await res.json();
  const raw = atob(String(data.content || "").replace(/\n/g, ""));
  return { ok: true, json: JSON.parse(raw || "{}"), sha: data.sha };
}

async function ghPutJson(repo, path, obj, sha, token, message, branch = "main") {
  const content = btoa(unescape(encodeURIComponent(JSON.stringify(obj, null, 2))));
  const body = { message, content, branch };
  if (sha) body.sha = sha;
  const res = await fetch(`https://api.github.com/repos/${repo}/contents/${path}`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "tr-market-worker",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  return { ok: res.ok || res.status === 201, status: res.status, text: await res.text() };
}

async function dispatchEngine(repo, workflowId, token, requestId) {
  const res = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflowId}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "tr-market-worker",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { request_id: requestId || "" } }),
    }
  );
  return { ok: res.status === 204 || res.ok, status: res.status };
}

function normalizeSymbol(sym) {
  const s = String(sym || "").trim().toUpperCase();
  const map = { US30: "USA30IDXUSD", USA30: "USA30IDXUSD", DJI: "USA30IDXUSD", "US30.CASH": "USA30IDXUSD" };
  return map[s] || s;
}

function findArtifacts(manifest, symbol, day) {
  const sym = normalizeSymbol(symbol);
  const pk = `dukascopy|${sym}|${day}`;
  const pkg = (manifest.packages || {})[pk];
  if (!pkg || !pkg.artifacts) return null;
  const out = {};
  for (const [name, art] of Object.entries(pkg.artifacts)) {
    const fid = art && (art.file_id || art.telegram_file_id);
    if (fid) out[name] = fid;
  }
  return Object.keys(out).length ? out : null;
}

function parseGet(text) {
  const parts = text.trim().split(/\s+/);
  if (parts.length < 3) return null;
  const symbol = parts[1];
  const day = parts[2];
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const kind = (parts[3] || "both").toLowerCase();
  return { symbol, day, kind };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/health")) {
      return json({ status: "ok", service: "tr-market-data-worker" });
    }
    if (request.method !== "POST" || url.pathname !== "/telegram") {
      return json({ error: "not found" }, 404);
    }
    const secret = env.TELEGRAM_WEBHOOK_SECRET;
    if (secret) {
      const hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
      if (hdr !== secret) return json({ error: "unauthorized" }, 401);
    }
    const token = env.TELEGRAM_BOT_TOKEN;
    if (!token) return json({ error: "misconfigured" }, 500);
    let update;
    try {
      update = await request.json();
    } catch {
      return json({ error: "bad json" }, 400);
    }
    const msg = update.message || update.edited_message;
    if (!msg || !msg.text) return json({ ok: true, ignored: true });
    const chatId = msg.chat.id;
    const userId = msg.from && msg.from.id;
    const admins = parseAdmins(env.TELEGRAM_ADMIN_IDS);
    const text = String(msg.text || "");
    if (admins.size && userId && !admins.has(userId)) {
      await tg(token, "sendMessage", { chat_id: chatId, text: "Unauthorized." });
      return json({ ok: true, unauthorized: true });
    }
    if (text.startsWith("/start") || text.startsWith("/help")) {
      await tg(token, "sendMessage", {
        chat_id: chatId,
        text:
          "TR Market Data (Worker)\n/get SYMBOL YYYY-MM-DD [both|tick|1s]\n/status\nArchived days send immediately; others queue to GitHub Actions.",
      });
      return json({ ok: true });
    }
    if (text.startsWith("/status")) {
      await tg(token, "sendMessage", {
        chat_id: chatId,
        text: "Worker online. Download engine runs on GitHub Actions.",
      });
      return json({ ok: true });
    }
    if (text.startsWith("/get")) {
      const parsed = parseGet(text);
      if (!parsed) {
        await tg(token, "sendMessage", {
          chat_id: chatId,
          text: "Usage: /get SYMBOL YYYY-MM-DD [both|tick|1s]",
        });
        return json({ ok: true });
      }
      const repo = env.ARCHIVE_MANIFEST_REPO;
      const mtoken = env.ARCHIVE_MANIFEST_TOKEN;
      const mpath = env.ARCHIVE_MANIFEST_PATH || "archive_manifest.json";
      if (!repo || !mtoken) {
        await tg(token, "sendMessage", { chat_id: chatId, text: "Manifest not configured." });
        return json({ ok: true, error: "no_manifest_config" });
      }
      const man = await ghGetJson(repo, mpath, mtoken);
      if (man.ok) {
        const arts = findArtifacts(man.json, parsed.symbol, parsed.day);
        if (arts) {
          const wantTick = parsed.kind === "both" || parsed.kind === "tick";
          const want1s = parsed.kind === "both" || parsed.kind === "1s";
          const order = [];
          if (wantTick && arts["ticks.parquet"]) order.push(["ticks.parquet", arts["ticks.parquet"]]);
          if (want1s && arts["bars_1s.parquet"]) order.push(["bars_1s.parquet", arts["bars_1s.parquet"]]);
          if (arts["metadata.json"]) order.push(["metadata.json", arts["metadata.json"]]);
          if (order.length) {
            for (const [name, fileId] of order) {
              await tg(token, "sendDocument", {
                chat_id: chatId,
                document: fileId,
                caption: `${normalizeSymbol(parsed.symbol)} ${parsed.day} · ${name}`,
              });
            }
            return json({ ok: true, delivered: order.map((x) => x[0]) });
          }
        }
      }
      const id = crypto.randomUUID().replace(/-/g, "");
      const reqBody = {
        id,
        created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"),
        requester_chat_id: chatId,
        requester_user_id: userId,
        symbol: normalizeSymbol(parsed.symbol),
        start: parsed.day,
        end: parsed.day,
        kind: parsed.kind === "tick" || parsed.kind === "1s" ? parsed.kind : "both",
        status: "queued",
        attempts: 0,
      };
      const put = await ghPutJson(
        repo,
        `queue/${id}.json`,
        reqBody,
        null,
        mtoken,
        `queue: add ${id} ${reqBody.symbol} ${reqBody.start}`
      );
      if (!put.ok) {
        await tg(token, "sendMessage", {
          chat_id: chatId,
          text: `Failed to enqueue (${put.status}). Try again later.`,
        });
        return json({ ok: true, error: "enqueue_failed", detail: put.status });
      }
      const dispatchRepo = env.ENGINE_DISPATCH_REPO || "zarifahmadafzali-ctrl/tr-market-data";
      const dtoken = env.ENGINE_DISPATCH_TOKEN || mtoken;
      const wf = env.ENGINE_WORKFLOW_ID || "engine.yml";
      const dis = await dispatchEngine(dispatchRepo, wf, dtoken, id);
      await tg(token, "sendMessage", {
        chat_id: chatId,
        text:
          `Queued ${reqBody.symbol} ${reqBody.start}.\nrequest_id=${id}\n` +
          `engine_dispatch=${dis.ok ? "ok" : "failed:" + dis.status}\n` +
          `You will be notified when the Actions job finishes.`,
      });
      return json({ ok: true, queued: id, dispatch: dis.ok });
    }
    await tg(token, "sendMessage", { chat_id: chatId, text: "Unknown command. Try /help" });
    return json({ ok: true });
  },
};
