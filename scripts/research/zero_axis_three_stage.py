#!/usr/bin/env python3
"""零轴金叉：按「前期下跌 → 0 轴附近震荡 → 再起来」三段结构定义并检验。

文献口径（见 output/backtest/zero-axis-literature.md）：
  - 前期深幅下跌：绩优股约 30%，一般股约 50%，质劣股约 70%
  - 长期缩量横盘：60/120 日均线由下行转走平；成交量极度萎缩
  - 第一次上穿零轴不买（多为解套盘/试盘，常伴随洗盘）
  - 股价不再创新低后再次上穿零轴才是买点
  - 零轴下金叉可信度低；震荡区间越窄假信号越多
  - 量能：金叉放量 1.2~2 倍均量；20 日内金叉 ≥5 次判为震荡放弃

本脚本把三段各自量化，并做逐段增量检验：
  只测某一条件，相对「全部零轴金叉」是否有增量。
这样才知道哪一段真正有用、哪一段是文献的空话。

三段量化：
  D  下跌      距 120 日高点回撤 ≥ X%（默认 30%）
  S  震荡      近 N 日内 20 日振幅 ≤ Y%，且 60 日均线走平（斜率在 ±Z% 内），
               且均量萎缩（近 20 日均量 / 前 60 日均量 ≤ R）
  U  起来      DIF 上穿 DEA 且 DIF > 0（或在水下但上穿零轴），且当日放量

用法：python3 scripts/research/zero_axis_three_stage.py
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_bottom_base import LONG_KLINE_DIR, benchmark_index  # noqa: E402
from screen_latest_30 import ema, rsi  # noqa: E402

HOLD = (10, 20, 40)
MIN_AMOUNT = 5e7
PERIODS = (("前段", "2023-07", "2025-01"), ("后段", "2025-01", "2026-07"), ("新段", "2026-07", "9999"))


def scan(file: Path) -> list[dict]:
    try:
        rows = json.loads(file.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return []
    if len(rows) < 360 or any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return []
    close = [r["close"] for r in rows]
    volume = [r["volume"] for r in rows]
    fast, slow = ema(close, 12), ema(close, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    r14 = rsi(close, 14)

    def ma(n: int, k: int) -> float:
        if k - n + 1 < 0:
            return close[k]
        return sum(close[k - n + 1:k + 1]) / n

    out = []
    for k in range(250, len(rows) - 1):
        if r14[k] is None or not (dif[k - 1] < dea[k - 1] and dif[k] >= dea[k]):
            continue
        amt20 = sum(close[i] * volume[i] for i in range(k - 19, k + 1)) / 20
        if amt20 < MIN_AMOUNT:
            continue
        price = close[k]
        # --- D 前期下跌：从 120 日高点回落
        hi120 = max(r["high"] for r in rows[k - 119:k + 1])
        drawdown = 1 - price / hi120
        # --- S 震荡：20/60 日振幅、60 日均线斜率、量能萎缩
        amp20 = (max(r["high"] for r in rows[k - 19:k + 1])
                 - min(r["low"] for r in rows[k - 19:k + 1])) / price * 100
        amp60 = (max(r["high"] for r in rows[k - 59:k + 1])
                 - min(r["low"] for r in rows[k - 59:k + 1])) / price * 100
        m60, m60b = ma(60, k), ma(60, k - 20)
        slope60 = (m60 / m60b - 1) * 100
        vol20 = sum(volume[k - 19:k + 1]) / 20
        vol60 = sum(volume[k - 59:k + 1]) / 60
        vol_shrink = vol20 / vol60 if vol60 else 1.0
        # --- U 起来
        vol_ratio = volume[k] / vol20 if vol20 else 1.0
        # 金叉次数（震荡程度）
        cross_cnt = sum(1 for j in range(k - 19, k + 1)
                        if (dif[j] - dea[j]) * (dif[j - 1] - dea[j - 1]) < 0)
        # 是否创 60 日新低（趋势未止）
        new_low = min(r["low"] for r in rows[k - 19:k + 1]) <= min(
            r["low"] for r in rows[k - 59:k - 19]) * 1.005
        item = {
            "code": file.stem, "date": rows[k]["date"], "close": price,
            "drawdown": drawdown * 100, "amp20": amp20, "amp60": amp60,
            "slope60": slope60, "volShrink": vol_shrink, "volRatio": vol_ratio,
            "crossCnt": cross_cnt, "newLow": new_low,
            "difPct": dif[k] / price * 100, "deaPct": dea[k] / price * 100,
            "above0": dif[k] > 0,
            "distLow": (price / min(r["low"] for r in rows[k - 59:k + 1]) - 1) * 100,
        }
        entry = rows[k + 1]["open"]
        for h in HOLD:
            if k + h < len(rows):
                item[f"r{h}"] = (rows[k + h]["close"] / entry - 1) * 100
                item[f"d{h}"] = rows[k + h]["date"]
        out.append(item)
    return out


def load() -> list[dict]:
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            events.extend(result)
    index = benchmark_index()
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09")
              and e["date"] in index]
    for e in events:
        for h in HOLD:
            if f"r{h}" in e and e[f"d{h}"] in index:
                e[f"x{h}"] = e[f"r{h}"] - (index[e[f"d{h}"]] / index[e["date"]] - 1) * 100
    return events


def stat(events: list[dict], test, h: int = 20) -> dict:
    sub = [e for e in events if f"x{h}" in e and test(e)]
    if len(sub) < 30:
        return {"n": len(sub)}
    vals = [e[f"x{h}"] for e in sub]
    # 按月聚合的 t
    by_month: dict[str, list[float]] = defaultdict(list)
    for e in sub:
        by_month[e["date"][:7]].append(e[f"x{h}"])
    monthly = [st.mean(v) for v in by_month.values()]
    t = (st.mean(monthly) / (st.stdev(monthly) / math.sqrt(len(monthly)))
         if len(monthly) > 2 else 0)
    return {"n": len(sub), "mean": st.mean(vals), "t": t,
            "win": sum(1 for v in vals if v > 0) / len(vals)}


def row(events: list[dict], name: str, test, h: int = 20) -> str:
    s = stat(events, test, h)
    if s.get("n", 0) < 30:
        return f"| {name} | n{s.get('n', 0)} | — | — | — |"
    cells = []
    for _l, lo, hi in PERIODS:
        part = [e for e in events if test(e) and f"x{h}" in e and lo <= e["date"] < hi]
        cells.append(f"{st.mean(e[f'x{h}'] for e in part):+.2f}" if len(part) >= 20 else f"n{len(part)}")
    return (f"| {name} | {s['mean']:+.2f}（t{s['t']:+.1f}） | {s['win']:.0%} | "
            + " / ".join(cells) + f" | {s['n']} |")


def main() -> None:
    events = load()
    print(f"零轴金叉样本 {len(events)}（全部 AA/UU/XC 类型）\n")
    print("## 一、基准：全部零轴金叉\n")
    print("| 组 | 20日超额 | 胜率 | 前/后/新 | 样本 |")
    print("| --- | --- | --- | --- | --- |")
    for h in (10, 20, 40):
        s = stat(events, lambda e: True, h)
        print(f"| 全部金叉 持{h}日 | {s['mean']:+.2f}（t{s['t']:+.1f}） | {s['win']:.0%} | "
              f"— | {s['n']} |")
    print()

    print("## 二、逐段增量：单独加一个条件，能否跑赢全部金叉\n")
    print("| 条件 | 20日超额 | 胜率 | 前/后/新 | 样本 |")
    print("| --- | --- | --- | --- | --- |")
    print(row(events, "**D 回撤 ≥30%**", lambda e: e["drawdown"] >= 30))
    print(row(events, "D 回撤 ≥50%", lambda e: e["drawdown"] >= 50))
    print(row(events, "D 回撤 ≥70%", lambda e: e["drawdown"] >= 70))
    print(row(events, "D 回撤 <20%（不是深跌）", lambda e: e["drawdown"] < 20))
    print("| | | | | |")
    print(row(events, "**S 20日振幅 ≤15%**", lambda e: e["amp20"] <= 15))
    print(row(events, "S 20日振幅 ≤25%", lambda e: e["amp20"] <= 25))
    print(row(events, "S 20日振幅 >25%（太乱）", lambda e: e["amp20"] > 25))
    print(row(events, "S 60日均线走平（±3%）", lambda e: abs(e["slope60"]) <= 3))
    print(row(events, "S 60日线仍下行（<-3%）", lambda e: e["slope60"] < -3))
    print(row(events, "S 缩量（近20日均量/前60日 ≤0.8）", lambda e: e["volShrink"] <= 0.8))
    print(row(events, "S 放量（>1.2，未缩量）", lambda e: e["volShrink"] > 1.2))
    print(row(events, "S 20日内交叉≤2次（不是缠绕）", lambda e: e["crossCnt"] <= 2))
    print(row(events, "S 20日内交叉≥5次（缠绕）", lambda e: e["crossCnt"] >= 5))
    print(row(events, "S 未创60日新低", lambda e: not e["newLow"]))
    print("| | | | | |")
    print(row(events, "**U DIF>0（已上零轴）**", lambda e: e["above0"]))
    print(row(events, "U DIF<0（还在水下）", lambda e: not e["above0"]))
    print(row(events, "U 当日放量 ≥1.2倍", lambda e: e["volRatio"] >= 1.2))
    print(row(events, "U 当日放量 ≥2倍", lambda e: e["volRatio"] >= 2.0))
    print(row(events, "U 当日缩量 <0.8倍", lambda e: e["volRatio"] < 0.8))
    print()


if __name__ == "__main__":
    main()
