/**
 * Cloudflare Worker — Telegram webhook front door for TR Market Data (Phase 2).
 *
 * - Admin-only by default (TELEGRAM_ADMIN_IDS).
 * - Manifest hit → sendDocument(file_id) immediately.
 * - Miss → queue/{id}.json on private index repo + workflow_dispatch(engine.yml).
 *
 * IMPORTANT: Only one getUpdates consumer may run. Disable Render polling before
 * setWebhook, or Telegram returns 409 Conflict.
 */

const TG = "https://api.telegram.org";

export function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

export function parseAdmins(raw) {
  const set = new Set();
  for (const p of String(raw || "").split(/[,;]/)) {
    const n = parseInt(String(p).trim(), 10);
    if (!Number.isNaN(n)) set.add(n);
  }
  return set;
}

export function normalizeSymbol(sym) {
  const s = String(sym || "").trim().toUpperCase();
  const map = {
    US30: "USA30IDXUSD",
    USA30: "USA30IDXUSD",
    DJI: "USA30IDXUSD",
    "US30.CASH": "USA30IDXUSD",
  };
  return map[s] || s;
}

/** /get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both] */
export function parseGet(text) {
  const parts = String(text || "").trim().split(/\s+/);
  if (parts.length < 3) return null;
  const cmd = parts[0].split("@")[0].toLowerCase();
  if (cmd !== "/get") return null;
  const symbol = parts[1];
  const start = parts[2];
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start)) return null;
  let end = start;
  let kind = "both";
  if (parts[3] && /^\d{4}-\d{2}-\d{2}$/.test(parts[3])) {
    end = parts[3];
    if (parts[4]) kind = parts[4].toLowerCase();
  } else if (parts[3]) {
    kind = parts[3].toLowerCase();
  }
  if (end < start) return null;
  if (!["tick", "ticks", "1s", "bars", "both", "all"].includes(kind)) kind = "both";
  if (kind === "ticks") kind = "tick";
  if (kind === "bars" || kind === "all") kind = kind === "bars" ? "1s" : "both";
  return { symbol, start, end, kind };
}

export function findArtifacts(manifest, symbol, day) {
  const sym = normalizeSymbol(symbol);
  const pk = `dukascopy|${sym}|${day}`;
  const pkg = (manifest && manifest.packages && manifest.packages[pk]) || null;
  if (!pkg || !pkg.artifacts) return null;
  const out = {};
  for (const [name, art] of Object.entries(pkg.artifacts)) {
    const fid = art && (art.file_id || art.telegram_file_id);
    const st = art && art.status;
    if (fid && (!st || st === "INDEXED" || st === "UPLOADED")) out[name] = fid;
  }
  return Object.keys(out).length ? out : null;
}

export function pickArtifactsForKind(arts, kind) {
  if (!arts) return [];
  const order = [];
  const wantTick = kind === "tick" || kind === "both";
  const want1s = kind === "1s" || kind === "both";
  if (wantTick && arts["ticks.parquet"]) order.push(["ticks.parquet", arts["ticks.parquet"]]);
  if (want1s && arts["bars_1s.parquet"]) order.push(["bars_1s.parquet", arts["bars_1s.parquet"]]);
  if (arts["metadata.json"]) order.push(["metadata.json", arts["metadata.json"]]);
  return order;
}

async function tg(token, method, body) {
  const res = await fetch(`${TG}/bot${token}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json().catch(() => ({ ok: false }));
}

async function ghGetJson(repo, path, token, branch = "main") {
  const url = `https://api.github.com/repos/${repo}/contents/${path}?ref=${encodeURIComponent(branch)}`;
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
  try {
    return { ok: true, json: JSON.parse(raw || "{}"), sha: data.sha };
  } catch {
    return { ok: false, status: 500, text: "invalid json" };
  }
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
  return { ok: res.status === 200 || res.status === 201, status: res.status, text: await res.text() };
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

function helpText() {
  return (
    "TR Market Data (Worker)\n" +
    "/get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both]\n" +
    "Examples:\n" +
    "/get US30 2024-06-12\n" +
    "/get US30 2024-06-01 2024-06-07 both\n" +
    "Archived days are sent immediately; new days are queued for GitHub Actions."
  );
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
    if (!token) return json({ error: "misconfigured: TELEGRAM_BOT_TOKEN" }, 500);

    let update;
    try {
      update = await request.json();
    } catch {
      return json({ error: "bad json" }, 400);
    }

    const msg = update.message || update.edited_message;
    if (!msg || !msg.text) return json({ ok: true, ignored: true });

    const chatId = msg.chat && msg.chat.id;
    const userId = msg.from && msg.from.id;
    const text = String(msg.text || "").trim();
    const admins = parseAdmins(env.TELEGRAM_ADMIN_IDS);
    if (admins.size && userId != null && !admins.has(userId)) {
      await tg(token, "sendMessage", { chat_id: chatId, text: "Unauthorized." });
      return json({ ok: true, unauthorized: true });
    }

    const cmd = text.split(/\s+/)[0].split("@")[0].toLowerCase();
    if (cmd === "/start" || cmd === "/help") {
      await tg(token, "sendMessage", { chat_id: chatId, text: helpText() });
      return json({ ok: true, help: true });
    }

    if (cmd === "/get") {
      const parsed = parseGet(text);
      if (!parsed) {
        await tg(token, "sendMessage", {
          chat_id: chatId,
          text: "Usage: /get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both]",
        });
        return json({ ok: true, error: "bad_args" });
      }

      const repo = env.ARCHIVE_MANIFEST_REPO;
      const mtoken = env.ARCHIVE_MANIFEST_TOKEN;
      const branch = env.ARCHIVE_MANIFEST_BRANCH || "main";
      const mpath = env.ARCHIVE_MANIFEST_PATH || "archive_manifest.json";
      if (!repo || !mtoken) {
        await tg(token, "sendMessage", { chat_id: chatId, text: "Server misconfigured (manifest)." });
        return json({ ok: true, error: "misconfigured_manifest" });
      }

      // Single-day fast path: try manifest delivery
      if (parsed.start === parsed.end) {
        const man = await ghGetJson(repo, mpath, mtoken, branch);
        if (man.ok && man.json) {
          const arts = findArtifacts(man.json, parsed.symbol, parsed.start);
          const order = pickArtifactsForKind(arts, parsed.kind);
          if (order.length) {
            for (const [name, fileId] of order) {
              await tg(token, "sendDocument", {
                chat_id: chatId,
                document: fileId,
                caption: `${normalizeSymbol(parsed.symbol)} ${parsed.start} · ${name}`,
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
        start: parsed.start,
        end: parsed.end,
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
        `queue: add ${id} ${reqBody.symbol} ${reqBody.start}..${reqBody.end}`,
        branch
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
          `Queued ${reqBody.symbol} ${reqBody.start}..${reqBody.end}.\n` +
          `request_id=${id}\n` +
          `engine_dispatch=${dis.ok ? "ok" : "failed:" + dis.status}\n` +
          `You will be notified when the Actions job finishes.`,
      });
      return json({ ok: true, queued: id, dispatch: dis.ok });
    }

    await tg(token, "sendMessage", { chat_id: chatId, text: "Unknown command. Try /help" });
    return json({ ok: true });
  },
};
