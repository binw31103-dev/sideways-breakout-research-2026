#!/usr/bin/env python3
"""RSI 单独是否站得住：这是决定方案的关键一步。

前面已经发现：
  只要 距MA30 < -5%      20日 +0.027%（t+0.1）  ← 几乎没有贡献
  只要 RSI14 < 35        20日 +0.557%（t+1.5）
  RSI<35 且 距MA30<-5%   20日 +0.720%（t+1.9）
  同条件 10 日           +0.944%（t+2.9），限当日 80 只以内 +1.147%（t+2.8）

也就是说 MA 距离单独没用，RSI 才是主力。那必须回答三个问题，
任何一个不及格，这个方案就不能用：

  1. RSI<35 是不是「距60日低点」的另一个写法？（分档内部再比）
  2. RSI<35 三段是否同向？
  3. 它的路径是「先赚」还是「先亏」？
  4. 阈值敏感性：30/35/40/45 是不是单调？

用法：python3 scripts/research/rsi_final_check.py
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
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backtest_bottom_base import LONG_KLINE_DIR  # noqa: E402
from ma_rsi_ic import scan  # noqa: E402
from screen_latest_30 import ema, rsi  # noqa: E402

PERIODS = (("前段", "2023-07", "2025-01"), ("后段", "2025-01", "2026-07"), ("新段", "2026-07", "9999"))
MIN_AMOUNT = 5e7


def path_scan(file: Path) -> list[dict]:
    """记录入场后 20 日内先触 +8% 还是 -5%。"""
    try:
        rows = json.loads(file.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return []
    if len(rows) < 260 or any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return []
    close = [r["close"] for r in rows]
    volume = [r["volume"] for r in rows]
    r14 = rsi(close, 14)
    out = []
    for k in range(120, len(rows) - 22, 3):
        if r14[k] is None:
            continue
        amt20 = sum(close[i] * volume[i] for i in range(k - 19, k + 1)) / 20
        if amt20 < MIN_AMOUNT:
            continue
        low60 = min(r["low"] for r in rows[k - 59:k + 1])
        if close[k] / low60 - 1 > 0.10:
            continue
        m30 = sum(close[k - 29:k + 1]) / 30
        entry = rows[k + 1]["open"]
        path = "none"
        for t in range(k + 1, k + 21):
            up = rows[t]["high"] / entry - 1 >= 0.08
            down = rows[t]["low"] / entry - 1 <= -0.05
            if up and down:
                path = "both"
                break
            if up:
                path = "up"
                break
            if down:
                path = "down"
                break
        out.append({"date": rows[k]["date"], "rsi14": r14[k], "d30": close[k] / m30 - 1,
                    "distLow": close[k] / low60 - 1, "path": path})
    return out


def load_events() -> tuple[list[dict], dict[str, list[dict]]]:
    jobs = [(p, 3, "bottom") for p in sorted(LONG_KLINE_DIR.glob("*.json"))]
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, jobs, chunksize=20):
            events.extend(result)
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09")
              and e["distLow"] <= 0.10]
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if "r20" in e:
            by_day[e["date"]].append(e)
    for items in by_day.values():
        for h in (10, 20):
            pool = [x for x in items if f"r{h}" in x]
            if len(pool) < 10:
                continue
            mean = st.mean(x[f"r{h}"] for x in pool)
            for x in pool:
                x[f"y{h}"] = x[f"r{h}"] - mean
    return events, by_day


def evaluate(by_day: dict[str, list[dict]], test, h: int = 20, cap: int | None = None) -> dict:
    per = []
    for _d, items in by_day.items():
        sub = [x for x in items if f"y{h}" in x and test(x)]
        if len(sub) < 2 or (cap and len(sub) > cap):
            continue
        per.append(st.mean(x[f"y{h}"] for x in sub))
    if len(per) < 8:
        return {"mean": None, "t": None, "days": len(per)}
    m = st.mean(per)
    sd = st.stdev(per) if len(per) > 1 else 0
    return {"mean": m, "t": m / (sd / math.sqrt(len(per))) if sd else 0, "days": len(per)}


def show(r: dict, digits: int = 3) -> str:
    if r["mean"] is None:
        return f"n/a（{r['days']}日）"
    return f"{r['mean'] * 100:+.{digits}f}%（t{r['t']:+.1f}，{r['days']}日）"


def main() -> None:
    events, by_day = load_events()
    print(f"样本 {len(events)}（距60日低点 ≤10%，每 3 日采样）\n")

    # 1. RSI 是否只是距低点的代理
    print("## 一、控制「距60日低点」后，RSI<35 是否还有增量\n")
    dl = sorted(e["distLow"] for e in events)
    edges = [dl[min(len(dl) - 1, int(len(dl) * i / 4))] for i in range(5)]
    print("| 距低点档 | RSI<35 组 | 同档 RSI≥35 | 差 |")
    print("| --- | --- | --- | --- |")
    for i in range(4):
        lo, hi = edges[i], edges[i + 1] if i < 3 else 1e9
        a, b = [], []
        for _d, items in by_day.items():
            pool = [x for x in items if "y20" in x and lo <= x["distLow"] < hi]
            g1 = [x for x in pool if x["rsi14"] < 35]
            g2 = [x for x in pool if x["rsi14"] >= 35]
            if len(g1) >= 2:
                a.append(st.mean(x["y20"] for x in g1))
            if len(g2) >= 2:
                b.append(st.mean(x["y20"] for x in g2))
        if len(a) < 8 or len(b) < 8:
            print(f"| {lo * 100:.1f}~{min(hi, 0.1) * 100:.1f}% | n/a | n/a | — |")
            continue
        print(f"| {lo * 100:.1f}~{min(hi, 0.1) * 100:.1f}% | {st.mean(a) * 100:+.3f}% | "
              f"{st.mean(b) * 100:+.3f}% | {(st.mean(a) - st.mean(b)) * 100:+.3f}% |")
    print()

    # 2. 三段稳定性 + 阈值敏感性
    print("## 二、阈值敏感性与三段稳定性（按日等权，10 日 / 20 日）\n")
    print("| 阈值 | 10日 | 20日 | 前段 | 后段 | 新段 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for thr in (25, 30, 35, 40, 45, 50):
        test = (lambda t: (lambda e: e["rsi14"] < t))(thr)
        cells = [show(evaluate(by_day, test, 10)), show(evaluate(by_day, test, 20))]
        for _l, lo, hi in PERIODS:
            sub = {d: v for d, v in by_day.items() if lo <= d < hi}
            cells.append(show(evaluate(sub, test, 20), 2))
        print(f"| RSI < {thr} | " + " | ".join(cells) + " |")
    print()

    # 3. 广度限制
    print("## 三、加当日只数上限（避免全市场普跌时满仓抄底）\n")
    print("| 条件 | 10日 | 20日 | 20日（前/后/新） |")
    print("| --- | --- | --- | --- |")
    for cap in (None, 100, 60, 40, 25):
        test = lambda e: e["rsi14"] < 35
        parts = []
        for _l, lo, hi in PERIODS:
            sub = {d: v for d, v in by_day.items() if lo <= d < hi}
            r = evaluate(sub, test, 20, cap)
            parts.append(f"{r['mean'] * 100:+.2f}" if r["mean"] is not None else "n/a")
        label = "不限" if cap is None else f"≤{cap} 只"
        print(f"| {label} | {show(evaluate(by_day, test, 10, cap))} | "
              f"{show(evaluate(by_day, test, 20, cap))} | " + " / ".join(parts) + " |")
    print()

    # 4. 路径
    print("## 四、路径：20 日内先摸 +8% 还是先跌 -5%\n")
    paths: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(path_scan, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            paths.extend(result)
    paths = [p for p in paths if p["date"] >= "2023-07-01" and not p["date"].startswith("2024-09")]
    print("| 组 | 样本 | 先到 +8% | 先到 -5% | 都没触及 |")
    print("| --- | --- | --- | --- | --- |")
    for name, test in (("RSI<35", lambda x: x["rsi14"] < 35),
                       ("RSI<30", lambda x: x["rsi14"] < 30),
                       ("RSI<35 且 距MA30<-5%", lambda x: x["rsi14"] < 35 and x["d30"] < -0.05),
                       ("全部底部样本", lambda x: True)):
        sub = [x for x in paths if test(x)]
        if len(sub) < 100:
            print(f"| {name} | n{len(sub)} | — | — | — |")
            continue
        up = sum(1 for x in sub if x["path"] == "up") / len(sub)
        dn = sum(1 for x in sub if x["path"] == "down") / len(sub)
        print(f"| {name} | {len(sub)} | {up:.0%} | {dn:.0%} | {1 - up - dn:.0%} |")
    print()


if __name__ == "__main__":
    main()
