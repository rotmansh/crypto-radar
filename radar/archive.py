"""Binance public data archive (data.binance.vision) — research dataset for the phase 5 backtest.

Why: the live store holds only the coins in today's universe, so a backtest built on it would only ever trade
today's survivors. The archive keeps monthly kline files for every spot pair ever listed, including delisted ones.

  list_symbols()                         every symbol directory under spot/monthly/klines (S3 listing, paginated)
  list_months(symbol, interval)          available monthly files
  fetch(symbol, interval, month, cache)  download once, verify against the official .CHECKSUM, cache the zip
  load(symbol, interval, months, cache)  parsed candles (radar.sources.base.Candle), in time order

Read-only public files, no keys. The files are reached through the bucket's S3 endpoint (same content as
https://data.binance.vision). Everything is cached under data/research_cache/ (not committed); the backtest
writes a manifest of every file with its sha256 so the dataset can be rebuilt exactly.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import requests

from .sources.base import Candle

BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
PREFIX = "data/spot/monthly/klines/"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
MONTH_RE = re.compile(r"-(\d{4}-\d{2})\.zip$")


class ArchiveError(RuntimeError):
    pass


def _get(url: str, params: dict | None = None, tries: int = 4) -> requests.Response:
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                raise ArchiveError(f"404 {url}")
        except requests.RequestException:
            if i == tries - 1:
                raise
        time.sleep(2 ** i)
    raise ArchiveError(f"HTTP {r.status_code} {url}")


def _list(prefix: str, delimiter: str | None) -> list[str]:
    """All keys (delimiter=None) or common prefixes under `prefix`, following pagination."""
    out, marker = [], ""
    while True:
        params = {"prefix": prefix, "marker": marker}
        if delimiter:
            params["delimiter"] = delimiter
        root = ET.fromstring(_get(BUCKET, params).content)
        if delimiter:
            items = [p.findtext(f"{NS}Prefix") for p in root.iter(f"{NS}CommonPrefixes")]
        else:
            items = [c.findtext(f"{NS}Key") for c in root.iter(f"{NS}Contents")]
        out += items
        if root.findtext(f"{NS}IsTruncated") != "true" or not items:
            return out
        marker = root.findtext(f"{NS}NextMarker") or items[-1]


def list_symbols() -> list[str]:
    return sorted(p[len(PREFIX):].strip("/") for p in _list(PREFIX, "/"))


def list_months(symbol: str, interval: str) -> list[str]:
    keys = _list(f"{PREFIX}{symbol}/{interval}/", None)
    return sorted({m.group(1) for k in keys if (m := MONTH_RE.search(k))})


def _name(symbol: str, interval: str, month: str) -> str:
    return f"{symbol}-{interval}-{month}.zip"


def fetch(symbol: str, interval: str, month: str, cache: Path) -> tuple[Path, str]:
    """Return (cached zip path, sha256). Downloads and checks the official checksum on first use."""
    name = _name(symbol, interval, month)
    path = cache / interval / symbol / name
    if path.exists():
        return path, hashlib.sha256(path.read_bytes()).hexdigest()
    url = f"{BUCKET}/{PREFIX}{symbol}/{interval}/{name}"
    body = _get(url).content
    want = _get(url + ".CHECKSUM").text.split()[0]
    got = hashlib.sha256(body).hexdigest()
    if got != want:
        raise ArchiveError(f"checksum mismatch {name}: {got} != {want}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path, got


QUIRKS: dict[str, int] = {"microsecond_rows_converted": 0, "malformed_rows_dropped": 0}


def parse(zip_path: Path) -> list[Candle]:
    """Kline CSV rows -> Candle. Timestamps are milliseconds; a few files (around delistings) mix in rows with
    microsecond timestamps, which are converted and counted. A row whose close time is not after its open time is
    malformed: dropped and counted, never repaired."""
    with zipfile.ZipFile(zip_path) as z:
        text = z.read(z.namelist()[0]).decode()
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].isdigit():
            continue  # some files carry a header line
        ot, ct = int(row[0]), int(row[6])
        if ot > 10**14 or ct > 10**14:
            ot, ct = ot // 1000, ct // 1000
            QUIRKS["microsecond_rows_converted"] += 1
        if ct <= ot:
            QUIRKS["malformed_rows_dropped"] += 1
            continue
        out.append(Candle(open_time=ot, open=float(row[1]), high=float(row[2]), low=float(row[3]),
                          close=float(row[4]), volume=float(row[5]), close_time=ct, quote_volume=float(row[7])))
    return out
