#!/usr/bin/env python3
"""MA / RSI 的最终裁决：逐日全样本，一次算清。

背景：前几轮用「每 3 日采样」且要求事件落在周最后交易日，得到 RSI<35 有 +0.814%（t+2.6）
的结果。改成逐日全样本后变成 -0.059%（t-0.9），符号翻转。本脚本用统一口径重算，
并解释差异来源，避免把采样假象当成结论。

口径（全部统一）：
  - 逐交易日（stride=1），不做任何日期采样
  - 池子：20日均额 ≥ 5000万
  - 去均值基准：当日该池内全部股票 10/20 日收益的均值
  - 超额 = 个股收益 − 当日均值

三张表：
  1. 底部池（距60日低点 ≤10%）里 RSI 十分位
  2. 真实 v2.6 候选池里 RSI 十分位（这是系统实际用的池子）
  3. RSI 与「距60日低点」的共线性，以及两者的偏效应

用法：python3 scripts/research/ma_rsi_verdict.py
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

from backtest_bottom_base import LONG_KLINE_DIR  # noqa: E402
from screen_latest_30 import rsi  # noqa: E402

MIN_AMOUNT = 5e7


def scan(file: Path) -> list[dict]:
    try:
        rows = json.loads(file.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return []
    if len(rows) < 260 or any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return []
    close = [r["close"] for r in rows]
    volume = [r["volume"] for r in rows]
    r14 = rsi(close, 14)
    out = []
    for k in range(120, len(rows) - 21):
        if r14[k] is None:
            continue
        amt20 = sum(close[i] * volume[i] for i in range(k - 19, k + 1)) / 20
        if amt20 < MIN_AMOUNT:
            continue
        low60 = min(r["low"] for r in rows[k - 59:k + 1])
        dist_low = close[k] / low60 - 1
        if dist_low > 0.10:
            continue
        hi20 = max(r["high"] for r in rows[k - 19:k + 1])
        lo20 = min(r["low"] for r in rows[k - 19:k + 1])
        ma15 = sum(close[k - 14:k + 1]) / 15
        ma30 = sum(close[k - 29:k + 1]) / 30
        in_v26 = (dist_low <= 0.08 and (hi20 - lo20) / close[k] <= 0.28
                  and close[k] <= ma15 * 1.03 and close[k] <= ma30 * 1.03)
        entry = rows[k + 1]["open"]
        item = {"code": file.stem, "date": rows[k]["date"], "rsi": r14[k],
                "distLow": dist_low, "inV26": in_v26,
                "ret20": close[k] / close[k - 20] - 1}
        for h in (10, 20):
            if k + h < len(rows):
                item[f"r{h}"] = rows[k + h]["close"] / entry - 1
        out.append(item)
    return out


def load() -> list[dict]:
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            events.extend(result)
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09")]
    return events


def demean(events: list[dict]) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if "r10" in e:
            by_day[e["date"]].append(e)
    for items in by_day.values():
        for h in (10, 20):
            pool = [x for x in items if f"r{h}" in x]
            if len(pool) < 10:
                continue
            mean = st.mean(x[f"r{h}"] for x in pool)
            for x in pool:
                x[f"y{h}"] = x[f"r{h}"] - mean
    return by_day


def daily_stat(by_day: dict[str, list[dict]], test, h: int = 10) -> tuple[float, float, int, int]:
    """按日等权，返回 (均值%, t, 有效日, 样本)。"""
    per = []
    for _d, items in by_day.items():
        sub = [x for x in items if f"y{h}" in x and test(x)]
        if len(sub) < 3:
            continue
        per.append(st.mean(x[f"y{h}"] for x in sub))
    if len(per) < 8:
        return (float("nan"), float("nan"), len(per), 0)
    m = st.mean(per)
    sd = st.stdev(per) if len(per) > 1 else 0
    return (m * 100, m / (sd / math.sqrt(len(per))) if sd else 0, len(per), 0)


def decile_table(by_day: dict[str, list[dict]], label: str, universe, h: int = 10) -> None:
    pool = [x for items in by_day.values() for x in items if f"y{h}" in x and universe(x)]
    if len(pool) < 5000:
        print(f"（{label} 样本不足 {len(pool)}）\n")
        return
    vals = sorted(x["rsi"] for x in pool)
    edges = [vals[min(len(vals) - 1, int(len(vals) * i / 10))] for i in range(11)]
    print(f"**{label}**（样本 {len(pool)}）\n")
    print("| RSI 区间 | 10日超额 | t | 样本 |")
    print("| --- | --- | --- | --- |")
    for i in range(10):
        lo, hi = edges[i], (edges[i + 1] if i < 9 else 1e9)
        test = (lambda a, b: (lambda e: a <= e["rsi"] < b))(lo, hi)
        m, t, days, _ = daily_stat(by_day, test, h)
        n = sum(1 for x in pool if lo <= x["rsi"] < hi)
        print(f"| {lo:.0f}~{hi:.0f} | {m:+.3f}% | t{t:+.1f} | {n} |")
    print()


def main() -> None:
    events = load()
    by_day = demean(events)
    print(f"逐日样本 {len(events)}，交易日 {len(by_day)}\n")

    print("## 一、底部池（距60日低点≤10%）RSI 十分位\n")
    decile_table(by_day, "全部底部池", lambda e: True)

    print("## 二、真实 v2.6 候选池 RSI 十分位\n")
    decile_table(by_day, "v2.6 候选池", lambda e: e["inV26"])

    print("## 三、RSI 与「距60日低点」的共线性\n")
    pool = [x for items in by_day.values() for x in items if "y10" in x]
    rv = sorted(x["rsi"] for x in pool)
    edges = [rv[min(len(rv) - 1, int(len(rv) * i / 5))] for i in range(6)]
    print("| RSI 档 | 距低点均值 | 20日涨幅中位 | 10日超额 |")
    print("| --- | --- | --- | --- |")
    for i in range(5):
        lo, hi = edges[i], (edges[i + 1] if i < 4 else 1e9)
        sub = [x for x in pool if lo <= x["rsi"] < hi]
        if not sub:
            continue
        m, _t, _d, _n = daily_stat(by_day, (lambda a, b: (lambda e: a <= e["rsi"] < b))(lo, hi))
        print(f"| {lo:.0f}~{hi:.0f} | {st.mean(x['distLow'] for x in sub) * 100:+.2f}% | "
              f"{st.median(x['ret20'] for x in sub) * 100:+.1f}% | {m:+.3f}% |")
    print()

    print("## 四、v2.6 形态本身相对底部池的表现\n")
    print("| 组 | 10日超额 | t | 20日超额 | t |")
    print("| --- | --- | --- | --- | --- |")
    for name, test in (("v2.6 形态内", lambda e: e["inV26"]),
                       ("非 v2.6 形态", lambda e: not e["inV26"]),
                       ("v2.6 内且 RSI<35", lambda e: e["inV26"] and e["rsi"] < 35),
                       ("v2.6 内且 RSI 28~45（现行）", lambda e: e["inV26"] and 28 <= e["rsi"] <= 45)):
        a = daily_stat(by_day, test, 10)
        b = daily_stat(by_day, test, 20)
        print(f"| {name} | {a[0]:+.3f}% | t{a[1]:+.1f} | {b[0]:+.3f}% | t{b[1]:+.1f} |")
    print()


if __name__ == "__main__":
    main()
