#!/usr/bin/env python3
"""给"0 轴附近金叉"这份研究做三项防守测试。任何一条不通过，这条规则就不能用。

1. 参数敏感性：|DIF|/收盘价 本身是参数（12/26/9）的函数。换 (8,17,9)、(10,22,8)、
   (12,26,9)、(12,30,10)，看「离 0 轴近」这个结论是否还在。只在原参数下成立的效应，
   多半是参数造成的假象。

2. 对照：非金叉日的"平时样本"。金叉日天然是价格短期偏强的一天，而且 DIF 贴近 0 意味着
   MA12≈MA26，也就是低波动/横盘。所以要把"金叉 + 近 0 轴"和"同样的低波动、同样的距低点，
   但不是金叉"作对比。如果后者一样好，那 MACD 就没有增量信息。

3. 显著性：同上日超额做按月聚合，算跨月 t 值。全样本几万个样本的均值很容易"显著"，
   但收益是同日强相关的，按月看更接近真实不确定性。

用法：python3 scripts/research/zero_axis_robustness.py
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
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backtest_bottom_base import LONG_KLINE_DIR, benchmark_index  # noqa: E402
from zero_axis_rule_study import build_events  # noqa: E402

HOLD = (10, 20)
NEAR = 1.0
PARAMS = ((12, 26, 9), (8, 17, 9), (10, 22, 8), (12, 30, 10))


def monthly_stats(sample: list[dict], key: str) -> tuple[float, float, int]:
    by_month: dict[str, list[float]] = defaultdict(list)
    for e in sample:
        if key in e:
            by_month[e["date"][:7]].append(e[key])
    if len(by_month) < 6:
        return 0.0, 0.0, 0
    means = [st.mean(v) for v in by_month.values()]
    t = st.mean(means) / (st.stdev(means) / len(means) ** 0.5)
    return st.mean(means), t, len(means)


def param_table() -> None:
    print("## 一、参数敏感性：换 MACD 参数后，各类别「离 0 轴近」是否还是更好\n")
    print("每格：0~0.3% 档 − 1~3% 档 的同日相对超额差（正数=越近 0 轴越好），括号内为 0~0.3% 档值\n")
    for fast, slow, signal in PARAMS:
        events = build_events(fast, slow, signal)
        print(f"### MACD({fast},{slow},{signal})，金叉 {len(events)} 个")
        print("| 类型 | 10日：0~0.3% | 10日：1~3% | 差 | 20日：0~0.3% | 20日：1~3% | 差 |")
        print("| --- | --- | --- | --- | --- | --- | --- |")
        for side, name in (("UU", "纯水下"), ("AA", "纯水上")):
            row = [name]
            gaps = []
            for h in HOLD:
                near = [e for e in events if e["side"] == side and f"rel{h}" in e and abs(e["dif_pct"]) < 0.3]
                mid = [e for e in events if e["side"] == side and f"rel{h}" in e and 1.0 <= abs(e["dif_pct"]) < 3.0]
                a = st.mean(e[f"rel{h}"] for e in near) if len(near) > 20 else float("nan")
                b = st.mean(e[f"rel{h}"] for e in mid) if len(mid) > 20 else float("nan")
                row += [f"{a:+.2f}（n{len(near)}）", f"{b:+.2f}（n{len(mid)}）"]
                gaps.append(a - b)
            row.insert(3, f"{gaps[0]:+.2f}")
            row.append(f"{gaps[1]:+.2f}")
            print("| " + " | ".join(row) + " |")
        print()


def control_scan(args: tuple[Path, int]) -> list[dict]:
    """每 5 个交易日取一个「平时」样本，只保留与金叉组可比的低波动、近低点样本。"""
    path, stride = args
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return []
    if len(rows) < 260:
        return []
    if any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return []
    close = [r["close"] for r in rows]
    out = []
    for k in range(180, len(rows) - 1, stride):
        recent = rows[k - 19:k + 1]
        amt20 = sum(r["close"] * r["volume"] for r in recent) / 20
        if amt20 < 5e7:
            continue
        price = rows[k]["close"]
        low60 = min(r["low"] for r in rows[k - 59:k + 1])
        high60 = max(r["high"] for r in rows[k - 59:k + 1])
        amp20 = (max(r["high"] for r in recent) - min(r["low"] for r in recent)) / price
        entry = rows[k + 1]["open"]
        item = {"code": path.stem, "date": rows[k]["date"],
                "dist_low": (price / low60 - 1) * 100,
                "pos_range": (price - low60) / (high60 - low60) * 100 if high60 > low60 else 100,
                "amp20": amp20 * 100,
                "ret5": (price / close[k - 5] - 1) * 100}
        for h in HOLD:
            if k + h < len(rows):
                item[f"r{h}"] = (rows[k + h]["close"] / entry - 1) * 100
                item[f"d{h}"] = rows[k + h]["date"]
        out.append(item)
    return out


def control_table(events: list[dict]) -> None:
    jobs = [(p, 5) for p in sorted(LONG_KLINE_DIR.glob("*.json"))]
    base: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(control_scan, jobs, chunksize=20):
            base.extend(result)
    index = benchmark_index()
    base = [e for e in base if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09")
            and e["date"] in index]
    for e in base:
        for h in HOLD:
            if f"r{h}" in e and e[f"d{h}"] in index:
                e[f"x{h}"] = e[f"r{h}"] - (index[e[f"d{h}"]] / index[e["date"]] - 1) * 100
    print("## 二、对照：同样的「低波动 + 贴着底部」，但不是金叉日\n")
    print("| 组 | 样本 | 10日 绝对超额 | 20日 绝对超额 |")
    print("| --- | --- | --- | --- |")
    for label, test in (
        ("对照：非金叉日（振幅≤12%，距低点≤10%）", lambda e: e["amp20"] <= 12 and e["dist_low"] <= 10),
        ("对照：非金叉日（振幅≤12%，距低点≤10%，5日涨幅≤3%）",
         lambda e: e["amp20"] <= 12 and e["dist_low"] <= 10 and e["ret5"] <= 3),
    ):
        sample = [e for e in base if test(e) and "x10" in e and "x20" in e]
        print(f"| {label} | n{len(sample)} | {st.mean(e['x10'] for e in sample):+.2f} | "
              f"{st.mean(e['x20'] for e in sample):+.2f} |")
    cross = [e for e in events if abs(e["dif_pct"]) <= NEAR and e["amp20"] <= 12
             and e["dist_low"] <= 10 and "x10" in e and "x20" in e]
    print(f"| 0轴附近金叉（振幅≤12%，距低点≤10%） | n{len(cross)} | "
          f"{st.mean(e['x10'] for e in cross):+.2f} | {st.mean(e['x20'] for e in cross):+.2f} |")
    print("\n注意：对照组的绝对超额与本组不同月、不同容量，只能看量级是否一致，不能当成精确比较。\n")


def significance(events: list[dict]) -> None:
    print("## 三、显著性：按月聚合后的 t 值\n")
    print("| 组 | 10日 均值 | t | 月数 | 20日 均值 | t | 月数 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    groups = {
        "纯水下 0~0.3%": [e for e in events if e["side"] == "UU" and abs(e["dif_pct"]) < 0.3],
        "纯水下 ≥3%": [e for e in events if e["side"] == "UU" and abs(e["dif_pct"]) >= 3],
        "纯水上 0~0.3%": [e for e in events if e["side"] == "AA" and abs(e["dif_pct"]) < 0.3],
        "纯水上 1~3%": [e for e in events if e["side"] == "AA" and 1 <= abs(e["dif_pct"]) < 3],
        "跨轴 0~1%": [e for e in events if e["side"] == "XC" and abs(e["dif_pct"]) < 1],
    }
    for name, sample in groups.items():
        m10, t10, n10 = monthly_stats(sample, "rel10")
        m20, t20, n20 = monthly_stats(sample, "rel20")
        print(f"| {name} | {m10:+.2f} | {t10:+.1f} | {n10} | {m20:+.2f} | {t20:+.1f} | {n20} |")
    print()


def stack(events: list[dict]) -> None:
    print("## 四、叠加条件（三段同向才算）\n")
    print("| 组合 | 全部 | 前段 | 后段 | 新段 |")
    print("| --- | --- | --- | --- | --- |")
    combos = {
        "近0轴 + 距低点≤10 + 振幅≤12": lambda e: abs(e["dif_pct"]) <= NEAR and e["dist_low"] <= 10 and e["amp20"] <= 12,
        "近0轴 + 距低点≤10 + 振幅≤12 + 5日≤3": lambda e: (abs(e["dif_pct"]) <= NEAR and e["dist_low"] <= 10
                                                            and e["amp20"] <= 12 and e["ret5"] <= 3),
        "近0轴 + 距低点≤10 + 振幅≤12 + 量比≤1.2": lambda e: (abs(e["dif_pct"]) <= NEAR and e["dist_low"] <= 10
                                                                and e["amp20"] <= 12 and e["vol_ratio"] <= 1.2),
        "纯水下 ≥3%（对照组）": lambda e: e["side"] == "UU" and abs(e["dif_pct"]) >= 3,
    }
    for name, test in combos.items():
        row = [name]
        for period in (None, "p1", "p2", "p3"):
            sample = [e for e in events if test(e) and "rel20" in e]
            if period == "p1":
                sample = [e for e in sample if e["date"] < "2025-01"]
            elif period == "p2":
                sample = [e for e in sample if "2025-01" <= e["date"] < "2026-07"]
            elif period == "p3":
                sample = [e for e in sample if e["date"] >= "2026-07"]
            row.append(f"{st.mean(e['rel20'] for e in sample):+.2f}（n{len(sample)}）" if len(sample) >= 20
                       else f"n{len(sample)}")
        print("| " + " | ".join(row) + " |")
    print()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-params", action="store_true")
    parser.add_argument("--skip-control", action="store_true")
    args = parser.parse_args()

    events = build_events(12, 26, 9)
    if not args.skip_params:
        param_table()
    if not args.skip_control:
        control_table(events)
    significance(events)
    stack(events)


if __name__ == "__main__":
    main()
