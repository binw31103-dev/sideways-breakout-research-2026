#!/usr/bin/env python3
"""Event study: daily MACD golden cross below the zero axis (水下金叉), near vs away from the low.

Prompted by 国电南瑞 600406: 60-day low 2026-09-16, underwater golden cross 2026-09-23
(5 days later, +2.7% above the low, DIF -1.4% of price), price-DIF bullish divergence, no
dead cross afterwards, DIF above zero 2026-10-19. Variants were fixed before running:

A  all underwater golden crosses (DIF < 0, DEA < 0, DIF crosses above DEA)
F  control: underwater cross with close > 60-day low x 1.10 (not at the low)
B  at the low: close <= 60-day low x 1.05, low made 0-10 trading days before the cross
C  B + fell >= 12% from the 120-day high to the low
D  B + bullish divergence: DIF on the low day > lowest DIF of the 60 days before the low
E  C + D (the 600406 pattern)
G  E + clean: no other underwater golden cross in the previous 20 trading days

Outcome: entry next open, excess vs CSI 1000 after 10 / 20 / 40 trading days. "同月差" is
the variant's mean minus all crosses (A) of the same month, so market timing cancels out.
Also for E: entering at the cross vs waiting for DIF to cross zero.

Usage:
  python3 scripts/research/underwater_cross.py                          # event study
  python3 scripts/research/underwater_cross.py live --as-of 2026-10-27 [--days 3] [--json-output F]
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

HORIZONS = (10, 20, 40)
PERIODS = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"), ("2026-07起", "2026-07", "9999"))
VARIANTS = ("A", "F", "B", "C", "D", "E", "G")
LABELS = {"A": "A 全部水下金叉", "F": "F 对照：远离低点(>10%)", "B": "B 低点附近", "C": "C B+前期跌≥12%",
          "D": "D B+底背离", "E": "E C+D（国电南瑞式）", "G": "G E+此前20日无金叉（不反复）"}


def macd_lines(rows: list[dict]) -> tuple[list[float], list[float], list[int]]:
    c = [r["close"] for r in rows]
    fast, slow = ema(c, 12), ema(c, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    crosses = [k for k in range(1, len(c)) if dif[k - 1] < dea[k - 1] and dif[k] >= dea[k] and dif[k] < 0 and dea[k] < 0]
    return dif, dea, crosses


def classify(rows: list[dict], k: int, dif: list[float], crosses: list[int]) -> dict[str, bool]:
    """Variant tags for an underwater golden cross on bar k (see module docstring)."""
    c = rows[k]["close"]
    lo_k = min(range(k - 59, k + 1), key=lambda j: rows[j]["low"])
    low = rows[lo_k]["low"]
    hi_k = max(range(k - 119, lo_k + 1), key=lambda j: rows[j]["high"])
    tags = {"A": True, "F": c > low * 1.10}
    tags["B"] = c <= low * 1.05 and 0 <= k - lo_k <= 10
    tags["C"] = tags["B"] and low / rows[hi_k]["high"] - 1 <= -0.12
    prior_min = min(dif[max(0, lo_k - 60):lo_k]) if lo_k > 0 else dif[lo_k]
    tags["D"] = tags["B"] and dif[lo_k] > prior_min
    tags["E"] = tags["C"] and tags["D"]
    tags["G"] = tags["E"] and not any(k - 20 <= j < k for j in crosses)
    return tags


def scan(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    if len(rows) < 200 or any(r["low"] <= 0 or r["close"] <= 0 or r["open"] <= 0 for r in rows):
        return []
    dif, dea, crosses = macd_lines(rows)
    out = []
    for k in crosses:
        if k < 180 or k + 1 >= len(rows):
            continue
        amount20 = sum(r["close"] * r["volume"] for r in rows[k - 19:k + 1]) / 20
        if amount20 < 5e7:
            continue
        tags = classify(rows, k, dif, crosses)
        entry = rows[k + 1]["open"]
        res = {"code": path.stem, "k": k, "date": rows[k]["date"], "tags": [t for t, on in tags.items() if on]}
        for h in HORIZONS:
            if k + h < len(rows):
                res[f"r{h}"], res[f"d{h}"] = rows[k + h]["close"] / entry - 1, rows[k + h]["date"]
                res[f"up{h}"] = max(r["close"] for r in rows[k + 1:k + h + 1]) / entry - 1
        if tags["E"]:
            z = next((j for j in range(k + 1, min(k + 31, len(rows))) if dif[j - 1] < 0 <= dif[j]), None)
            res["zeroDays"] = None if z is None else z - k
            if z is not None and z + 1 < len(rows):
                res["zeroGainFromCross"] = rows[z]["close"] / entry - 1
                if z + 40 < len(rows):
                    res["rZero40"], res["dZero"], res["dZero40"] = rows[z + 40]["close"] / rows[z + 1]["open"] - 1, rows[z]["date"], rows[z + 40]["date"]
        out.append(res)
    return out


def live(as_of: str, days: int, json_output: Path | None = None) -> list[dict]:
    """Stocks whose latest underwater golden cross (variant G) is within the last `days` bars.

    Uses the daily job's K-line cache and quote snapshot (no extra downloads) and the same
    core market-cap pool as the v2.6 / v1.3 screens. Observation only."""
    from bottom_base_context import fundamental_trend
    from screen_latest_30 import fetch_financials
    from screen_red_zone_5 import MARKET_POOL_DEFAULTS, fetch_daily_quotes
    from screen_v26_unified import prepare_rows
    names = fetch_financials(include_financial_yellow=True)
    quotes = fetch_daily_quotes(names.keys())
    limits = MARKET_POOL_DEFAULTS["core"]

    def one(code: str) -> dict | None:
        try:
            rows, _, volume_scale = prepare_rows(code, quotes)
        except Exception:
            return None
        rows = [r for r in rows if r["date"] <= as_of]
        if len(rows) < 200 or rows[-1]["date"] != as_of or any(r["low"] <= 0 or r["close"] <= 0 for r in rows):
            return None
        q = quotes.get(code) or {}
        if (float(q.get("totalMarketCap") or 0) < limits["minTotalMarketCap"]
                or float(q.get("floatMarketCap") or 0) < limits["minFloatMarketCap"]
                or sum(r["close"] * r["volume"] * volume_scale for r in rows[-20:]) / 20 < limits["minAverageAmount20"]):
            return None
        dif, dea, crosses = macd_lines(rows)
        recent = [k for k in crosses if k >= len(rows) - days]
        if not recent or not classify(rows, recent[-1], dif, crosses)["G"]:
            return None
        k, i = recent[-1], len(rows) - 1
        lo_k = min(range(k - 59, k + 1), key=lambda j: rows[j]["low"])
        hi_k = max(range(k - 119, lo_k + 1), key=lambda j: rows[j]["high"])
        return {
            "code": code, "name": str(names[code].get("SECURITY_NAME_ABBR") or code),
            "industry": str(names[code].get("BOARD_NAME") or "待分类"), "state": "低位水下金叉",
            "totalMarketCap": float(q.get("totalMarketCap") or 0), "crossDate": rows[k]["date"],
            "close": rows[i]["close"], "lowDate": rows[lo_k]["date"], "low": rows[lo_k]["low"],
            "drop": rows[lo_k]["low"] / rows[hi_k]["high"] - 1, "riseLow": rows[i]["close"] / rows[lo_k]["low"] - 1,
            "difPct": dif[i] / rows[i]["close"], "deadCrossSince": dif[i] < dea[i],
        }

    hits, scanned = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for hit in executor.map(one, sorted(names)):
            scanned += 1
            if hit:
                hits.append(hit)
    for h in hits:
        try:
            h["fundamentalTrend"] = fundamental_trend(h["code"], as_of)
        except Exception as exc:
            h["fundamentalTrend"] = {"tier": 2, "error": str(exc)}
    hits.sort(key=lambda h: (h["fundamentalTrend"].get("tier", 2), -h["totalMarketCap"]))
    if json_output:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps({"dataThrough": as_of, "method": "underwater-cross-G", "days": days,
                                           "selected": hits}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"as of {as_of}: scanned {scanned}, G crosses in last {days} bars: {len(hits)}")
    for h in hits:
        print(f"{h['code']} {h['name']}｜{h['industry']}｜金叉{h['crossDate']}｜收{h['close']:.2f}｜距低+{h['riseLow']:.1%}"
              f"｜基本面层级{h['fundamentalTrend'].get('tier')}{'｜已再次死叉' if h['deadCrossSince'] else ''}")
    return hits


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "live":
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("cmd")
        parser.add_argument("--as-of", required=True)
        parser.add_argument("--days", type=int, default=3)
        parser.add_argument("--json-output", type=Path)
        args = parser.parse_args()
        live(args.as_of, args.days, args.json_output)
        return
    index = benchmark_index()
    events: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            events.extend(result)
    events = [e for e in events if e["date"] >= "2023-07-01" and not e["date"].startswith("2024-09") and e["date"] in index]
    for e in events:
        for h in HORIZONS:
            if f"r{h}" in e and e[f"d{h}"] in index:
                e[f"x{h}"] = e[f"r{h}"] - (index[e[f"d{h}"]] / index[e["date"]] - 1)
    month_mean = {h: {} for h in HORIZONS}
    for h in HORIZONS:
        by_month = defaultdict(list)
        for e in events:
            if f"x{h}" in e:
                by_month[e["date"][:7]].append(e[f"x{h}"])
        month_mean[h] = {m: st.mean(v) for m, v in by_month.items()}

    lines = ["# 水下金叉事件研究（比例复权数据，信号 2023-07 起，剔除 2024-09，20日均额≥5000万）", "",
             "超额 = 相对中证1000；同月差 = 该组 − 同月全部水下金叉（A），抵消大盘影响。", ""]
    for h in HORIZONS:
        lines += [f"## 持有 {h} 日", "", "| 组 | " + " | ".join(p[0] for p in PERIODS) + " |", "| --- |" + " --- |" * len(PERIODS)]
        for v in VARIANTS:
            cells = []
            for _, lo, hi in PERIODS:
                xs = [e for e in events if v in e["tags"] and lo <= e["date"] < hi and f"x{h}" in e]
                if not xs:
                    cells.append("n/a")
                    continue
                ex = [e[f"x{h}"] for e in xs]
                dm = st.mean(e[f"x{h}"] - month_mean[h][e["date"][:7]] for e in xs)
                launch = sum(e[f"up{h}"] >= 0.15 for e in xs) / len(xs)
                cells.append(f"{st.mean(ex):+.1%}（同月差{dm:+.1%}，跑赢{sum(x > 0 for x in ex) / len(ex):.0%}，涨≥15% {launch:.0%}，n{len(xs)}）")
            lines.append(f"| {LABELS[v]} | " + " | ".join(cells) + " |")
        lines.append("")

    e_events = [e for e in events if "E" in e["tags"]]
    lines += ["## E 组：金叉时买 vs 等 DIF 上穿 0 轴再买", ""]
    reached = [e for e in e_events if e.get("zeroDays") is not None]
    lines.append(f"- E 组 {len(e_events)} 个；30 日内 DIF 上穿 0 轴的 {len(reached)} 个（{len(reached) / max(len(e_events), 1):.0%}），"
                 f"平均用时 {st.mean(e['zeroDays'] for e in reached):.1f} 日，到那时已从金叉买点涨了 {st.mean(e['zeroGainFromCross'] for e in reached if 'zeroGainFromCross' in e):+.1%}。")
    for _, lo, hi in PERIODS:
        both = [e for e in reached if lo <= e["date"] < hi and "x40" in e and "rZero40" in e and e["dZero40"] in index and e["dZero"] in index]
        miss = [e for e in e_events if lo <= e["date"] < hi and e.get("zeroDays") is None and "x40" in e]
        if both:
            at_cross = st.mean(e["x40"] for e in both)
            at_zero = st.mean(e["rZero40"] - (index[e["dZero40"]] / index[e["dZero"]] - 1) for e in both)
            lines.append(f"- {lo}起：上穿0轴的样本，金叉买40日超额 {at_cross:+.1%}，等0轴买40日超额 {at_zero:+.1%}（n{len(both)}）；"
                         f"没等到上穿0轴的 {len(miss)} 个，金叉买40日超额 {st.mean(e['x40'] for e in miss) if miss else 0:+.1%}")
    out = ROOT / "output" / "backtest" / "underwater-cross.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
