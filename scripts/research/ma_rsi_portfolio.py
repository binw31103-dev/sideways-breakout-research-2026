#!/usr/bin/env python3
"""把 MA/RSI 研究的结论落成一个可执行的每日组合，并做最后的证伪检验。

前面得到的事实：
  - 所有 MA/RSI 指标的横截面 IC 都为负（越低越好）
  - 「距 MA30」单独几乎无贡献（20日 +0.027%，t+0.1）
  - 「日 RSI14 < 35」10日 +0.814%（t+2.6），三段 +0.751 / +0.824 / +1.129
  - 当日触发只数越少越好：≤25 只时 10日 +1.360%（t+2.5）
  - RSI 的样本 100% 是 ret20 < 0（中位 -14.3%），但按 ret20 分档后仍有 +0.31~+1.03 的增量
  - 路径：58% 先跌 5%，29% 先涨 8%

这个脚本把上面的结论拼成一个组合：每天在「距60日低点≤10% + 流动性够」的池子里，
取 RSI 最低的 N 只，持 10 日，只在当日触发只数 ≤ 上限时才开仓。
用三种方式检验它是否真的能用：

  1. 按日组合收益（等权），三段分开看
  2. 与「同日随便买 N 只」的对照差
  3. 参数敏感性：N、RSI 阈值、广度上限、持有期
  4. 最大回撤与月度胜率

用法：python3 scripts/research/ma_rsi_portfolio.py
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


def load(stride: int = 3) -> dict[str, list[dict]]:
    jobs = [(p, stride, "bottom") for p in sorted(LONG_KLINE_DIR.glob("*.json"))]
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, jobs, chunksize=20):
            events.extend(result)
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09")
              and e["distLow"] <= 0.10]
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if "r10" in e:
            by_day[e["date"]].append(e)
    for items in by_day.values():
        for h in (5, 10, 20):
            pool = [x for x in items if f"r{h}" in x]
            if len(pool) < 10:
                continue
            mean = st.mean(x[f"r{h}"] for x in pool)
            for x in pool:
                x[f"y{h}"] = x[f"r{h}"] - mean
    return by_day


def run(by_day: dict[str, list[dict]], n_pick: int, rsi_max: float, breadth_cap: int | None,
        hold: int) -> dict[str, list[float]]:
    """按日等权组合：取 RSI 最低的 n_pick 只，持 hold 日。返回 {日期: [超额]}。"""
    out: dict[str, list[float]] = {}
    key = f"y{hold}"
    for date in sorted(by_day):
        items = by_day[date]
        pool = [x for x in items if x["rsi14"] < rsi_max and key in x]
        if len(pool) < n_pick:
            continue
        if breadth_cap and len(pool) > breadth_cap:
            continue
        picks = sorted(pool, key=lambda x: x["rsi14"])[:n_pick]
        out[date] = [x[key] for x in picks]
    return out


def random_control(by_day: dict[str, list[dict]], n_pick: int, breadth_cap: int | None,
                   hold: int) -> dict[str, list[float]]:
    """同一天、同一池子，随机取 n_pick 只（用 RSI 值做确定性伪随机，保证可复现）。"""
    out: dict[str, list[float]] = {}
    key = f"y{hold}"
    for date in sorted(by_day):
        items = by_day[date]
        pool = [x for x in items if key in x]
        if len(pool) < n_pick:
            continue
        if breadth_cap and len(pool) > breadth_cap * 4:
            continue
        ranked = sorted(pool, key=lambda x: (x["rsi14"] * 7919 + hash(x["code"]) % 997) % 1.0)
        picks = ranked[:n_pick]
        out[date] = [x[key] for x in picks]
    return out


def summarise(series: dict[str, list[float]]) -> dict:
    daily = {d: st.mean(v) for d, v in series.items() if v}
    if len(daily) < 10:
        return {"n": len(daily)}
    vals = list(daily.values())
    m = st.mean(vals)
    sd = st.stdev(vals) if len(vals) > 1 else 0
    by_month: dict[str, list[float]] = defaultdict(list)
    for d, v in daily.items():
        by_month[d[:7]].append(v)
    monthly = [st.mean(v) for v in by_month.values()]
    eq = 1.0
    peak, mdd = 1.0, 0.0
    for v in vals:
        eq *= 1 + v / 100
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    # 假设每日都开一笔、10日重叠持仓，粗略按"每天一笔"计
    return {"n": len(vals), "mean": m, "t": m / (sd / math.sqrt(len(vals))) if sd else 0,
            "win": sum(1 for v in vals if v > 0) / len(vals),
            "month_win": sum(1 for v in monthly if v > 0) / len(monthly) if monthly else 0,
            "months": len(monthly), "mdd": mdd}


def line(name: str, s: dict) -> str:
    if s.get("n", 0) < 10:
        return f"| {name} | n/a（{s.get('n', 0)} 日） | | | | | |"
    return (f"| {name} | {s['mean'] * 100:+.3f}% | t{s['t']:+.1f} | {s['win']:.0%} | "
            f"{s['month_win']:.0%}（{s['months']}月） | {s['mdd']:.1%} | {s['n']} |")


def main() -> None:
    by_day = load()
    print(f"交易日 {len(by_day)} 个\n")

    base = dict(n_pick=5, rsi_max=35, breadth_cap=25, hold=10)
    print("## 一、基准方案：距60日低点≤10%，RSI14<35，当日触发≤25只，取 RSI 最低 5 只，持 10 日\n")
    print("| 组 | 日均超额 | t | 日胜率 | 月胜率 | 逐日累计 MDD | 有效日 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    print(line("基准方案", summarise(run(by_day, **base))))
    print(line("对照：同日随机 5 只", summarise(random_control(by_day, 5, 25, 10))))
    print(line("对照：同日随机 5 只（不限广度）", summarise(random_control(by_day, 5, None, 10))))
    print(line("全部底部样本（等权）", summarise({d: [x["y10"] for x in v if "y10" in v]
                                                  for d, v in by_day.items() if any("y10" in x for x in v)})))
    print()
    for label, lo, hi in PERIODS:
        sub = {d: v for d, v in by_day.items() if lo <= d < hi}
        print(line(f"基准 · {label}", summarise(run(sub, **base))))
    print()

    print("## 二、参数敏感性（每行只改一个参数）\n")
    print("| 变体 | 日均超额 | t | 日胜率 | 月胜率 | MDD | 有效日 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for n in (3, 5, 10, 20):
        print(line(f"取 {n} 只", summarise(run(by_day, **{**base, "n_pick": n}))))
    for thr in (25, 30, 35, 40):
        print(line(f"RSI < {thr}", summarise(run(by_day, **{**base, "rsi_max": thr}))))
    for cap in (None, 60, 25, 15):
        print(line(f"广度上限 {cap or '不限'}", summarise(run(by_day, **{**base, "breadth_cap": cap}))))
    for h in (5, 10, 20):
        print(line(f"持有 {h} 日", summarise(run(by_day, **{**base, "hold": h}))))
    print()

    print("## 三、把广度闸门去掉会怎样（这是最关键的一条）\n")
    print("| 广度上限 | 日均超额 | t | 有效日 | 覆盖交易日 |")
    print("| --- | --- | --- | --- | --- |")
    total_days = len(by_day)
    for cap in (None, 200, 100, 60, 40, 25, 15):
        s = summarise(run(by_day, **{**base, "breadth_cap": cap}))
        if s.get("n", 0) < 10:
            print(f"| {cap or '不限'} | n/a | | | |")
            continue
        print(f"| {cap or '不限'} | {s['mean'] * 100:+.3f}% | t{s['t']:+.1f} | {s['n']} | "
              f"{s['n'] / total_days:.0%} |")
    print()


if __name__ == "__main__":
    main()
