"""Hebrew report for the strategy D in-sample run (summary dict from scripts/backtest_d.py). Presentation only."""
from __future__ import annotations

from backtest_report import KIND_HE, f

KIND_HE = dict(KIND_HE, breakeven="נקודת איזון", breakeven_gap="נקודת איזון בגאפ", trail_gap="סטופ נגרר בגאפ",
               review="בדיקת 72 שעות", trend="יציאת מגמה")
MGMT_HE = {"0.1.0": "איזון, בלי גרירה", "0.2.0": "איזון + גרירה 2·ATR", "0.3.0": "איזון + גרירה 3·ATR",
           "0.4.0": "בלי איזון, בלי גרירה", "0.5.0": "גרירה 2·ATR מהכניסה", "0.6.0": "גרירה 3·ATR מהכניסה"}


def render(S: dict) -> str:
    L = ["# אסטרטגיה D (מחקר): בדיקה היסטורית In-Sample", "",
         "> **In-Sample בלבד, ולא ראיה.** הכללים והגרסאות הוצהרו מראש ב-`docs/STRATEGY_D_SPEC.md`, לפני ההרצה. "
         "לא הורצה בדיקת Out-of-Sample, ו-D לא פועלת בסורק החי. התיקים וירטואליים (10,000 USDT). "
         "D תוכננה אחרי שתוצאות ה-In-Sample של A כבר היו ידועות, ולכן גם בגלל זה התוצאות כאן אופטימיות.", "",
         f"- **חלון:** {S['window_start_utc'][:10]} עד 2024-12-31 · **סדרות:** {' · '.join(f'{k} {v}' for k, v in S['series'].items())} · "
         f"**טביעת נתונים** `{S['data_fingerprint']}` · **קוד** `{S['code_commit']}` · נתוני 1H: `{S['dataset_1h']}`", ""]
    for tf in ("1h", "4h"):
        L += [f"## גרף {tf}", "",
              "| גרסה | ניהול | עסקאות | הצלחה | תוחלת R (95%) | PF | R ב-0× / 2× | חציון החזקה | תיק: סוף | CAGR | ירידה מקס' |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for v in MGMT_HE:
            s = S["streams"][f"D-SMA50SWING@{v}·{tf}"]
            t, p = s["trades"][1.0], s["portfolio"][1.0]["curve"]
            if not t.get("n"):
                L.append(f"| {v} | {MGMT_HE[v]} | 0 | — | — | — | — | — | — | — | — |")
                continue
            L.append(f"| {v} | {MGMT_HE[v]} | {t['n']} | {f(t['win_rate'], 1, True)} | {f(t['exp_r'], 3)} "
                     f"({f(t['exp_ci'][0], 3)} … {f(t['exp_ci'][1], 3)}) | {f(t['pf'])} | "
                     f"{f(s['trades'][0.0]['exp_r'], 3)} / {f(s['trades'][2.0]['exp_r'], 3)} | "
                     f"{f(s['hold'].get('median_hours'), 0)} שעות | ${p['final']:,.0f} | {f(p['cagr'], 1, True)} | {f(p['max_dd'], 1, True)} |")
        fl = S["filtered_signals"][tf]
        L += ["", f"- **איתותים שנפסלו:** {fl['stop_too_wide']} בגלל סטופ רחוק מ-3·ATR, ו-{fl['risk_below_4x_cost']} "
                  "בגלל סיכון שקטן מפי 4 מהעלויות. אלה נרות שעמדו בכל שאר הכללים.", ""]
        L += ["### נקודת איזון מול בלי (השוואה מזווגת: אותן כניסות, 1×)", "",
              "| זוג | כניסות משותפות | הפרש ממוצע R (95%) | עדיפה עם איזון / בלי |", "|---|---|---|---|"]
        for k, c in S["comparisons"][tf]["break_even"].items():
            if c.get("n"):
                L.append(f"| {k} | {c['n']} | {f(c['mean_diff'], 3)} ({f(c['ci'][0], 3)} … {f(c['ci'][1], 3)}) | "
                         f"{c['a_better']} / {c['b_better']} |")
        L += ["", "### גרירה מול בלי גרירה (מזווגת)", "",
              "| זוג | כניסות משותפות | הפרש ממוצע R (95%) | עדיפה עם גרירה / בלי |", "|---|---|---|---|"]
        for k, c in S["comparisons"][tf]["trail"].items():
            if c.get("n"):
                L.append(f"| {k} | {c['n']} | {f(c['mean_diff'], 3)} ({f(c['ci'][0], 3)} … {f(c['ci'][1], 3)}) | "
                         f"{c['a_better']} / {c['b_better']} |")
        L += ["", "### פירוט לפי גרסה", ""]
        for v in MGMT_HE:
            s = S["streams"][f"D-SMA50SWING@{v}·{tf}"]
            t = s["trades"][1.0]
            if not t.get("n"):
                continue
            h, st, c = s["hold"], s["portfolio"][1.0]["stats"], s["portfolio"][1.0]["curve"]
            ex = ", ".join(f"{KIND_HE.get(k, k)} {n}" for k, n in sorted(t["exits"].items(), key=lambda x: -x[1]))
            L += [f"**{v} · {MGMT_HE[v]}** (hash `{s['params_hash']}`)", "",
                  f"- **יציאות:** {ex}",
                  f"- **זמן החזקה:** עד 24 שעות {h['upto_24h']['n']} (תוחלת {f(h['upto_24h']['exp_r'], 3)}R) · 24–72 שעות "
                  f"{h['24_72h']['n']} ({f(h['24_72h']['exp_r'], 3)}R) · **מעל 72 שעות {h['over_72h']['n']} "
                  f"({f(h['over_72h']['exp_r'], 3)}R, סכום {f(h['over_72h']['sum_r'], 1)}R)**",
                  f"- **R:** חציון {f(t['median_r'], 3)} · גרוע {f(t['worst_r'])} · טוב {f(t['best_r'])} · הפסד מעבר ל-1R: "
                  f"{t['below_minus_1r']} · עלות ממוצעת {f(t['cost_r'], 3)}R · 10 העסקאות הטובות = "
                  f"{f(t['top10_share_of_sum'], 0, True)} מהסכום · בלי 5 הטובות: {f(t['exp_r_without_top5'], 3)}R",
                  f"- **תיק (1×):** Sharpe {f(c['sharpe'])} · זמן בשוק {f(s['portfolio'][1.0].get('time_in_market'), 1, True)} · "
                  f"נכנסו {st['taken']} · דולגו (10 פוזיציות) {st['skipped_max_positions']} · דולגו (מזומן) "
                  f"{st['skipped_no_cash']} · הוקטנו {st['reduced_by_cash']} · עמלות ${st['fees_usd']:,.0f} · "
                  f"החלקה ${st['slippage_usd']:,.0f} · הפסד מעל 1% {st['loss_over_1pct']}", ""]
        sel = S["selection"][tf]
        L += ["### בחירה (In-Sample בלבד)", "",
              (f"- נבחרה **`{sel['selected']}`** (hash `{sel['params_hash']}`), תוחלת {f(sel['exp_r_1x'], 3)}R על {sel['n']} "
               "עסקאות. היא תיבדק ב-Out-of-Sample פעם אחת, רק באישורך."
               if sel.get("selected") else f"- **לא נבחרה גרסה:** {sel['reason']}.")]
        mc = S["monte_carlo"].get(tf)
        if mc:
            L.append(f"- **כניסה אקראית ({mc['sims']} סימולציות, גרסה {mc['version']}):** התוחלת בפועל {f(mc['actual_exp_r'], 3)}R "
                     f"נמצאת באחוזון **{f(mc['percentile_of_actual'], 1)}**. בכניסות אקראיות: ממוצע "
                     f"{f(mc['mean_of_means'], 3)}R, 90% מהסימולציות בין {f(mc['p5'], 3)} ל-{f(mc['p95'], 3)}. "
                     f"מאגר הכניסות האפשריות: {mc['pool']:,} נרות.")
        L.append("")
    a = S.get("a_4h_phase5a")
    if a:
        L += ["## לצד אסטרטגיה A (4H, מתוך דוח 5a, בלי שינוי)", "",
              f"- A-SMA50PB@1.0.0·4h: {a['trades']['n']} עסקאות, תוחלת {f(a['trades']['exp_r'], 3)}R, "
              f"תיק ${a['portfolio']['final']:,.0f} (CAGR {f(a['portfolio']['cagr'], 1, True)}). "
              f"דוח `{a['report']}`. מוצג להשוואה בלבד; לא מערבבים בין התוצאות.", ""]
    b = S["benchmarks_1x"]
    L += ["## מדדי השוואה (1×, באותו חלון)", "",
          f"- קנה והחזק BTC: ${b['btc_buy_hold']['final']:,.0f} (CAGR {f(b['btc_buy_hold']['cagr'], 1, True)}, "
          f"ירידה מקסימלית {f(b['btc_buy_hold']['max_dd'], 1, True)})",
          f"- מגמת SMA200 על BTC: ${b['btc_sma200']['final']:,.0f} (CAGR {f(b['btc_sma200']['cagr'], 1, True)})",
          f"- שווה משקל, איזון חודשי: ${b['equal_weight_monthly']['final']:,.0f} (CAGR {f(b['equal_weight_monthly']['cagr'], 1, True)})",
          "", "## מגבלות", "",
          "- **בדיקות מרובות:** הוצגו 6 גרסאות לכל גרף. הגרסה הטובה ביותר מתוכן נראית טובה יותר ממה שהיא באמת.",
          "- **השוק פתוח 24/7,** ולכן כמעט אין פערי פתיחה. הפסדים מעבר ל-1R כמעט לא נוצרים במודל. במציאות, בקריסות חדות, "
          "הביצוע בסטופ גרוע יותר. בגרף שעה ההשפעה של זה גדולה יותר, כי הסטופים קרובים.",
          "- ההחלקה היא מודל לפי מדרגות מחזור. סדר התנועות בתוך הנר לא ידוע (הסטופ נחשב ראשון). התיק מחושב לפי מחירי סגירה.",
          "- **הטיית הישרדות:** מטבעות שנמחקו נכללים מהארכיון. חסרים צמדים שנמחקו לפני 2017-08. נתוני 1H מתחילים ב-2019-10.",
          "- **ההשוואה המזווגת** נעשית רק על כניסות משותפות: כלל \"עסקה אחת לכל מטבע\" תלוי בזמן היציאה, ולכן סט העסקאות "
          "קצת שונה בין הגרסאות.",
          "- **בקרת הכניסה האקראית** לא מחילה את כלל \"עסקה אחת לכל מטבע\".",
          "", "> ביצועי עבר אינם ראיה לרווחיות עתידית. אין כאן המלצת מסחר."]
    return "\n".join(L) + "\n"
