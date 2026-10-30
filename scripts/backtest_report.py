"""Hebrew report for the phase 5a in-sample backtest (summary dict from scripts/backtest.py). Presentation only."""
from __future__ import annotations

import math

KIND_HE = {"stop": "סטופ", "stop_gap": "סטופ בגאפ", "target": "יעד", "time": "יציאת זמן", "trail": "סטופ נגרר",
           "sma50": "סגירה מתחת ל-SMA50", "period_end": "סוף התקופה", "data_end": "סוף הנתונים (מחיקה)"}
SKIP_HE = {"gap_below_stop": "פתיחה מתחת לסטופ", "already_in_position": "כבר בפוזיציה במטבע",
           "no_next_candle": "אין נר כניסה", "target_at_or_below_entry": "יעד לא מעל הכניסה", "no_risk": "סיכון לא חיובי"}
BENCH_HE = {"btc_buy_hold": "קנה והחזק BTC", "btc_sma200": "מגמת SMA200 על BTC",
            "equal_weight_monthly": "שווה משקל, איזון חודשי"}


def f(x, d=2, pct=False):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "∞" if isinstance(x, float) and math.isinf(x) else "—"
    return f"{x * 100:,.{d}f}%" if pct else f"{x:,.{d}f}"


def label(tm: dict) -> str:
    n = tm.get("n", 0)
    if n < 30:
        return "מדגם קטן מדי — אין מסקנה"
    base = "" if n >= 100 else "תוצאה ראשונית (30–99 עסקאות) · "
    return base + "In-Sample בלבד — לא ראיה"


def verdict(s: dict) -> str:
    t0, t1, t2 = (s["trades"][m] for m in (0.0, 1.0, 2.0))
    if t1.get("n", 0) < 30:
        return "מדגם קטן מדי"
    if t1["exp_ci"][1] < 0:
        return "תוחלת שלילית (גם הגבול העליון של רווח הסמך מתחת ל-0)"
    if t1["exp_r"] <= 0 < t0["exp_r"]:
        return "לא שורדת עלויות"
    if t1["exp_r"] <= 0:
        return "תוחלת שלילית או אפסית"
    if t2["exp_r"] <= 0:
        return "חיובית ב-1×, לא שורדת 2×"
    if t1["exp_ci"][0] <= 0:
        return "חיובית, אבל רווח הסמך כולל 0"
    return "חיובית ב-1× וב-2× (In-Sample בלבד)"


