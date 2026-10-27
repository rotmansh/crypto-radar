// Time Machine gateway: Replay must never hand the UI anything after as_of. TEST FIXTURES only.
const test = require("node:test");
const assert = require("node:assert");
const { createGateway, lastClosedOpen } = require("../../web/tm-data.js");

const DAY = 86400;
const T0 = Date.UTC(2025, 2, 1) / 1000; // 2025-03-01
const rows = Array.from({ length: 60 }, (_, i) => [T0 + i * DAY, 1, 2, 0.5, 1.5, 10, null, null, null, null, null, 0]);
const files = {
  "data/candles/1d/XUSDT/2025.json": rows,
  "data/tm/1d/2025.json": {
    entries: rows.map((r, i) => ({ t: r[0], s: "XUSDT", st: "B-BBSQ", k: i % 2 ? "W" : "S" })),
    members: Object.fromEntries(rows.map((r) => [String(r[0]), ["XUSDT"]])),
  },
};
const meta = { candles_index: { XUSDT: { "1d": { chunks: ["2025"] } } }, tm_index: { "1d": ["2025"] } };
const gw = () => createGateway(async (p) => { if (!(p in files)) throw new Error("404 " + p); return files[p]; }, meta);

test("last closed candle: the candle of day D is available only after D closes", () => {
  const mar15 = Date.UTC(2025, 2, 15) / 1000;
  assert.strictEqual(lastClosedOpen(mar15, "1d"), mar15 - DAY);           // 15 Mar 00:00 -> 14 Mar candle
  assert.strictEqual(lastClosedOpen(mar15 + DAY, "1d"), mar15);           // 16 Mar 00:00 -> 15 Mar candle
  assert.strictEqual(lastClosedOpen(mar15 + DAY - 1, "1d"), mar15 - DAY); // 15 Mar 23:59:59 -> still 14 Mar
});

test("replayCandles returns nothing after as_of although the file holds later candles", async () => {
  const asOf = T0 + 20 * DAY + 3600;
  const got = await gw().replayCandles("XUSDT", "1d", asOf);
  assert.ok(got.length > 0);
  assert.ok(got.every((r) => r[0] + DAY <= asOf), "a candle that had not closed leaked");
  assert.strictEqual(got[got.length - 1][0], T0 + 19 * DAY);
});

test("replayState returns only alerts of the last closed candle", async () => {
  const asOf = T0 + 20 * DAY;
  const st = await gw().replayState("1d", asOf);
  assert.strictEqual(st.last, T0 + 19 * DAY);
  assert.ok(st.entries.length === 1 && st.entries[0].t === st.last);
  assert.deepStrictEqual(st.members, ["XUSDT"]);
});

test("future data exists only through reveal()", async () => {
  const g = gw();
  assert.strictEqual(typeof g.candlesAfter, "undefined");
  const asOf = T0 + 20 * DAY;
  const r = g.reveal(asOf);
  const fut = await r.candlesAfter("XUSDT", "1d", T0 + 30 * DAY);
  assert.ok(fut.length === 11 && fut.every((x) => x[0] > T0 + 19 * DAY));
  const al = await r.alertsAfter("1d", "XUSDT", "B-BBSQ", T0 + 25 * DAY);
  assert.ok(al.every((e) => e.t > T0 + 19 * DAY && e.t <= T0 + 25 * DAY));
});
