/* Crypto Radar dashboard. Display and filtering only — every alert, level and rule result comes from the
 * shared Python engine (data/*.json). The Time Machine reads through tm-data.js, which never returns data after
 * the selected moment until the user reveals it. */
(function () {
  "use strict";

  // ------------------------------------------------------------------ helpers
  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const LETTER = { "A-SMA50PB": "A", "B-BBSQ": "B", "C-TRS": "C" };
  const TFNAME = { "1d": "יומי", "4h": "4 שעות" };
  const ORIGIN = {
    live_new: ["בזמן אמת · התחילה בנר הזה", "known"],
    live_already_active: ["בזמן אמת · הייתה פעילה כבר כשנצפתה לראשונה", "watch"],
    replay: ["שחזור היסטורי · לא התראה שהתקבלה בזמן אמת", "replay"],
  };
  const DAY = 86400;

  function fmt(x) {
    if (x === null || x === undefined || Number.isNaN(x)) return "—";
    const a = Math.abs(x);
    if (a >= 1) return x.toLocaleString("en-US", { maximumSignificantDigits: 6 });
    return Number(x.toPrecision(4)).toLocaleString("en-US", { maximumSignificantDigits: 4, maximumFractionDigits: 12 });
  }
  const pct = (x, d = 2) => (x === null || x === undefined ? "—" : `${Number(x).toFixed(d)}%`);
  const usd = (x) => (x === null || x === undefined || x === "" ? "—" : `$${(Number(x) / 1e6).toFixed(1)}M`);
  const num = (x) => (x === "" || x === null || x === undefined ? null : Number(x));
  const ilTime = (d) => new Intl.DateTimeFormat("he-IL", { timeZone: "Asia/Jerusalem", dateStyle: "short", timeStyle: "short" }).format(d);
  const utcTime = (sec) => new Date(sec * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC";
  const isoSec = (iso) => Math.floor(Date.parse(iso) / 1000);
  const span = (tf) => (tf === "1d" ? DAY : 4 * 3600);

  async function getJSON(path) {
    const r = await fetch(path, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${r.status} ${path}`);
    return r.json();
  }

  // ------------------------------------------------------------------ state
  const S = { meta: null, scan: null, journal: [], universe: null, filters: { strat: "all", tf: "all", kind: "all" }, gw: null };

  // ------------------------------------------------------------------ shared rendering
  function stratOf(id) { return S.meta.strategies.find((s) => s.id === id); }

  function levelsHTML(x) {
    if (x.state === "SIGNAL") {
      const rr = x.rr !== null && x.rr !== undefined ? fmt(x.rr) : `<span class="note">${esc(x.rr_note || "—")}</span>`;
      return `<div class="levels">
        <div><div class="k">מחיר ייחוס</div><div class="v num">${fmt(x.ref_price)}</div></div>
        <div><div class="k">רמת ביטול (סטופ)</div><div class="v num">${fmt(x.stop)}</div><div class="k num">${pct(x.risk_pct_to_stop)}</div></div>
        <div><div class="k">יעד · R:R</div><div class="v num">${fmt(x.target)}</div><div class="k">${rr}</div></div>
      </div>`;
    }
    const bl = x.breakout_level !== null && x.breakout_level !== undefined
      ? `<div><div class="k">רמת פריצה</div><div class="v num">${fmt(x.breakout_level)}</div></div>` : "";
    return `<div class="levels">
      <div><div class="k">מחיר</div><div class="v num">${fmt(x.ref_price)}</div></div>
      <div><div class="k">ביטול ההתראה</div><div class="v num">${fmt(x.watch_invalidation)}</div></div>${bl}
    </div>
    <div class="note">התראת מעקב אינה כניסה: אין מחיר כניסה, סטופ, יעד או R:R עד אישור.</div>`;
  }

  function rulesHTML(strategyId, rules) {
    const st = stratOf(strategyId);
    if (!st || !rules) return "";
    const items = st.rules.map((r, i) => {
      const m = Array.isArray(rules) ? (rules[i] ? rules[i].met : null) : (rules[i] === "1" ? true : rules[i] === "0" ? false : null);
      const cls = m === true ? "ok" : m === false ? "no" : "na";
      const sym = m === true ? "✓" : m === false ? "✗" : "—";
      const tag = r.signal && !r.watch ? "אישור" : !r.signal ? "מעקב" : "";
      return `<li><span class="m ${cls}">${sym}</span><span>${esc(r.label_he)} ${tag ? `<span class="tag">· ${tag}</span>` : ""}</span></li>`;
    });
    return `<ul class="rules">${items.join("")}</ul>`;
  }

  function alertCard(x, opts = {}) {
    const kind = x.state === "SIGNAL" ? "signal" : "watch";
    const st = stratOf(x.strategy);
    const badges = [
      `<span class="badge ${kind}">${kind === "signal" ? "🟢 איתות מאושר" : "🟡 התראת מעקב"}</span>`,
      `<span class="badge">${LETTER[x.strategy] || ""} · ${esc(st ? st.name_he.split("·")[1] || st.name_he : x.strategy)}</span>`,
      `<span class="badge">${TFNAME[x.timeframe]}</span>`,
    ];
    if (x.high_risk) badges.push(`<span class="badge risk">סיכון גבוה · לא המלצת כניסה</span>`);
    if (opts.origin) badges.push(`<span class="badge ${ORIGIN[opts.origin][1]}">${ORIGIN[opts.origin][0]}</span>`);
    const warns = [];
    if (x.high_risk) warns.push(`<div class="note risk">הסטופ במרחק ${pct(x.risk_pct_to_stop)} מהמחיר (מעל ${S.meta.high_risk_stop_pct}%). הסטופ לא צומצם; האיתות אינו המלצת כניסה.</div>`);
    if (x.liquidity_warnings && x.liquidity_warnings.length) warns.push(`<div class="note warn">⚠️ אזהרת נזילות: ${esc(x.liquidity_warnings.map(liqHe).join(", "))}</div>`);
    if (x.partial_in_window) warns.push(`<div class="note warn">⚠️ נר חלקי (עצירת מסחר) ב-20 הנרות האחרונים</div>`);
    const liq = x.median_qvol_30d !== undefined && x.median_qvol_30d !== null
      ? `<div class="note">מחזור יומי חציוני ${usd(x.median_qvol_30d)} · ספרד ${x.spread_pct !== null && x.spread_pct !== undefined ? pct(x.spread_pct, 3) : "—"} · עומק ±1% ${usd(x.depth_bid_1pct_usd)} / ${usd(x.depth_ask_1pct_usd)}</div>` : "";
    return `<article class="card alert ${kind}">
      <div class="head">
        <span class="coin" data-coin="${esc(x.symbol)}" role="link" tabindex="0">${esc(x.base || x.symbol.replace(/USDT$/, ""))}</span>
        <span class="meta">נר ${esc(opts.candleLabel || utcTime(isoSec(x.candle_open_utc)))}</span>
      </div>
      <div class="badges">${badges.join("")}</div>
      ${levelsHTML(x)}
      <div class="note">ATR ${pct(x.atr_pct)} · תנודתיות ממומשת ${x.realized_vol_30 !== null && x.realized_vol_30 !== undefined ? pct(x.realized_vol_30 * 100, 0) : "—"}</div>
      ${liq}${warns.join("")}
      <details><summary>למה? הכללים והערכים</summary>
        <p class="why">${esc(x.reason_he || "")}</p>${rulesHTML(x.strategy, x.rules || x.rm)}
      </details>
      ${opts.extra || ""}
    </article>`;
  }
  const liqHe = (w) => ({ wide_spread: "ספרד רחב", thin_book_bid: "ספר דליל בצד הקנייה", thin_book_ask: "ספר דליל בצד המכירה", order_book_unavailable: "ספר פקודות לא זמין" }[w] || w);

  function bindCoinLinks(root) {
    root.querySelectorAll("[data-coin]").forEach((el) => {
      const go = () => { location.hash = `#coin/${el.dataset.coin}`; };
      el.addEventListener("click", go);
      el.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    });
  }

  function chipGroup(label, key, options) {
    return `<div class="chip-group"><span class="lbl">${label}</span>${options.map(([v, t]) =>
      `<button class="chip" data-f="${key}" data-v="${v}" aria-pressed="${S.filters[key] === v}">${t}</button>`).join("")}</div>`;
  }

  // ------------------------------------------------------------------ freshness
  function renderFreshness() {
    const gen = new Date(S.scan.generated_utc);
    const ageH = (Date.now() - gen.getTime()) / 3.6e6;
    $("#freshness").innerHTML = `סריקה אחרונה: <b>${ilTime(gen)}</b> (שעון ישראל)`;
    const b = [];
    if (ageH > S.meta.stale_after_hours) b.push(`<div class="banner">⚠️ הנתונים אינם עדכניים: הסריקה האחרונה לפני ${ageH.toFixed(1)} שעות. ההרצה האוטומטית תופעל בשלב 4; כרגע הסריקה רצה ידנית.</div>`);
    if (S.scan.stale && S.scan.stale.length) b.push(`<div class="banner">⚠️ ${S.scan.stale.length} סדרות לא עודכנו ולא נסרקו: ${esc(S.scan.stale.slice(0, 8).map((s) => s.symbol + " " + s.timeframe).join(", "))}</div>`);
    if (S.meta.self_check.some((c) => !c.match)) b.push(`<div class="banner">⚠️ בדיקת התאמה של מכונת הזמן נכשלה</div>`);
    $("#banner").innerHTML = b.join("");
  }

  // ------------------------------------------------------------------ views
  function viewOverview() {
    const sc = S.scan;
    const btc = sc.btc_regime;
    const cards = [];
    cards.push(`<div class="card kpi"><div class="label">מצב BTC (מידע בלבד, לא מסנן)</div>
      <div class="value num">${btc ? fmt(btc.close) : "—"}</div>
      <div class="sub">${btc ? (btc.above_sma200 ? "מעל" : "מתחת ל") + " SMA200 היומי (" + fmt(btc.sma200) + ")" : "אין מספיק נתונים"}</div></div>`);
    for (const st of S.meta.strategies) {
      const blocks = sc.strategies.filter((b) => b.strategy === st.id);
      const lines = blocks.map((b) => `${TFNAME[b.timeframe]}: <b class="num">${b.signals.length}</b> 🟢 · <b class="num">${b.watch.length}</b> 🟡`).join("<br>");
      const hr = blocks.reduce((n, b) => n + b.signals.filter((x) => x.high_risk).length, 0);
      cards.push(`<a class="card kpi" style="text-decoration:none;color:inherit" href="#strategies" data-strat="${st.id}">
        <div class="label">${esc(st.name_he)} <span class="badge">${esc(st.ref)}</span></div>
        <div class="sub" style="font-size:14px;color:var(--text);margin-top:6px">${lines}</div>
        ${hr ? `<div class="note risk">${hr} בסיכון גבוה</div>` : ""}</a>`);
    }
    const rev = S.universe.rows.filter((r) => r.status === "review");
    cards.push(`<div class="card kpi"><div class="label">מטבעות בסריקה</div><div class="value num">${sc.universe_size}</div>
      <div class="sub">${rev.length} בתור בדיקת חריגים · רשימה מ-${esc(S.universe.snapshot.slice(0, 10))}</div></div>`);
    const ok = S.meta.self_check.every((c) => c.match);
    cards.push(`<div class="card kpi"><div class="label">מכונת הזמן · בדיקת התאמה למנוע</div><div class="value">${ok ? "תקין" : "נכשל"}</div>
      <div class="sub">${S.meta.self_check.length} נקודות זמן נבדקו מול שחזור אמיתי על נתונים חתוכים</div></div>`);
    $("#main").innerHTML = `<h2>סקירה</h2><p class="lead">מצב הסריקה בנר הסגור האחרון. 🟢 = כל תנאי האסטרטגיה התקיימו · 🟡 = מתגבש, ללא אישור.</p>
      <div class="grid">${cards.join("")}</div>
      <h3>חשוב לדעת</h3><div class="card"><ul class="limits">${sc.disclaimers_he.map((d) => `<li>${esc(d).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</li>`).join("")}</ul></div>`;
    document.querySelectorAll("[data-strat]").forEach((a) => a.addEventListener("click", () => { S.filters.strat = a.dataset.strat; }));
  }

  function viewStrategies() {
    const f = S.filters;
    const blocks = S.scan.strategies.filter((b) => (f.strat === "all" || b.strategy === f.strat) && (f.tf === "all" || b.timeframe === f.tf));
    const sig = blocks.flatMap((b) => b.signals), wat = blocks.flatMap((b) => b.watch);
    const ins = blocks.flatMap((b) => b.insufficient.map((x) => `${x.symbol.replace(/USDT$/, "")} ${TFNAME[b.timeframe]} (${x.history_candles}/${x.needs})`));
    const sec = (cls, title, list, emptyText) => `<div class="section-h ${cls}">${title} <span class="count">(${list.length})</span></div>
      ${list.length ? `<div class="grid">${list.map((x) => alertCard(x)).join("")}</div>` : `<div class="empty">${emptyText}</div>`}`;
    $("#main").innerHTML = `<h2>איתותים והתראות</h2>
      <p class="lead">נר סגור אחרון בכל טווח. שני האזורים נפרדים: איתות מאושר עומד בכל התנאים; התראת מעקב היא setup שמתגבש בלבד.</p>
      <div class="chips">
        ${chipGroup("אסטרטגיה", "strat", [["all", "הכול"], ["A-SMA50PB", "A · SMA50"], ["B-BBSQ", "B · בולינגר"], ["C-TRS", "C · חוזק יחסי"]])}
        ${chipGroup("טווח", "tf", [["all", "הכול"], ["1d", "יומי"], ["4h", "4 שעות"]])}
        ${chipGroup("סוג", "kind", [["all", "הכול"], ["signal", "🟢 מאושר"], ["watch", "🟡 מעקב"]])}
      </div>
      ${f.kind !== "watch" ? sec("signal", "🟢 איתותים מאושרים", sig, "אין איתות מאושר בנר האחרון בסינון הזה.") : ""}
      ${f.kind !== "signal" ? sec("watch", "🟡 התראות מעקב (לפני אישור)", wat, "אין התראות מעקב בסינון הזה.") : ""}
      ${ins.length ? `<p class="note" style="margin-top:14px">היסטוריה לא מספיקה (לא מחושב, לא משוער): ${esc(ins.join(" · "))}</p>` : ""}`;
    document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => { S.filters[c.dataset.f] = c.dataset.v; viewStrategies(); }));
    bindCoinLinks($("#main"));
  }

  // ---- charts
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function precisionFor(p) { if (!p) return 2; const d = Math.max(2, Math.ceil(-Math.log10(Math.abs(p))) + 3); return Math.min(d, 10); }
  function makeChart(el, timeVisible) {
    const chart = LightweightCharts.createChart(el, {
      autoSize: true,
      layout: { background: { color: cssVar("--surface") }, textColor: cssVar("--muted"), fontFamily: "Heebo, system-ui, sans-serif" },
      grid: { vertLines: { color: cssVar("--surface-2") }, horzLines: { color: cssVar("--surface-2") } },
      rightPriceScale: { borderColor: cssVar("--border") },
      timeScale: { borderColor: cssVar("--border"), timeVisible, secondsVisible: false },
      crosshair: { mode: 0 },
    });
    return chart;
  }
  function priceChart(el, rows, tf, opts = {}) {
    const chart = makeChart(el, tf === "4h");
    const prec = precisionFor(rows.length ? rows[rows.length - 1][4] : 1);
    const pf = { type: "price", precision: prec, minMove: Math.pow(10, -prec) };
    const candles = chart.addCandlestickSeries({ upColor: "#16a34a", downColor: "#dc2626", borderVisible: false, wickUpColor: "#16a34a", wickDownColor: "#dc2626", priceFormat: pf });
    candles.setData(rows.map((r) => ({ time: r[0], open: r[1], high: r[2], low: r[3], close: r[4] })));
    const line = (idx, color, style = 0, width = 2) => {
      const s = chart.addLineSeries({ color, lineWidth: width, lineStyle: style, priceLineVisible: false, lastValueVisible: false, priceFormat: pf });
      s.setData(rows.filter((r) => r[idx] !== null).map((r) => ({ time: r[0], value: r[idx] })));
      return s;
    };
    line(6, "#f59e0b"); line(7, "#8b5cf6");
    line(8, "#94a3b8", 2, 1); line(9, "#94a3b8", 1, 1); line(10, "#94a3b8", 2, 1);
    chart.timeScale().fitContent();
    if (rows.length > 160) chart.timeScale().setVisibleLogicalRange({ from: rows.length - 160, to: rows.length + 5 });
    return { chart, candles, pf };
  }
  const legendHTML = `<div class="legend"><span><i style="background:#f59e0b"></i>SMA50</span><span><i style="background:#8b5cf6"></i>SMA200</span>
    <span><i style="background:#94a3b8"></i>רצועות בולינגר (20,2)</span><span>▲ איתות בזמן אמת · ● התראת מעקב בזמן אמת · ■ שחזור היסטורי (לא התראה אמיתית)</span><span>זמנים ב-UTC</span></div>`;

  async function loadCandles(sym, tf, nChunks) {
    const idx = (S.meta.candles_index[sym] || {})[tf];
    if (!idx) return [];
    const chunks = idx.chunks.slice(-nChunks);
    const parts = await Promise.all(chunks.map((c) => getJSON(`data/candles/${tf}/${sym}/${c}.json`)));
    return parts.flat();
  }

  async function viewCoin(sym) {
    const symbols = Object.keys(S.meta.candles_index).sort();
    sym = sym && S.meta.candles_index[sym] ? sym : "BTCUSDT";
    const tf = S.coinTf || "1d";
    const showReplay = S.showReplay !== false;
    const live = S.scan.strategies.flatMap((b) => [...b.signals, ...b.watch]).filter((x) => x.symbol === sym);
    const uni = S.universe.rows.find((r) => r.symbol === sym);
    $("#main").innerHTML = `<h2>${esc(sym.replace(/USDT$/, ""))} <span class="badge">${esc(sym)}</span></h2>
      <div class="controls" style="margin-bottom:10px">
        <label class="f">מטבע<select id="coinSel">${symbols.map((s) => `<option ${s === sym ? "selected" : ""}>${s}</option>`).join("")}</select></label>
        <label class="f">טווח<select id="tfSel"><option value="1d" ${tf === "1d" ? "selected" : ""}>יומי</option><option value="4h" ${tf === "4h" ? "selected" : ""}>4 שעות</option></select></label>
        <label class="f">סימונים<select id="repSel"><option value="1" ${showReplay ? "selected" : ""}>בזמן אמת + שחזור היסטורי</option><option value="0" ${!showReplay ? "selected" : ""}>בזמן אמת בלבד</option></select></label>
      </div>
      <div class="two">
        <div class="card"><div id="pc" class="chart-wrap"></div>${legendHTML}
          ${tf === "1d" ? `<h3>חוזק יחסי מול BTC (מחיר המטבע / מחיר BTC)</h3><div id="rsc" class="chart-wrap small"></div>` : ""}</div>
        <div>
          <div class="card"><h3 style="margin-top:0">מצב נוכחי</h3>${uni ? `<div class="note">סטטוס ברשימה: <b>${esc(uni.status)}</b>${uni.review_reason ? " · " + esc(uni.review_reason) : ""}</div>
            <div class="note">מחזור יומי חציוני ${usd(uni.median_quote_volume_30d)} · ספרד ${uni.spread_pct ? pct(Number(uni.spread_pct), 3) : "—"} · עומק ±1% ${usd(uni.depth_bid_1pct_usd)} / ${usd(uni.depth_ask_1pct_usd)}</div>
            ${uni.liquidity_warnings ? `<div class="note warn">⚠️ ${esc(uni.liquidity_warnings.split(";").map(liqHe).join(", "))}</div>` : ""}` : `<div class="note">המטבע אינו ברשימת הסריקה הנוכחית.</div>`}
            ${live.length ? "" : `<p class="note">אין איתות או התראת מעקב פעילים כרגע.</p>`}</div>
          ${live.map((x) => alertCard(x)).join("")}
        </div>
      </div>
      <h3>היסטוריית איתותים — יומן בזמן אמת</h3><div id="jl"></div>
      <h3>היסטוריית איתותים — שחזור היסטורי <span class="badge replay">לא התראות שהתקבלו בזמן אמת</span></h3>
      <p class="note">חושב בדיעבד מאותו מנוע, מנתונים שהיו זמינים בכל נקודת זמן. מוצג לשם עיון בלבד; אין כאן ביצועים.</p><div id="hl"></div>`;
    $("#coinSel").addEventListener("change", (e) => { location.hash = `#coin/${e.target.value}`; });
    $("#tfSel").addEventListener("change", (e) => { S.coinTf = e.target.value; viewCoin(sym); });
    $("#repSel").addEventListener("change", (e) => { S.showReplay = e.target.value === "1"; viewCoin(sym); });

    const rows = await loadCandles(sym, tf, tf === "1d" ? 99 : 8);
    if (!rows.length) { $("#pc").innerHTML = `<div class="empty">אין נתוני נרות למטבע הזה בטווח הזה.</div>`; return; }
    const { chart, candles } = priceChart($("#pc"), rows, tf);
    const first = rows[0][0];
    const jl = S.journal.filter((r) => r.symbol === sym);
    let hist = [];
    try { hist = await getJSON(`data/history/${sym}.json`); } catch (e) { hist = []; }
    const markers = [];
    jl.filter((r) => r.timeframe === tf && isoSec(r.candle_open_utc) >= first).forEach((r) => markers.push({
      time: isoSec(r.candle_open_utc), position: "belowBar", color: r.type === "SIGNAL" ? "#16a34a" : "#d97706",
      shape: r.type === "SIGNAL" ? "arrowUp" : "circle", text: LETTER[r.strategy] }));
    if (showReplay) hist.filter((h) => h.tf === tf && h.t >= first).forEach((h) => markers.push({
      time: h.t, position: "aboveBar", color: h.k === "S" ? "#64748b" : "#a3a3a3", shape: "square", text: (LETTER[h.st] || "") + (h.k === "S" ? "" : "·") }));
    markers.sort((a, b) => a.time - b.time);
    candles.setMarkers(markers);
    for (const x of live.filter((x) => x.timeframe === tf)) {
      const lines = x.state === "SIGNAL" ? [[x.stop, "#dc2626", "סטופ"], [x.target, "#16a34a", "יעד"]] : [[x.watch_invalidation, "#d97706", "ביטול"], [x.breakout_level, "#2563eb", "פריצה"]];
      lines.filter((l) => l[0]).forEach(([price, color, title]) => candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: `${LETTER[x.strategy]} ${title}` }));
    }
    if (tf === "1d") {
      const rs = rows.filter((r) => r[12] !== null && r[12] !== undefined);
      const el = $("#rsc");
      if (rs.length) {
        const c2 = makeChart(el, false);
        const rp = precisionFor(rs[rs.length - 1][12]);
        const s2 = c2.addLineSeries({ color: "#0ea5e9", lineWidth: 2, priceFormat: { type: "price", precision: rp, minMove: Math.pow(10, -rp) } });
        s2.setData(rs.map((r) => ({ time: r[0], value: r[12] })));
        c2.timeScale().fitContent();
        if (rs.length > 160) c2.timeScale().setVisibleLogicalRange({ from: rs.length - 160, to: rs.length + 5 });
      } else el.innerHTML = `<div class="empty">אין קו RS (BTC עצמו, או היסטוריה חסרה).</div>`;
    }
    $("#jl").innerHTML = journalTable(jl, false);
    const hrows = hist.slice(-40).reverse();
    $("#hl").innerHTML = hrows.length ? `<div class="table-wrap"><table><thead><tr><th>נר</th><th>טווח</th><th>אסטרטגיה</th><th>סוג</th><th>מחיר</th><th>ביטול</th><th>יעד</th><th>R:R</th></tr></thead><tbody>
      ${hrows.map((h) => `<tr><td class="num">${utcTime(h.t)}</td><td>${TFNAME[h.tf]}</td><td>${LETTER[h.st]}</td><td>${h.k === "S" ? "🟢 מאושר" : "🟡 תחילת מעקב"}${h.hr ? ' <span class="badge risk">סיכון גבוה</span>' : ""}</td>
      <td class="num">${fmt(h.p)}</td><td class="num">${fmt(h.k === "S" ? h.stop : h.inv)}</td><td class="num">${fmt(h.tgt)}</td><td class="num">${h.rr ? fmt(h.rr) : "—"}</td></tr>`).join("")}</tbody></table></div>
      <p class="note">${hist.length} רשומות בסך הכול; מוצגות 40 האחרונות.</p>` : `<div class="empty">אין שחזורים היסטוריים למטבע הזה.</div>`;
  }

  function journalTable(rows, withCoin = true) {
    if (!rows.length) return `<div class="empty">אין רשומות ביומן. היומן התחיל ב-27.10.2026 ונכתב רק מסריקות בזמן אמת.</div>`;
    rows = rows.slice().sort((a, b) => (a.detected_at_utc < b.detected_at_utc ? 1 : -1));
    return `<div class="table-wrap jt"><table><thead><tr>${withCoin ? "<th>מטבע</th>" : ""}<th>סוג</th><th>אסטרטגיה · טווח</th><th>מקור</th><th>נר</th><th>זוהה ב-</th><th>מחיר</th><th>ביטול / סטופ</th><th>יעד</th></tr></thead><tbody>
      ${rows.map((r) => {
        const o = ORIGIN[r.origin] || ["—", ""];
        const ep = r.origin === "live_already_active" ? `<div class="note">התחלת המעקב לפי הנרות: ${esc(r.episode_started_utc.replace("T", " ").replace("Z", " UTC"))}</div>` : "";
        return `<tr>${withCoin ? `<td data-l="מטבע"><span class="coin" data-coin="${esc(r.symbol)}" role="link" tabindex="0" style="font-weight:700;cursor:pointer">${esc(r.symbol.replace(/USDT$/, ""))}</span></td>` : ""}
        <td data-l="סוג">${r.type === "SIGNAL" ? "🟢 מאושר" : "🟡 מעקב"}${r.high_risk === "True" ? ' <span class="badge risk">סיכון גבוה</span>' : ""}</td>
        <td data-l="אסטרטגיה">${LETTER[r.strategy] || ""} <span class="note ltr">@${esc(r.strategy_version)}</span> · ${TFNAME[r.timeframe]}</td>
        <td data-l="מקור"><span class="badge ${o[1]}">${o[0]}</span>${ep}</td>
        <td data-l="נר" class="num">${esc(r.candle_open_utc.replace("T", " ").replace(":00Z", " UTC"))}</td>
        <td data-l="זוהה ב-">${ilTime(new Date(r.detected_at_utc))}</td>
        <td data-l="מחיר" class="num">${fmt(num(r.ref_price))}</td><td data-l="${r.type === "SIGNAL" ? "סטופ" : "ביטול"}" class="num">${fmt(num(r.type === "SIGNAL" ? r.stop : r.watch_invalidation))}</td><td data-l="יעד" class="num">${fmt(num(r.target))}</td></tr>`;
      }).join("")}</tbody></table></div>`;
  }

  function viewJournal() {
    const f = S.jf || (S.jf = { origin: "all", type: "all" });
    let rows = S.journal.filter((r) => (f.origin === "all" || r.origin === f.origin) && (f.type === "all" || r.type === f.type));
    const counts = { live_new: 0, live_already_active: 0 };
    S.journal.forEach((r) => { counts[r.origin] = (counts[r.origin] || 0) + 1; });
    $("#main").innerHTML = `<h2>יומן איתותים</h2>
      <p class="lead">רק התראות שזוהו בסריקות בזמן אמת. שחזורים היסטוריים (מכונת הזמן) לעולם אינם נכתבים ליומן.</p>
      <div class="card" style="margin-bottom:12px"><ul class="limits">
        <li><span class="badge known">${ORIGIN.live_new[0]}</span> — זוהתה בסריקה של הנר שבו התחילה (${counts.live_new || 0}).</li>
        <li><span class="badge watch">${ORIGIN.live_already_active[0]}</span> — כשנצפתה לראשונה היא כבר הייתה פעילה, למשל כי התחילה לפני שהיומן נוצר (${counts.live_already_active || 0}). זמן תחילתה מחושב מהנרות; זמן הזיהוי הוא הזמן האמיתי שבו נצפתה. לא הומצא זמן יצירה.</li>
        <li>התראת מעקב נרשמת פעם אחת לכל רצף (episode), ואיתות מאושר פעם אחת לכל נר.</li></ul></div>
      <div class="chips">
        <div class="chip-group"><span class="lbl">מקור</span>${[["all", "הכול"], ["live_new", "חדשה"], ["live_already_active", "הייתה פעילה"]].map(([v, t]) => `<button class="chip" data-o="${v}" aria-pressed="${f.origin === v}">${t}</button>`).join("")}</div>
        <div class="chip-group"><span class="lbl">סוג</span>${[["all", "הכול"], ["SIGNAL", "🟢 מאושר"], ["WATCH", "🟡 מעקב"]].map(([v, t]) => `<button class="chip" data-t="${v}" aria-pressed="${f.type === v}">${t}</button>`).join("")}</div>
      </div>${journalTable(rows)}`;
    document.querySelectorAll("[data-o]").forEach((c) => c.addEventListener("click", () => { f.origin = c.dataset.o; viewJournal(); }));
    document.querySelectorAll("[data-t]").forEach((c) => c.addEventListener("click", () => { f.type = c.dataset.t; viewJournal(); }));
    bindCoinLinks($("#main"));
  }

  // ------------------------------------------------------------------ Time Machine
  const TM_DEFAULT = { date: "2025-03-15", time: "00:00", tf: "1d", strategy: "all", coin: "" };
  function fromTM(e, tf) {  // engine item (compact) -> card shape
    return { symbol: e.s, base: e.s.replace(/USDT$/, ""), strategy: e.st, timeframe: tf, state: e.k === "S" ? "SIGNAL" : "WATCH",
      candle_open_utc: new Date(e.t * 1000).toISOString(), ref_price: e.p, stop: e.stop, target: e.tgt, rr: e.rr, rr_note: e.rr_note,
      risk_pct_to_stop: e.risk, high_risk: e.hr, watch_invalidation: e.inv, breakout_level: e.bl, atr_pct: e.atr,
      realized_vol_30: e.rv, partial_in_window: e.pw, reason_he: e.why, rm: e.rm, ep: e.ep };
  }

  async function viewTM() {
    const t = S.tm || (S.tm = { ...TM_DEFAULT, asOf: null, revealed: false, horizon: 30 });
    const pool = S.meta.candidate_pool;
    $("#main").innerHTML = `<h2>מכונת זמן</h2>
      <p class="lead">בחר רגע בעבר וראה מה האסטרטגיות היו מציגות אז, רק מנתונים שהיו זמינים באותו רגע. העתיד נחשף רק בלחיצה נפרדת.</p>
      <div class="card">
        <div class="controls">
          <label class="f">תאריך (UTC)<input type="date" id="tmDate" value="${t.date}" min="2017-08-17" max="${new Date().toISOString().slice(0, 10)}"></label>
          <label class="f">שעה (UTC)<input type="time" id="tmTime" value="${t.time}" step="3600"></label>
          <label class="f">טווח<select id="tmTf"><option value="1d">יומי</option><option value="4h">4 שעות</option></select></label>
          <label class="f">אסטרטגיה<select id="tmSt"><option value="all">הכול (A, B, C)</option><option value="A-SMA50PB">A · SMA50</option><option value="B-BBSQ">B · בולינגר</option><option value="C-TRS">C · חוזק יחסי</option></select></label>
          <label class="f">מטבע לגרף<select id="tmCoin"><option value="">— בחר —</option>${pool.map((s) => `<option value="${s}">${s.replace(/USDT$/, "")}</option>`).join("")}</select></label>
        </div>
        <div class="btns">
          <button class="btn primary" id="tmRun">הפעל שחזור</button>
          <button class="btn" id="tmStep" ${t.asOf ? "" : "disabled"}>התקדם נר ◂</button>
          <button class="btn reveal" id="tmReveal" ${t.asOf && !t.revealed ? "" : "disabled"}>חשוף מה קרה בהמשך</button>
          <button class="btn" id="tmSave" ${t.asOf ? "" : "disabled"}>שמור תמונת מצב</button>
          <button class="btn" id="tmReset">אפס שחזור</button>
        </div>
      </div>
      <div id="tmOut"></div>`;
    $("#tmTf").value = t.tf; $("#tmSt").value = t.strategy; $("#tmCoin").value = t.coin;
    const read = () => { t.date = $("#tmDate").value; t.time = $("#tmTime").value || "00:00"; t.tf = $("#tmTf").value; t.strategy = $("#tmSt").value; t.coin = $("#tmCoin").value; };
    $("#tmRun").onclick = () => { read(); t.asOf = Math.floor(Date.parse(`${t.date}T${t.time}:00Z`) / 1000); t.revealed = false; viewTM(); };
    $("#tmStep").onclick = () => { read(); t.asOf += span(t.tf); const d = new Date(t.asOf * 1000).toISOString(); t.date = d.slice(0, 10); t.time = d.slice(11, 16); t.revealed = false; viewTM(); };
    $("#tmReveal").onclick = () => { t.revealed = true; viewTM(); };
    $("#tmReset").onclick = () => { S.tm = null; viewTM(); };
    $("#tmSave").onclick = () => saveSnapshot(t);
    $("#tmCoin").onchange = () => { read(); if (t.asOf) viewTM(); };
    if (t.asOf) await renderReplay(t);
    else $("#tmOut").innerHTML = `<div class="card" style="margin-top:12px"><ul class="limits">${S.meta.limits_he.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>`;
  }

  async function renderReplay(t) {
    const gw = S.gw;
    const st = await gw.replayState(t.tf, t.asOf);
    t.lastState = st;
    const entries = st.entries.filter((e) => t.strategy === "all" || e.st === t.strategy);
    const cTf = t.tf === "4h" && (t.strategy === "C-TRS" || t.strategy === "all");
    const items = entries.map((e) => fromTM(e, t.tf));
    const sig = items.filter((x) => x.state === "SIGNAL"), wat = items.filter((x) => x.state === "WATCH");
    const lastClose = st.last + span(t.tf);
    const out = $("#tmOut");
    out.innerHTML = `
      <div class="card panel-known" style="margin-top:12px">
        <span class="stamp known">ידוע בזמן השחזור</span>
        <p style="margin:8px 0 4px"><b>נקודת השחזור:</b> <span class="num">${utcTime(t.asOf)}</span> · <b>הנר האחרון שהיה זמין:</b> <span class="num">${utcTime(st.last)}</span> (${TFNAME[t.tf]}, נסגר ב-<span class="num">${utcTime(lastClose)}</span>)</p>
        <p class="note">מטבעות ברשימה באותו יום (לפי כלל נקודת-זמן): ${st.members ? st.members.length : "לא זמין"} מתוך ${S.meta.candidate_pool.length} מועמדים שיש לנו עליהם נתונים.
        ${cTf ? " · אסטרטגיה C פועלת רק בטווח היומי." : ""}</p>
        <details><summary>מגבלות השחזור</summary><ul class="limits">${S.meta.limits_he.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></details>
      </div>
      <div class="section-h signal">🟢 איתותים מאושרים בנר הזה <span class="count">(${sig.length})</span></div>
      ${sig.length ? `<div class="grid">${sig.map((x) => alertCard(x, { origin: "replay" })).join("")}</div>` : `<div class="empty">אין איתות מאושר בנר הזה.</div>`}
      <div class="section-h watch">🟡 התראות מעקב בנר הזה <span class="count">(${wat.length})</span></div>
      ${wat.length ? `<div class="grid">${wat.map((x) => alertCard(x, { origin: "replay", extra: x.ep !== Math.floor(Date.parse(x.candle_open_utc) / 1000) ? `<div class="note">המעקב התחיל בנר <span class="num">${utcTime(x.ep)}</span></div>` : "" })).join("")}</div>` : `<div class="empty">אין התראות מעקב בנר הזה.</div>`}
      <div id="tmChart"></div><div id="tmRevealOut"></div>`;
    bindTMCoins(out, t);
    if (!t.coin && items.length) t.coin = items[0].symbol;
    if (t.coin) await renderTMChart(t, items);
    if (t.revealed) await renderReveal(t, items);
  }

  function bindTMCoins(root, t) {
    root.querySelectorAll("[data-coin]").forEach((el) => {
      el.addEventListener("click", () => { t.coin = el.dataset.coin; $("#tmCoin").value = t.coin; viewTM(); });
    });
  }

  async function renderTMChart(t, items) {
    const rows = await S.gw.replayCandles(t.coin, t.tf, t.asOf, 300);
    const box = $("#tmChart");
    box.innerHTML = `<h3>${esc(t.coin.replace(/USDT$/, ""))} — הגרף נחתך בנקודת השחזור</h3><div class="card"><div id="tmPc" class="chart-wrap"></div>${legendHTML}</div>`;
    if (!rows.length) { $("#tmPc").innerHTML = `<div class="empty">אין נתונים למטבע הזה עד נקודת השחזור (לא נסחר אז, או שאין היסטוריה).</div>`; return; }
    const { chart, candles, pf } = priceChart($("#tmPc"), rows, t.tf);
    t.chart = { chart, candles, pf, rows };
    const mine = items.filter((x) => x.symbol === t.coin);
    const last = rows[rows.length - 1][0];
    candles.setMarkers([{ time: last, position: "aboveBar", color: "#1d4ed8", shape: "arrowDown", text: "כאן נעצר השחזור" }]);
    for (const x of mine) {
      const lines = x.state === "SIGNAL" ? [[x.stop, "#dc2626", "סטופ"], [x.target, "#16a34a", "יעד"]] : [[x.watch_invalidation, "#d97706", "ביטול"], [x.breakout_level, "#2563eb", "פריצה"]];
      lines.filter((l) => l[0]).forEach(([price, color, title]) => candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: `${LETTER[x.strategy]} ${title}` }));
    }
  }

  async function renderReveal(t, items) {
    const box = $("#tmRevealOut");
    const horizons = [["candle", "נר הבא"], [7, "7 ימים"], [30, "30 ימים"], [60, "60 ימים"], [90, "90 ימים"]];
    const sp = span(t.tf);
    const lastOpen = t.lastState.last;
    const until = t.horizon === "candle" ? lastOpen + sp : lastOpen + t.horizon * DAY;
    const rv = S.gw.reveal(t.asOf);
    let future = [];
    if (t.coin) future = await rv.candlesAfter(t.coin, t.tf, until);
    if (t.chart && future.length) {
      const fs = t.chart.chart.addCandlestickSeries({ upColor: "#a855f7", downColor: "#7e22ce", borderVisible: false, wickUpColor: "#a855f7", wickDownColor: "#7e22ce", priceFormat: t.chart.pf });
      fs.setData(future.map((r) => ({ time: r[0], open: r[1], high: r[2], low: r[3], close: r[4] })));
      t.chart.chart.timeScale().fitContent();
    }
    const ref = t.chart && t.chart.rows.length ? t.chart.rows[t.chart.rows.length - 1][4] : null;
    const end = future.length ? future[future.length - 1] : null;
    const hi = future.length ? Math.max(...future.map((r) => r[2])) : null, lo = future.length ? Math.min(...future.map((r) => r[3])) : null;
    const follow = [];
    for (const x of items.filter((x) => x.state === "WATCH")) {
      const after = await rv.alertsAfter(t.tf, x.symbol, x.strategy, until);
      const s = after.find((e) => e.k === "S");
      follow.push(`<li>${esc(x.base)} · ${LETTER[x.strategy]}: ${s ? `הפכה לאיתות מאושר ב-<span class="num">${utcTime(s.t)}</span>` : "לא הפכה לאיתות מאושר בתוך האופק"}</li>`);
    }
    box.innerHTML = `<div class="card panel-revealed" style="margin-top:12px">
      <span class="stamp revealed">נחשף לאחר נקודת השחזור — לא היה ידוע אז</span>
      <div class="chips" style="margin-top:10px">${horizons.map(([v, l]) => `<button class="chip" data-h="${v}" aria-pressed="${String(t.horizon) === String(v)}">${l}</button>`).join("")}</div>
      ${t.coin ? `<p><b>${esc(t.coin.replace(/USDT$/, ""))}</b>: ${future.length} נרות נחשפו (סגולים בגרף) עד <span class="num">${utcTime(until)}</span>.
        ${ref && end ? `סגירה בנקודת השחזור <span class="num">${fmt(ref)}</span> → סגירה בסוף האופק <span class="num">${fmt(end[4])}</span> (<span class="num">${pct((end[4] / ref - 1) * 100)}</span>) · שיא <span class="num">${fmt(hi)}</span> · שפל <span class="num">${fmt(lo)}</span>.` : "אין עדיין נרות אחרי נקודת השחזור."}
        <span class="note">שינוי מחיר בלבד — זו אינה תוצאת עסקה.</span></p>` : `<p class="note">בחר מטבע כדי לראות את התפתחות המחיר.</p>`}
      ${follow.length ? `<h3>האם התראות המעקב הפכו לאיתות מאושר?</h3><ul class="limits">${follow.join("")}</ul>` : ""}
      <h3>תוצאות עסקה מדומה ובנצ'מרקים</h3>
      <div class="empty">חישוב תוצאות עדיין לא זמין. כניסה בפתיחת הנר הבא, עמלות, מרווח, החלקה, יציאה לפי כללים קבועים, והשוואה ל-BTC ולקבוצות ביקורת — יחוברו למנוע הבדיקות ההיסטוריות המשותף בשלב 5. עד אז לא מוצגת שום תשואה.</div>
    </div>`;
    box.querySelectorAll("[data-h]").forEach((c) => c.addEventListener("click", () => { t.horizon = c.dataset.h === "candle" ? "candle" : Number(c.dataset.h); viewTM(); }));
  }

  function saveSnapshot(t) {
    if (!t.lastState) return;
    const snap = { origin: "replay", saved_at_utc: new Date().toISOString(), as_of_utc: new Date(t.asOf * 1000).toISOString(),
      timeframe: t.tf, strategy: t.strategy, last_available_candle_utc: new Date(t.lastState.last * 1000).toISOString(),
      revealed_when_saved: t.revealed, timeline_fingerprint: S.meta.timeline_fingerprint, code_commit: S.meta.code_commit,
      strategies: S.meta.strategies.map((s) => ({ ref: s.ref, params_hash: s.params_hash })),
      entries: t.lastState.entries.filter((e) => t.strategy === "all" || e.st === t.strategy), members: t.lastState.members, limits_he: S.meta.limits_he };
    const blob = new Blob([JSON.stringify(snap, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `crypto-radar-replay_${snap.as_of_utc.slice(0, 16).replace(/[:T]/g, "-")}_${t.tf}_${t.strategy}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function viewAbout() {
    const st = S.meta.strategies;
    $("#main").innerHTML = `<h2>אודות ומגבלות</h2>
      <div class="card"><h3 style="margin-top:0">מקור הנתונים</h3><p>Binance Spot דרך <span class="ltr">data-api.binance.vision</span> (ציבורי, ללא מפתח). נרות סגורים בלבד, יומי ו-4 שעות. גיבוי מתועד: OKX, ללא מעבר אוטומטי.</p>
        <p class="note">קוד: <span class="num">${esc(S.meta.code_commit)}</span> · נבנה: ${ilTime(new Date(S.meta.generated_utc))} · טביעת נתוני ציר הזמן: <span class="num">${esc(S.meta.timeline_fingerprint)}</span></p></div>
      ${st.map((s) => `<div class="card" style="margin-top:12px"><h3 style="margin-top:0">${esc(s.name_he)} <span class="badge">${esc(s.ref)}</span> <span class="badge">hash ${esc(s.params_hash)}</span></h3>
        <p class="note">טווחים: ${s.timeframes.map((x) => TFNAME[x]).join(", ")} · נדרשים ${s.min_candles} נרות</p>
        <div class="table-wrap"><table><thead><tr><th>#</th><th>כלל</th><th>🟡 מעקב</th><th>🟢 איתות</th></tr></thead><tbody>
        ${s.rules.map((r, i) => `<tr><td>${i + 1}</td><td>${esc(r.label_he)}</td><td>${r.watch ? "✓" : ""}</td><td>${r.signal ? "✓" : ""}</td></tr>`).join("")}</tbody></table></div>
        <details><summary>פרמטרים</summary><pre class="ltr" style="white-space:pre-wrap;font-size:12px">${esc(JSON.stringify(s.params, null, 1))}</pre></details></div>`).join("")}
      <div class="card" style="margin-top:12px"><h3 style="margin-top:0">בדיקת התאמה: מכונת הזמן מול שחזור אמיתי</h3>
        <div class="table-wrap"><table><thead><tr><th>טווח</th><th>נקודת זמן</th><th>פריטים</th><th>התאמה</th></tr></thead><tbody>
        ${S.meta.self_check.map((c) => `<tr><td>${TFNAME[c.timeframe]}</td><td class="num">${esc(c.as_of_utc)}</td><td class="num">${c.replay_items}</td><td>${c.match ? "✓" : "✗"}</td></tr>`).join("")}</tbody></table></div></div>
      <div class="card" style="margin-top:12px"><h3 style="margin-top:0">מגבלות</h3><ul class="limits">
        <li>${esc(S.meta.survivorship_he.replace(/\*\*/g, ""))}</li><li>${esc(S.meta.not_proof_he.replace(/\*\*/g, ""))}</li>
        ${S.meta.limits_he.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>`;
  }

  // ------------------------------------------------------------------ router
  async function route() {
    const [tab, arg] = (location.hash.slice(1) || "overview").split("/");
    document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
    try {
      if (tab === "strategies") viewStrategies();
      else if (tab === "coin") await viewCoin(arg);
      else if (tab === "journal") viewJournal();
      else if (tab === "tm") await viewTM();
      else if (tab === "about") viewAbout();
      else viewOverview();
    } catch (e) {
      $("#main").innerHTML = `<div class="banner">שגיאה בטעינת התצוגה: ${esc(e.message)}</div>`;
      console.error(e);
    }
  }

  async function init() {
    try {
      [S.meta, S.scan, S.journal, S.universe] = await Promise.all(["data/meta.json", "data/scan.json", "data/journal.json", "data/universe.json"].map(getJSON));
    } catch (e) {
      $("#main").innerHTML = `<div class="banner">לא ניתן לטעון את נתוני הסריקה (${esc(e.message)}). לא מוצגים נתונים חלופיים.</div>`;
      return;
    }
    S.gw = TMData.createGateway(getJSON, S.meta);
    renderFreshness();
    window.addEventListener("hashchange", route);
    route();
  }
  init();
})();
