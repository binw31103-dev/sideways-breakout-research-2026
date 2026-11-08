#!/usr/bin/env python3
"""Backtest the v1.3 right-side start model and compare it with v2.6.

Replays `right_start_candidate` (v1.3's exact technical code) on cached K-lines.
The sector switch is rebuilt from today's industry membership (BOARD_NAME), so
it is approximate. Research only.

Usage: python3 scripts/research/backtest_v13.py --kline-dir DIR --v26-signals FILE
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_bottom_base import EXITS, annotate_market, benchmark_index, outcome, row, stats, v26, HEADER  # noqa: E402
from bottom_base_context import bottom_base_profile  # noqa: E402
from screen_latest_30 import fetch_financials, normalize_industry  # noqa: E402
from screen_v13_right_start import right_start_candidate  # noqa: E402

GAP = 20


def prefilter(rows: list[dict], i: int) -> bool:
    """Cheap superset of v1.3: a close at a 20-day high within the last 5 days, still low."""
    closes = [r["close"] for r in rows[i - 25:i + 1]]
    if not any(closes[k] >= max(closes[k - 20:k]) for k in range(len(closes) - 5, len(closes))):
        return False
    low60 = min(r["low"] for r in rows[i - 59:i + 1])
    return 1.04 <= rows[i]["close"] / low60 <= 1.22 and rows[i]["close"] / rows[i - 20]["close"] <= 1.20


def scan(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    if any(r["low"] <= 0 or r["close"] <= 0 for r in rows):
        return []
    out = []
    for i in range(266, len(rows) - 1):  # incomplete windows are flagged by outcome()
        if not prefilter(rows, i):
            continue
        m = right_start_candidate(rows[:i + 1])
        if m is None:
            continue
        out.append({
            "code": path.stem, "i": i, "date": rows[i]["date"], "stage": m["stage"],
            "f": {**bottom_base_profile(rows[:i + 1]), "price": rows[i]["close"]},
            "out": {name: outcome(rows, i, days, stop) for name, (days, stop) in EXITS.items()},
        })
    return out


def daily_returns(path: Path) -> tuple[str, dict[str, float]]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    return path.stem, {
        cur["date"]: cur["close"] / prev["close"] - 1
        for prev, cur in zip(rows, rows[1:]) if prev["close"] > 0
    }


def sector_activity(codes_by_board: dict[str, list[str]], returns: dict[str, dict[str, float]]) -> dict:
    """Per board and date: breadth and the two largest gains (to exclude the candidate itself)."""
    activity: dict[str, dict[str, tuple[float, list[tuple[float, str]]]]] = {}
    for board, codes in codes_by_board.items():
        by_date: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for code in codes:
            for date, value in returns.get(code, {}).items():
                by_date[date].append((value, code))
        activity[board] = {
            date: (sum(v > 0 for v, _ in vals) / len(vals), sorted(vals, reverse=True)[:2])
            for date, vals in by_date.items() if len(vals) >= 5
        }
    return activity


def sector_pass(activity: dict, board: str, code: str, date: str, dates: list[str], pos: dict[str, int]) -> bool:
    days = activity.get(board) or {}

    def active(d: str) -> bool:
        if d not in days:
            return False
        breadth, top = days[d]
        leader = next((v for v, c in top if c != code), 0.0)
        return breadth >= 0.55 and leader >= 0.03

    k = pos.get(date)
    if k is None or k < 4 or not active(date):
        return False
    return sum(active(d) for d in dates[k - 4:k + 1]) >= 3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kline-dir", type=Path, required=True)
    parser.add_argument("--v26-signals", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("/tmp/sideways-analysis/v13_signals.json"))
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "backtest" / "v13-vs-v26.md")
    args = parser.parse_args()

    paths = sorted(args.kline_dir.glob("*.json"))
    if args.cache.exists():
        signals = json.loads(args.cache.read_text(encoding="utf-8"))
    else:
        signals = []
        with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
            for result in executor.map(scan, paths, chunksize=20):
                signals.extend(result)
        args.cache.write_text(json.dumps(signals), encoding="utf-8")

    profitable = fetch_financials()  # v1.3 universe: profitable, non-ST
    everyone = fetch_financials(include_financial_yellow=True)
    board_of = {code: normalize_industry(str(f.get("BOARD_NAME") or "")) for code, f in everyone.items()}
    codes_by_board: dict[str, list[str]] = defaultdict(list)
    for code, board in board_of.items():
        codes_by_board[board].append(code)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        returns = dict(executor.map(daily_returns, paths))
    activity = sector_activity(codes_by_board, returns)

    index = benchmark_index()
    dates = sorted({d for r in returns.values() for d in r})
    pos = {d: k for k, d in enumerate(dates)}
    keep = lambda s: s["date"] >= "2023-07-01" and not s["date"].startswith("2024-09")
    signals = sorted((s for s in signals if keep(s)), key=lambda s: (s["code"], s["i"]))
    for s in signals:
        s["profitable"] = s["code"] in profitable
        s["sector"] = sector_pass(activity, board_of.get(s["code"], ""), s["code"], s["date"], dates, pos)
    v26_signals = [s for s in json.loads(args.v26_signals.read_text(encoding="utf-8")) if keep(s)]
    annotate_market(v26_signals, index)

    def pick(items: list[dict], pred, exit_name: str) -> list[dict]:
        trades, last = [], {}
        for s in items:
            if not pred(s) or s["i"] - last.get(s["code"], -GAP) < GAP:
                continue
            o = s["out"][exit_name]
            if o["exitDate"] not in index or s["date"] not in index:
                continue
            last[s["code"]] = s["i"]
            trades.append({**o, "excess": o["ret"] - (index[o["exitDate"]] / index[s["date"]] - 1),
                           "date": s["date"], "code": s["code"]})
        return trades

    variants = {
        "v2.6 · 持有10日": (v26_signals, lambda s: s["m"] is not None and v26(s), "hold10"),
        "v2.6 · 40日+止损": (v26_signals, lambda s: s["m"] is not None and v26(s), "stop40"),
        "v1.3 技术面 · 持有10日": (signals, lambda s: s["profitable"], "hold10"),
        "v1.3 技术面 · 40日+止损": (signals, lambda s: s["profitable"], "stop40"),
        "v1.3 技术面+行业扩散(近似) · 持有10日": (signals, lambda s: s["profitable"] and s["sector"], "hold10"),
        "v1.3 技术面+行业扩散(近似) · 40日+止损": (signals, lambda s: s["profitable"] and s["sector"], "stop40"),
        "v1.3 强启动确认+行业 · 持有10日": (signals, lambda s: s["profitable"] and s["sector"] and s["stage"] == "强启动确认", "hold10"),
        "v1.3 右沿初确认+行业 · 持有10日": (signals, lambda s: s["profitable"] and s["sector"] and s["stage"] == "右沿初确认", "hold10"),
    }
    results = {name: pick(items, pred, exit_name) for name, (items, pred, exit_name) in variants.items()}
    lines = ["# v1.3 与 v2.6 回测对比（信号 2023-07 起，剔除 2024-09，基准中证1000）", "",
             "行业扩散按当前行业成分重建：当日板块上涨宽度≥55%且另一只涨幅≥3%，近5日至少3日满足。", "",
             "## 总体", "", *HEADER]
    lines += [row(name, stats(t)) for name, t in results.items()]
    for period, test in (("2023-07 ~ 2024-12", lambda d: d < "2025-01"), ("2025-01 起", lambda d: d >= "2025-01")):
        lines += ["", f"## {period}", "", *HEADER]
        lines += [row(name, stats([x for x in t if test(x["date"])])) for name, t in results.items()]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
