#!/usr/bin/env python3
"""Today's bar for every stock from several quote platforms, cross-checked.

Used by the 14:45 intraday screen. No platform carries the whole job alone:
  Tencent  qt.gtimg.cn        full market, batches of 60 (also market caps)
  Sina     hq.sinajs.cn       full market, batches of 80
  Eastmoney push2delay clist  only when Tencent or Sina is missing many stocks (delayed feed)
A stock's bar is "双源一致" when two platforms agree on the previous close (<=0.1%) and the
latest price (<= PRICE_TOLERANCE, quotes are taken a minute apart while trading). One
platform only -> "单源"; platforms disagree -> dropped. Market caps come from Tencent or
Eastmoney, else from output/cache/shares.json (share counts from the last good snapshot).

Intraday volume is scaled to a full-day estimate by 1 / volume_share (14:45 ≈ 92.6% of the
day on a 160-stock sample, scripts/research/intraday_profile.py).
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SHARES_PATH = ROOT / "output" / "cache" / "shares.json"
PRICE_TOLERANCE = 0.008
PREV_TOLERANCE = 0.001
SINA_BATCH = 80


def symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def _sina_batch(codes: list[str]) -> dict[str, dict[str, Any]]:
    raw = b""
    for attempt in range(3):
        raw = subprocess.run(
            ["curl", "-sS", "-m", "15", "-H", "User-Agent: Mozilla/5.0", "-H", "Referer: https://finance.sina.com.cn",
             "https://hq.sinajs.cn/list=" + ",".join(symbol(c) for c in codes)],
            capture_output=True,
        ).stdout
        if raw:
            break
        time.sleep(1 + attempt)
    out = {}
    for line in raw.decode("gbk", errors="replace").splitlines():
        if '"' not in line or "=" not in line:
            continue
        code = line.split("=")[0][-6:]
        f = line.split('"')[1].split(",")
        if len(f) < 32 or code not in codes or not f[3] or float(f[3]) <= 0:
            continue
        out[code] = {
            "date": f[30], "time": f[31][:8], "open": float(f[1]), "prevClose": float(f[2]), "close": float(f[3]),
            "high": float(f[4]), "low": float(f[5]), "volume": float(f[8]), "amount": float(f[9]), "source": "新浪",
        }
    return out


def fetch_sina(codes: list[str]) -> dict[str, dict[str, Any]]:
    batches = [codes[k:k + SINA_BATCH] for k in range(0, len(codes), SINA_BATCH)]
    out: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for part in executor.map(_sina_batch, batches):
            out.update(part)
    return out


def fetch_tencent(codes: list[str]) -> dict[str, dict[str, Any]]:
    from screen_red_zone_5 import fetch_tencent_daily_quotes
    quotes = fetch_tencent_daily_quotes(codes)
    for q in quotes.values():
        q["source"] = "腾讯"
    return quotes


def fetch_eastmoney() -> dict[str, dict[str, Any]]:
    from screen_red_zone_5 import fetch_eastmoney_daily_quotes
    quotes = fetch_eastmoney_daily_quotes()
    for q in quotes.values():
        q["source"] = "东财"
    return quotes


def agree(a: dict[str, Any], b: dict[str, Any]) -> bool:
    pa, pb = a.get("prevClose"), b.get("prevClose")
    if pa and pb and abs(pa / pb - 1) > PREV_TOLERANCE:
        return False
    return abs(a["close"] / b["close"] - 1) <= PRICE_TOLERANCE


def load_shares() -> dict[str, list[float]]:
    return json.loads(SHARES_PATH.read_text(encoding="utf-8")) if SHARES_PATH.exists() else {}


def save_shares(quotes: dict[str, dict[str, Any]], date: str) -> None:
    shares = load_shares()
    for code, q in quotes.items():
        if q.get("totalMarketCap") and q.get("close"):
            shares[code] = [q["totalMarketCap"] / q["close"], (q.get("floatMarketCap") or 0) / q["close"], date]
    SHARES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHARES_PATH.write_text(json.dumps(shares), encoding="utf-8")


def merged_quotes(codes: list[str], today: str, volume_share: float) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Cross-checked bars for `today`, volume scaled to a full-day estimate."""
    started = time.time()
    results: dict[str, dict[str, dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(fetch_tencent, codes): "腾讯", executor.submit(fetch_sina, codes): "新浪"}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = {c: q for c, q in future.result().items() if q.get("date") == today}
            except Exception as exc:
                results[name], errors[name] = {}, str(exc)
    if min(len(results.get("腾讯", {})), len(results.get("新浪", {}))) < 0.8 * len(codes):
        try:
            results["东财"] = {c: q for c, q in fetch_eastmoney().items() if q.get("date") == today}
        except Exception as exc:
            results["东财"], errors["东财"] = {}, str(exc)

    shares = load_shares()
    merged: dict[str, dict[str, Any]] = {}
    counts = {"双源一致": 0, "单源": 0, "不一致剔除": 0, "无行情": 0}
    for code in codes:
        have = [(name, results[name][code]) for name in ("腾讯", "新浪", "东财") if code in results.get(name, {})]
        if not have:
            counts["无行情"] += 1
            continue
        pick = None
        if len(have) == 1:
            pick, check = have[0][1], "单源"
        else:
            pairs = [(a, b) for i, a in enumerate(have) for b in have[i + 1:] if agree(a[1], b[1])]
            if pairs:
                pick, check = pairs[0][0][1], "双源一致"
            else:
                counts["不一致剔除"] += 1
                continue
        counts[check] += 1
        bar = {k: pick[k] for k in ("date", "open", "high", "low", "close") if k in pick}
        bar["volume"] = pick["volume"] / volume_share
        bar["amount"] = pick.get("amount", 0) / volume_share
        bar["rawVolume"] = pick["volume"]
        bar["prevClose"] = pick.get("prevClose")
        caps = next((q for _, q in have if q.get("totalMarketCap")), None)
        if caps:
            ratio = bar["close"] / caps["close"]
            bar["totalMarketCap"], bar["floatMarketCap"] = caps["totalMarketCap"] * ratio, caps.get("floatMarketCap", 0) * ratio
        elif code in shares:
            bar["totalMarketCap"], bar["floatMarketCap"] = shares[code][0] * bar["close"], shares[code][1] * bar["close"]
        bar["sources"] = "/".join(name for name, _ in have)
        bar["check"] = check
        bar["source"] = f"盘中{bar['sources']}（{check}）"
        merged[code] = bar
    save_shares({c: q for name in ("腾讯", "东财") for c, q in results.get(name, {}).items()}, today)
    summary = {
        "date": today, "volumeShare": volume_share, "seconds": round(time.time() - started, 1),
        "perSource": {name: len(v) for name, v in results.items()}, "errors": errors, "counts": counts,
        "builtAt": dt.datetime.now().isoformat(timespec="seconds"),
    }
    return merged, summary
