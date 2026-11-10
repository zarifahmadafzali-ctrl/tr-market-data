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
