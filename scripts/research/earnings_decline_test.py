#!/usr/bin/env python3
"""Does a big earnings decline at the time of selection predict a worse v1.3 outcome?

爱施德 002416 was selected 2026-10-16 (right-side start, all technical checks passed) and
fell 20% in two weeks: its Q3 report, out the next evening, showed net profit -65.6% yoy
(H1 was already -79%). The financial switch only flags "profit falling sharply" as watch,
not reject. This asks whether that should become a hard filter.

Point in time: only reports announced before the signal date are used, earliest notice per
period. Groups (fixed before running):
  both<=-50 / both<=-30 / both<=-20   latest two reported quarters both at or below
  latest<=-50                          only the newest report
  latest>0 and prev>0                  control: growing both periods
Metrics: hit rate of v1.3 signals, excess vs CSI 1000 at 10 and 20 days by period, and the
same-month difference against the rest of that month's signals (market timing cancels out).

Usage: python3 scripts/research/earnings_decline_test.py
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backtest_bottom_base import LONG_KLINE_DIR, benchmark_index  # noqa: E402
from screen_latest_30 import fetch_financials  # noqa: E402

FUND = ROOT / "output" / "cache" / "fundamentals"
PERIODS = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"),
           ("2026-07起", "2026-07", "9999"))
GROUPS = {
    "两期均≤-50%": lambda y, p: y is not None and p is not None and y <= -50 and p <= -50,
    "两期均≤-30%": lambda y, p: y is not None and p is not None and y <= -30 and p <= -30,
    "两期均≤-20%": lambda y, p: y is not None and p is not None and y <= -20 and p <= -20,
    "最新一期≤-50%": lambda y, p: y is not None and y <= -50,
    "两期均>0（对照）": lambda y, p: y is not None and p is not None and y > 0 and p > 0,
    "无财报数据": lambda y, p: y is None,
}


@lru_cache(maxsize=None)
def rows_of(code: str) -> list[dict]:
    path = LONG_KLINE_DIR / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))["rows"] if path.exists() else []


def latest_two_yoy() -> dict[str, dict[str, tuple[str, float | None]]]:
    perf: dict[str, dict[str, tuple[str, float | None]]] = defaultdict(dict)
    for r in json.loads((FUND / "perf.json").read_text(encoding="utf-8")):
        if r.get("NOTICE_DATE"):
            q, n = r["REPORTDATE"][:10], r["NOTICE_DATE"][:10]
            if q not in perf[r["SECURITY_CODE"]] or n < perf[r["SECURITY_CODE"]][q][0]:
                perf[r["SECURITY_CODE"]][q] = (n, r.get("SJLTZ"))
    return perf


def yoy_at(perf, code: str, date: str) -> tuple[float | None, float | None]:
    known = sorted((q, v[1]) for q, v in perf.get(code, {}).items() if v[0] < date)
    y = known[-1][1] if known else None
    p = known[-2][1] if len(known) > 1 else None
    y = float(y) if isinstance(y, (int, float)) else None
    p = float(p) if isinstance(p, (int, float)) else None
    return y, p


def main() -> None:
    index = benchmark_index()
    keep = lambda d: d >= "2023-07-01" and not d.startswith("2024-09")  # noqa: E731
    profitable = set(fetch_financials())
    perf = latest_two_yoy()
    signals = [s for s in json.loads((ROOT / "output/backtest/v13_signals.json").read_text(encoding="utf-8"))
               if keep(s["date"]) and s["code"] in profitable]
    trades = []
    last: dict[str, int] = {}
    for s in sorted(signals, key=lambda s: (s["code"], s["i"])):
        if s["i"] - last.get(s["code"], -20) < 20:
            continue
        last[s["code"]] = s["i"]
        rows = rows_of(s["code"])
        dates = [r["date"] for r in rows]
        if s["date"] not in dates:
            continue
        i = dates.index(s["date"])
        entry = rows[i + 1]["open"] if i + 1 < len(rows) else None
        if entry is None or s["date"] not in index:
            continue
        y, p = yoy_at(perf, s["code"], s["date"])
        t = {"code": s["code"], "date": s["date"], "yoy": y, "prev": p, "stage": s.get("stage")}
        for days in (10, 20):
            k = i + days
            if k < len(rows) and rows[k]["date"] in index:
                t[days] = rows[k]["close"] / entry - 1 - (index[rows[k]["date"]] / index[s["date"]] - 1)
        trades.append(t)

    covered = sum(1 for t in trades if t["yoy"] is not None)
    lines = [f"# 入选时业绩大幅下滑是否该硬性剔除（v1.3，{len(trades)} 个信号，有财报数据 {covered} 个）", ""]
    lines.append("数值 = 相对中证1000 平均超额（跑赢比例）；“同月差”= 该组 − 同月其余信号，抵消大盘影响。")
    lines.append("")
    for days in (20, 10):
        lines += [f"## 持有 {days} 日", "", "| 组 | 命中率 | " + " | ".join(p[0] for p in PERIODS) + " |",
                  "| --- | ---: |" + " --- |" * len(PERIODS)]
        by_month = defaultdict(dict)
        for t in trades:
            if days in t:
                by_month[t["date"][:7]][t["code"] + t["date"]] = t[days]
        month_mean = {m: st.mean(v.values()) for m, v in by_month.items() if v}
        for name, test in GROUPS.items():
            hit = [t for t in trades if test(t["yoy"], t["prev"])]
            cells = []
            for _, lo, hi in PERIODS:
                xs = [t for t in hit if lo <= t["date"] < hi and days in t]
                if len(xs) < 10:
                    cells.append(f"n{len(xs)}")
                    continue
                ex = [t[days] for t in xs]
                dm = st.mean(t[days] - month_mean[t["date"][:7]] for t in xs if month_mean.get(t["date"][:7]) is not None)
                cells.append(f"{st.mean(ex):+.1%}（胜{sum(v > 0 for v in ex) / len(ex):.0%}，同月差{dm:+.1%}，n{len(xs)}）")
            share = len(hit) / max(len([t for t in trades if t["yoy"] is not None]), 1)
            lines.append(f"| {name} | {share:.0%} | " + " | ".join(cells) + " |")
        lines.append("")
        rest = [t for t in trades if t["yoy"] is not None and not GROUPS["两期均≤-50%"](t["yoy"], t["prev"])]
        cells = []
        for _, lo, hi in PERIODS:
            xs = [t for t in rest if lo <= t["date"] < hi and days in t]
            cells.append(f"{st.mean(t[days] for t in xs):+.1%}（n{len(xs)}）" if xs else "n/a")
        lines.append(f"其余信号（全部减去“两期均≤-50%”）：" + " | ".join(cells))
        lines.append("")
    out = ROOT / "output" / "backtest" / "earnings-decline.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