def render(S: dict) -> str:
    L = [
        "# שלב 5a — בדיקה היסטורית In-Sample", "",
        "> **In-Sample בלבד, ולא ראיה.** הכללים נקבעו מראש ב-`docs/PHASE5_PLAN.md`, לפני ההרצה. לא הורצה בדיקת "
        "Out-of-Sample. ביצועי עבר אינם ראיה לרווחיות עתידית. התיקים וירטואליים (10,000 USDT) ואין בהם מסחר אמיתי.",
        "",
        f"- **חלון:** {S['window_start_utc'][:10]} עד 2024-12-31 (נתונים עד `{S['data_end_utc']}`)",
        f"- **נתונים:** ארכיון Binance הרשמי (`{S['dataset']}`), {S['series_1d']} סדרות יומיות, {S['series_4h']} סדרות 4H. "
        f"טביעת נתונים `{S['data_fingerprint']}` · קוד `{S['code_commit']}`",
        f"- **יקום:** {S['universe_size_at_start']} מטבעות בתחילת החלון, חציון {S['universe_size_median']:.0f} לאורך החלון",
        "", "## תמונה כללית (עלויות 1×)", "",
        "| זרם | עסקאות | הצלחה | תוחלת R (95%) | PF | R ב-0× / 2× | הפסד < −1R | תיק: סוף | CAGR | ירידה מקס' | הערכה |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, s in S["streams"].items():
        t = s["trades"][1.0]
        p = s["portfolio"][1.0]["curve"]
        if not t.get("n"):
            L.append(f"| {name}{' (מחקר)' if s['research'] else ''} | 0 | — | — | — | — | — | — | — | — | אין עסקאות |")
            continue
        L.append(f"| {name}{' (מחקר)' if s['research'] else ''} | {t['n']} | {f(t['win_rate'], 1, True)} | "
                 f"{f(t['exp_r'], 3)} ({f(t['exp_ci'][0], 3)} … {f(t['exp_ci'][1], 3)}) | {f(t['pf'])} | "
                 f"{f(s['trades'][0.0]['exp_r'], 3)} / {f(s['trades'][2.0]['exp_r'], 3)} | {t['below_minus_1r']} | "
                 f"${p['final']:,.0f} | {f(p['cagr'], 1, True)} | {f(p['max_dd'], 1, True)} | {verdict(s)} |")
    L += ["", "## מדדי השוואה (באותו חלון)", "",
          "| מדד | עלויות | סוף | תשואה | CAGR | ירידה מקס' | Sharpe | הערות |", "|---|---|---|---|---|---|---|---|"]
    for m, b in S["benchmarks"].items():
        for k, v in b.items():
            c = v["curve"]
            extra = ", ".join(f"{a}: {f(x, 0) if isinstance(x, float) else x}" for a, x in v.items() if a != "curve")
            L.append(f"| {BENCH_HE[k]} | {float(m):g}× | ${c['final']:,.0f} | {f(c['total_return'], 1, True)} | "
                     f"{f(c['cagr'], 1, True)} | {f(c['max_dd'], 1, True)} | {f(c['sharpe'])} | {extra} |")

    L += ["", "## פירוט לפי זרם", ""]
    for name, s in S["streams"].items():
        t1 = s["trades"][1.0]
        L += [f"### {name}{' — גרסת מחקר' if s['research'] else ''} (hash `{s['params_hash']}`)", "",
              f"**{label(t1)}** · {verdict(s)}", ""]
        if not t1.get("n"):
            L += ["אין עסקאות בחלון.", ""]
            continue
        L += ["| עלויות | עסקאות | הצלחה (95%) | תוחלת R (95%) | חציון R | PF | R גרוע | R טוב | סכום R |",
              "|---|---|---|---|---|---|---|---|---|"]
        for m in (0.0, 1.0, 2.0):
            t = s["trades"][m]
            L.append(f"| {m:g}× | {t['n']} | {f(t['win_rate'], 1, True)} ({f(t['win_ci'][0], 1, True)}–{f(t['win_ci'][1], 1, True)}) | "
                     f"{f(t['exp_r'], 3)} ({f(t['exp_ci'][0], 3)} … {f(t['exp_ci'][1], 3)}) | {f(t['median_r'], 3)} | "
                     f"{f(t['pf'])} | {f(t['worst_r'])} | {f(t['best_r'])} | {f(t['sum_r'], 1)} |")
        sk = ", ".join(f"{SKIP_HE.get(k, k)} {v}" for k, v in s["skipped"].items()) or "אין"
        ex = ", ".join(f"{KIND_HE.get(k, k)} {v}" for k, v in sorted(t1["exits"].items(), key=lambda x: -x[1]))
        hist = " · ".join(f"{k}: {v}" for k, v in t1["r_hist"].items())
        L += ["", f"- **לא נכנסו:** {sk}",
              f"- **יציאות:** {ex}",
              f"- **התפלגות R (1×):** {hist}",
              f"- **הפסד גדול מ-1R (גאפ):** {t1['below_minus_1r']} עסקאות · סטופ ויעד באותו נר: {t1['same_candle_stop_target']} · "
              f"סטופ רחוק מ-10% (סיכון גבוה): {t1['high_risk']}",
              f"- **עלות ממוצעת לעסקה:** {f(t1['cost_r'], 3)}R · החזקה ממוצעת: {f(t1['avg_bars'], 1)} נרות",
              f"- **ריכוזיות:** 10 העסקאות הטובות ביותר הן {f(t1['top10_share_of_sum'], 0, True)} מסכום ה-R. "
              f"בלי 5 הטובות ביותר, התוחלת היא {f(t1['exp_r_without_top5'], 3)}R"]
        np_ = s["no_partial_window_1x"]
        if np_.get("n"):
            L.append(f"- **רגישות, בלי נר חלקי ב-20 הנרות שלפני האיתות:** {np_['n']} עסקאות, תוחלת {f(np_['exp_r'], 3)}R")
        L += ["", "| תיק (1×) | |", "|---|---|"]
        p = s["portfolio"][1.0]
        c, st = p["curve"], p["stats"]
        L += [f"| שווי סופי | ${c['final']:,.0f} ({f(c['total_return'], 1, True)}) |",
              f"| CAGR · תנודתיות | {f(c['cagr'], 1, True)} · {f(c['vol'], 1, True)} |",
              f"| ירידה מקסימלית · הארוכה ביותר | {f(c['max_dd'], 1, True)} · {c['longest_dd_days']} ימים |",
              f"| Sharpe · Sortino | {f(c['sharpe'])} · {f(c['sortino'])} |",
              f"| זמן בשוק | {f(p.get('time_in_market'), 1, True)} |",
              f"| עסקאות שנכנסו · דולגו (10 פוזיציות) · דולגו (אין מזומן) · הוקטנו (מזומן) | {st['taken']} · "
              f"{st['skipped_max_positions']} · {st['skipped_no_cash']} · {st['reduced_by_cash']} |",
              f"| עמלות · החלקה (משוער) | ${st['fees_usd']:,.0f} · ${st['slippage_usd']:,.0f} |",
              f"| הפסד מעל 1% מהתיק · הגרוע ביותר | {st['loss_over_1pct']} · {f(st['worst_trade_pct_equity'], 2)}% |",
              f"| מזומן מינימלי | ${p['min_cash']:,.2f} |",
              f"| מתאם · בטא מול BTC (יומי) | {f(p['vs_btc']['corr'])} · {f(p['vs_btc']['beta'])} |",
              f"| שווי סופי ב-0× / 2× | ${s['portfolio'][0.0]['curve']['final']:,.0f} / ${s['portfolio'][2.0]['curve']['final']:,.0f} |",
              "", "| שנה | עסקאות | תוחלת R | סכום R |", "|---|---|---|---|"]
        L += [f"| {y} | {v['n']} | {f(v['exp_r'], 3)} | {f(v['sum_r'], 1)} |" for y, v in s["yearly_1x"].items()]
        L.append("")

    L += ["## בחירת גרסת המחקר A-TAIL (In-Sample בלבד, סעיף 11)", ""]
    for tf, sel in S["tail_selection"].items():
        L.append(f"- **{tf}:** " + (f"נבחרה `{sel['selected']}` (hash `{sel['params_hash']}`), תוחלת {f(sel['exp_r_1x'], 3)}R "
                                    f"על {sel['n']} עסקאות. בדיקת Out-of-Sample שלה תרוץ פעם אחת, רק אחרי אישור."
                                    if sel["selected"] else f"לא נבחרה גרסה ({sel['reason']})."))
        if sel["selected"] and sel["exp_r_1x"] <= 0:
            L.append("  - **שימו לב:** גם הגרסה שנבחרה מפסידה ב-In-Sample. הבחירה רק מדרגת בין הגרסאות, ואינה אומרת שיש יתרון.")
    sv = S["survivorship"]
    xc = sv.get("cross_check_vs_api") or {}
    L += ["", "## הטיית הישרדות ואמינות הנתונים", "",
          f"- ביקום ההיסטורי היו {sv.get('ever_members')} סדרות, ו-{sv.get('ever_members_not_in_live_store')} מהן "
          "אינן במאגר החי של היום (מטבעות שנמחקו או שירדו מהיקום). כולן נכללו מהארכיון.",
          f"- **השוואה לנתוני ה-API** על צמדים משותפים: {xc.get('compared')} נרות יומיים, {xc.get('price_mismatched')} פערי מחיר, "
          f"ו-{xc.get('volume_only_differences')} פערים קלים בנפח בלבד.",
          "- **מה עדיין חסר:** צמדים שנמחקו לפני שהארכיון התחיל (אוגוסט 2017) ושווקים שאינם USDT. אין היסטוריה של החלטות "
          "סוג נכס, כך שההחרגות הן של היום, בתוספת בדיקת דמוי מטבע יציב לפי כל תאריך. הטוקנים הממונפים הוחרגו לפי שם.",
          "", "## מגבלות", "",
          "- אין היסטוריה של ספר הפקודות, ולכן ההחלקה היא מודל לפי מדרגות מחזור ולא מדידה.",
          "- סדר התנועות בתוך הנר לא ידוע. לכן, כשהסטופ והיעד נוגעים באותו נר, מניחים שהסטופ נפגע קודם.",
          "- **הפסד מעבר ל-1R כמעט לא יכול להופיע במודל הזה:** השוק פתוח 24/7, ולכן כל נר נפתח כמעט במחיר הסגירה של הקודם, "
          "ואין כמעט פערי פתיחה. במציאות, בקריסות חדות (מרץ 2020, מאי 2021, LUNA, FTX) ביצוע בסטופ יכול להיות גרוע בהרבה "
          "מ-`stop × (1 − החלקה)`. לכן היעדר הפסדים מעל 1R הוא מגבלה של המודל, ולא סימן לבטיחות.",
          "- נר נחשב זמין רק מרגע הסגירה. כניסה בפתיחה הבאה מניחה ביצוע מיידי.",
          "- התיק מחושב לפי מחירי סגירה, ולכן ירידה מקסימלית בתוך הנר לא נמדדת.",
          "- מטבע שהנתונים שלו נגמרים (מחיקה) נסגר בסגירה האחרונה. במציאות היציאה סביב מחיקה גרועה יותר, ולכן זו הערכה אופטימית.",
          "- זו בדיקת In-Sample אחת, על כללים שהוגדרו מראש. אין בה כניסה אקראית (Monte Carlo, שלב 5b) ואין Out-of-Sample.",
          "", "> ביצועי עבר אינם ראיה לרווחיות עתידית. אין כאן המלצת מסחר."]
    return "\n".join(L) + "\n"
