#!/usr/bin/env python3
"""Holding-period curve for the three daily lists: buy at the next day's open, sell at the
close after N trading days, no stop. Excess vs CSI 1000, by period.

  v1.3   right-side starts (profitable universe)        output/backtest/v13_signals.json
  v2.6   bottom sideways, all checks passed              output/backtest/signals.json
  水下金叉 variant G of underwater_cross.py (600406 pattern), scanned from kline_long

Same stock counted once per 20 trading days. Also reports the per-stock path: how often
the best close within 40 days came early vs late, to see whether a fixed exit wastes gains.

Usage: python3 scripts/research/holding_period.py
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
from underwater_cross import PERIODS, classify, macd_lines  # noqa: E402

DAYS = (1, 2, 3, 5, 10, 15, 20, 30, 40, 60)
GAP = 20


@lru_cache(maxsize=None)
def rows_of(code: str) -> list[dict]:
    path = LONG_KLINE_DIR / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))["rows"] if path.exists() else []


def g_events(path: Path) -> list[tuple[str, str]]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    if len(rows) < 200 or any(r["low"] <= 0 or r["close"] <= 0 or r["open"] <= 0 for r in rows):
        return []
    dif, _, crosses = macd_lines(rows)
    out = []
    for k in crosses:
        if k >= 180 and sum(r["close"] * r["volume"] for r in rows[k - 19:k + 1]) / 20 >= 5e7 \
                and classify(rows, k, dif, crosses)["G"]:
            out.append((path.stem, rows[k]["date"]))
    return out


def dedupe(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out, last = [], {}
    for code, date in sorted(pairs):
        rows = rows_of(code)
        dates = [r["date"] for r in rows]
        if date not in dates:
            continue
        i = dates.index(date)
        if i - last.get(code, -GAP) >= GAP:
            last[code] = i
            out.append((code, date))
    return out


def paths(pairs: list[tuple[str, str]], index: dict[str, float]) -> list[dict]:
    out = []
    for code, date in pairs:
        rows = rows_of(code)
        dates = [r["date"] for r in rows]
        i = dates.index(date)
        if i + 1 >= len(rows) or date not in index:
            continue
        entry, base_day = rows[i + 1]["open"], rows[i]["date"]
        e = {"code": code, "date": date}
        for n in DAYS:
            k = i + n
            if k < len(rows) and rows[k]["date"] in index:
                e[n] = rows[k]["close"] / entry - 1 - (index[rows[k]["date"]] / index[base_day] - 1)
        if i + 40 < len(rows):
            closes = [rows[k]["close"] for k in range(i + 1, i + 41)]
            e["bestDay"] = 1 + max(range(40), key=lambda j: closes[j])
            e["best"] = max(closes) / entry - 1
        out.append(e)
    return out


def main() -> None:
    index = benchmark_index()
    keep = lambda d: d >= "2023-07-01" and not d.startswith("2024-09")  # noqa: E731
    from screen_latest_30 import fetch_financials
    profitable = set(fetch_financials())
    v13 = [(s["code"], s["date"]) for s in json.loads((ROOT / "output/backtest/v13_signals.json").read_text(encoding="utf-8"))
           if keep(s["date"]) and s["code"] in profitable]
    v26s = [(s["code"], s["date"]) for s in json.loads((ROOT / "output/backtest/signals.json").read_text(encoding="utf-8"))
            if keep(s["date"]) and v26(s)]
    g: list[tuple[str, str]] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(g_events, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            g.extend(x for x in result if keep(x[1]))
    models = {"右侧启动 v1.3": dedupe(v13), "底部观察池 v2.6": dedupe(v26s), "低位水下金叉": dedupe(g)}

    lines = ["# 持有期曲线：次日开盘买入，持有 N 个交易日收盘卖出，不设止损", "",
             "数值 = 相对中证1000 的平均超额（括号内：跑赢比例）；同股 20 日内只计一次；剔除 2024-09。", ""]
    for name, pairs in models.items():
        ev = paths(pairs, index)
        lines += [f"## {name}（{len(ev)} 个信号）", "",
                  "| 持有 | " + " | ".join(p[0] for p in PERIODS) + " | 三段都为正 |", "| ---: |" + " ---: |" * (len(PERIODS) + 1)]
        for n in DAYS:
            cells, signs = [], []
            for _, lo, hi in PERIODS:
                xs = [e[n] for e in ev if lo <= e["date"] < hi and n in e]
                if len(xs) < 20:
                    cells.append(f"n{len(xs)}")
                    signs.append(None)
                    continue
                cells.append(f"{st.mean(xs):+.1%}（{sum(x > 0 for x in xs) / len(xs):.0%}）")
                signs.append(st.mean(xs) > 0)
            ok = "✓" if all(s for s in signs if s is not None) and None not in signs else ""
            lines.append(f"| {n}日 | " + " | ".join(cells) + f" | {ok} |")
        done = [e for e in ev if "bestDay" in e]
        buckets = defaultdict(int)
        for e in done:
            buckets["1-5" if e["bestDay"] <= 5 else "6-10" if e["bestDay"] <= 10 else "11-20" if e["bestDay"] <= 20 else "21-40"] += 1
        lines += ["", f"40日内收盘最高点出现在第几天：" + "，".join(f"{k}日 {buckets[k] / len(done):.0%}" for k in ("1-5", "6-10", "11-20", "21-40"))
                  + f"；40日内最高涨幅中位数 {st.median(e['best'] for e in done):+.1%}", ""]
    out = ROOT / "output" / "backtest" / "holding-period.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
