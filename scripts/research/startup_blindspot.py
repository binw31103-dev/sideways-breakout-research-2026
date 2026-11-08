#!/usr/bin/env python3
"""The gap between the two models: days a stock is dropped as "already started" but v1.3
would not take it either.

`screen_v26_unified.startup_confirmation` removes a bottom candidate when RSI is hot
(day >=55 or week >=50), it has left the base, and volume expanded. v1.3 needs day RSI
38-58 and week RSI 32-48, so the two sets barely overlap: a stock like 中国卫星 600118
(60-day low 2026-10-15, +16% in 10 days with a limit-up on 2026-10-29, RSI 55) is in
neither model. This measures how those days did afterwards.

Groups (fixed before running):
  all      every day startup_confirmation is true
  inBase   same, and the stock passed all 13 v2.6 checks within the previous 60 bars
  limitUp  same, and the startup day itself gained >= 9.5%
  rest     control: the v2.6 signals themselves (all 13 checks, not dropped)
Outcome: next-open entry, excess vs CSI 1000 at 5 / 10 / 20 days.

Usage: python3 scripts/research/startup_blindspot.py
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

from backtest_bottom_base import LONG_KLINE_DIR, benchmark_index, v26  # noqa: E402
from screen_v26_unified import calculate_metrics, startup_confirmation  # noqa: E402

BASE_DATES = ROOT / "output" / "cache" / "v26_base_dates.json"
PERIODS = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"),
           ("2026-07起", "2026-07", "9999"))
HOLD = (5, 10, 20)
INDEX: dict[str, float] = {}
BASES: dict[str, list[str]] = {}


def base_dates() -> dict[str, list[str]]:
    """Workers re-import this module (macOS uses spawn), so they read the cache themselves."""
    return json.loads(BASE_DATES.read_text(encoding="utf-8")) if BASE_DATES.exists() else {}


def build_base_dates() -> dict[str, list[str]]:
    if BASE_DATES.exists():
        return json.loads(BASE_DATES.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = defaultdict(list)
    for s in json.loads((ROOT / "output" / "backtest" / "signals.json").read_text(encoding="utf-8")):
        if v26(s):
            out[s["code"]].append(s["date"])
    BASE_DATES.write_text(json.dumps(out), encoding="utf-8")
    return out


def cheap(rows: list[dict], i: int) -> bool:
    """Superset of startup_confirmation's cheap half: volume expanded and not deep in the base."""
    vols = [r["volume"] for r in rows[i - 19:i + 1]]
    if not vols:
        return False
    ratio = (sum(vols[-5:]) / 5) / max(sum(vols) / 20, 1)
    med20 = sorted(vols)[10]
    med5 = sorted(vols[-5:])[2]
    if ratio <= 1.10 and med5 / max(med20, 1) <= 1.05:
        return False
    c = [r["close"] for r in rows[: i + 1]]
    low60 = min(r["low"] for r in rows[i - 59:i + 1])
    if c[i] / low60 - 1 > 0.12 or c[i] / c[i - 5] - 1 > 0.08 or c[i] / c[i - 10] - 1 > 0.12 or c[i] / c[i - 20] - 1 > 0.18:
        return True
    ma5, ma10, ma15, ma30 = (st.mean(c[-n:]) for n in (5, 10, 15, 30))
    return c[i] >= ma5 and c[i] >= ma10 and (c[i] >= ma15 or c[i] >= ma30 * 0.99)


def scan(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    if len(rows) < 200 or any(r["low"] <= 0 or r["close"] <= 0 or r["open"] <= 0 for r in rows):
        return []
    bases = base_dates().get(path.stem, [])
    out = []
    for i in range(260, len(rows) - 1):
        if not cheap(rows, i):
            continue
        m = calculate_metrics(rows[: i + 1])
        if m is None or not startup_confirmation(m):
            continue
        e = {"code": path.stem, "date": rows[i]["date"], "i": i,
             "dayGain": rows[i]["close"] / rows[i - 1]["close"] - 1,
             "inBase": any(rows[max(0, i - 60)]["date"] <= d <= rows[i - 1]["date"] for d in bases)}
        entry = rows[i + 1]["open"]
        e["ret1"] = rows[i + 1]["close"] / entry - 1
        for h in HOLD:
            k = i + h
            if k < len(rows):
                e[f"r{h}"], e[f"d{h}"] = rows[k]["close"] / entry - 1, rows[k]["date"]
        out.append(e)
    return out


def main() -> None:
    global INDEX
    build_base_dates()
    INDEX = benchmark_index()
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            events.extend(result)
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09") and e["date"] in INDEX]
    for e in events:
        for h in HOLD:
            if f"r{h}" in e and e[f"d{h}"] in INDEX:
                e[f"x{h}"] = e[f"r{h}"] - (INDEX[e[f"d{h}"]] / INDEX[e["date"]] - 1)
    limit_up = lambda e: e["dayGain"] >= 0.095  # noqa: E731
    groups = {
        "全部“已启动”剔除日": events,
        "其中：60日内做过底部候选": [e for e in events if e["inBase"]],
        "其中：启动日涨停": [e for e in events if limit_up(e)],
        "其中：底部候选+涨停": [e for e in events if e["inBase"] and limit_up(e)],
    }
    lines = ["# 中间盲区：“已启动被剔除、v1.3 也不收”的票后来怎样", "",
             "剔除口径 = screen_v26_unified.startup_confirmation（RSI 偏热 + 离开底部 + 放量）。",
             "v1.3 要求日 RSI 38–58、周 RSI 32–48，与“偏热”几乎不相交，所以这批票两个模型都不收。", "",
             f"共 {len(events)} 个交易日/股票组合，其中 60 日内做过 v2.6 底部候选的 {len(groups['其中：60日内做过底部候选'])} 个"
             f"（{len(groups['其中：60日内做过底部候选']) / max(len(events), 1):.0%}）。", ""]
    for h in HOLD:
        lines += [f"## 持有 {h} 日", "", "| 组 | 信号 | " + " | ".join(p[0] for p in PERIODS) + " |",
                  "| --- | ---: |" + " --- |" * len(PERIODS)]
        for name, items in groups.items():
            cells = []
            for _, lo, hi in PERIODS:
                xs = [e[f"x{h}"] for e in items if lo <= e["date"] < hi and f"x{h}" in e]
                if len(xs) < 20:
                    cells.append(f"n{len(xs)}")
                    continue
                cells.append(f"{st.mean(xs):+.1%}（中位{st.median(xs):+.1%}，胜{sum(v > 0 for v in xs) / len(xs):.0%}，n{len(xs)}）")
            lines.append(f"| {name} | {len(items)} | " + " | ".join(cells) + " |")
        lines.append("")
    # A second look: entry on the close after the hot day instead of the next open, to see
    # whether waiting one day helps for the limit-up cases.
    lu = [e for e in events if limit_up(e) and "x5" in e]
    blocks = defaultdict(list)
    for e in lu:
        blocks[e["date"][:4]].append(e["x5"])
    lines += ["## 启动日涨停的样本：按年份看 5 日超额", "",
              "| 年份 | 均值 | 中位 | 胜率 | 样本 |", "| --- | ---: | ---: | ---: | ---: |"]
    for year in sorted(blocks):
        v = blocks[year]
        lines.append(f"| {year} | {st.mean(v):+.1%} | {st.median(v):+.1%} | {sum(x > 0 for x in v) / len(v):.0%} | {len(v)} |")
    out = ROOT / "output" / "backtest" / "startup-blindspot.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
