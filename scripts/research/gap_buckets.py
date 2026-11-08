#!/usr/bin/env python3
"""20-day excess by the size of the opening gap (T close -> T+1 open), for the three daily
lists. Sets the threshold used by scripts/preopen_check.py.

Usage: python3 scripts/research/gap_buckets.py
"""

from __future__ import annotations

import concurrent.futures
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

import entry_gap as E  # noqa: E402
from backtest_bottom_base import LONG_KLINE_DIR, benchmark_index, v26  # noqa: E402

BINS = ((-1, -0.03, "低开>3%"), (-0.03, -0.01, "低开1~3%"), (-0.01, 0.01, "±1%"),
        (0.01, 0.02, "高开1~2%"), (0.02, 0.03, "高开2~3%"), (0.03, 1, "高开>3%"))


def main() -> None:
    from screen_latest_30 import fetch_financials
    index = benchmark_index()
    profitable = set(fetch_financials())
    keep = lambda d: d >= "2023-07-01" and not d.startswith("2024-09")  # noqa: E731
    v13 = [(s["code"], s["date"]) for s in json.loads((ROOT / "output/backtest/v13_signals.json").read_text(encoding="utf-8"))
           if keep(s["date"]) and s["code"] in profitable]
    v26_pairs = [(s["code"], s["date"]) for s in json.loads((ROOT / "output/backtest/signals.json").read_text(encoding="utf-8"))
                 if keep(s["date"]) and v26(s)]
    g: list[tuple[str, str]] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(E.g_crosses, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            g.extend(x for x in result if keep(x[1]))
    lines = ["# 开盘跳空分档：按 T+1 开盘买入的 20 日超额（相对中证1000）", ""]
    pooled = []
    for name, pairs in (("v1.3 右侧启动", v13), ("v2.6 底部观察池", v26_pairs), ("低位水下金叉", g)):
        t = E.trades(pairs, index)
        for x in t:
            x["ex"] = x["hold"] - x["bench_exit"]
        pooled += t
        lines += [f"## {name}（{len(t)} 笔）", "", "| 跳空 | 占比 | 全部 | 中位 | " + " | ".join(p[0] for p in E.PERIODS) + " |",
                  "| --- | ---: | ---: | ---: |" + " ---: |" * len(E.PERIODS)]
        for lo, hi, label in BINS:
            xs = [x for x in t if lo <= x["gap"] < hi]
            if not xs:
                continue
            cells = []
            for _, a, b in E.PERIODS:
                ys = [x["ex"] for x in xs if a <= x["date"] < b]
                cells.append(f"{st.mean(ys):+.1%}（n{len(ys)}）" if len(ys) >= 10 else f"n{len(ys)}")
            lines.append(f"| {label} | {len(xs) / len(t):.1%} | {st.mean(x['ex'] for x in xs):+.1%} | "
                         f"{st.median(x['ex'] for x in xs):+.1%} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += ["## 三类合计", "", "| 跳空 | 笔数 | 全部 | 中位 | 跑赢 |", "| --- | ---: | ---: | ---: | ---: |"]
    for lo, hi, label in BINS:
        xs = [x["ex"] for x in pooled if lo <= x["gap"] < hi]
        if xs:
            lines.append(f"| {label} | {len(xs)} | {st.mean(xs):+.1%} | {st.median(xs):+.1%} | {sum(v > 0 for v in xs) / len(xs):.0%} |")
    out = ROOT / "output" / "backtest" / "gap-buckets.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
