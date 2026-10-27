/* Crypto Radar — Time Machine data gateway.
 *
 * The ONLY way the UI receives Time Machine data. In Replay mode it never returns a candle, alert or membership
 * row after the selected moment, even though a downloaded period file may contain later rows (they are dropped
 * here, before anything reaches the UI). Future data is available only through the object returned by
 * reveal(), which the UI creates when the user presses "חשוף מה קרה בהמשך".
 *
 * No strategy logic lives here: alerts come precomputed by the shared Python engine (scripts/build_site.py),
 * which verifies them against true truncated replays before publishing.
 */
(function (root) {
  "use strict";
  const SPAN = { "1d": 86400, "4h": 14400 };  // seconds
  const DAY = 86400;

  function chunkOf(t, tf) {
    const d = new Date(t * 1000);
    const y = d.getUTCFullYear();
    return tf === "1d" ? String(y) : `${y}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
  }

  /** Open time (s) of the newest candle that had closed by asOf (s). */
  function lastClosedOpen(asOf, tf) {
    const span = SPAN[tf];
    return Math.floor(asOf / span) * span - span;
  }

  function createGateway(fetchJson, meta) {
    const cache = new Map();
    const get = (path) => {
      if (!cache.has(path)) cache.set(path, fetchJson(path));
      return cache.get(path);
    };
    const candleChunks = (sym, tf) => ((meta.candles_index[sym] || {})[tf] || {}).chunks || [];
    const tmChunks = (tf) => (meta.tm_index[tf] || []);

    async function candlesBetween(sym, tf, fromT, toT) {
      const want = candleChunks(sym, tf).filter((c) => c >= chunkOf(fromT, tf) && c <= chunkOf(toT, tf));
      const parts = await Promise.all(want.map((c) => get(`data/candles/${tf}/${sym}/${c}.json`)));
      return parts.flat().filter((r) => r[0] >= fromT && r[0] <= toT);
    }

    /** Replay: candles of `sym` that had closed by asOf — nothing later. */
    async function replayCandles(sym, tf, asOf, lookbackCandles = 400) {
      const last = lastClosedOpen(asOf, tf);
      return candlesBetween(sym, tf, last - lookbackCandles * SPAN[tf], last);
    }

    /** Replay: alerts on the last closed candle at asOf (precomputed by the engine), and the universe then. */
    async function replayState(tf, asOf) {
      const last = lastClosedOpen(asOf, tf);
      const ch = chunkOf(last, tf);
      if (!tmChunks(tf).includes(ch)) return { last, entries: [], members: null };
      const body = await get(`data/tm/${tf}/${ch}.json`);
      const entries = body.entries.filter((e) => e.t === last);
      // membership: the latest daily row closed by this candle's close
      const dayKey = String((Math.floor((last + SPAN[tf]) / DAY) - 1) * DAY);
      let members = body.members[dayKey];
      if (!members) {
        const prev = await get(`data/tm/${tf}/${chunkOf(Number(dayKey), tf)}.json`).catch(() => null);
        members = prev ? prev.members[dayKey] || null : null;
      }
      return { last, entries, members };
    }

    /** Created only when the user asks to reveal. Gives access to what happened after asOf. */
    function reveal(asOf) {
      return {
        asOf,
        async candlesAfter(sym, tf, untilT) {
          const last = lastClosedOpen(asOf, tf);
          return (await candlesBetween(sym, tf, last + SPAN[tf], untilT));
        },
        /** Alerts of the same coin+strategy after asOf (e.g. did a WATCH become a SIGNAL within the horizon?). */
        async alertsAfter(tf, sym, strategy, untilT) {
          const last = lastClosedOpen(asOf, tf);
          const chunks = tmChunks(tf).filter((c) => c >= chunkOf(last, tf) && c <= chunkOf(untilT, tf));
          const bodies = await Promise.all(chunks.map((c) => get(`data/tm/${tf}/${c}.json`)));
          return bodies.flatMap((b) => b.entries)
            .filter((e) => e.t > last && e.t <= untilT && e.s === sym && e.st === strategy);
        },
      };
    }

    return { replayCandles, replayState, reveal, lastClosedOpen, chunkOf, SPAN };
  }

  const api = { createGateway, lastClosedOpen, chunkOf, SPAN };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.TMData = api;
})(typeof window !== "undefined" ? window : globalThis);
