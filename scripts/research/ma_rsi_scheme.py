#!/usr/bin/env python3
"""MA / RSI 信号：找出真正可用的组合，并检验它是不是「普跌效应」的伪装。

前一步的 IC 检验发现：所有 MA/RSI 指标的横截面 IC 都是负的（越低越好），
但在控制了「距 60 日低点」之后 RSI 的 IC 几乎归零（-0.004 ~ -0.016）。
同时发现一个矛盾：
  RSI14 < 30 按「每日等权」算 20 日超额 +0.209%（t 0.6）
  但把样本汇总起来算，同一批样本是 +0.953%
两者差 4 倍，说明**信号的效果取决于当天有多少只股票同时满足**。

这个脚本就检验这件事，并给出最终可用的组合：

  1. 广度效应：当天满足条件的股票越多，信号越差？
     如果是，那说明"孤立的超卖"和"全市场普跌"是两件事，只有前者可用。

  2. 组合筛选：在广度可控的前提下，哪组 MA/RSI 条件三段同向、t 值最高。

  3. 分段稳定性：每个候选组合在三段的表现。

  4. 路径：先摸 +8% 还是先跌 -5%。

用法：python3 scripts/research/ma_rsi_scheme.py
"""

from __future__ import annotations

import concurrent.futures
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backtest_bottom_base import LONG_KLINE_DIR  # noqa: E402
from ma_rsi_ic import scan  # noqa: E402

PERIODS = (("前段", "2023-07", "2025-01"), ("后段", "2025-01", "2026-07"), ("新段", "2026-07", "9999"))


def load(limit_low: float = 0.10, stride: int = 3) -> list[dict]:
    jobs = [(p, stride, "bottom") for p in sorted(LONG_KLINE_DIR.glob("*.json"))]
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, jobs, chunksize=20):
            events.extend(result)
    return [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09")
            and e["distLow"] <= limit_low]


def attach_excess(events: list[dict]) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if "r20" in e:
            by_day[e["date"]].append(e)
    for items in by_day.values():
        for h in (10, 20):
            key = f"r{h}"
            pool = [x for x in items if key in x]
            if len(pool) < 10:
                continue
            mean = st.mean(x[key] for x in pool)
            for x in pool:
                x[f"y{h}"] = x[key] - mean
    return by_day


def evaluate(by_day: dict[str, list[dict]], test, h: int = 20) -> dict:
    """按日等权，返回均值/t/有效日/日均只数。"""
    key = f"y{h}"
    per, counts = [], []
    for _d, items in by_day.items():
        sub = [x for x in items if key in x and test(x)]
        if len(sub) < 2:
            continue
        per.append(st.mean(x[key] for x in sub))
        counts.append(len(sub))
    if len(per) < 8:
        return {"mean": None, "t": None, "days": len(per), "per_day": 0}
    m = st.mean(per)
    sd = st.stdev(per) if len(per) > 1 else 0
    return {"mean": m, "t": m / (sd / math.sqrt(len(per))) if sd else 0,
            "days": len(per), "per_day": st.mean(counts)}


def fmt(r: dict, h: int = 20) -> str:
    if r["mean"] is None:
        return f"n/a（{r['days']} 日）"
    return f"{r['mean'] * 100:+6.3f}%（t{r['t']:+.1f}，{r['days']}日，日均{r['per_day']:.0f}只）"


