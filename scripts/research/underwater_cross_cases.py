#!/usr/bin/env python3
"""Past cases of the 国电南瑞-style underwater golden cross (variant G) and how they went.

Sub-groups fixed before running, all observable in real time:
  G     variant G on the cross day (see underwater_cross.py), entry next open
  G3    G, and over the next 3 bars: no close below the low and no dead cross; entry on
        the open of the 4th bar (what 600406 looked like a few days after its cross)
  G3+F  G3 and fundamentals tier 0 at the cross date (earnings inflection, or improving
        with falling holder count; point in time)
Also: the project's own past picks (output/daily/tracking.json) that had a G cross within
10 bars before being selected.

Usage: python3 scripts/research/underwater_cross_cases.py
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
from combo_v13_v26 import fundamentals_tier0  # noqa: E402
from underwater_cross import PERIODS, classify, macd_lines  # noqa: E402

H = (20, 40)


def scan(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    if len(rows) < 200 or any(r["low"] <= 0 or r["close"] <= 0 or r["open"] <= 0 for r in rows):
        return []
    dif, dea, crosses = macd_lines(rows)
    out = []
    for k in crosses:
        if k < 180 or k + 4 >= len(rows):
            continue
        if sum(r["close"] * r["volume"] for r in rows[k - 19:k + 1]) / 20 < 5e7:
            continue
        if not classify(rows, k, dif, crosses)["G"]:
            continue
        low = min(r["low"] for r in rows[k - 59:k + 1])
        held = all(rows[j]["close"] >= low and dif[j] >= dea[j] for j in range(k + 1, k + 4))
        e = {"code": path.stem, "date": rows[k]["date"], "held": held, "close": rows[k]["close"], "low": low}
        for tag, start in (("x", k + 1), ("y", k + 4)):
            entry = rows[start]["open"]
            for h in H:
                end = start - 1 + h
                if end < len(rows):
                    e[f"{tag}{h}"], e[f"{tag}d{h}"] = rows[end]["close"] / entry - 1, rows[end]["date"]
                    e[f"{tag}up{h}"] = max(r["close"] for r in rows[start:end + 1]) / entry - 1
                    e[f"{tag}brk{h}"] = any(r["close"] < low for r in rows[start:end + 1])
        e["entryDate3"] = rows[k + 3]["date"]
        out.append(e)
    return out


def main() -> None:
    index = benchmark_index()
    tier0 = fundamentals_tier0()
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            events.extend(result)
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09") and e["date"] in index]
    for e in events:
        e["tier0"] = tier0(e["code"], e["date"])
        for tag, base in (("x", e["date"]), ("y", e["entryDate3"])):
            for h in H:
                if f"{tag}{h}" in e and e[f"{tag}d{h}"] in index and base in index:
                    e[f"{tag}x{h}"] = e[f"{tag}{h}"] - (index[e[f"{tag}d{h}"]] / index[base] - 1)
    groups = {
        "G 金叉日次日买": ([e for e in events], "x"),
        "G3 金叉后3日站稳再买": ([e for e in events if e["held"]], "y"),
        "G3 未站稳（对照）": ([e for e in events if not e["held"]], "y"),
        "G3+基本面向好": ([e for e in events if e["held"] and e["tier0"]], "y"),
    }
    # Same-month benchmark: all G events of that month, entry next open.
    month = {h: defaultdict(list) for h in H}
    for e in events:
        for h in H:
            if f"xx{h}" in e:
                month[h][e["date"][:7]].append(e[f"xx{h}"])
    lines = ["# 国电南瑞式水下金叉：历史案例（比例复权数据，2023-07 起，剔除 2024-09）", "",
             "超额 = 相对中证1000；同月差 = 该组 − 同月全部 G 案例；跌破 = 持有期内收盘跌破金叉前60日低点。", ""]
    for h in H:
        lines += [f"## 持有 {h} 日", "", "| 组 | " + " | ".join(p[0] for p in PERIODS) + " |", "| --- |" + " --- |" * len(PERIODS)]
        for name, (items, tag) in groups.items():
            cells = []
            for _, lo, hi in PERIODS:
                xs = [e for e in items if lo <= e["date"] < hi and f"{tag}x{h}" in e]
                if len(xs) < 5:
                    cells.append(f"n{len(xs)}")
                    continue
                ex = [e[f"{tag}x{h}"] for e in xs]
                dm = st.mean(e[f"{tag}x{h}"] - st.mean(month[h][e["date"][:7]]) for e in xs if month[h][e["date"][:7]])
                cells.append(f"{st.mean(ex):+.1%}（中位{st.median(ex):+.1%}，同月差{dm:+.1%}，跑赢{sum(x > 0 for x in ex) / len(ex):.0%}，"
                             f"涨≥15% {sum(e[f'{tag}up{h}'] >= 0.15 for e in xs) / len(xs):.0%}，跌破{sum(e[f'{tag}brk{h}'] for e in xs) / len(xs):.0%}，n{len(xs)}）")
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
        lines.append("")

    # Concrete examples: biggest names (by 20d amount proxy unavailable here -> use well-known large caps by code list in tracking)
    done = [e for e in events if e["held"] and e["tier0"] and "yx40" in e]
    done.sort(key=lambda e: e["date"])
    lines += ["## G3+基本面向好 的已完结案例（按日期，每月最多列3个）", "",
              "| 金叉日 | 股票 | 金叉收盘 | 40日涨幅 | 40日超额 | 期间最高 | 是否跌破低点 |", "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    per_month = defaultdict(int)
    for e in done:
        if per_month[e["date"][:7]] >= 3:
            continue
        per_month[e["date"][:7]] += 1
        lines.append(f"| {e['date']} | {e['code']} | {e['close']:.2f} | {e['y40']:+.1%} | {e['yx40']:+.1%} | {e['yup40']:+.1%} | {'是' if e['ybrk40'] else '否'} |")

    tracking = json.loads((ROOT / "output" / "daily" / "tracking.json").read_text(encoding="utf-8"))["records"]
    by_code = defaultdict(list)
    for e in events:
        by_code[e["code"]].append(e)
    lines += ["", "## 项目以前选出的股票里，入选前10个交易日内出现过 G 金叉的", "",
              "| 模型 | 股票 | 金叉日 | 首次入选 | 跟踪状态 | 入选后涨跌 | 期间最高 |", "| --- | --- | --- | --- | --- | ---: | ---: |"]
    found = 0
    for r in sorted(tracking.values(), key=lambda r: r["firstSelectedDate"]):
        path = LONG_KLINE_DIR / f"{r['code']}.json"
        if not path.exists():
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
        dates = [x["date"] for x in rows]
        if r["firstSelectedDate"] not in dates:
            continue
        s = dates.index(r["firstSelectedDate"])
        dif, dea, crosses = macd_lines(rows)
        ks = [k for k in crosses if s - 10 <= k <= s and k >= 180 and classify(rows, k, dif, crosses)["G"]]
        if not ks:
            continue
        found += 1
        f = r.get("followUp") or {}
        lines.append(f"| {r['model']} | {r['name']} {r['code']} | {rows[ks[-1]]['date']} | {r['firstSelectedDate']} | {f.get('status', '待核')} | "
                     f"{f.get('change', 0):+.1%} | {f.get('maxUp', 0):+.1%} |")
    if not found:
        lines.append("| — | 无 | | | | | |")
    out = ROOT / "output" / "backtest" / "underwater-cross-cases.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
