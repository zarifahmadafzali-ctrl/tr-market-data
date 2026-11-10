/**
 * Cloudflare Worker — Telegram webhook front door for TR Market Data (Phase 2).
 *
 * Commands + inline menu; /get still uses manifest fast-path and queue + Actions.
 * Durable jobs live in private index repo queue/*.json (not Render SQLite).
 */

const TG = "https://api.telegram.org";

/** Aliases aligned with downloader/symbols.py (Worker cannot import Python). */
export const SYMBOL_ALIASES = {
  US30: "USA30IDXUSD",
  USA30: "USA30IDXUSD",
  DJ30: "USA30IDXUSD",
  DJIA: "USA30IDXUSD",
  "US30.CASH": "USA30IDXUSD",
  DJI: "USA30IDXUSD",
  GOLD: "XAUUSD",
  XAUUSD: "XAUUSD",
  BTCUSD: "BTCUSD",
  EURUSD: "EURUSD",
};

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
  const s = String(sym || "").trim().toUpperCase().replace(/[/. ]/g, "");
  return SYMBOL_ALIASES[s] || s;
}

export function supportedSymbolsList() {
  const canonical = new Set(Object.values(SYMBOL_ALIASES));
  const lines = [];
  for (const [alias, can] of Object.entries(SYMBOL_ALIASES)) {
    if (alias === can) lines.push(can);
    else lines.push(`${alias} → ${can}`);
  }
  for (const c of canonical) {
    if (!lines.some((l) => l === c || l.endsWith(`→ ${c}`))) lines.push(c);
  }
  return [...new Set(lines)].sort();
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
  if (kind === "bars") kind = "1s";
  if (kind === "all") kind = "both";
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

export function mainMenuKeyboard() {
  return {
    inline_keyboard: [
      [
        { text: "Status", callback_data: "menu:status" },
        { text: "Symbols", callback_data: "menu:symbols" },
      ],
      [
        { text: "Get data", callback_data: "menu:get" },
        { text: "Jobs", callback_data: "menu:jobs" },
      ],
      [{ text: "Help", callback_data: "menu:help" }],
    ],
  };
}

export function welcomeText() {
  return (
    "TR Market Data Bot\n\n" +
    "Historical Dukascopy tick and 1-second OHLC data.\n" +
    "Completed files are archived in a private Telegram channel so they survive server restarts.\n" +
    "The download engine runs on GitHub Actions; this Worker is the always-on Telegram front door.\n\n" +
    "Use the menu buttons or /help for commands."
  );
}

export function helpText() {
  return (
    "Commands:\n" +
    "/start — welcome + menu\n" +
    "/status — Worker + queue/manifest summary\n" +
    "/symbols — supported symbols and aliases\n" +
    "/get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both]\n" +
    "/jobs — queue requests from the durable index repo\n" +
    "/help — this message\n\n" +
    "Examples:\n" +
    "/get US30 2024-06-12\n" +
    "/get US30 2024-06-01 2024-06-07 both\n\n" +
    "Archived single days are sent immediately via Telegram file_id.\n" +
    "Missing data is queued for the GitHub Actions engine."
  );
}

export function formatStatus({ workerOk, queueSummary, manifestSummary, note }) {
  const lines = [
    "Status",
    `Worker webhook: ${workerOk ? "available" : "unknown"}`,
  ];
  if (queueSummary) lines.push(queueSummary);
  if (manifestSummary) lines.push(manifestSummary);
  if (note) lines.push(note);
  return lines.join("\n");
}

export function summarizeQueue(requests) {
  if (!requests || !requests.length) return "Queue: empty or unavailable";
  const counts = {};
  for (const r of requests) {
    const st = r.status || "unknown";
    counts[st] = (counts[st] || 0) + 1;
  }
  const parts = Object.entries(counts)
    .map(([k, v]) => `${k}=${v}`)
    .join(" ");
  return `Queue files: ${requests.length} (${parts})`;
}

export function summarizeManifest(manifest) {
  if (!manifest || !manifest.packages) return "Manifest: unavailable or empty";
  const pkgs = Object.keys(manifest.packages);
  let indexed = 0;
  for (const pk of pkgs) {
    const arts = manifest.packages[pk].artifacts || {};
    for (const art of Object.values(arts)) {
      if (art && (art.status === "INDEXED" || art.file_id || art.telegram_file_id)) indexed += 1;
    }
  }
  return `Manifest packages: ${pkgs.length}; artifacts with file_id/INDEXED: ${indexed}`;
}

export function formatJobs(requests, limit = 15) {
  if (!requests || !requests.length) return "No queue requests found.";
  const sorted = [...requests].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const lines = sorted.slice(0, limit).map((r) => {
    const id = String(r.id || "?").slice(0, 8);
    const st = r.status || "?";
    const sym = r.symbol || "?";
    const range = r.start === r.end ? r.start : `${r.start}→${r.end}`;
    return `${id}… ${st} ${sym} ${range || ""}`.trim();
  });
  if (sorted.length > limit) lines.push(`…and ${sorted.length - limit} more`);
  return lines.join("\n");
}

export function parseCommand(text) {
  const raw = String(text || "").trim();
  if (!raw.startsWith("/")) return { type: "unknown", raw };
  const parts = raw.split(/\s+/);
  const cmd = parts[0].split("@")[0].toLowerCase();
  return { type: cmd, parts, raw };
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

async function ghListDir(repo, path, token, branch = "main") {
  const url = `https://api.github.com/repos/${repo}/contents/${path}?ref=${encodeURIComponent(branch)}`;
  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "tr-market-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
  if (res.status === 404) return { ok: true, files: [] };
  if (!res.ok) return { ok: false, status: res.status, files: [] };
  const data = await res.json();
  if (!Array.isArray(data)) return { ok: true, files: [] };
  return {
    ok: true,
    files: data.filter((d) => d && d.type === "file"),
  };
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

async function loadQueueRequests(env) {
  const repo = env.ARCHIVE_MANIFEST_REPO;
  const token = env.ARCHIVE_MANIFEST_TOKEN;
  const branch = env.ARCHIVE_MANIFEST_BRANCH || "main";
  if (!repo || !token) return { ok: false, requests: [], reason: "manifest credentials not configured" };
  const listed = await ghListDir(repo, "queue", token, branch);
  if (!listed.ok) return { ok: false, requests: [], reason: `queue list failed (${listed.status})` };
  const requests = [];
  for (const f of listed.files.slice(0, 40)) {
    const name = f.name || "";
    if (!name.endsWith(".json")) continue;
    const got = await ghGetJson(repo, `queue/${name}`, token, branch);
    if (got.ok && got.json) requests.push(got.json);
  }
  return { ok: true, requests };
}

async function loadManifest(env) {
  const repo = env.ARCHIVE_MANIFEST_REPO;
  const token = env.ARCHIVE_MANIFEST_TOKEN;
  const branch = env.ARCHIVE_MANIFEST_BRANCH || "main";
  const path = env.ARCHIVE_MANIFEST_PATH || "archive_manifest.json";
  if (!repo || !token) return { ok: false, json: null };
  return ghGetJson(repo, path, token, branch);
}

async function replyStatus(token, chatId, messageId, env, edit) {
  const q = await loadQueueRequests(env);
  const m = await loadManifest(env);
  const text = formatStatus({
    workerOk: true,
    queueSummary: q.ok ? summarizeQueue(q.requests) : `Queue: unavailable (${q.reason || "error"})`,
    manifestSummary: m.ok ? summarizeManifest(m.json) : "Manifest: unavailable",
    note:
      "Engine activity is shown only via queue file statuses in the index repo; " +
      "this does not claim a live Actions runner heartbeat.",
  });
  if (edit && messageId) {
    await tg(token, "editMessageText", { chat_id: chatId, message_id: messageId, text });
  } else {
    await tg(token, "sendMessage", { chat_id: chatId, text });
  }
}

async function replyJobs(token, chatId, messageId, env, edit) {
  const q = await loadQueueRequests(env);
  const text = q.ok ? formatJobs(q.requests) : `Jobs unavailable: ${q.reason || "error"}`;
  if (edit && messageId) {
    await tg(token, "editMessageText", { chat_id: chatId, message_id: messageId, text });
  } else {
    await tg(token, "sendMessage", { chat_id: chatId, text });
  }
}

async function handleGet(token, chatId, userId, text, env) {
  const parsed = parseGet(text);
  if (!parsed) {
    await tg(token, "sendMessage", {
      chat_id: chatId,
      text: "Usage: /get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both]",
    });
    return { ok: true, error: "bad_args" };
  }

  const repo = env.ARCHIVE_MANIFEST_REPO;
  const mtoken = env.ARCHIVE_MANIFEST_TOKEN;
  const branch = env.ARCHIVE_MANIFEST_BRANCH || "main";
  const mpath = env.ARCHIVE_MANIFEST_PATH || "archive_manifest.json";
  if (!repo || !mtoken) {
    await tg(token, "sendMessage", { chat_id: chatId, text: "Server misconfigured (manifest)." });
    return { ok: true, error: "misconfigured_manifest" };
  }

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
        return { ok: true, delivered: order.map((x) => x[0]) };
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
    return { ok: true, error: "enqueue_failed", detail: put.status };
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
  return { ok: true, queued: id, dispatch: dis.ok };
}

async function handleCallback(token, chatId, messageId, data, env) {
  if (data === "menu:status") {
    await replyStatus(token, chatId, messageId, env, true);
    return;
  }
  if (data === "menu:symbols") {
    const text = "Supported symbols / aliases:\n" + supportedSymbolsList().join("\n");
    await tg(token, "editMessageText", { chat_id: chatId, message_id: messageId, text });
    return;
  }
  if (data === "menu:jobs") {
    await replyJobs(token, chatId, messageId, env, true);
    return;
  }
  if (data === "menu:help") {
    await tg(token, "editMessageText", {
      chat_id: chatId,
      message_id: messageId,
      text: helpText(),
    });
    return;
  }
  if (data === "menu:get") {
    await tg(token, "editMessageText", {
      chat_id: chatId,
      message_id: messageId,
      text:
        "Send:\n/get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both]\n\n" +
        "Example: /get US30 2024-06-12 both",
    });
    return;
  }
  await tg(token, "editMessageText", {
    chat_id: chatId,
    message_id: messageId,
    text: "Unknown menu action. Try /help",
  });
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

    // Inline keyboard callbacks
    if (update.callback_query) {
      const cq = update.callback_query;
      const userId = cq.from && cq.from.id;
      const chatId = cq.message && cq.message.chat && cq.message.chat.id;
      const messageId = cq.message && cq.message.message_id;
      const data = String(cq.data || "");
      const admins = parseAdmins(env.TELEGRAM_ADMIN_IDS);
      if (admins.size && userId != null && !admins.has(userId)) {
        await tg(token, "answerCallbackQuery", {
          callback_query_id: cq.id,
          text: "Unauthorized",
          show_alert: true,
        });
        return json({ ok: true, unauthorized: true });
      }
      await tg(token, "answerCallbackQuery", { callback_query_id: cq.id });
      if (chatId != null) await handleCallback(token, chatId, messageId, data, env);
      return json({ ok: true, callback: data });
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

    const { type: cmd } = parseCommand(text);

    if (cmd === "/start") {
      await tg(token, "sendMessage", {
        chat_id: chatId,
        text: welcomeText(),
        reply_markup: mainMenuKeyboard(),
      });
      return json({ ok: true, command: "start" });
    }

    if (cmd === "/help") {
      await tg(token, "sendMessage", { chat_id: chatId, text: helpText() });
      return json({ ok: true, command: "help" });
    }

    if (cmd === "/status") {
      await replyStatus(token, chatId, null, env, false);
      return json({ ok: true, command: "status" });
    }

    if (cmd === "/symbols") {
      await tg(token, "sendMessage", {
        chat_id: chatId,
        text: "Supported symbols / aliases:\n" + supportedSymbolsList().join("\n"),
      });
      return json({ ok: true, command: "symbols" });
    }

    if (cmd === "/jobs") {
      await replyJobs(token, chatId, null, env, false);
      return json({ ok: true, command: "jobs" });
    }

    if (cmd === "/get") {
      const result = await handleGet(token, chatId, userId, text, env);
      return json(result);
    }

    await tg(token, "sendMessage", { chat_id: chatId, text: "Unknown command. Try /help" });
    return json({ ok: true, command: "unknown" });
  },
};
