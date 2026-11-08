#!/usr/bin/env python3
"""Do v2.6 bottom picks later become v1.3 strong starts (强启动确认), and is that worth it?

Groups fixed before running (signals 2023-07 on, 2024-09 excluded, rebuilt data):
  conversion  share of v2.6 signals (one per stock per 20 days) followed by a v1.3 signal
              within 10 / 20 / 40 trading days, split by stage, and the lag distribution
  outcome     v1.3 signals entered at the next open, excess vs CSI 1000 after 7 / 10 / 20 days:
                强启动·来自v2.6   strong start, stock was a v2.6 signal in the prior 40 bars
                强启动·其他        strong start without that
                右沿·来自v2.6 / 右沿·其他   same split for the early stage
  same-month  each group minus all v1.3 signals of the same month (market timing cancels)
v1.3 universe is the profitable list, as in the daily job.

Usage: python3 scripts/research/v26_to_strong.py
"""

from __future__ import annotations

import bisect
import json
import statistics as st
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_bottom_base import GAP, LONG_KLINE_DIR, benchmark_index, v26  # noqa: E402

PERIODS = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"),
           ("2026-07起", "2026-07", "9999"))
LOOKBACK = 40
HOLD = (7, 10, 20)


@lru_cache(maxsize=None)
def rows_of(code: str) -> list[dict]:
    path = LONG_KLINE_DIR / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))["rows"] if path.exists() else []


@lru_cache(maxsize=None)
def date_index(code: str) -> dict[str, int]:
    return {r["date"]: k for k, r in enumerate(rows_of(code))}


def dedupe(signals: list[dict]) -> list[dict]:
    out, last = [], {}
    for s in sorted(signals, key=lambda s: (s["code"], s["date"])):
        k = date_index(s["code"]).get(s["date"])
        if k is None or k - last.get(s["code"], -GAP) < GAP:
            continue
        last[s["code"]] = k
        out.append({**s, "k": k})
    return out


