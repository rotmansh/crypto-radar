// Live check of the published dashboard (run by .github/workflows/site-check.yml).
// Opens the public URL on a phone-sized and a desktop browser, walks the main views and the Time Machine,
// fails on HTTP errors or JavaScript errors, and saves screenshots.
const { chromium } = require("playwright");
const fs = require("fs");
const BASE = process.env.SITE_URL || "https://rotmansh.github.io/crypto-radar/";
const OUT = process.argv[2] || "shots";
const SHOTS = process.env.SCREENSHOTS !== "0";
const EXPECT_RUN_ID = process.env.EXPECT_RUN_ID || "";
fs.mkdirSync(OUT, { recursive: true });

/** Wait until the published site serves this run's health record (GitHub Pages CDN caches up to ~10 min). */
async function waitForRun(problems) {
  if (!EXPECT_RUN_ID) return { matched: null, health: null };
  let health = null;
  for (let i = 0; i < 24; i++) {
    try {
      const r = await fetch(`${BASE}data/health.json?t=${Date.now()}`);
      if (r.ok) {
        health = await r.json();
        if (String(health.last_run.run_id) === EXPECT_RUN_ID) return { matched: true, health };
      }
    } catch (e) { /* retry */ }
    await new Promise((res) => setTimeout(res, 30000));
  }
  problems.push(`published site does not serve run ${EXPECT_RUN_ID} (serves ${health ? health.last_run.run_id : "nothing"})`);
  return { matched: false, health };
}

(async () => {
  const problems = [];
  const report = [];
  const { matched, health } = await waitForRun(problems);
  const browser = await chromium.launch();
  async function visit(viewport, name, hash, act, expect) {
    const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, locale: "he-IL", isMobile: viewport.width < 500, hasTouch: viewport.width < 500 });
    const p = await ctx.newPage();
    p.on("pageerror", (e) => problems.push(`${name}: JS error ${e.message}`));
    p.on("console", (m) => { if (m.type() === "error") problems.push(`${name}: console ${m.text()}`); });
    p.on("response", (r) => { if (r.status() >= 400) problems.push(`${name}: HTTP ${r.status()} ${r.url()}`); });
    const t0 = Date.now();
    await p.goto(BASE + hash, { waitUntil: "networkidle", timeout: 60000 });
    if (act) await act(p);
    await p.waitForTimeout(1500);
    const text = await p.locator("main").innerText();
    for (const e of expect || []) if (!text.includes(e)) problems.push(`${name}: expected text missing: ${e}`);
    const charts = await p.locator("canvas").count();
    report.push(`${name}: ${((Date.now() - t0) / 1000).toFixed(1)}s, canvases=${charts}`);
    if (SHOTS) await p.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
    await ctx.close();
  }
  const M = { width: 390, height: 844 }, D = { width: 1280, height: 900 };
  // If the last run did not fully succeed, the dashboard must say so (never look current).
  const runStatus = health ? health.last_run.status : null;
  const overviewExpect = ["סקירה", "מצב BTC"].concat(runStatus && runStatus !== "success" ? ["הריצה האחרונה"] : []);
  await visit(M, "m1-overview", "#overview", null, overviewExpect);
  await visit(M, "m8-system", "#system", null, ["מצב המערכת", "ריצות אחרונות"]);
  await visit(M, "m2-signals", "#strategies", null, ["איתותים מאושרים", "התראות מעקב"]);
  await visit(M, "m3-coin", "#coin/BTCUSDT", async (p) => p.waitForSelector("#pc canvas"), ["היסטוריית איתותים"]);
  await visit(M, "m4-journal", "#journal", null, ["יומן איתותים"]);
  await visit(M, "m5-tm-replay", "#tm", async (p) => { await p.click("#tmRun"); await p.waitForSelector("#tmPc canvas"); },
    ["ידוע בזמן השחזור", "2025-03-14 00:00 UTC"]);
  await visit(M, "m6-tm-replay-4h", "#tm", async (p) => { await p.selectOption("#tmTf", "4h"); await p.click("#tmRun"); await p.waitForSelector("#tmPc canvas"); },
    ["ידוע בזמן השחזור", "התראות מעקב בנר הזה"]);
  await visit(M, "m7-tm-reveal", "#tm", async (p) => {
    await p.selectOption("#tmTf", "4h"); await p.click("#tmRun"); await p.waitForSelector("#tmPc canvas");
    await p.click("#tmReveal"); await p.waitForSelector(".panel-revealed");
    await p.locator(".panel-revealed").scrollIntoViewIfNeeded();
  }, ["נחשף לאחר נקודת השחזור", "חישוב תוצאות עדיין לא זמין"]);
  await visit(D, "d1-signals", "#strategies", null, ["איתותים מאושרים"]);
  await visit(D, "d2-tm-reveal", "#tm", async (p) => {
    await p.selectOption("#tmTf", "4h"); await p.click("#tmRun"); await p.waitForSelector("#tmPc canvas");
    await p.click("#tmReveal"); await p.waitForSelector(".panel-revealed");
  }, ["נחשף לאחר נקודת השחזור"]);
  await browser.close();
  const checked = new Date().toISOString();
  const summary = [`URL: ${BASE}`, `checked: ${checked}`, `published run matches: ${matched}`, ...report,
    problems.length ? "PROBLEMS:" : "no problems", ...problems].join("\n");
  fs.writeFileSync(`${OUT}/RESULT.txt`, summary + "\n");
  fs.writeFileSync(`${OUT}/RESULT.json`, JSON.stringify({ checked_utc: checked, published_run_matches: matched,
    run_status_on_site: runStatus, timings: report, problems }, null, 1));
  console.log(summary);
  process.exit(problems.length ? 1 : 0);
})();
