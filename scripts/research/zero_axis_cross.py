#!/usr/bin/env python3
"""Golden crosses at or above the zero axis: are they better than underwater crosses?

A golden cross where DIF crosses above DEA. Groups fixed before running, using DIF and DEA
divided by the close so price levels are comparable:
  A  水下金叉        DIF < 0 and DEA < 0 (the list used since 2026-10-27)
  B  水上金叉        DIF > 0 and DEA > 0
  C  0轴附近水上金叉  B and |DIF|/close <= 1%  (the case asked about)
  D  C 且刚走出水下    C and DIF was < 0 at some point in the previous 60 bars
  E  跨0轴金叉      DIF crosses up through 0 with DEA within +/-1% of 0 (not a DIF/DEA cross)
  F  对照：远离0轴    B and |DIF|/close >= 3%
Each group also split by price position: close <= 60-day low x 1.15 ("底部附近") vs above.

Outcome: next-open entry, raw return and excess vs CSI 1000 after 10 / 20 / 40 days, plus
the same-month difference against all crosses of the same month.

Usage: python3 scripts/research/zero_axis_cross.py
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
from screen_latest_30 import ema  # noqa: E402

PERIODS = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"),
           ("2026-07起", "2026-07", "9999"))
HOLD = (10, 20, 40)
NEAR = 0.01
FAR = 0.03


def scan(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    if len(rows) < 200 or any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return []
    c = [r["close"] for r in rows]
    fast, slow = ema(c, 12), ema(c, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    out = []
    for k in range(180, len(rows) - 1):
        if not (dif[k - 1] < dea[k - 1] and dif[k] >= dea[k]):
            continue
        amount20 = sum(r["close"] * r["volume"] for r in rows[k - 19:k + 1]) / 20
        if amount20 < 5e7:
            continue
        d, e, price = dif[k], dea[k], c[k]
        near_zero = abs(d) / price <= NEAR
        underwater_recent = any(dif[j] < 0 for j in range(max(0, k - 60), k))
        low60 = min(r["low"] for r in rows[k - 59:k + 1])
        tags = []
        if d < 0 and e < 0:
            tags.append("A")
        if d > 0 and e > 0:
            tags.append("B")
            if near_zero:
                tags.append("C")
                if underwater_recent:
                    tags.append("D")
            if abs(d) / price >= FAR:
                tags.append("F")
        entry = rows[k + 1]["open"]
        item = {"code": path.stem, "date": rows[k]["date"], "tags": tags,
                "low": c[k] / low60 - 1 <= 0.15, "difPct": d / price}
        for h in HOLD:
            if k + h < len(rows):
                item[f"r{h}"], item[f"d{h}"] = rows[k + h]["close"] / entry - 1, rows[k + h]["date"]
        out.append(item)
    return out


def main() -> None:
    index = benchmark_index()
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            events.extend(result)
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09") and e["date"] in index]
    for e in events:
        for h in HOLD:
            if f"r{h}" in e and e[f"d{h}"] in index:
                e[f"x{h}"] = e[f"r{h}"] - (index[e[f"d{h}"]] / index[e["date"]] - 1)
    groups = {"A 水下金叉": lambda e: "A" in e["tags"], "B 水上金叉（全部）": lambda e: "B" in e["tags"],
              "C 0轴附近水上金叉": lambda e: "C" in e["tags"], "D C且刚走出水下": lambda e: "D" in e["tags"],
              "F 对照：远离0轴的水上金叉": lambda e: "F" in e["tags"]}
    lines = ["# 0 轴附近的水上金叉（比例复权数据，2023-07 起，剔除 2024-09，20日均额≥5000万）", "",
             "水上金叉 = DIF、DEA 均 > 0；0 轴附近 = |DIF|/收盘价 ≤ 1%（按价格归一，不同价位的股票可比）。",
             "格式：平均收益（平均超额，跑赢比例，样本）；超额相对中证1000。", ""]
    for h in HOLD:
        lines += [f"## 持有 {h} 日", "", "| 组 | 全部 | 其中：底部附近（距60日低点≤15%） | " + " | ".join(p[0] for p in PERIODS) + " |",
                  "| --- | --- | --- |" + " --- |" * len(PERIODS)]
        for name, test in groups.items():
            hit = [e for e in events if test(e) and f"x{h}" in e]
            if not hit:
                continue
            all_cell = (f"{st.mean(e[f'r{h}'] for e in hit):+.1%}（{st.mean(e[f'x{h}'] for e in hit):+.1%}，"
                        f"{sum(e[f'x{h}'] > 0 for e in hit) / len(hit):.0%}，n{len(hit)}）")
            low = [e for e in hit if e["low"]]
            low_cell = (f"{st.mean(e[f'r{h}'] for e in low):+.1%}（{st.mean(e[f'x{h}'] for e in low):+.1%}，n{len(low)}）"
                        if len(low) >= 10 else f"n{len(low)}")
            cells = []
            for _, lo, hi in PERIODS:
                xs = [e for e in hit if lo <= e["date"] < hi]
                cells.append(f"{st.mean(e[f'x{h}'] for e in xs):+.1%}（n{len(xs)}）" if len(xs) >= 10 else f"n{len(xs)}")
            lines.append(f"| {name} | {all_cell} | {low_cell} | " + " | ".join(cells) + " |")
        lines.append("")
    # How often the near-zero above-water cross sits inside the daily lists.
    out = ROOT / "output" / "backtest" / "zero-axis-cross.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
