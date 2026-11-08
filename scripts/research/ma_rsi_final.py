#!/usr/bin/env python3
"""MA/RSI 方案的最后一道关：去掉重叠持仓造成的 t 值虚高，并做真正的样本外检验。

前一步的基准方案是 +1.038%（t+2.1，47 个有效日）。但那 47 天里每 3 天取一个信号、
每个信号持有 10 天，持仓区间**互相重叠**，相邻样本高度相关，
真实的独立样本远少于 47，t 值是被高估的。
逐日复利的 MDD（-0.1%）也毫无意义，因为叠加的是重叠的前瞻收益，不是资金曲线。

这个脚本用三种方式重新算：

  1. 不重叠队列：每隔 hold 个交易日才开一次仓，样本真正独立
  2. 按月分块 bootstrap：以「月」为独立单位重抽样，给出置信区间
  3. 完全样本外：只用前段（2023-07~2024-12）定参数，后段与前段之外做检验

另外检验：如果只用一个条件（RSI 或 MA），和两个条件相比差多少 —— 决定方案要不要复杂化。

用法：python3 scripts/research/ma_rsi_final.py
"""

from __future__ import annotations

import math
import random
import statistics as st
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts" / "research"))

from ma_rsi_portfolio import load  # noqa: E402

PERIODS = (("前段 2023-07~2024-12", "2023-07", "2025-01"),
           ("后段 2025-01~2026-06", "2025-01", "2026-07"),
           ("新段 2026-07 起", "2026-07", "9999"))


def pick_days(by_day: dict[str, list[dict]], n_pick: int, rsi_max: float,
              breadth_cap: int | None, hold: int, stride: int = 1) -> dict[str, list[float]]:
    """stride>1 时只在每 stride 个交易日开一次仓，避免持仓重叠。"""
    dates = sorted(by_day)
    out: dict[str, list[float]] = {}
    key = f"y{hold}"
    for i, date in enumerate(dates):
        if i % stride:
            continue
        items = by_day[date]
        pool = [x for x in items if x["rsi14"] < rsi_max and key in x]
        if len(pool) < n_pick:
            continue
        if breadth_cap and len(pool) > breadth_cap:
            continue
        picks = sorted(pool, key=lambda x: x["rsi14"])[:n_pick]
        out[date] = [x[key] for x in picks]
    return out


def stats(series: dict[str, list[float]]) -> dict:
    daily = {d: st.mean(v) for d, v in series.items() if v}
    vals = list(daily.values())
    n = len(vals)
    if n < 5:
        return {"n": n}
    m = st.mean(vals)
    sd = st.stdev(vals) if n > 1 else 0
    return {"n": n, "mean": m, "t": m / (sd / math.sqrt(n)) if sd else 0,
            "win": sum(1 for v in vals if v > 0) / n}


def show(name: str, s: dict) -> str:
    if s.get("n", 0) < 5:
        return f"| {name} | n/a（{s.get('n', 0)}） | | |"
    return f"| {name} | {s['mean'] * 100:+.3f}% | t{s['t']:+.1f} | {s['win']:.0%}（{s['n']}次） |"


def bootstrap(series: dict[str, list[float]], draws: int = 2000) -> tuple[float, float]:
    """按月分块 bootstrap，返回 5% / 95% 分位。"""
    from collections import defaultdict
    by_month: dict[str, list[float]] = defaultdict(list)
    for d, v in series.items():
        if v:
            by_month[d[:7]].append(st.mean(v))
    months = [st.mean(v) for v in by_month.values() if v]
    if len(months) < 6:
        return (float("nan"), float("nan"))
    rng = random.Random(20261107)
    means = []
    for _ in range(draws):
        sample = [rng.choice(months) for _ in months]
        means.append(st.mean(sample))
    means.sort()
    return means[int(draws * 0.05)], means[int(draws * 0.95)]


def main() -> None:
    by_day = load(stride=1)
    cfg = dict(n_pick=5, rsi_max=35, breadth_cap=25, hold=10)
    print(f"交易日 {len(by_day)} 个\n")

    print("## 一、去掉持仓重叠后，还剩多少\n")
    print("数据按逐交易日扫描。持 10 日需要 stride ≥ 10 才无重叠。\n")
    print("| 开仓间隔 | 日均超额 | t | 胜率 |")
    print("| --- | --- | --- | --- |")
    for stride in (1, 2, 3, 5, 10, 20):
        print(show(f"每 {stride} 个交易日一次", stats(pick_days(by_day, stride=stride, **cfg))))
    print()

    print("## 二、按月分块 bootstrap（独立单位=月）\n")
    for stride in (1, 10, 20):
        s = pick_days(by_day, stride=stride, **cfg)
        r = stats(s)
        if r.get("n", 0) < 5:
            continue
        lo, hi = bootstrap(s)
        print(f"- 间隔 {stride:2d} 日：均值 {r['mean'] * 100:+.3f}%，"
              f"90% 区间 [{lo * 100:+.3f}%, {hi * 100:+.3f}%]"
              f"{'（区间跨 0，不显著）' if lo < 0 < hi else '（区间不含 0）'}")
    print()

    print("## 三、三段表现（间隔 10 日，样本独立）\n")
    print("| 时段 | 日均超额 | t | 胜率 |")
    print("| --- | --- | --- | --- |")
    for label, lo, hi in PERIODS:
        sub = {d: v for d, v in by_day.items() if lo <= d < hi}
        print(show(label, stats(pick_days(sub, stride=10, **cfg))))
    print()

    print("## 四、方案要不要复杂化：单条件 vs 双条件（间隔 10 日）\n")
    print("| 方案 | 日均超额 | t | 胜率 |")
    print("| --- | --- | --- | --- |")
    variants = {
        "只 RSI<35，取最低 5 只": dict(n_pick=5, rsi_max=35, breadth_cap=25, hold=10),
        "只 RSI<35，不限广度": dict(n_pick=5, rsi_max=35, breadth_cap=None, hold=10),
        "只 RSI<30": dict(n_pick=5, rsi_max=30, breadth_cap=25, hold=10),
        "只 RSI<40": dict(n_pick=5, rsi_max=40, breadth_cap=25, hold=10),
    }
    for name, v in variants.items():
        print(show(name, stats(pick_days(by_day, stride=10, **v))))
    print()

    print("## 五、广度闸门到底值不值（间隔 10 日）\n")
    print("| 广度上限 | 日均超额 | t | 胜率 |")
    print("| --- | --- | --- | --- |")
    for cap in (None, 200, 100, 60, 40, 25, 15):
        print(show(f"{cap or '不限'}", stats(pick_days(by_day, stride=10, **{**cfg, "breadth_cap": cap}))))
    print()


if __name__ == "__main__":
    main()
