#!/usr/bin/env python3
"""Strong starts (强启动确认) that came out of the v2.6 pool vs the rest, in raw returns.

Same sample as v26_to_strong.py section 2, but no benchmark: return = close after N trading
days / next-day open - 1. Lists every "from the pool" trade so each number can be checked.

Steps:
 1. v1.3 signals: output/backtest/v13_signals.json (production right_start_candidate replayed
    day by day on output/cache/kline_long), stage 强启动确认, profitable list, 2023-07 on,
    2024-09 excluded, one signal per stock per 20 trading days.
 2. "来自 v2.6": the same stock passed all 13 v2.6 checks (output/backtest/signals.json) on
    some day within the 40 trading days before the strong-start day.
 3. Entry: next day's open. Exit: close on the 7th / 20th trading day after the signal day.

Usage: python3 scripts/research/v26_to_strong_raw.py
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from v26_to_strong import LOOKBACK, PERIODS, date_index, dedupe, rows_of  # noqa: E402
from backtest_bottom_base import v26  # noqa: E402

HOLD = (7, 20)


def main() -> None:
    from screen_latest_30 import fetch_financials
    fin = fetch_financials(include_financial_yellow=True)
    profitable = set(fetch_financials())
    keep = lambda d: d >= "2023-07-01" and not d.startswith("2024-09")  # noqa: E731
    v26_raw = [s for s in json.loads((ROOT / "output/backtest/signals.json").read_text(encoding="utf-8")) if keep(s["date"]) and v26(s)]
    v26_days: dict[str, list[tuple[int, str]]] = {}
    for s in v26_raw:
        k = date_index(s["code"]).get(s["date"])
        if k is not None:
            v26_days.setdefault(s["code"], []).append((k, s["date"]))
    strong = [s for s in dedupe([s for s in json.loads((ROOT / "output/backtest/v13_signals.json").read_text(encoding="utf-8"))
                                 if keep(s["date"]) and s["code"] in profitable and s.get("stage") == "强启动确认"])]
    trades = []
    for s in strong:
        rows = rows_of(s["code"])
        k = s["k"]
        if k + 1 >= len(rows):
            continue
        pool = [d for j, d in v26_days.get(s["code"], []) if k - LOOKBACK <= j < k]
        t = {"code": s["code"], "name": str(fin.get(s["code"], {}).get("SECURITY_NAME_ABBR") or s["code"]),
             "date": s["date"], "fromPool": bool(pool), "poolDate": pool[-1] if pool else "",
             "close": rows[k]["close"], "entry": rows[k + 1]["open"]}
        for h in HOLD:
            if k + h < len(rows):
                t[f"r{h}"] = rows[k + h]["close"] / t["entry"] - 1
        trades.append(t)

    lines = ["# 强启动：来自 v2.6 池 vs 其他（真实涨跌幅，不扣指数）", "",
             "收益 = 第 N 个交易日收盘价 / 次日开盘买入价 − 1。", ""]
    lines += ["| 持有 | 时段 | 来自 v2.6：平均 / 中位 / 赚钱比例 / 笔数 | 其他：平均 / 中位 / 赚钱比例 / 笔数 |", "| --- | --- | --- | --- |"]
    for h in HOLD:
        for name, lo, hi in PERIODS:
            cells = []
            for flag in (True, False):
                xs = [t[f"r{h}"] for t in trades if t["fromPool"] == flag and lo <= t["date"] < hi and f"r{h}" in t]
                cells.append(f"{st.mean(xs):+.1%} / {st.median(xs):+.1%} / {sum(x > 0 for x in xs) / len(xs):.0%} / {len(xs)}"
                             if xs else "无")
            lines.append(f"| {h}日 | {name} | {cells[0]} | {cells[1]} |")
    lines += ["", "## 来自 v2.6 的每一笔", "",
              "| 强启动日 | 股票 | 最近一次 v2.6 入选 | 信号日收盘 | 次日开盘买入 | 7日收益 | 20日收益 |", "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for t in sorted((t for t in trades if t["fromPool"]), key=lambda t: t["date"]):
        r7 = f"{t['r7']:+.1%}" if "r7" in t else "未满"
        r20 = f"{t['r20']:+.1%}" if "r20" in t else "未满"
        lines.append(f"| {t['date']} | {t['name']} {t['code']} | {t['poolDate']} | {t['close']:.2f} | {t['entry']:.2f} | {r7} | {r20} |")
    out = ROOT / "output" / "backtest" / "v26-to-strong-raw.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
