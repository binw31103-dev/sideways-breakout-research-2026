#!/usr/bin/env python3
"""「0 轴附近金叉 → 回踩下水 → 二次金叉」这个形态，到底值不值钱。

来源：用户 2026-11-07 指出的牧原股份（002714）走势 —— 10-22 在 0 轴上金叉（DIF +0.008 /
DEA +0.002），10-28 死叉且 DIF 下水（-0.052），震荡几天，11-03 二次金叉（DIF +0.068 /
DEA -0.020），11-04 DEA 翻正，随后拉起。

定义（只用二次金叉当日及之前的数据，不含未来信息）：
  第一次金叉 j   DIF 上穿 DEA，且 |DIF|/收盘 ≤ near（默认 1%）
  回踩            j 与 k 之间出现过死叉，且 DIF 曾 < 0（下水）
  二次金叉 k     DIF 再次上穿 DEA，DIF > 0，DEA ≥ -dea_tol%（默认 0.2%）
  间隔           min_gap ≤ k-j ≤ max_gap（默认 3~15 个交易日）
  回撤           j 到 k 之间最低收盘 ≥ 第一次金叉收盘 × (1 - max_dip)（默认 8%）

对照组：
  N1 首次金叉（60 日内没有更早的金叉）        —— 没有二次确认的长什么样
  N2 二次金叉但第一次金叉不靠近 0 轴           —— 0 轴位置有没有用
  N3 二次金叉但中间没下水（DIF 一直在 0 以上） —— 下水这一步有没有用
  N4 所有金叉（同日基准）

指标：绝对超额（相对中证1000）与同日相对超额（减掉当天全部金叉的平均超额），
持有 5 / 10 / 20 / 40 日，次日开盘入场。

用法：
  python3 scripts/research/zero_axis_second_cross.py                 # 全样本回测
  python3 scripts/research/zero_axis_second_cross.py --scan          # 扫当前符合形态的票
  python3 scripts/research/zero_axis_second_cross.py --gap 2 20      # 改间隔区间
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
from screen_latest_30 import ema, rsi  # noqa: E402

HOLD = (5, 10, 20, 40)
PERIODS = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"),
           ("2026-07起", "2026-07", "9999"))
MIN_AMOUNT = 5e7
LATEST_DIR = ROOT / "output" / "cache" / "kline" / "2026-11-06"


def load_rows(path: Path) -> list[dict] | None:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return None
    if len(rows) < 260:
        return None
    if any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return None
    return rows


def macd(close: list[float]) -> tuple[list[float], list[float]]:
    fast, slow = ema(close, 12), ema(close, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    return dif, ema(dif, 9)


def classify(code: str, rows: list[dict], near: float, dea_tol: float, min_gap: int,
             max_gap: int, max_dip: float) -> list[dict]:
    close = [r["close"] for r in rows]
    dif, dea = macd(close)
    r14 = rsi(close, 14)
    crosses = [k for k in range(1, len(rows)) if dif[k - 1] < dea[k - 1] and dif[k] >= dea[k]]
    cross_set = set(crosses)
    deaths = [k for k in range(1, len(rows)) if dif[k - 1] > dea[k - 1] and dif[k] <= dea[k]]
    out = []
    for idx, k in enumerate(crosses):
        if k < 120 or k >= len(rows) - 1:
            continue
        price = close[k]
        amt20 = sum(r["close"] * r["volume"] for r in rows[k - 19:k + 1]) / 20
        if amt20 < MIN_AMOUNT:
            continue
        prev = [c for c in crosses if c < k]
        j = prev[-1] if prev else None
        # 中间是否死叉 + DIF 是否下水
        between_death = [d for d in deaths if j is not None and j < d < k]
        dif_min = min(dif[j:k + 1]) if j is not None else None
        gap = (k - j) if j is not None else None
        dip = (1 - min(r["close"] for r in rows[j:k + 1]) / close[j]) if j is not None else None

        is_second = (j is not None and min_gap <= gap <= max_gap and bool(between_death)
                     and dif_min < 0 and dip <= max_dip)
        first_near = abs(dif[j]) / close[j] * 100 <= near if j is not None else False
        dif_pct = dif[k] / price * 100
        dea_pct = dea[k] / price * 100

        target = (is_second and first_near and dif[k] > 0 and dea_pct >= -dea_tol)
        if not target:
            # 仍然记录对照组所需的形态标记
            if not (dif_pct < 0 and dea_pct < 0):
                pass
        low60 = min(r["low"] for r in rows[k - 59:k + 1])
        high60 = max(r["high"] for r in rows[k - 59:k + 1])
        item = {
            "code": code, "date": rows[k]["date"], "close": price,
            "dif_pct": dif_pct, "dea_pct": dea_pct, "gap": gap, "dip": dip,
            "dif_min_pct": (dif_min / close[min(range(j, k), key=lambda x: close[x])] * 100
                            if dif_min is not None else None),
            "first_dif_pct": (dif[j] / close[j] * 100) if j is not None else None,
            "dist_low": (price / low60 - 1) * 100,
            "pos_range": (price - low60) / (high60 - low60) * 100 if high60 > low60 else 100,
            "rsi": r14[k], "ret5": (price / close[k - 5] - 1) * 100,
            "ret20": (price / close[k - 20] - 1) * 100, "amt20_yi": amt20 / 1e8,
            "first_cross": j is None or not (j in cross_set and any(c < j for c in crosses)),
            "target": target, "second": is_second, "first_near": first_near,
            "target_dea_pos": target and dea_pct > 0,
        }
        entry = rows[k + 1]["open"]
        for h in HOLD:
            if k + h < len(rows):
                item[f"r{h}"] = (rows[k + h]["close"] / entry - 1) * 100
                item[f"d{h}"] = rows[k + h]["date"]
        out.append(item)
    return out


def scan_task(args: tuple[Path, float, float, int, int, float]) -> list[dict]:
    path, near, dea_tol, min_gap, max_gap, max_dip = args
    rows = load_rows(path)
    if rows is None:
        return []
    return classify(path.stem, rows, near, dea_tol, min_gap, max_gap, max_dip)


def collect(near: float = 1.0, dea_tol: float = 0.2, min_gap: int = 3, max_gap: int = 15,
            max_dip: float = 8.0) -> list[dict]:
    jobs = [(p, near, dea_tol, min_gap, max_gap, max_dip) for p in sorted(LONG_KLINE_DIR.glob("*.json"))]
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan_task, jobs, chunksize=20):
            events.extend(result)
    index = benchmark_index()
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09")
              and e["date"] in index]
    for e in events:
        for h in HOLD:
            if f"r{h}" in e and e[f"d{h}"] in index:
                e[f"x{h}"] = e[f"r{h}"] - (index[e[f"d{h}"]] / index[e["date"]] - 1) * 100
    for h in HOLD:
        by_day: dict[str, list[float]] = defaultdict(list)
        for e in events:
            if f"x{h}" in e:
                by_day[e["date"]].append(e[f"x{h}"])
        means = {d: st.mean(v) for d, v in by_day.items()}
        for e in events:
            if f"x{h}" in e:
                e[f"rel{h}"] = e[f"x{h}"] - means[e["date"]]
    return events


def cell(sample: list[dict], key: str) -> str:
    if len(sample) < 20:
        return f"n{len(sample)}"
    return f"{st.mean(e[key] for e in sample):+.2f}（n{len(sample)}）"


def group_of(e: dict) -> str | None:
    if e["target"]:
        return "T"
    if e["second"] and not e["first_near"]:
        return "N2"
    return None


def report(events: list[dict]) -> str:
    lines = []
    add = lines.append
    add("# 0 轴附近金叉 → 回踩下水 → 二次金叉（2026-11-07）")
    add("")
    add("单位：百分点。绝对超额 = 相对中证1000；同日相对 = 再减掉当天全部金叉的平均超额。")
    add("")
    groups = {
        "T  目标形态（首次近0轴→下水→二次金叉）": lambda e: e["target"],
        "T+ 目标形态且二次金叉时 DEA 已 >0": lambda e: e["target_dea_pos"],
        "N1 首次金叉（60日内无更早金叉，各类型混合）": lambda e: e["first_cross"] and not e["second"],
        "N2 二次金叉但首次不近0轴": lambda e: e["second"] and not e["first_near"],
        "N3 近0轴金叉但中间没下水（gap 内无死叉或 DIF 未 <0）":
            lambda e: (not e["second"]) and e["first_near"] and e["gap"] is not None and 3 <= e["gap"] <= 15,
        "N4 全部金叉（基准）": lambda e: True,
    }
    for h in HOLD:
        add(f"## 持有 {h} 日")
        add("")
        add("| 组 | 绝对超额 | 同日相对 |")
        add("| --- | --- | --- |")
        for name, test in groups.items():
            sample = [e for e in events if test(e) and f"x{h}" in e]
            add(f"| {name} | {cell(sample, f'x{h}')} | {cell(sample, f'rel{h}')} |")
        add("")
    # 三段稳定性（只用目标组，20 日）
    add("## 目标形态的三段稳定性（20 日同日相对）")
    add("")
    add("| 组 | 前段 | 后段 | 新段 |")
    add("| --- | --- | --- | --- |")
    for name, test in list(groups.items())[:2] + [("N2 对照", lambda e: e["second"] and not e["first_near"])]:
        cells = []
        for _, lo, hi in PERIODS:
            part = [e for e in events if test(e) and lo <= e["date"] < hi and "rel20" in e]
            cells.append(cell(part, "rel20"))
        add(f"| {name} | " + " | ".join(cells) + " |")
    add("")
    # 容量
    target = [e for e in events if e["target"]]
    days = len({e["date"] for e in events})
    add("## 容量")
    add("")
    add(f"目标形态在 {days} 个交易日里共 {len(target)} 次，平均每 {days / max(1, len(target)):.1f} 个交易日 1 次。")
    add("")
    return "\n".join(lines)


def scan_now(near: float, dea_tol: float, min_gap: int, max_gap: int, max_dip: float) -> None:
    if not LATEST_DIR.exists():
        print(f"缺少最新行情缓存 {LATEST_DIR}")
        return
    hits = []
    for path in sorted(LATEST_DIR.glob("*.json")):
        rows = load_rows(path)
        if rows is None:
            continue
        events = classify(path.stem, rows, near, dea_tol, min_gap, max_gap, max_dip)
        recent = [e for e in events if e["date"] >= rows[-6]["date"]]
        for e in recent:
            if e["target"]:
                hits.append(e)
    hits.sort(key=lambda e: e["date"], reverse=True)
    print(f"截至 {rows[-1]['date']}，最近 5 个交易日内出现目标形态的股票：{len(hits)} 只\n")
    print("| 代码 | 信号日 | 收盘 | 首次金叉 DIF% | 二次 DIF% | 二次 DEA% | 间隔 | 回撤 | 距60日低点 | 20日涨幅 | 额(亿) |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for e in hits[:60]:
        print(f"| {e['code']} | {e['date']} | {e['close']:.2f} | {e['first_dif_pct']:+.2f} | {e['dif_pct']:+.2f} | "
              f"{e['dea_pct']:+.2f} | {e['gap']} | {e['dip']:.1f}% | {e['dist_low']:+.1f}% | "
              f"{e['ret20']:+.1f}% | {e['amt20_yi']:.1f} |")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--near", type=float, default=1.0)
    parser.add_argument("--dea-tol", type=float, default=0.2)
    parser.add_argument("--gap", type=int, nargs=2, default=[3, 15])
    parser.add_argument("--max-dip", type=float, default=8.0)
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    if args.scan:
        scan_now(args.near, args.dea_tol, args.gap[0], args.gap[1], args.max_dip)
        return
    events = collect(args.near, args.dea_tol, args.gap[0], args.gap[1], args.max_dip)
    text = report(events)
    print(text)
    if args.out:
        (ROOT / args.out).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
