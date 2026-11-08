#!/usr/bin/env python3
"""诊断：零轴金叉规则在 2026-11-06 缓存上逐级过滤后剩多少只。

不联网。直接读 output/cache/kline/2026-11-06/*.json 与 output/cache/quotes/2026-11-06.json，
逐级打印每一步剩下多少只，用来区分"规则真的选不到"和"实现或数据日期有 bug"。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from screen_latest_30 import ema  # noqa: E402
from screen_v26_unified import calculate_metrics  # noqa: E402
from screen_red_zone_5 import MARKET_POOL_DEFAULTS  # noqa: E402
from kline_units import load_payload  # noqa: E402

AS_OF = sys.argv[1] if len(sys.argv) > 1 else "2026-11-06"
KLINE = ROOT / "output" / "cache" / "kline" / AS_OF
QUOTES = ROOT / "output" / "cache" / "quotes" / f"{AS_OF}.json"
limits = MARKET_POOL_DEFAULTS["core"]

quotes = json.loads(QUOTES.read_text(encoding="utf-8"))
quotes = quotes.get("quotes", quotes) if isinstance(quotes, dict) else {}
print(f"quote snapshot: {len(quotes)} 只")

stage = {"文件": 0, "日期=当日": 0, "市值池": 0, "当日水下金叉": 0, "贴近零轴1%": 0,
         "距低≤8%": 0, "振幅20≤12%": 0, "5日涨幅≤3%": 0, "量比≤1.2": 0}
candidates_near = []

for path in sorted(KLINE.glob("*.json")):
    stage["文件"] += 1
    code = path.stem
    try:
        payload = load_payload(path)
    except Exception:
        continue
    rows = payload["rows"]
    q = quotes.get(code) or {}
    scale = 1.0  # shared loader guarantees shares; quote snapshots also use shares
    if q and str(q.get("date") or "") >= rows[-1]["date"]:
        last = {"date": q.get("date"), "open": q.get("open"), "close": q.get("price") or q.get("close"),
                "high": q.get("high"), "low": q.get("low"), "volume": (q.get("volume") or 0) / scale}
        if last["date"] == rows[-1]["date"]:
            rows[-1] = last
        else:
            rows.append(last)
    if rows[-1]["date"] != AS_OF:
        continue
    stage["日期=当日"] += 1

    total_cap = float(q.get("totalMarketCap") or 0)
    float_cap = float(q.get("floatMarketCap") or 0)
    if total_cap < limits["minTotalMarketCap"] or float_cap < limits["minFloatMarketCap"]:
        continue
    amount20 = sum(r["close"] * r["volume"] * scale for r in rows[-20:]) / 20
    if amount20 < limits["minAverageAmount20"]:
        continue
    stage["市值池"] += 1

    closes = [r["close"] for r in rows]
    fast, slow = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    k = len(rows) - 1
    if not (dif[k - 1] < dea[k - 1] and dif[k] >= dea[k] and dif[k] < 0 and dea[k] < 0):
        continue
    stage["当日水下金叉"] += 1

    price = closes[k]
    if abs(dif[k]) / price > 0.01:
        continue
    stage["贴近零轴1%"] += 1
    candidates_near.append((code, dif[k] / price))

    metrics = calculate_metrics(rows[-260:])
    if metrics is None:
        continue
    if metrics["riseRecentLow"] > 8:
        continue
    stage["距低≤8%"] += 1
    if metrics["range20"] > 12:
        continue
    stage["振幅20≤12%"] += 1
    if metrics["change5"] > 3:
        continue
    stage["5日涨幅≤3%"] += 1
    vol20 = sum(r["volume"] for r in rows[-21:-1]) / 20
    if vol20 <= 0 or rows[k]["volume"] / vol20 > 1.2:
        continue
    stage["量比≤1.2"] += 1

print("\n逐级过滤：")
for key, value in stage.items():
    print(f"  {key:14s} {value}")
print(f"\n当日贴近零轴的水下金叉（未加位置条件）：{len(candidates_near)} 只")
for code, pct in candidates_near[:20]:
    print(f"  {code}  |DIF|/价 = {abs(pct):.3%}")
