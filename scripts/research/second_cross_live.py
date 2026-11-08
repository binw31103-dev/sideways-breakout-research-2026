#!/usr/bin/env python3
"""扫当前（最新行情缓存）符合「金叉 → 下水 → 二次金叉」的股票。

两种形态分开列，因为回测结论完全不同：
  V1  首次金叉 |DIF|/价 ≤ 1%（牧原 002714 的形态）—— 20日同日相对为负，只作观察
  V2  首次金叉 |DIF|/价 ≥ 3%（深水首次）—— 三段、四个持有期都为正，是唯一站得住的

用法：
  python3 scripts/research/second_cross_live.py                 # 默认：最近 10 个交易日
  python3 scripts/research/second_cross_live.py --days 20
  python3 scripts/research/second_cross_live.py --date 2026-11-06
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from screen_latest_30 import ema, rsi  # noqa: E402
from kline_units import normalize_payload  # noqa: E402

MIN_AMOUNT = 5e7


def side_of(dif1: float, dif2: float) -> str:
    """分组依据回测结论：只有『首次深水 + 二次仍在水下』三段全正，其余只作参考。"""
    if dif1 <= -3 and dif2 <= -3:
        return "S"      # 最强子组
    if dif1 <= -3:
        return "V2a"    # 首次深水，二次已浮上来
    if dif1 >= 3:
        return "V2b"    # 首次在高位（三段方向不一致，不认）
    if abs(dif1) <= 1:
        return "V1"     # 牧原式：全部为负，只作观察
    return "mid"


def normalise_volume(data: dict) -> list[dict]:
    """Use the shared source-aware cache contract; never scale a normalized row twice."""
    return normalize_payload(data, str(data.get("code") or "000000"))["rows"]


def analyse(rows: list[dict], days: int) -> list[dict]:
    close = [r["close"] for r in rows]
    volume = [r["volume"] for r in rows]
    fast, slow = ema(close, 12), ema(close, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    r14 = rsi(close, 14)
    crosses = [k for k in range(1, len(rows)) if dif[k - 1] < dea[k - 1] and dif[k] >= dea[k]]
    deaths = [k for k in range(1, len(rows)) if dif[k - 1] > dea[k - 1] and dif[k] <= dea[k]]
    out = []
    for pos in range(1, len(crosses)):
        k = crosses[pos]
        if k < len(rows) - days:
            continue
        j = crosses[pos - 1]
        gap = k - j
        if not (2 <= gap <= 20):
            continue
        if not any(j < d < k for d in deaths):
            continue
        if min(dif[j:k + 1]) >= 0:
            continue
        amt20 = sum(close[i] * volume[i] for i in range(k - 19, k + 1)) / 20
        if amt20 < MIN_AMOUNT:
            continue
        price = close[k]
        dif1 = dif[j] / close[j] * 100
        low60 = min(r["low"] for r in rows[k - 59:k + 1])
        high60 = max(r["high"] for r in rows[k - 59:k + 1])
        out.append({
            "date": rows[k]["date"], "close": price,
            "side": side_of(dif1, dif[k] / price * 100),
            "dif1": dif1, "dif2": dif[k] / price * 100, "dea2": dea[k] / price * 100,
            "gap": gap, "dip": (1 - min(close[j:k + 1]) / close[j]) * 100,
            "dist_low": (price / low60 - 1) * 100,
            "pos_range": (price - low60) / (high60 - low60) * 100 if high60 > low60 else 100,
            "rsi": r14[k] if r14[k] is not None else 0,
            "ret5": (price / close[k - 5] - 1) * 100, "ret20": (price / close[k - 20] - 1) * 100,
            "amt20_yi": amt20 / 1e8,
            "vol_ratio": volume[k] / (sum(volume[k - 20:k]) / 20),
        })
    return out


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-11-06")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--s-only", action="store_true", help="只看 S 组（首次深水 + 二次仍在水下）")
    parser.add_argument("--strict", action="store_true", help="S 组再叠 RSI≤45 且距60日低点≤8%")
    args = parser.parse_args()
    folder = ROOT / "output" / "cache" / "kline" / args.date
    if not folder.exists():
        print(f"没有缓存目录 {folder}")
        return
    hits = []
    last_date = ""
    for path in sorted(folder.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["code"] = path.stem
            rows = normalise_volume(payload)
        except Exception:
            continue
        if len(rows) < 260 or any(r["close"] <= 0 or r["open"] <= 0 for r in rows):
            continue
        last_date = max(last_date, rows[-1]["date"])
        for e in analyse(rows, args.days):
            e["code"] = path.stem
            hits.append(e)
    if args.s_only or args.strict:
        hits = [h for h in hits if h["side"] == "S"]
    if args.strict:
        hits = [h for h in hits if h["rsi"] <= 45 and h["dist_low"] <= 8]
    hits.sort(key=lambda e: (e["side"], -int(e["date"].replace("-", ""))))
    print(f"数据截至 {last_date}，最近 {args.days} 个交易日内的二次金叉：{len(hits)} 个\n")
    for side, title in (
            ("S", "S 首次深水 + 二次仍在水下（20日三段全正：+1.12 / +0.37 / +2.01）"),
            ("V2a", "V2a 首次深水、二次已浮上（20日 +0.91 / +0.07 / +1.38）"),
            ("V1", "V1 首次金叉贴近 0 轴（牧原式）—— 全部为负，只作观察，不作入场依据"),
            ("V2b", "V2b 首次在 0 轴上方 3% 外（三段方向不一致，不认）"),
            ("mid", "mid 首次金叉距 0 轴 1%~3%")):
        if side == "S":
            title += " ★"
        group = [h for h in hits if h["side"] == side]
        print(f"## {title}：{len(group)} 只\n")
        if not group:
            continue
        print("| 代码 | 信号日 | 收盘 | 首次DIF% | 二次DIF% | 二次DEA% | 间隔 | 回撤 | 距60日低点 | 区间位置 | RSI | 20日涨幅 | 额(亿) |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for h in group[:40]:
            print(f"| {h['code']} | {h['date']} | {h['close']:.2f} | {h['dif1']:+.1f} | {h['dif2']:+.2f} | "
                  f"{h['dea2']:+.2f} | {h['gap']} | {h['dip']:.1f}% | {h['dist_low']:+.1f}% | "
                  f"{h['pos_range']:.0f}% | {h['rsi']:.0f} | {h['ret20']:+.1f}% | {h['amt20_yi']:.1f} |")
        print()


if __name__ == "__main__":
    main()