def main() -> None:
    from screen_latest_30 import fetch_financials
    index = benchmark_index()
    profitable = set(fetch_financials())
    keep = lambda d: d >= "2023-07-01" and not d.startswith("2024-09")  # noqa: E731
    v26_raw = [s for s in json.loads((ROOT / "output/backtest/signals.json").read_text(encoding="utf-8")) if keep(s["date"]) and v26(s)]
    v13_raw = [s for s in json.loads((ROOT / "output/backtest/v13_signals.json").read_text(encoding="utf-8"))
               if keep(s["date"]) and s["code"] in profitable]
    v26_sig, v13_sig = dedupe(v26_raw), dedupe(v13_raw)
    # All raw v2.6 days per stock (not deduped), for the "was in the pool" lookback.
    v26_days: dict[str, list[int]] = defaultdict(list)
    for s in v26_raw:
        k = date_index(s["code"]).get(s["date"])
        if k is not None:
            v26_days[s["code"]].append(k)
    for v in v26_days.values():
        v.sort()
    v13_by_code: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for s in v13_raw:
        k = date_index(s["code"]).get(s["date"])
        if k is not None:
            v13_by_code[s["code"]].append((k, s.get("stage", "")))
    for v in v13_by_code.values():
        v.sort()

    lines = ["# v2.6 底部票能否走成 v1.3 强启动（比例复权数据，2023-07 起，剔除 2024-09）", ""]
    # 1. Conversion
    lines += ["## 1. 转化率：v2.6 入选后 N 个交易日内出现 v1.3 信号的比例", "",
              "| 时段 | v2.6 信号 | 10日内→强启动 | 20日内→强启动 | 40日内→强启动 | 40日内→右沿初确认 | 40日内→任一 v1.3 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    lags = []
    for name, lo, hi in PERIODS + (("全部", "0", "9999"),):
        xs = [s for s in v26_sig if lo <= s["date"] < hi]
        if not xs:
            continue
        def hit(s: dict, days: int, stage: str | None) -> int | None:
            for k, st_ in v13_by_code.get(s["code"], []):
                if s["k"] < k <= s["k"] + days and (stage is None or st_ == stage):
                    return k - s["k"]
            return None
        cells = [f"{sum(hit(s, d, '强启动确认') is not None for s in xs) / len(xs):.1%}" for d in (10, 20, 40)]
        cells.append(f"{sum(hit(s, 40, '右沿初确认') is not None for s in xs) / len(xs):.1%}")
        cells.append(f"{sum(hit(s, 40, None) is not None for s in xs) / len(xs):.1%}")
        lines.append(f"| {name} | {len(xs)} | " + " | ".join(cells) + " |")
        if name == "全部":
            lags = [h for s in xs if (h := hit(s, 40, "强启动确认")) is not None]
    if lags:
        lags.sort()
        lines += ["", f"v2.6 入选到强启动的间隔：中位 {st.median(lags):.0f} 个交易日，"
                  f"25%分位 {lags[len(lags) // 4]}，75%分位 {lags[3 * len(lags) // 4]}（共 {len(lags)} 次）。"]
    total_strong = sum(1 for s in v13_sig if s.get("stage") == "强启动确认")
    from_pool = sum(1 for s in v13_sig if s.get("stage") == "强启动确认"
                    and any(s["k"] - LOOKBACK <= k < s["k"] for k in v26_days.get(s["code"], [])))
    lines += [f"反过来看：全部 {total_strong} 次强启动里，{from_pool} 次（{from_pool / max(total_strong, 1):.0%}）此前 {LOOKBACK} 个交易日内在 v2.6 池里出现过。", ""]

    # 2. Outcomes
    for s in v13_sig:
        s["fromPool"] = any(s["k"] - LOOKBACK <= k < s["k"] for k in v26_days.get(s["code"], []))
        rows = rows_of(s["code"])
        if s["k"] + 1 >= len(rows) or s["date"] not in index:
            continue
        entry = rows[s["k"] + 1]["open"]
        for h in HOLD:
            j = s["k"] + h
            if j < len(rows) and rows[j]["date"] in index:
                s[f"x{h}"] = rows[j]["close"] / entry - 1 - (index[rows[j]["date"]] / index[s["date"]] - 1)
    month = {h: defaultdict(list) for h in HOLD}
    for s in v13_sig:
        for h in HOLD:
            if f"x{h}" in s:
                month[h][s["date"][:7]].append(s[f"x{h}"])
    groups = {
        "强启动·来自v2.6": lambda s: s.get("stage") == "强启动确认" and s["fromPool"],
        "强启动·其他": lambda s: s.get("stage") == "强启动确认" and not s["fromPool"],
        "右沿·来自v2.6": lambda s: s.get("stage") == "右沿初确认" and s["fromPool"],
        "右沿·其他": lambda s: s.get("stage") == "右沿初确认" and not s["fromPool"],
    }
    lines += ["## 2. 强启动当天发现、次日开盘买入：来自 v2.6 池 vs 其他", "",
              "格式：平均超额（同月差，跑赢比例，样本）。同月差 = 该组 − 同月全部 v1.3 信号。", ""]
    for h in HOLD:
        lines += [f"### 持有 {h} 日", "", "| 组 | " + " | ".join(p[0] for p in PERIODS) + " |", "| --- |" + " --- |" * len(PERIODS)]
        for name, test in groups.items():
            cells = []
            for _, lo, hi in PERIODS:
                xs = [s for s in v13_sig if test(s) and lo <= s["date"] < hi and f"x{h}" in s]
                if len(xs) < 8:
                    cells.append(f"n{len(xs)}")
                    continue
                ex = [s[f"x{h}"] for s in xs]
                dm = st.mean(s[f"x{h}"] - st.mean(month[h][s["date"][:7]]) for s in xs)
                cells.append(f"{st.mean(ex):+.1%}（{dm:+.1%}，{sum(v > 0 for v in ex) / len(ex):.0%}，n{len(xs)}）")
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
        lines.append("")
    out = ROOT / "output" / "backtest" / "v26-to-strong.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
