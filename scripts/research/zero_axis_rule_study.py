#!/usr/bin/env python3
"""把"0 轴附近金叉"当成一条正式候选规则来检验：在哪一段有效、靠什么条件才成立。

上一次（zero_axis_cross.py）只分了"水上 / 水下 / 水上且 0 轴附近"，这次拆得更细：

  金叉按 DIF、DEA 的符号分三类
    UU  纯水下      DIF<0 且 DEA<0
    XC  跨轴        DIF>0 且 DEA<0   （DEA 还没跟上，002714 的 11-03 属于这类）
    AA  纯水上      DIF>0 且 DEA>0

  每一类再按 |DIF|/收盘价 分档（0~0.3%、0.3~1%、1~2%、2~3%、3%以上），
  看"离 0 轴近"到底是一个单调的梯度，还是一堆噪声。

  关键指标是**同日相对超额**：当天所有金叉的平均超额先减掉，剩下的才是"选了这一只
  而不是当天其他金叉"的价值。绝对超额混进了大盘择时，做规则筛选时会误判。

另外检验几条候选过滤条件（首次金叉、靠近 60 日低点、振幅收敛、量能、前期高位回落），
每条都要求三段（2023-07~2024-12 / 2025-01~2026-06 / 2026-07 起）方向一致才认。

用法：
  python3 scripts/research/zero_axis_rule_study.py                # 分档主表 + 过滤条件
  python3 scripts/research/zero_axis_rule_study.py --bins-only    # 只出分档表（换参数时用）
  python3 scripts/research/zero_axis_rule_study.py --fast 10 --slow 20 --signal 5
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

HOLD = (10, 20, 40)
BINS = ((0.0, 0.3), (0.3, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 99.0))
PERIODS = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"),
           ("2026-07起", "2026-07", "9999"))
MIN_AMOUNT = 5e7


def scan_one(args: tuple[Path, int, int, int]) -> list[dict]:
    path, fast_p, slow_p, signal_p = args
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return []
    if len(rows) < 260:
        return []
    if any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return []
    close = [r["close"] for r in rows]
    fast, slow = ema(close, fast_p), ema(close, slow_p)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, signal_p)
    r14 = rsi(close, 14)
    amount = [r["close"] * r["volume"] for r in rows]

    amt_prefix = [0.0]
    for value in amount:
        amt_prefix.append(amt_prefix[-1] + value)

    out = []
    for k in range(180, len(rows) - 1):
        if not (dif[k - 1] < dea[k - 1] and dif[k] >= dea[k]):
            continue
        amt20 = (amt_prefix[k + 1] - amt_prefix[k - 19]) / 20
        if amt20 < MIN_AMOUNT:
            continue
        price = close[k]
        d, e = dif[k], dea[k]
        side = "UU" if (d < 0 and e < 0) else ("XC" if (d > 0 and e < 0) else "AA")
        low60 = min(r["low"] for r in rows[k - 59:k + 1])
        high60 = max(r["high"] for r in rows[k - 59:k + 1])
        amp20 = (max(r["high"] for r in rows[k - 19:k + 1]) - min(r["low"] for r in rows[k - 19:k + 1])) / price
        whipsaw = sum(1 for j in range(k - 59, k) if (dif[j] - dea[j]) * (dif[j - 1] - dea[j - 1]) < 0)
        prior_dif = max(dif[j] / close[j] for j in range(max(0, k - 60), k)) * 100
        since_zero = 0
        for j in range(k, max(0, k - 120), -1):
            if (dif[j] - 0) * (dif[j - 1] - 0) < 0:
                break
            since_zero += 1
        item = {
            "code": path.stem, "date": rows[k]["date"], "side": side, "close": price,
            "dif_pct": d / price * 100, "gap_pct": (d - e) / price * 100,
            "dist_low": (price / low60 - 1) * 100, "pos_range": (price - low60) / (high60 - low60) * 100,
            "amp20": amp20 * 100, "rsi": r14[k],
            "vol_ratio": rows[k]["volume"] / (sum(r["volume"] for r in rows[k - 20:k]) / 20),
            "whipsaw60": whipsaw, "prior_dif_max": prior_dif, "since_zero": since_zero,
            "ret5": (price / close[k - 5] - 1) * 100, "ret20": (price / close[k - 20] - 1) * 100,
            "amt20_yi": amt20 / 1e8,
            "gap_open": (rows[k + 1]["open"] / price - 1) * 100,
            "day_gain": (price / close[k - 1] - 1) * 100,
        }
        entry = rows[k + 1]["open"]
        for h in HOLD:
            if k + h < len(rows):
                item[f"r{h}"] = (rows[k + h]["close"] / entry - 1) * 100
                item[f"d{h}"] = rows[k + h]["date"]
        out.append(item)
    return out


def build_events(fast_p: int, slow_p: int, signal_p: int) -> list[dict]:
    jobs = [(p, fast_p, slow_p, signal_p) for p in sorted(LONG_KLINE_DIR.glob("*.json"))]
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan_one, jobs, chunksize=20):
            events.extend(result)
    index = benchmark_index()
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09")
              and e["date"] in index]
    for e in events:
        for h in HOLD:
            if f"r{h}" in e and e[f"d{h}"] in index:
                e[f"x{h}"] = e[f"r{h}"] - (index[e[f"d{h}"]] / index[e["date"]] - 1) * 100
    # 同日相对超额：减掉当天全部金叉的平均超额
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


def bin_of(value: float) -> tuple[float, float]:
    for lo, hi in BINS:
        if lo <= value < hi:
            return lo, hi
    return BINS[-1]


def cell(sample: list[dict], key: str, digits: int = 2) -> str:
    if len(sample) < 20:
        return f"n{len(sample)}"
    return f"{st.mean(e[key] for e in sample):+.{digits}f}（n{len(sample)}）"


def bins_table(events: list[dict], h: int) -> list[str]:
    lines = [f"### 持有 {h} 日：金叉类型 × 距 0 轴距离", "",
             "格式：同日相对超额（样本）；同日相对 = 该金叉超额 − 当天全部金叉平均超额。", "",
             "| 类型 | " + " | ".join(f"{lo}~{hi}%" if hi < 99 else f"≥{lo}%" for lo, hi in BINS) + " | 合计 |",
             "| --- |" + " --- |" * (len(BINS) + 1)]
    names = {"UU": "纯水下（DIF<0,DEA<0）", "XC": "跨轴（DIF>0,DEA<0）", "AA": "纯水上（DIF>0,DEA>0）"}
    for side in ("UU", "XC", "AA"):
        row = [names[side]]
        for lo, hi in BINS:
            sample = [e for e in events if e["side"] == side and f"rel{h}" in e
                      and bin_of(abs(e["dif_pct"])) == (lo, hi)]
            row.append(cell(sample, f"rel{h}"))
        sample = [e for e in events if e["side"] == side and f"rel{h}" in e]
        row.append(cell(sample, f"rel{h}"))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def period_table(events: list[dict], subset: list[dict], h: int) -> str:
    cells = []
    for _, lo, hi in PERIODS:
        part = [e for e in subset if lo <= e["date"] < hi and f"rel{h}" in e]
        cells.append(cell(part, f"rel{h}"))
    return " | ".join(cells)


def filters_table(events: list[dict], near: float, h: int) -> list[str]:
    base = [e for e in events if abs(e["dif_pct"]) <= near and f"rel{h}" in e]
    tests = [
        ("全部 0 轴附近金叉", lambda e: True),
        ("首次金叉（60日内反向次数≤1）", lambda e: e["whipsaw60"] <= 1),
        ("反复缠绕（60日内反向次数≥4）", lambda e: e["whipsaw60"] >= 4),
        ("DIF 由深水回升（此前60日最高 DIF% ≥ 2）", lambda e: e["prior_dif_max"] >= 2),
        ("DIF 一直在水下（此前60日最高 DIF% < 0）", lambda e: e["prior_dif_max"] < 0),
        ("距 60 日低点 ≤ 10%", lambda e: e["dist_low"] <= 10),
        ("距 60 日低点 > 20%", lambda e: e["dist_low"] > 20),
        ("20 日振幅 ≤ 12%（箱体收敛）", lambda e: e["amp20"] <= 12),
        ("20 日振幅 ≥ 20%", lambda e: e["amp20"] >= 20),
        ("信号日量比 ≥ 1.5", lambda e: e["vol_ratio"] >= 1.5),
        ("信号日量比 ≤ 1.0", lambda e: e["vol_ratio"] <= 1.0),
        ("近 5 日涨幅 ≤ 3%", lambda e: e["ret5"] <= 3),
        ("近 5 日涨幅 > 8%", lambda e: e["ret5"] > 8),
        ("DIF-DEA 落差 ≥ 0.3%", lambda e: e["gap_pct"] >= 0.3),
        ("收盘在 60 日区间 60%~90%", lambda e: 60 <= e["pos_range"] <= 90),
        ("日 RSI ≤ 55", lambda e: e["rsi"] is not None and e["rsi"] <= 55),
        ("日 RSI ≥ 65", lambda e: e["rsi"] is not None and e["rsi"] >= 65),
        ("日均成交额 ≥ 2 亿", lambda e: e["amt20_yi"] >= 2),
    ]
    lines = [f"### 过滤条件检验（0 轴附近 |DIF%| ≤ {near}%，持有 {h} 日）", "",
             "格式：同日相对超额 × 三段（2023-07~2024-12 | 2025-01~2026-06 | 2026-07 起）", "",
             "| 条件 | 全部 | 前段 | 后段 | 新段 |", "| --- | --- | --- | --- | --- |"]
    for name, test in tests:
        sample = [e for e in base if test(e)]
        if len(sample) < 50:
            lines.append(f"| {name} | n{len(sample)} | - | - | - |")
            continue
        lines.append(f"| {name} | {cell(sample, f'rel{h}')} | {period_table(events, sample, h)} |")
    lines.append("")
    return lines


def capacity(events: list[dict], near: float) -> list[str]:
    per_day: dict[str, int] = defaultdict(int)
    for e in events:
        if abs(e["dif_pct"]) <= near:
            per_day[e["date"]] += 1
    counts = sorted(per_day.values())
    days = len(counts)
    total = sum(counts)
    lines = [f"0 轴附近金叉（|DIF%| ≤ {near}%）出现频率：共 {total} 个信号、{days} 个交易日，"
             f"平均每天 {total / days:.1f} 个；中位数 {st.median(counts):.0f}，"
             f"最多的那天 {counts[-1]} 个。", ""]
    return lines


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", type=int, default=12)
    parser.add_argument("--slow", type=int, default=26)
    parser.add_argument("--signal", type=int, default=9)
    parser.add_argument("--near", type=float, default=1.0)
    parser.add_argument("--bins-only", action="store_true")
    args = parser.parse_args()

    print(f"参数 MACD({args.fast},{args.slow},{args.signal})，正在扫描全市场金叉 ……", flush=True)
    events = build_events(args.fast, args.slow, args.signal)
    print(f"样本内金叉 {len(events)} 个\n")

    print("## 一、按类型和距离分档\n")
    for h in HOLD:
        print("\n".join(bins_table(events, h)))

    if args.bins_only:
        return

    print("## 二、0 轴附近这一组，加什么条件才有用\n")
    for h in (10, 20):
        print("\n".join(filters_table(events, args.near, h)))

    print("## 三、容量\n")
    print("\n".join(capacity(events, args.near)))

    # 可交易性：次日高开幅度、信号日是否涨停
    near = [e for e in events if abs(e["dif_pct"]) <= args.near and "rel10" in e]
    gaps = sorted(e["gap_open"] for e in near)
    hot = [e for e in near if e["gap_open"] > 5]
    limit = [e for e in near if e["day_gain"] >= 9.5]
    print(f"次日开盘高开幅度：中位数 {st.median(gaps):+.2f}%，"
          f"高开>5% 的占 {len(hot) / len(near):.1%}，"
          f"信号日涨停的占 {len(limit) / len(near):.1%}。")
    if hot:
        print(f"  高开>5% 那批的同日相对超额（10日）：{cell(hot, 'rel10')}")
    if limit:
        print(f"  信号日涨停那批的同日相对超额（10日）：{cell(limit, 'rel10')}")

    dump = ROOT / "output" / "backtest" / f"zero-axis-events-{args.fast}-{args.slow}-{args.signal}.json"
    dump.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    print(f"\n明细已写入 {dump.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