def breadth_test(by_day: dict[str, list[dict]]) -> None:
    print("## 一、广度效应：当天满足条件的股票越多，信号越差？\n")
    for name, test in (("RSI14 < 30", lambda e: e["rsi14"] < 30),
                       ("RSI14 < 35", lambda e: e["rsi14"] < 35),
                       ("距MA30 < -8%", lambda e: e["d30"] < -0.08)):
        counts = []
        for _d, items in by_day.items():
            sub = [x for x in items if "y20" in x and test(x)]
            if len(sub) >= 2:
                counts.append((len(sub), st.mean(x["y20"] for x in sub)))
        if len(counts) < 20:
            continue
        counts.sort()
        third = max(1, len(counts) // 3)
        print(f"**{name}**\n")
        print("| 当天满足只数 | 20日相对超额 |")
        print("| --- | --- |")
        for label, part in (("最少 1/3 天（孤立超卖）", counts[:third]),
                            ("中间 1/3 天", counts[third:2 * third]),
                            ("最多 1/3 天（全市场普跌）", counts[2 * third:])):
            print(f"| {label}（{st.median(c[0] for c in part):.0f} 只） | "
                  f"{st.mean(c[1] for c in part) * 100:+.3f}% |")
        print()


def scheme_table(by_day: dict[str, list[dict]]) -> None:
    print("## 二、候选组合（20 日，按日等权）\n")
    print("| 组合 | 全部 | 前段 | 后段 | 新段 |")
    print("| --- | --- | --- | --- | --- |")
    base = lambda e: e["rsi14"] < 35 and e["d30"] < -0.05
    combos = {
        "RSI<35 且 距MA30<-5%": base,
        "RSI<35 且 距MA60<-8%": lambda e: e["rsi14"] < 35 and e["d60"] < -0.08,
        "RSI<35 且 距MA30<-5% 且 MA空头排列": lambda e: base(e) and e["align"] == 0,
        "RSI<35 且 距MA30<-5% 且 RSI回升": lambda e: base(e) and e["rsiChg5"] > 0,
        "RSI<35 且 距MA30<-5% 且 周RSI<35": lambda e: base(e) and e["rsiW"] < 35,
        "RSI<30 且 距MA30<-8%": lambda e: e["rsi14"] < 30 and e["d30"] < -0.08,
        "RSI<40 且 距MA30<-8%": lambda e: e["rsi14"] < 40 and e["d30"] < -0.08,
        "距MA30<-8% 且 MA10斜率<-2%": lambda e: e["d30"] < -0.08 and e["s10"] < -0.02,
        "距MA60<-10% 且 RSI<40": lambda e: e["d60"] < -0.10 and e["rsi14"] < 40,
    }
    for name, test in combos.items():
        cells = [fmt(evaluate(by_day, test))]
        for _label, lo, hi in PERIODS:
            sub = {d: v for d, v in by_day.items() if lo <= d < hi}
            r = evaluate(sub, test)
            cells.append(f"{r['mean'] * 100:+.3f}（{r['days']}日）" if r["mean"] is not None else f"n/a")
        print(f"| {name} | " + " | ".join(cells) + " |")
    print()


def breadth_conditioned(by_day: dict[str, list[dict]]) -> None:
    """把广度作为条件：只在「当天满足条件的不多」时才用信号。"""
    print("## 三、加上广度条件后（只在孤立超卖时用）\n")
    print("| 组合 | 全部 | 前段 | 后段 | 新段 |")
    print("| --- | --- | --- | --- | --- |")

    def make(test, max_count):
        def run(day_items):
            sub = [x for x in day_items if test(x)]
            if len(sub) < 2 or len(sub) > max_count:
                return None
            scored = [x["y20"] for x in sub if "y20" in x]
            return st.mean(scored) if scored else None
        return run

    for name, test in (("RSI<35 且 距MA30<-5%", lambda e: e["rsi14"] < 35 and e["d30"] < -0.05),
                       ("RSI<30 且 距MA30<-8%", lambda e: e["rsi14"] < 30 and e["d30"] < -0.08)):
        for cap in (40, 80, 150):
            runner = make(test, cap)
            cells = []
            for lo, hi in (("0000", "9999"),) + tuple((a, b) for _, a, b in PERIODS):
                per = []
                for d, items in by_day.items():
                    if not (lo <= d < hi):
                        continue
                    v = runner(items)
                    if v is not None:
                        per.append(v)
                if len(per) < 8:
                    cells.append("n/a")
                    continue
                m = st.mean(per)
                sd = st.stdev(per) if len(per) > 1 else 0
                t = m / (sd / math.sqrt(len(per))) if sd else 0
                cells.append(f"{m * 100:+.3f}（t{t:+.1f}，{len(per)}日）")
            print(f"| {name} ≤{cap}只 | " + " | ".join(cells) + " |")
    print()


def main() -> None:
    events = load()
    print(f"样本 {len(events)}（距60日低点 ≤10%，每 3 日采样）\n")
    by_day = attach_excess(events)
    breadth_test(by_day)
    scheme_table(by_day)
    breadth_conditioned(by_day)


if __name__ == "__main__":
    main()
