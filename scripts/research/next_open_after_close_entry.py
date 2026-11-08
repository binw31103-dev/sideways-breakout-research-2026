#!/usr/bin/env python3
"""If you already bought near the close (14:45 pre-screen), what should the next open do?

For each trade of the three daily lists, entry at the signal-day close (14:45 is within a
median 0.6% of it), the next morning's gap decides between:
  A  sell at the T+1 open                     return = gap
  B  keep for 20 days                         return = T+20 close / T close - 1
B - A is what holding adds after the gap is already in your pocket. Excess is against CSI
1000 close-to-close over the same window (the index open is not in our data, so A's excess
uses the index's T -> T+1 close move as an approximation).

Usage: python3 scripts/research/next_open_after_close_entry.py
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
from gap_buckets import BINS  # noqa: E402


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
    trades = []
    for pairs in (v13, v26_pairs, g):
        trades += E.trades(pairs, index)
    lines = ["# 尾盘已买入：次日开盘卖还是继续拿（三类清单合计，买入价=信号日收盘）", "",
             "| 次日开盘 | 笔数 | A 开盘卖（收益） | B 拿20日（收益） | B−A 中位 | B−A 均值 | 拿着更好的比例 | B 相对中证1000 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for lo, hi, label in BINS:
        xs = [t for t in trades if lo <= t["gap"] < hi]
        if len(xs) < 10:
            continue
        a = [t["gap"] for t in xs]
        b = [t["hold_from_signal"] for t in xs]
        diff = [y - x for x, y in zip(a, b)]
        bx = [t["hold_from_signal"] - t["bench_exit"] for t in xs]
        lines.append(f"| {label} | {len(xs)} | {st.mean(a):+.1%} | {st.mean(b):+.1%} | {st.median(diff):+.1%} | {st.mean(diff):+.1%} | "
                     f"{sum(d > 0 for d in diff) / len(diff):.0%} | {st.mean(bx):+.1%} |")
    out = ROOT / "output" / "backtest" / "next-open-after-close-entry.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
