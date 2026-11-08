#!/usr/bin/env python3
"""Pre-registered test of how v2.6 (bottom watchlist) and v1.3 (right-side start) combine.

The hypotheses below were written before looking at any result on the rebuilt data
(output/cache/kline_long, proportional adjustment). Do not add variants after seeing
the output: about 30 combinations were already compared on 2026-10-18, and every extra
one makes the best result look better than it is.

H0  baselines: v2.6, v1.3 (profitable universe).
H1  current main pick: v1.3 + 基本面向好 + CSI 1000 below MA120.
H2  funnel: v1.3 signal whose stock was a v2.6 signal in the previous 30 trading days.
H3  H2 + 基本面向好 + CSI 1000 below MA120.
H4  left-side version: v2.6 + 基本面向好 + CSI 1000 below MA120.
H5  H1 restricted to 弹性 stocks (tests the 2026-10-22 elasticity finding on v1.3).

Pass mark (same as 策略选择建议-2026-10-18): excess > 0 in 2023-07~2024-12 and in
2025-01~2026-06, excess > 0 after dropping the best 3 months, positive months > 50%.
2026-07 onward is reported but not used for the verdict: most 40-day windows are open.

Usage: python3 scripts/research/combo_v13_v26.py [--rescan]
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
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backtest_bottom_base import (  # noqa: E402
    GAP, HEADER, LONG_KLINE_DIR, PERIODS, annotate_market, benchmark_index, row, stats, v26,
)
from backtest_v13 import scan as scan_v13  # noqa: E402
from screen_latest_30 import fetch_financials  # noqa: E402

BACKTEST = ROOT / "output" / "backtest"
FUND = ROOT / "output" / "cache" / "fundamentals"
CHECK = ROOT / "output" / "cache" / "kline_long_check.json"
FUNNEL_DAYS = 30


def load_v13(rescan: bool) -> list[dict]:
    path = BACKTEST / "v13_signals.json"
    if path.exists() and not rescan:
        return json.loads(path.read_text(encoding="utf-8"))
    signals: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan_v13, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            signals.extend(result)
    path.write_text(json.dumps(signals, ensure_ascii=False), encoding="utf-8")
    return signals


def fundamentals_tier0():
    """Point in time: only reports announced before the signal day; earliest notice per period."""
    num = lambda v: isinstance(v, (int, float))  # noqa: E731
    perf: dict[str, dict[str, tuple[str, float | None]]] = defaultdict(dict)
    for r in json.loads((FUND / "perf.json").read_text(encoding="utf-8")):
        if r.get("NOTICE_DATE"):
            q, n = r["REPORTDATE"][:10], r["NOTICE_DATE"][:10]
            if q not in perf[r["SECURITY_CODE"]] or n < perf[r["SECURITY_CODE"]][q][0]:
                perf[r["SECURITY_CODE"]][q] = (n, r.get("SJLTZ"))
    hold: dict[str, list[tuple[str, str, float | None]]] = defaultdict(list)
    for r in json.loads((FUND / "holders.json").read_text(encoding="utf-8")):
        if r.get("HOLD_NOTICE_DATE"):
            hold[r["SECURITY_CODE"]].append((r["END_DATE"][:10], r["HOLD_NOTICE_DATE"][:10], r.get("HOLDER_NUM_RATIO")))

    def tier0(code: str, date: str) -> bool:
        reps = sorted((q, y) for q, (n, y) in perf.get(code, {}).items() if n < date)
        hs = sorted((e, v) for e, n, v in hold.get(code, []) if n < date)
        y, yp = (reps[-1][1] if reps else None), (reps[-2][1] if len(reps) > 1 else None)
        h, hp = (hs[-1][1] if hs else None), (hs[-2][1] if len(hs) > 1 else None)
        inflection = num(y) and num(yp) and y > 0 >= yp
        improving = num(y) and num(yp) and y > yp
        return bool(inflection or (improving and num(h) and num(hp) and h < 0 and hp < 0))
    return tier0


def pick(signals: list[dict], predicate, exit_name: str, index: dict[str, float]) -> list[dict]:
    trades, last = [], {}
    for s in signals:
        if s.get("m") is None or not predicate(s) or s["i"] - last.get(s["code"], -GAP) < GAP:
            continue
        o = s["out"][exit_name]
        if o["exitDate"] not in index or s["date"] not in index:
            continue
        last[s["code"]] = s["i"]
        trades.append({**o, "excess": o["ret"] - (index[o["exitDate"]] / index[s["date"]] - 1),
                       "date": s["date"], "code": s["code"]})
    return trades


def verdict(trades: list[dict]) -> str:
    early = stats([t for t in trades if t["date"] < "2025-01"])
    late = stats([t for t in trades if "2025-01" <= t["date"] < "2026-07"])
    whole = stats([t for t in trades if t["date"] < "2026-07"])
    if min(early["n"], late["n"]) < 20:
        return f"样本不足（前{early['n']}/后{late['n']}）"
    checks = {
        "前段超额>0": early["excess"] > 0,
        "后段超额>0": late["excess"] > 0,
        "去掉最好3个月>0": whole["exTop3"] > 0,
        "正超额月份>50%": whole["posMonths"] > 0.5,
    }
    failed = [k for k, ok in checks.items() if not ok]
    return "通过" if not failed else "未通过：" + "、".join(failed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rescan", action="store_true")
    parser.add_argument("--output", type=Path, default=BACKTEST / "combo-v13-v26.md")
    args = parser.parse_args()

    keep = lambda s: s["date"] >= "2023-07-01" and not s["date"].startswith("2024-09")  # noqa: E731
    index = benchmark_index()
    v26_all = [s for s in json.loads((BACKTEST / "signals.json").read_text(encoding="utf-8")) if keep(s) and v26(s)]
    v13_all = [s for s in load_v13(args.rescan) if keep(s)]
    annotate_market(v26_all, index)
    annotate_market(v13_all, index)

    profitable = set(fetch_financials())  # v1.3 universe; today's list, a known look-ahead
    tier0 = fundamentals_tier0()
    v26_days: dict[str, list[int]] = defaultdict(list)
    for s in v26_all:
        v26_days[s["code"]].append(s["i"])
    for s in v13_all:
        s["profitable"] = s["code"] in profitable
        s["funnel"] = any(s["i"] - FUNNEL_DAYS <= i < s["i"] for i in v26_days.get(s["code"], ()))
    for s in v13_all + v26_all:
        s["tier0"] = tier0(s["code"], s["date"])
        s["weak"] = bool(s.get("m") and s["m"]["belowMa120"])
    check = json.loads(CHECK.read_text(encoding="utf-8")) if CHECK.exists() else {}
    disputed = {f["code"] for f in check.get("flagged", [])}

    base13 = lambda s: s["profitable"]  # noqa: E731
    variants = {
        "H0 v2.6 全部": (v26_all, lambda s: True),
        "H0 v1.3 全部": (v13_all, base13),
        "H1 v1.3+基本面+弱势（现主推）": (v13_all, lambda s: base13(s) and s["tier0"] and s["weak"]),
        "H2 v2.6池→v1.3突破": (v13_all, lambda s: base13(s) and s["funnel"]),
        "H3 H2+基本面+弱势": (v13_all, lambda s: base13(s) and s["funnel"] and s["tier0"] and s["weak"]),
        "H4 v2.6+基本面+弱势": (v26_all, lambda s: s["tier0"] and s["weak"]),
        "H5 H1+弹性": (v13_all, lambda s: base13(s) and s["tier0"] and s["weak"] and s["f"].get("elastic")),
    }
    lines = [
        "# v1.3 × v2.6 组合检验（预先登记的假设，数据：比例前复权重建版）", "",
        "信号 2023-07 起，剔除 2024-09；基准中证1000；止损=收盘跌破信号前60日低点×95%；同股20日内只取首个信号。",
        "“弱势”=中证1000收盘低于MA120（与每日任务一致）。“基本面向好”=净利拐点，或净利改善且股东户数连降（只用信号日前已公告数据）。",
        "判定只看 2023-07~2026-06；2026-07 起多数40日窗口未走完，单独列示。", "",
    ]
    results = {}
    for exit_name, label in (("hold10", "持有10日"), ("stop40", "40日+止损")):
        lines += [f"## {label}", "", "| 方案 | 判定 | 判定（排除复权存疑股票） |", "| --- | --- | --- |"]
        for name, (items, pred) in variants.items():
            t = pick(items, pred, exit_name, index)
            results[(name, exit_name)] = t
            clean = [x for x in t if x["code"] not in disputed]
            lines.append(f"| {name} | {verdict(t)} | {verdict(clean)} |")
        for period, test in (("全部（截至最新）", lambda d: True), *PERIODS):
            lines += ["", f"### {label} · {period}", "", *HEADER]
            lines += [row(name, stats([x for x in results[(name, exit_name)] if test(x["date"])])) for name in variants]
        lines.append("")
    lines += ["局限：仅当前在市股票；v1.3 的“盈利”用今天的名单；未扣交易成本；行业条件未回放。"]
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
