#!/usr/bin/env python3
"""「金叉 → 下水 → 二次金叉」的细化变体：差别到底在哪一步。

上一版（zero_axis_second_cross.py）的结论是：首次金叉贴近 0 轴的二次金叉不如
首次金叉远离 0 轴的二次金叉。那说明关键可能不在"0 轴附近"，而在别处。
这个脚本把形态拆成若干净，逐个加条件，看哪一步真正带来差异：

  V0 全部二次金叉（第一次与第二次之间有死叉且 DIF 下水）
  V1  首次金叉离 0 轴 ±1% 以内            （用户描述的牧原形态）
  V2  首次金叉离 0 轴 3% 以外            （深水首次金叉）
  V3  二次金叉时 DIF 已 > 0（跨上 0 轴）
  V4  二次金叉时 DEA 也 > 0（完全在 0 轴上）
  V5  二次金叉时 DIF 高于第一次金叉时的 DIF（动能抬升）
  V6  二次金叉当日放量（量比 ≥ 1.3）
  V7  二次金叉收盘站上 20 日最高收盘（突破箱体）
  V8  回撤 < 4%（浅回踩，几天震荡就回来）
  V9  两次金叉间隔 3~8 个交易日（短震荡）
  以及上述关键条件的组合。

所有条件只用信号日及之前的数据。指标同日相对超额（减掉当天全部金叉的平均超额）。

用法：python3 scripts/research/second_cross_variants.py
"""

from __future__ import annotations

import concurrent.futures
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_bottom_base import LONG_KLINE_DIR, benchmark_index  # noqa: E402
from screen_latest_30 import ema, rsi  # noqa: E402

HOLD = (5, 10, 20, 40)
PERIODS = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"),
           ("2026-07起", "2026-07", "9999"))
MIN_AMOUNT = 5e7


