#!/usr/bin/env python3
"""Daily-pick simulation: ranking rule x exit rule x market switch, judged by period.

Mirrors the live job: each day take at most N candidates, skip stocks picked in the
previous GAP trading days, enter at the next open. The literature composite
(factor_study.FEATURES with a sign) is scored against a reference distribution built from
2023-07~2024-12 signals only, so later periods are out of sample for the scaling too.

Usage: python3 scripts/research/portfolio_sim.py [--top 10]
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backtest_bottom_base import GAP, annotate_market, benchmark_index, v26  # noqa: E402
from combo_v13_v26 import fundamentals_tier0  # noqa: E402
from factor_study import FEATURES, PERIODS, QUOTES, features, load_fundamentals, rows_of  # noqa: E402

PRIOR = {k: v for k, v in FEATURES.items() if v}
EXITS = {
    # name: (max days, stop buffer or None, take-profit or None)
    "持有10日": (10, None, None),
    "持有20日": (20, None, None),
    "持有40日": (40, None, None),
    "40日·止损2%": (40, 0.98, None),
    "40日·止损5%（现行）": (40, 0.95, None),
    "40日·止损10%": (40, 0.90, None),
    "60日·止损5%": (60, 0.95, None),
    "40日·止损5%·止盈20%": (40, 0.95, 0.20),
}


def exit_result(code: str, i: int, days: int, buffer: float | None, take: float | None) -> dict | None:
    rows = rows_of(code)
    if i + 1 >= len(rows):
        return None
    entry = rows[i + 1]["open"]
    stop = min(r["low"] for r in rows[i - 59:i + 1]) * buffer if buffer else None
    last = min(i + days, len(rows) - 1)
    k_exit = last
    for k in range(i + 1, last + 1):
        c = rows[k]["close"]
        if (stop and c < stop) or (take and c / entry - 1 >= take):
            k_exit = k
            break
    done = k_exit < last or i + days <= len(rows) - 1
    return {"exitDate": rows[k_exit]["date"], "ret": rows[k_exit]["close"] / entry - 1, "complete": done}


def summary(trades: list[dict]) -> str:
    done = [t for t in trades if t["complete"]]
    if len(done) < 10:
        return f"n={len(done)}"
    by_month = defaultdict(list)
    for t in done:
        by_month[t["date"][:7]].append(t["excess"])
    top3 = sorted(by_month, key=lambda m: -sum(by_month[m]))[:3]
    ex3 = [t["excess"] for t in done if t["date"][:7] not in top3]
    pos = sum(st.mean(v) > 0 for v in by_month.values()) / len(by_month)
    return (f"{st.mean(t['excess'] for t in done):+.1%} / 中位{st.median(t['excess'] for t in done):+.1%} / "
            f"胜{sum(t['ret'] > 0 for t in done) / len(done):.0%} / 去3月{st.mean(ex3) if ex3 else 0:+.1%} / "
            f"正月{pos:.0%} / n{len(done)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "backtest" / "portfolio-sim.md")
    args = parser.parse_args()

    keep = lambda s: s["date"] >= "2023-07-01" and not s["date"].startswith("2024-09")  # noqa: E731
    index = benchmark_index()
    quotes = json.loads(QUOTES.read_text(encoding="utf-8"))
    shares = {c: (q["totalMarketCap"] / q["close"], q["floatMarketCap"] / q["close"])
              for c, q in quotes.items() if q.get("close") and q.get("totalMarketCap")}
    fund_at, tier0 = load_fundamentals(), fundamentals_tier0()
    models = {
        "v2.6": [s for s in json.loads((ROOT / "output/backtest/signals.json").read_text(encoding="utf-8")) if keep(s) and v26(s)],
        "v1.3": [s for s in json.loads((ROOT / "output/backtest/v13_signals.json").read_text(encoding="utf-8")) if keep(s)],
    }
    profitable = None
    lines = [f"# 每日选股模拟（每日最多 {args.top} 只，同股 {GAP} 日内不重复，相对中证1000超额）", "",
             "格式：平均超额 / 中位超额 / 盈利占比 / 去掉最好3个月 / 超额为正月份 / 已完结样本", ""]
    for model, signals in models.items():
        if model == "v1.3":
            from screen_latest_30 import fetch_financials
            profitable = profitable or set(fetch_financials())
            signals = [s for s in signals if s["code"] in profitable]
        annotate_market(signals, index)
        signals = [s for s in signals if s.get("m")]
        for s in signals:
            s["feat"] = features(s, fund_at, tier0, shares)
        train = [s for s in signals if s["date"] < "2025-01"]
        ref = {f: sorted(s["feat"][f] * sign for s in train if f in s["feat"]) for f, sign in PRIOR.items()}

        def composite(s: dict) -> float:
            parts = [bisect.bisect_left(ref[f], s["feat"][f] * sign) / max(len(ref[f]), 1)
                     for f, sign in PRIOR.items() if f in s["feat"] and ref[f]]
            return st.mean(parts) if parts else 0.5

        for s in signals:
            s["score"] = composite(s)
        rules = {
            "不排序（全部候选）": None,
            "现行排序": lambda s: (-s["feat"].get("fundTier0", 0), -min(s["feat"].get("bigUp250", 0), 3), s["feat"].get("price", 0)),
            "文献合成分": lambda s: -s["score"],
        }
        by_day = defaultdict(list)
        for s in signals:
            by_day[s["date"]].append(s)
        picks = {}
        for rule, key in rules.items():
            chosen, last = [], {}
            for day in sorted(by_day):
                cands = [s for s in by_day[day] if s["i"] - last.get(s["code"], -GAP) >= GAP]
                if key:
                    cands = sorted(cands, key=key)[: args.top]
                for s in cands:
                    last[s["code"]] = s["i"]
                    chosen.append(s)
            picks[rule] = chosen
        counts = [len(v) for v in by_day.values()]
        lines += [f"## {model}（交易日 {len(by_day)}，日均候选 {st.mean(counts):.1f}，超过 {args.top} 只的天数 {sum(c > args.top for c in counts) / len(counts):.0%}）", ""]

        def trades_for(chosen: list[dict], exit_name: str) -> list[dict]:
            days, buffer, take = EXITS[exit_name]
            out = []
            for s in chosen:
                r = exit_result(s["code"], s["i"], days, buffer, take)
                if r and r["exitDate"] in index:
                    out.append({**r, "date": s["date"], "weak": s["m"]["belowMa120"],
                                "excess": r["ret"] - (index[r["exitDate"]] / index[s["date"]] - 1)})
            return out

        lines += ["### 排序（离场：40日·止损5%）", "", f"| 排序 | {' | '.join(p[0] for p in PERIODS)} |", "| --- |" + " --- |" * len(PERIODS)]
        for rule, chosen in picks.items():
            t = trades_for(chosen, "40日·止损5%（现行）")
            lines.append(f"| {rule} | " + " | ".join(summary([x for x in t if lo <= x["date"] < hi]) for _, lo, hi in PERIODS) + " |")
        lines += ["", "### 离场规则（排序：文献合成分）", "", f"| 离场 | {' | '.join(p[0] for p in PERIODS)} |", "| --- |" + " --- |" * len(PERIODS)]
        for exit_name in EXITS:
            t = trades_for(picks["文献合成分"], exit_name)
            lines.append(f"| {exit_name} | " + " | ".join(summary([x for x in t if lo <= x["date"] < hi]) for _, lo, hi in PERIODS) + " |")
        lines += ["", "### 大盘状态（排序：文献合成分；离场：40日·止损5%）", "", f"| 状态 | {' | '.join(p[0] for p in PERIODS)} |", "| --- |" + " --- |" * len(PERIODS)]
        t = trades_for(picks["文献合成分"], "40日·止损5%（现行）")
        for label, weak in (("中证1000低于MA120", True), ("中证1000高于MA120", False)):
            lines.append(f"| {label} | " + " | ".join(summary([x for x in t if lo <= x["date"] < hi and x["weak"] == weak]) for _, lo, hi in PERIODS) + " |")
        lines.append("")
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
