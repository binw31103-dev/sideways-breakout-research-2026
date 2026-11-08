#!/usr/bin/env python3
"""What the T+1 entry costs: the overnight gap between the signal close and the entry price.

Every model in this project assumes: screen after the close on day T, buy at the open of
T+1. That is realistic (the daily job runs at 17:05), but the gap is a real cost known
before entry, and it may differ by list. This measures:

  gap      T+1 open / T close − 1        what you pay to get in
  intraday T+1 close / T+1 open − 1      the rest of day one
  holdN    T+N close / T+1 open − 1      the realised path used in the backtests

and compares the 20-day excess measured from T close (unattainable, for reference) with the
same trade entered at T+1 open (what we actually report).

Groups: v1.3 (right-side start), v2.6 (bottom watchlist), 水下金叉 variant G, and the
"already started" blind spot (limit-up days), which is expected to gap up the most.

Usage: python3 scripts/research/entry_gap.py
"""

from __future__ import annotations

import concurrent.futures
import json
import statistics as st
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backtest_bottom_base import LONG_KLINE_DIR, benchmark_index, v26  # noqa: E402
from screen_latest_30 import fetch_financials  # noqa: E402
from underwater_cross import classify, macd_lines  # noqa: E402

PERIODS = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"),
           ("2026-07起", "2026-07", "9999"))
GAP = 20


@lru_cache(maxsize=None)
def rows_of(code: str) -> list[dict]:
    path = LONG_KLINE_DIR / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))["rows"] if path.exists() else []


def g_crosses(path: Path) -> list[tuple[str, str]]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    if len(rows) < 200 or any(r["low"] <= 0 or r["close"] <= 0 for r in rows):
        return []
    dif, _, crosses = macd_lines(rows)
    return [(path.stem, rows[k]["date"]) for k in crosses
            if k >= 180 and classify(rows, k, dif, crosses)["G"]
            and sum(r["close"] * r["volume"] for r in rows[k - 19:k + 1]) / 20 >= 5e7]


def trades(pairs: list[tuple[str, str]], index: dict[str, float]) -> list[dict]:
    out, last = [], {}
    for code, date in sorted(pairs, key=lambda p: (p[0], p[1])):
        rows = rows_of(code)
        dates = [r["date"] for r in rows]
        if date not in dates:
            continue
        i = dates.index(date)
        if i - last.get(code, -GAP) < GAP or i + 21 >= len(rows):
            continue
        last[code] = i
        signal_close, entry = rows[i]["close"], rows[i + 1]["open"]
        day1_close = rows[i + 1]["close"]
        exit_row = rows[i + 20]
        if date not in index or exit_row["date"] not in index:
            continue
        bench_next = index[rows[i + 1]["date"]] / index[date] - 1 if rows[i + 1]["date"] in index else None
        bench_exit = index[exit_row["date"]] / index[date] - 1
        out.append({
            "code": code, "date": date,
            "gap": entry / signal_close - 1,
            "intraday": day1_close / entry - 1,
            "hold": exit_row["close"] / entry - 1,
            "hold_from_signal": exit_row["close"] / signal_close - 1,
            "bench_next": bench_next,
            "bench_exit": bench_exit,
        })
    return out


def line(label: str, xs: list[float], pct: bool = True) -> str:
    if len(xs) < 10:
        return "n/a"
    return (f"{st.mean(xs):+.2%}" if pct else f"{st.mean(xs):.2f}") + f"（中位{st.median(xs):+.2%}，n{len(xs)}）"


def main() -> None:
    index = benchmark_index()
    keep = lambda d: d >= "2023-07-01" and not d.startswith("2024-09")  # noqa: E731
    profitable = set(fetch_financials())
    v13 = [(s["code"], s["date"]) for s in json.loads((ROOT / "output/backtest/v13_signals.json").read_text(encoding="utf-8"))
           if keep(s["date"]) and s["code"] in profitable]
    v13s = [(s["code"], s["date"]) for s in json.loads((ROOT / "output/backtest/v13_signals.json").read_text(encoding="utf-8"))
            if keep(s["date"]) and s["code"] in profitable and s.get("stage") == "强启动确认"]
    v26_pairs = [(s["code"], s["date"]) for s in json.loads((ROOT / "output/backtest/signals.json").read_text(encoding="utf-8"))
                 if keep(s["date"]) and v26(s)]
    g: list[tuple[str, str]] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(g_crosses, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            g.extend(x for x in result if keep(x[1]))

    groups = {"v1.3 右沿初确认": [(c, d) for c, d in v13 if (c, d) not in set(v13s)],
              "v1.3 强启动确认": v13s, "v2.6 底部观察池": v26_pairs, "低位水下金叉": g}
    lines = ["# 次日开盘买入的代价：发现价（T 收盘）到成交价（T+1 开盘）的跳空", "",
             "全部回测都是 T 日收盘筛选、T+1 开盘买入。下表衡量这两者之间的差额，20 日结果按 T+1 开盘计算。", ""]
    for name, pairs in groups.items():
        t = trades(pairs, index)
        lines += [f"## {name}（{len(t)} 笔）", "",
                  "| 时段 | 跳空（T收→T+1开） | 首日剩余（开→收） | 按 T+1 开盘买入的 20 日涨幅 |", "| --- | ---: | ---: | ---: |"]
        for label, lo, hi in PERIODS:
            xs = [x for x in t if lo <= x["date"] < hi]
            if len(xs) < 10:
                lines.append(f"| {label} | n{len(xs)} | | |")
                continue
            lines.append(f"| {label} | {line('', [x['gap'] for x in xs])} | {line('', [x['intraday'] for x in xs])} | "
                         f"{line('', [x['hold'] for x in xs])} |")
        # What the gap does to the reported excess: excess from T+1 open vs from T close.
        for label, lo, hi in PERIODS:
            xs = [x for x in t if lo <= x["date"] < hi and x["bench_next"] is not None]
            if len(xs) < 10:
                continue
            at_open = st.mean(x["hold"] - x["bench_exit"] for x in xs)
            at_close = st.mean(x["hold_from_signal"] - x["bench_exit"] for x in xs)
            lines.append(f"- {label}：20 日超额，按 T+1 开盘买入 **{at_open:+.2%}**；"
                         f"若能在 T 日收盘买入（实际做不到）{at_close:+.2%}；差额 {at_close - at_open:+.2%}")
        lines.append("")

    blind_up = trades(v26_pairs, index)
    lines += ["## 附：底部池里出现涨停的次日跳空（最差的一类）", ""]
    lu = [x for x in blind_up if x["gap"] > 0.03]
    if lu:
        lines.append(f"跳空 >3% 的 {len(lu)} 笔：20 日涨幅中位 {st.median([x['hold'] for x in lu]):+.2%}，"
                     f"平均 {st.mean([x['hold'] for x in lu]):+.2%}")
    out = ROOT / "output" / "backtest" / "entry-gap.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
