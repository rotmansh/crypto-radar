"""Notification infrastructure — prepared, NOT active.

New live alerts are turned into Hebrew messages and queued in an append-only outbox
(data/outbox/<YYYY-MM>.jsonl) with status "queued_not_sent". Nothing is sent anywhere: `send()` refuses unless
notifications are explicitly enabled in config/settings.toml AND a channel has been implemented and approved —
neither is the case today. No trading, no account access.
"""
from __future__ import annotations

import json
from pathlib import Path

LETTER = {"A-SMA50PB": "A", "B-BBSQ": "B", "C-TRS": "C"}


class NotificationsDisabled(RuntimeError):
    pass


def message(row: dict) -> dict:
    """One outbox message for a newly journaled live alert."""
    kind = "🟢 איתות מאושר" if row["type"] == "SIGNAL" else "🟡 התראת מעקב (לא כניסה)"
    base = row["symbol"].removesuffix("USDT")
    lines = [f"{kind} · {base} · {LETTER.get(row['strategy'], row['strategy'])} · {row['timeframe']}",
             f"נר {row['candle_open_utc']} · מחיר {row['ref_price']}"]
    if row["type"] == "SIGNAL":
        lines.append(f"סטופ {row['stop']} · יעד {row['target'] or '—'}")
        if str(row.get("high_risk")) == "True":
            lines.append("⚠️ סיכון גבוה — לא המלצת כניסה")
    else:
        lines.append(f"ביטול ההתראה: {row['watch_invalidation']}")
    lines.append("אינו ייעוץ השקעות.")
    return {"id": row["signal_id"], "created_utc": row["detected_at_utc"], "status": "queued_not_sent",
            "channel": None, "symbol": row["symbol"], "type": row["type"], "strategy": row["strategy"],
            "text_he": "\n".join(lines)}


def queue(rows: list[dict], outbox_dir: Path, month: str) -> int:
    """Append messages for live_new alerts (not 'already active' ones) to the outbox; never twice for one id."""
    outbox_dir.mkdir(parents=True, exist_ok=True)
    seen = set()
    for p in outbox_dir.glob("*.jsonl"):
        seen |= {json.loads(line)["id"] for line in p.read_text().splitlines() if line.strip()}
    new = [message(r) for r in rows if r.get("origin") == "live_new" and r["signal_id"] not in seen]
    if new:
        with (outbox_dir / f"{month}.jsonl").open("a") as f:
            for m in new:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    return len(new)


def send(msg: dict, settings: dict) -> None:
    """Deliberately not implemented. Sending requires enabling + an approved channel (a future phase)."""
    cfg = settings.get("notifications", {})
    if not cfg.get("enabled", False):
        raise NotificationsDisabled("notifications are disabled (config/settings.toml [notifications])")
    raise NotificationsDisabled("no notification channel has been implemented or approved yet")