def scan(path: Path) -> list[dict]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return []
    if len(rows) < 260 or any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return []
    close = [r["close"] for r in rows]
    volume = [r["volume"] for r in rows]
    fast, slow = ema(close, 12), ema(close, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    r14 = rsi(close, 14)
    crosses = [k for k in range(1, len(rows)) if dif[k - 1] < dea[k - 1] and dif[k] >= dea[k]]
    deaths = [k for k in range(1, len(rows)) if dif[k - 1] > dea[k - 1] and dif[k] <= dea[k]]
    out = []
    for pos, k in enumerate(crosses):
        if k < 120 or k >= len(rows) - 1 or pos == 0:
            continue
        amt20 = sum(close[i] * volume[i] for i in range(k - 19, k + 1)) / 20
        if amt20 < MIN_AMOUNT:
            continue
        j = crosses[pos - 1]
        gap = k - j
        if not (2 <= gap <= 20):
            continue
        if not any(j < d < k for d in deaths):
            continue
        if min(dif[j:k + 1]) >= 0:          # DIF 没有真正下水
            continue
        price = close[k]
        jprice = close[j]
        dip = 1 - min(close[j:k + 1]) / jprice
        item = {
            "code": path.stem, "date": rows[k]["date"], "close": price,
            "gap": gap, "dip": dip * 100,
            "dif1": dif[j] / jprice * 100, "dea1": dea[j] / jprice * 100,
            "dif2": dif[k] / price * 100, "dea2": dea[k] / price * 100,
            "rise": (dif[k] / price - dif[j] / jprice) * 100,
            "vol_ratio": volume[k] / (sum(volume[k - 20:k]) / 20),
            "break20": price >= max(close[k - 20:k]),
            "dist_low": (price / min(r["low"] for r in rows[k - 59:k + 1]) - 1) * 100,
            "rsi": r14[k], "ret20": (price / close[k - 20] - 1) * 100,
            "amt20_yi": amt20 / 1e8,
            "low_age": k - max(i for i in range(k - 59, k + 1) if rows[i]["low"] == min(r["low"] for r in rows[k - 59:k + 1])),
        }
        entry = rows[k + 1]["open"]
        window = rows[k + 1:k + 21]
        if len(window) == 20:
            item["max_up20"] = (max(r["high"] for r in window) / entry - 1) * 100
            item["max_down20"] = (min(r["low"] for r in window) / entry - 1) * 100
        for h in HOLD:
            if k + h < len(rows):
                item[f"r{h}"] = (rows[k + h]["close"] / entry - 1) * 100
                item[f"d{h}"] = rows[k + h]["date"]
        out.append(item)
    return out


def collect() -> list[dict]:
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
    for h in HOLD:
        by_day: dict[str, list[float]] = defaultdict(list)
        for e in events:
            if f"x{h}" in e:
                by_day[e["date"]].append(e[f"x{h}"])
        means = {d: st.mean(v) for d, v in by_day.items()}
        for e in events:
            if f"x{h}" in e:
                e[f"rel{h}"] = e[f"x{h}"] - means[e["date"]]
    return events


def cell(sample: list[dict], key: str) -> str:
    if len(sample) < 30:
        return f"n{len(sample)}"
    return f"{st.mean(e[key] for e in sample):+.2f}（n{len(sample)}）"


VARIANTS = {
    "V0 全部二次金叉": lambda e: True,
    "V1 首次金叉 |DIF| ≤ 1%（牧原式）": lambda e: abs(e["dif1"]) <= 1,
    "V2 首次金叉 |DIF| ≥ 3%（深水首次）": lambda e: abs(e["dif1"]) >= 3,
    "V1+ 首次金叉在水下（DIF1 < 0）": lambda e: -1 <= e["dif1"] < 0,
    "V1− 首次金叉在水上（DIF1 ≥ 0）": lambda e: 0 <= e["dif1"] <= 1,
    "V3 二次金叉 DIF2 > 0": lambda e: e["dif2"] > 0,
    "V4 二次金叉 DEA2 > 0": lambda e: e["dea2"] > 0,
    "V5 二次 DIF 高于首次": lambda e: e["rise"] > 0,
    "V6 二次金叉放量（量比≥1.3）": lambda e: e["vol_ratio"] >= 1.3,
    "V7 二次金叉突破20日高点": lambda e: e["break20"],
    "V8 浅回踩（回撤<4%）": lambda e: e["dip"] < 4,
    "V9 短震荡（间隔3~8日）": lambda e: 3 <= e["gap"] <= 8,
    "— 组合 —": lambda e: False,
    "C1 V1+V3（水上二次）": lambda e: abs(e["dif1"]) <= 1 and e["dif2"] > 0,
    "C2 V1+V4（完全上0轴）": lambda e: abs(e["dif1"]) <= 1 and e["dea2"] > 0,
    "C3 V1+V7（突破箱体）": lambda e: abs(e["dif1"]) <= 1 and e["break20"],
    "C4 V1+V6（放量）": lambda e: abs(e["dif1"]) <= 1 and e["vol_ratio"] >= 1.3,
    "C5 V2+V3（深水首次+水上二次）": lambda e: abs(e["dif1"]) >= 3 and e["dif2"] > 0,
    "C6 V3+V7（水上二次+突破）": lambda e: e["dif2"] > 0 and e["break20"],
    "C7 V3+V7+V6（再加放量）": lambda e: e["dif2"] > 0 and e["break20"] and e["vol_ratio"] >= 1.3,
    "C8 V3+V7+距低点≤10%": lambda e: e["dif2"] > 0 and e["break20"] and e["dist_low"] <= 10,
}


def main() -> None:
    events = collect()
    print(f"二次金叉样本 {len(events)} 个（2023-07 起，剔除 2024-09）\n")
    print("同日的相对超额基准是「当天全部金叉」；下表为各组相对该基准的超额（百分点）。\n")
    for h in HOLD:
        print(f"## 持有 {h} 日\n")
        print("| 变体 | 同日相对 | 绝对超额 |")
        print("| --- | --- | --- |")
        for name, test in VARIANTS.items():
            if name.startswith("—"):
                print(f"| {name} | | |")
                continue
            sample = [e for e in events if test(e) and f"rel{h}" in e]
            print(f"| {name} | {cell(sample, f'rel{h}')} | {cell(sample, f'x{h}')} |")
        print()
    print("## 三段稳定性（持有 20 日，同日相对）\n")
    print("| 变体 | 前段 | 后段 | 新段 |")
    print("| --- | --- | --- | --- |")
    for name, test in VARIANTS.items():
        if name.startswith("—"):
            continue
        cells = []
        for _, lo, hi in PERIODS:
            part = [e for e in events if test(e) and lo <= e["date"] < hi and "rel20" in e]
            cells.append(cell(part, "rel20"))
        print(f"| {name} | " + " | ".join(cells) + " |")
    print()
    print("## 各条件命中率（占全部二次金叉）\n")
    total = len(events)
    for name, test in VARIANTS.items():
        if name.startswith("—"):
            continue
        hit = sum(1 for e in events if test(e))
        print(f"- {name}: {hit}（{hit / total:.0%}）")


if __name__ == "__main__":
    main()
