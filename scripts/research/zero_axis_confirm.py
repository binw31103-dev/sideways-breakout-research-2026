#!/usr/bin/env python3
"""零轴金叉的确认条件：MA / RSI 能不能帮你在金叉里挑出更好的那些。

**范围限定**：只研究"零轴金叉"这一个信号，不涉及其他策略。
问题是：已经出现零轴金叉了，看 MA / RSI 有没有用？

所以基准必须是**同一天、同类金叉的平均表现**，而不是全市场均值。
如果某条件让金叉组的表现显著高于"不加条件的金叉组"，它才有确认价值。

金叉分类（沿用 zero-axis-cross.md 的口径）：
  UU  纯水下      DIF<0 且 DEA<0
  XC  跨轴        DIF>0 但 DEA<0
  AA  纯水上      DIF>0 且 DEA>0
  C   0轴附近水上  AA 且 |DIF|/收盘 ≤ 1%
  CU  0轴附近水下  UU 且 |DIF|/收盘 ≤ 1%

在每个金叉类别内部，按以下条件分档，看表现是否分化：
  日 RSI14 分档、周 RSI14 分档
  距 MA20 / MA30 分档
  MA5/10/20 排列、MA 粘合度、MA10 斜率
  20 日涨幅分档（对照组：判断是不是单纯的反转因子）

用法：python3 scripts/research/zero_axis_confirm.py
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

HOLD = (10, 20, 40)
NEAR = 0.01
MIN_AMOUNT = 5e7
PERIODS = (("前段", "2023-07", "2025-01"), ("后段", "2025-01", "2026-07"), ("新段", "2026-07", "9999"))


def weekly_closes(rows: list[dict]) -> tuple[list[float], dict[int, int]]:
    import datetime as dt
    groups: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for row in rows:
        y, w, _ = dt.date.fromisoformat(row["date"]).isocalendar()
        groups[(y, w)].append(row)
    ordered = sorted(groups.items())
    closes = [v[-1]["close"] for _, v in ordered]
    last_index = {}
    for n, (key, values) in enumerate(ordered):
        last_index[rows.index(values[-1])] = n
    return closes, last_index


def scan(file: Path) -> list[dict]:
    try:
        rows = json.loads(file.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return []
    if len(rows) < 260 or any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return []
    close = [r["close"] for r in rows]
    volume = [r["volume"] for r in rows]
    fast, slow = ema(close, 12), ema(close, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    r14 = rsi(close, 14)
    wcloses, wlast = weekly_closes(rows)
    wr = rsi(wcloses, 14)

    def ma(n: int, k: int) -> float:
        return sum(close[k - n + 1:k + 1]) / n

    out = []
    for k in range(120, len(rows) - 1):
        if r14[k] is None or not (dif[k - 1] < dea[k - 1] and dif[k] >= dea[k]):
            continue
        amt20 = sum(close[i] * volume[i] for i in range(k - 19, k + 1)) / 20
        if amt20 < MIN_AMOUNT:
            continue
        price = close[k]
        d, e = dif[k], dea[k]
        near = abs(d) / price <= NEAR
        if d < 0 and e < 0:
            side = "CU" if near else "UU"
        elif d > 0 and e < 0:
            side = "XC"
        else:
            side = "C" if near else "AA"

        wp = wlast.get(k)
        wval = wr[wp] if wp is not None and wp < len(wr) and wr[wp] is not None else None
        if wval is None:
            continue
        m5, m10, m20, m30 = ma(5, k), ma(10, k), ma(20, k), ma(30, k)
        m10b, m20b = ma(10, k - 10), ma(20, k - 10)
        low60 = min(r["low"] for r in rows[k - 59:k + 1])
        item = {
            "code": file.stem, "date": rows[k]["date"], "side": side, "close": price,
            "rsi": r14[k], "rsiW": wval,
            "ma20d": (price / m20 - 1) * 100, "ma30d": (price / m30 - 1) * 100,
            "align": int(m5 > m10) + int(m10 > m20),
            "spread": (max(m5, m10, m20) - min(m5, m10, m20)) / price * 100,
            "s10": (m10 / m10b - 1) * 100, "s20": (m20 / m20b - 1) * 100,
            "ret20": (price / close[k - 20] - 1) * 100,
            "distLow": (price / low60 - 1) * 100,
            "difPct": d / price * 100,
        }
        entry = rows[k + 1]["open"]
        for h in HOLD:
            if k + h < len(rows):
                item[f"r{h}"] = (rows[k + h]["close"] / entry - 1) * 100
                item[f"d{h}"] = rows[k + h]["date"]
        out.append(item)
    return out


def load() -> tuple[list[dict], dict]:
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            events.extend(result)
    index = benchmark_index()
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09")
              and e["date"] in index]
    for e in events:
        for h in HOLD:
            if f"r{h}" in e and e[f"d{h}"] in index:
                e[f"x{h}"] = e[f"r{h}"] - (index[e[f"d{h}"]] / index[e["date"]] - 1) * 100
    return events, index


def attach_within_side(events: list[dict]) -> None:
    """基准：同一天、同一类别金叉的平均超额。这样比较的是「同类金叉里挑哪个」。"""
    for h in HOLD:
        groups: dict[tuple[str, str], list[float]] = defaultdict(list)
        for e in events:
            if f"x{h}" in e:
                groups[(e["date"], e["side"])].append(e[f"x{h}"])
        means = {k: st.mean(v) for k, v in groups.items()}
        for e in events:
            if f"x{h}" in e:
                e[f"z{h}"] = e[f"x{h}"] - means[(e["date"], e["side"])]


def band_test(events: list[dict], side: str, feat: str, edges: list[float],
              h: int = 20) -> list[tuple[str, int, float, float]]:
    """在某一类金叉内部，按 feat 分档，返回 (档名, 样本, 类内相对超额, 绝对超额)。"""
    pool = [e for e in events if e["side"] == side and f"z{h}" in e]
    if len(pool) < 200:
        return []
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        sub = [e for e in pool if lo <= e[feat] < hi]
        if len(sub) < 40:
            out.append((f"{lo:g}~{hi:g}" if hi < 1e8 else f"≥{lo:g}", len(sub), float("nan"), float("nan")))
            continue
        out.append((f"{lo:g}~{hi:g}" if hi < 1e8 else f"≥{lo:g}", len(sub),
                    st.mean(e[f"z{h}"] for e in sub), st.mean(e[f"x{h}"] for e in sub)))
    return out


def main() -> None:
    events, _ = load()
    attach_within_side(events)
    counts = {s: sum(1 for e in events if e["side"] == s) for s in ("C", "CU", "AA", "UU", "XC")}
    print("金叉样本：" + "，".join(f"{k} {v}" for k, v in counts.items()) + "\n")
    print("说明：'类内相对'= 该档超额 − 同一天同类金叉的平均超额。正=这个条件帮你挑到了更好的金叉。\n")

    specs = [
        ("日 RSI14", "rsi", [0, 25, 30, 35, 40, 45, 50, 60, 1e9]),
        ("距 MA20 %", "ma20d", [-1e9, -8, -5, -2, 0, 2, 5, 1e9]),
        ("距 MA30 %", "ma30d", [-1e9, -8, -5, -2, 0, 2, 5, 1e9]),
        ("20 日涨幅 %", "ret20", [-1e9, -20, -12, -6, -2, 2, 8, 1e9]),
        ("MA 排列(0~2)", "align", [0, 1, 2, 3, 4]),
        ("MA 粘合度 %", "spread", [0, 1, 2, 4, 7, 1e9]),
        ("MA10 斜率 %", "s10", [-1e9, -6, -3, -1, 1, 3, 1e9]),
        ("距60日低点 %", "distLow", [0, 3, 6, 10, 15, 25, 1e9]),
    ]

    for side, title in (("C", "0 轴附近水上金叉（AA 且 |DIF|/价 ≤1%）"),
                        ("CU", "0 轴附近水下金叉（UU 且 |DIF|/价 ≤1%）"),
                        ("XC", "跨轴金叉（DIF>0, DEA<0）")):
        print(f"\n{'=' * 70}\n## {title}\n")
        for label, feat, edges in specs:
            rows = band_test(events, side, feat, edges, 20)
            if not rows:
                print(f"### {label}：样本不足\n")
                continue
            print(f"**{label}**（20 日）\n")
            print("| 档 | 样本 | 类内相对 | 绝对超额 |")
            print("| --- | --- | --- | --- |")
            for name, n, rel, ab in rows:
                if math.isnan(rel):
                    print(f"| {name} | {n} | — | — |")
                else:
                    print(f"| {name} | {n} | {rel:+.3f} | {ab:+.3f} |")
            print()


if __name__ == "__main__":
    main()
