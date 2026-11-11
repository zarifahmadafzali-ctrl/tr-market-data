import { strict as assert } from "node:assert";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";

const __dirname = dirname(fileURLToPath(import.meta.url));
const mod = await import(pathToFileURL(join(__dirname, "../src/index.mjs")).href);

test("normalizeSymbol US30", () => {
  assert.equal(mod.normalizeSymbol("us30"), "USA30IDXUSD");
});

test("parseGet single day", () => {
  const p = mod.parseGet("/get US30 2024-06-12");
  assert.deepEqual(p, { symbol: "US30", start: "2024-06-12", end: "2024-06-12", kind: "both" });
});

test("parseGet range and kind", () => {
  const p = mod.parseGet("/get US30 2024-06-01 2024-06-07 tick");
  assert.equal(p.start, "2024-06-01");
  assert.equal(p.end, "2024-06-07");
  assert.equal(p.kind, "tick");
});

test("parseGet rejects inverted range", () => {
  assert.equal(mod.parseGet("/get US30 2024-06-10 2024-06-01"), null);
});

test("parseGet rejects date used as symbol (missing SYMBOL)", () => {
  const p = mod.parseGet("/get 2024-12-07 2024-12-14 tick");
  assert.ok(p && p.error === "missing_symbol");
  assert.match(p.message, /Missing SYMBOL/i);
  assert.equal(mod.parseGet("/get US30 2024-12-07 2024-12-14 tick").symbol, "US30");
});

test("findArtifacts + pick kind", () => {
  const man = {
    packages: {
      "dukascopy|USA30IDXUSD|2024-06-12": {
        artifacts: {
          "ticks.parquet": { file_id: "TICK", status: "INDEXED" },
          "bars_1s.parquet": { file_id: "BARS", status: "INDEXED" },
          "metadata.json": { file_id: "META", status: "INDEXED" },
        },
      },
    },
  };
  const arts = mod.findArtifacts(man, "US30", "2024-06-12");
  assert.ok(arts["ticks.parquet"]);
  const only1s = mod.pickArtifactsForKind(arts, "1s");
  assert.equal(only1s[0][0], "bars_1s.parquet");
  assert.ok(only1s.some((x) => x[0] === "metadata.json"));
});

test("parseAdmins", () => {
  const s = mod.parseAdmins("1, 2;3");
  assert.ok(s.has(1) && s.has(2) && s.has(3));
});

test("parseCommand routes", () => {
  assert.equal(mod.parseCommand("/start").type, "/start");
  assert.equal(mod.parseCommand("/status@bot").type, "/status");
  assert.equal(mod.parseCommand("/help").type, "/help");
  assert.equal(mod.parseCommand("/jobs").type, "/jobs");
  assert.equal(mod.parseCommand("/symbols").type, "/symbols");
  assert.equal(mod.parseCommand("/get US30 2024-01-01").type, "/get");
  assert.equal(mod.parseCommand("hello").type, "unknown");
});

test("welcomeText is not the same as helpText", () => {
  const w = mod.welcomeText();
  const h = mod.helpText();
  assert.ok(w.includes("Dukascopy"));
  assert.ok(w.includes("private Telegram channel"));
  assert.ok(h.includes("/status"));
  assert.ok(h.includes("/jobs"));
  assert.notEqual(w, h);
});

test("mainMenuKeyboard has required buttons", () => {
  const kb = mod.mainMenuKeyboard();
  const data = kb.inline_keyboard.flat().map((b) => b.callback_data);
  for (const need of ["menu:status", "menu:symbols", "menu:get", "menu:jobs", "menu:help"]) {
    assert.ok(data.includes(need), need);
  }
});

test("formatStatus does not claim engine running", () => {
  const t = mod.formatStatus({
    workerOk: true,
    queueSummary: "Queue files: 0",
    manifestSummary: "Manifest packages: 0; artifacts with file_id/INDEXED: 0",
    note: "Engine activity is shown only via queue file statuses",
  });
  assert.ok(t.includes("Worker webhook: available"));
  assert.ok(!/engine is running/i.test(t));
});

test("summarizeQueue and formatJobs", () => {
  const reqs = [
    { id: "abcdef012345", status: "queued", symbol: "USA30IDXUSD", start: "2024-06-12", end: "2024-06-12", created_at: "2024-06-12T00:00:00+00:00" },
    { id: "bbbbbb012345", status: "running", symbol: "EURUSD", start: "2024-01-01", end: "2024-01-02", created_at: "2024-06-13T00:00:00+00:00" },
  ];
  assert.match(mod.summarizeQueue(reqs), /queued=1/);
  assert.match(mod.summarizeQueue(reqs), /running=1/);
  const jobs = mod.formatJobs(reqs);
  assert.match(jobs, /running/);
  assert.match(jobs, /EURUSD/);
});

test("summarizeManifest counts packages", () => {
  const man = {
    packages: {
      "dukascopy|USA30IDXUSD|2024-06-12": {
        artifacts: { "ticks.parquet": { file_id: "x", status: "INDEXED" } },
      },
    },
  };
  assert.match(mod.summarizeManifest(man), /packages: 1/);
});

test("mapMenuText maps reply keyboard labels", () => {
  assert.equal(mod.mapMenuText("📊 Status"), "/status");
  assert.equal(mod.mapMenuText("📥 Get data"), "/get");
  assert.equal(mod.mapMenuText("🏠 Menu"), "/start");
  assert.equal(mod.mapMenuText("help"), "/help");
});

test("supportedSymbolsList includes US30 alias", () => {
  const list = mod.supportedSymbolsList().join("\n");
  assert.match(list, /US30/);
  assert.match(list, /USA30IDXUSD/);
});
