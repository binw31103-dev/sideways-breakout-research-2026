#!/usr/bin/env python3
"""MA / RSI 这一族指标，到底有没有横截面预测力。

项目现有的 MA/RSI 用法都是"门槛"（RSI 28~45、收盘 ≤ MA15×1.03 这类），
从来没检验过它们在**同一时点的股票之间**能不能分出好坏。这个脚本补这一课。

做法（横截面 IC，最干净的检验方式）：
  对每一个交易日 d，取当日全部满足流动性要求的股票，算出各指标的数值，
  再算它们的 h 日远期相对收益（减掉当日全市场均值），
  求 Spearman 秩相关。把所有交易日的 IC 平均，得到该指标的预测力。
  优点：自动剔除大盘涨跌，不考虑择时，只回答"同一天里挑哪个"。

指标（全部只用当日及之前的数据）：
  MA 距离   close/MA5,10,15,30,60 − 1
  MA 斜率   MA10/MA20/M60 近 10 日斜率
  MA 排列   MA5>MA10, MA10>MA20, MA20>MA60 的成立个数（0~3）
  MA 粘合   (max−min)/close，MA5/10/20 的最大最小差
  MA 上方   close > MA20 的最近 20 日中占比
  MA 交叉   距 MA5 上穿 MA10 的天数（无=99）
  RSI       日 RSI14、周 RSI14、日 RSI6、日 RSI24
  RSI 变化  日 RSI14 近 3/5 日变化
  RSI 结构  RSI6 − RSI24（短长分离度）

用法：
  python3 scripts/research/ma_rsi_ic.py                # 全样本 IC（默认每 5 日采样）
  python3 scripts/research/ma_rsi_ic.py --stride 10
  python3 scripts/research/ma_rsi_ic.py --context bottom
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_bottom_base import LONG_KLINE_DIR, benchmark_index  # noqa: E402
from screen_latest_30 import ema, rsi  # noqa: E402

HOLD = (5, 10, 20)
MIN_AMOUNT = 5e7
PERIODS = (("前段", "2023-07", "2025-01"), ("后段", "2025-01", "2026-07"), ("新段", "2026-07", "9999"))

FEATURES = (
    "d5", "d10", "d15", "d30", "d60",          # 距各均线
    "s10", "s20", "s60",                        # 均线斜率
    "align", "spread", "above20", "crossAge",   # 排列/粘合/位置
    "rsi14", "rsi6", "rsi24", "rsiW",           # RSI 水平
    "rsiChg3", "rsiChg5", "rsiSpan",            # RSI 变化与结构
    "ret5", "ret20", "distLow",                 # 对照用的价格动量
)


def weekly_closes(rows: list[dict]) -> list[float]:
    from collections import defaultdict as dd
    import datetime as dt
    groups: dict[tuple[int, int], list[dict]] = dd(list)
    for row in rows:
        y, w, _ = dt.date.fromisoformat(row["date"]).isocalendar()
        groups[(y, w)].append(row)
    return [v[-1]["close"] for _, v in sorted(groups.items())]


def scan(task: tuple[Path, int, str]) -> list[dict]:
    path, stride, context = task
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return []
    if len(rows) < 260 or any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return []
    close = [r["close"] for r in rows]
    volume = [r["volume"] for r in rows]
    dif_fast, dif_slow = ema(close, 12), ema(close, 26)
    dif = [a - b for a, b in zip(dif_fast, dif_slow)]
    dea = ema(dif, 9)
    r14 = rsi(close, 14)
    r6 = rsi(close, 6)
    r24 = rsi(close, 24)
    wcloses = weekly_closes(rows)
    wr = rsi(wcloses, 14)

    def ma(n: int, k: int) -> float:
        return sum(close[k - n + 1:k + 1]) / n

    # 周线 RSI 对齐：每周最后一个交易日更新
    week_last = {}
    import datetime as dt
    for i, r in enumerate(rows):
        y, w, _ = dt.date.fromisoformat(r["date"]).isocalendar()
        week_last[(y, w)] = i
    week_index = {}
    for n, (key, idx) in enumerate(sorted(week_last.items())):
        week_index[idx] = n

    out = []
    for k in range(120, len(rows) - max(HOLD) - 1, stride):
        amt20 = sum(close[i] * volume[i] for i in range(k - 19, k + 1)) / 20
        if amt20 < MIN_AMOUNT:
            continue
        price = close[k]
        low60 = min(r["low"] for r in rows[k - 59:k + 1])
        if context == "bottom" and price / low60 - 1 > 0.10:
            continue
        if context == "mid" and price / low60 - 1 <= 0.10:
            continue
        m5, m10, m15, m30, m60 = (ma(n, k) for n in (5, 10, 15, 30, 60))
        m10b, m20b, m60b = ma(10, k - 10), ma(20, k - 10), ma(60, k - 10)
        cross_age = 99
        for j in range(k, max(1, k - 40), -1):
            if ma(5, j) > ma(10, j) and ma(5, j - 1) <= ma(10, j - 1):
                cross_age = k - j
                break
        wpos = week_index.get(k)
        wval = wr[wpos] if wpos is not None and wpos < len(wr) and wr[wpos] is not None else None
        if wval is None or r14[k] is None or r6[k] is None or r24[k] is None:
            continue
        item = {
            "code": path.stem, "date": rows[k]["date"], "close": price,
            "d5": price / m5 - 1, "d10": price / m10 - 1, "d15": price / m15 - 1,
            "d30": price / m30 - 1, "d60": price / m60 - 1,
            "s10": m10 / m10b - 1, "s20": ma(20, k) / m20b - 1, "s60": m60 / m60b - 1,
            "align": int(m5 > m10) + int(m10 > ma(20, k)) + int(ma(20, k) > m60),
            "spread": (max(m5, m10, ma(20, k)) - min(m5, m10, ma(20, k))) / price,
            "above20": sum(1 for i in range(k - 19, k + 1) if close[i] > ma(20, i)) / 20,
            "crossAge": cross_age,
            "rsi14": r14[k], "rsi6": r6[k], "rsi24": r24[k], "rsiW": wval,
            "rsiChg3": r14[k] - r14[k - 3] if r14[k - 3] is not None else 0,
            "rsiChg5": r14[k] - r14[k - 5] if r14[k - 5] is not None else 0,
            "rsiSpan": r6[k] - r24[k],
            "ret5": price / close[k - 5] - 1, "ret20": price / close[k - 20] - 1,
            "distLow": price / low60 - 1,
        }
        entry = rows[k + 1]["open"]
        for h in HOLD:
            if k + h < len(rows):
                item[f"r{h}"] = rows[k + h]["close"] / entry - 1
                item[f"d{h}"] = rows[k + h]["date"]
        out.append(item)
    return out


def rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for pos, i in enumerate(order):
        ranks[i] = float(pos)
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 30:
        return None
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def ic_tables(events: list[dict]) -> None:
    for h in HOLD:
        usable = [e for e in events if f"r{h}" in e]
        by_day: dict[str, list[dict]] = defaultdict(list)
        for e in usable:
            by_day[e["date"]].append(e)
        # 当日相对收益
        for items in by_day.values():
            mean = st.mean(x[f"r{h}"] for x in items)
            for x in items:
                x[f"y{h}"] = x[f"r{h}"] - mean
        print(f"## 持有 {h} 日（横截面 IC，正=数值越大越好；括号为跨日 t 值）\n")
        print("| 指标 | 全部 | 前段 | 后段 | 新段 |")
        print("| --- | --- | --- | --- | --- |")
        rows_out = []
        for feat in FEATURES:
            ics_per_period = {}
            for name, lo, hi in (("全部", "0000", "9999"),) + PERIODS:
                per_date = []
                for d, items in by_day.items():
                    if not (lo <= d < hi):
                        continue
                    xs = [x[feat] for x in items]
                    ys = [x[f"y{h}"] for x in items]
                    ic = spearman(xs, ys)
                    if ic is not None:
                        per_date.append(ic)
                if len(per_date) < 8:
                    ics_per_period[name] = (None, None, len(per_date))
                    continue
                m = st.mean(per_date)
                sd = st.stdev(per_date) if len(per_date) > 1 else 0
                t = m / (sd / math.sqrt(len(per_date))) if sd else 0
                ics_per_period[name] = (m, t, len(per_date))
            def fmt(key):
                v = ics_per_period.get(key)
                if not v or v[0] is None:
                    return "—"
                return f"{v[0]:+.3f}（{v[1]:+.1f}）"
            rows_out.append((feat, fmt("全部"), fmt("前段"), fmt("后段"), fmt("新段")))
        for feat, a, b, c, d in rows_out:
            print(f"| {feat} | {a} | {b} | {c} | {d} |")
        print()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--stride", type=int, default=5)
    parser.add_argument("--context", default="all", choices=("all", "bottom", "mid"))
    args = parser.parse_args()
    jobs = [(p, args.stride, args.context) for p in sorted(LONG_KLINE_DIR.glob("*.json"))]
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, jobs, chunksize=20):
            events.extend(result)
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09")]
    print(f"事件 {len(events)} 个，日期 {len({e['date'] for e in events})} 个，"
          f"上下文 {args.context}，采样间隔 {args.stride}\n")
    ic_tables(events)


if __name__ == "__main__":
    main()
