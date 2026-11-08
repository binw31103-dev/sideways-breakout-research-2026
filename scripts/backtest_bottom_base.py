#!/usr/bin/env python3
"""Replay the v2.6 technical trigger on cached daily K-lines and compare variants.

Two stages:
1. `scan` (cached to output/backtest/signals.json): every day on which a stock
   passes the v2.6 price-structure checks, with the exact v2.6 checks, extra
   features and forward outcomes. Only data up to the signal day is used.
2. `report`: each variant is a filter over those signals. Entry is the next
   day's open; benchmark is CSI 1000. Results include monthly stability,
   because a few market-wide months dominate the averages.

Financial and announcement switches are not replayed. Research only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from bottom_base_context import STOP_BUFFER, bottom_base_profile  # noqa: E402
from screen_v26_unified import calculate_metrics, strict_condition_checks  # noqa: E402

PRICE_CHECKS = (
    "lowAge_5_35", "riseRecentLow_lte_8", "riseYearLow_lte_15", "change5_lte_5", "change10_lte_6",
    "change20_lte_7", "maxDailyGain10_lt_7_5", "ma_not_overheated", "structure",
)
GAP = 20  # one signal per stock per 20 trading days, per variant
EXITS = {"hold10": (10, False), "stop40": (40, True), "stop60": (60, True)}
# Signals are scanned up to the second-to-last bar (entry is the next open). Trades whose
# holding window has not finished and that have not been stopped are marked incomplete and
# reported separately, valued at the latest close.
LONG_KLINE_DIR = ROOT / "output" / "cache" / "kline_long"


def cheap_prefilter(rows: list[dict], i: int) -> bool:
    """Superset of the v2.6 price conditions, so exact metrics run on few days."""
    close = rows[i]["close"]
    if close / min(r["low"] for r in rows[i - 59:i + 1]) > 1.08:
        return False
    if close / min(r["low"] for r in rows[i - 249:i + 1]) > 1.15:
        return False
    if close / rows[i - 20]["close"] > 1.07 or close / rows[i - 5]["close"] > 1.05:
        return False
    return max(r["high"] for r in rows[i - 19:i + 1]) / min(r["low"] for r in rows[i - 19:i + 1]) <= 1.28


def outcome(rows: list[dict], i: int, days: int, use_stop: bool) -> dict:
    """Same stop as the live tracking: close below the pre-signal 60-day low x STOP_BUFFER."""
    entry = rows[i + 1]["open"]
    stop = min(r["low"] for r in rows[i - 59:i + 1]) * STOP_BUFFER
    last = min(i + days, len(rows) - 1)
    exit_index = last
    if use_stop:
        exit_index = next((k for k in range(i + 1, last + 1) if rows[k]["close"] < stop), last)
    stopped = use_stop and rows[exit_index]["close"] < stop
    return {
        "exitDate": rows[exit_index]["date"],
        "ret": rows[exit_index]["close"] / entry - 1,
        "maxUp": max(r["close"] for r in rows[i + 1:exit_index + 1]) / entry - 1,
        "stopped": stopped,
        "complete": stopped or i + days <= len(rows) - 1,
    }


def scan_stock(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    # Forward adjustment can push old prices to zero or below; such series are unusable.
    if any(r["low"] <= 0 or r["close"] <= 0 for r in rows):
        return []
    out = []
    for i in range(260, len(rows) - 1):
        if not cheap_prefilter(rows, i):
            continue
        metrics = calculate_metrics(rows[:i + 1])
        if metrics is None:
            continue
        checks = strict_condition_checks(metrics)
        if not all(checks[k] for k in PRICE_CHECKS):
            continue
        closes = [r["close"] for r in rows[i - 79:i + 1]]
        ma60 = statistics.mean(closes[-60:])
        ma60_prev = statistics.mean(closes[-70:-10])
        out.append({
            "code": path.stem,
            "i": i,
            "date": rows[i]["date"],
            "checks": checks,
            "f": {
                **bottom_base_profile(rows[:i + 1]),
                "ret60": rows[i]["close"] / rows[i - 60]["close"] - 1,
                "closeVsMa60": rows[i]["close"] / ma60 - 1,
                "ma60Slope10": ma60 / ma60_prev - 1,
                "dailyRsi": metrics["dailyRsi"],
                "weeklyRsi": metrics["weeklyRsi"],
                "volumeRatio": metrics["volumeRatio5v20"],
                "range20": metrics["range20"],
                "lowAge": metrics["lowAge"],
                "platformCount": metrics["platformEvidenceCount"],
                "dailyMacd": metrics["dailyMacd"]["state"],
                "price": rows[i]["close"],
            },
            "out": {name: outcome(rows, i, days, stop) for name, (days, stop) in EXITS.items()},
        })
    return out


def _curl_json(url: str) -> dict | None:
    raw = subprocess.run(["curl", "-sS", "-m", "30", "-H", "User-Agent: Mozilla/5.0", url],
                         capture_output=True, text=True).stdout
    try:
        return json.loads(raw)
    except ValueError:
        return None


def benchmark_index() -> dict[str, float]:
    """CSI 1000 daily close from Eastmoney and Sina; each fills the other's gaps.

    Index levels are not adjusted, so the two sources should agree to the cent; a large
    disagreement means one feed is broken and the run stops rather than guess."""
    em = _curl_json("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000852"
                    "&fields1=f1&fields2=f51,f53&klt=101&fqt=0&beg=20220101&end=20500101") or {}
    sina = _curl_json("https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData?"
                      "symbol=sh000852&scale=240&ma=no&datalen=1023") or {}
    a = {k.split(",")[0]: float(k.split(",")[1]) for k in ((em.get("data") or {}).get("klines") or [])}
    b = {r["day"]: float(r["close"]) for r in ((sina.get("result") or {}).get("data") or [])}
    if not a and not b:
        raise RuntimeError("CSI 1000 unavailable from Eastmoney and Sina")
    bad = [d for d in set(a) & set(b) if abs(a[d] / b[d] - 1) > 0.001]
    if len(bad) > 3:
        raise RuntimeError(f"CSI 1000 sources disagree on {len(bad)} days, e.g. {sorted(bad)[:3]}")
    return {**b, **a}


def annotate_market(signals: list[dict], index: dict[str, float]) -> None:
    dates = sorted(index)
    pos = {d: k for k, d in enumerate(dates)}
    for s in signals:
        k = pos.get(s["date"])
        if k is None or k < 60:
            s["m"] = None
            continue
        close = index[s["date"]]
        s["m"] = {
            "aboveMa60": close > statistics.mean(index[d] for d in dates[k - 59:k + 1]),
            "aboveMa20": close > statistics.mean(index[d] for d in dates[k - 19:k + 1]),
            "change20": close / index[dates[k - 20]] - 1,
            "ret60": close / index[dates[k - 60]] - 1,
            # Live switch (bottom_base_context.REGIME_PRIMARY): CSI 1000 vs MA120.
            "belowMa120": k >= 119 and close < statistics.mean(index[d] for d in dates[k - 119:k + 1]),
        }
        if "ret60" in s.get("f", {}):
            s["f"]["rs60"] = s["f"]["ret60"] - s["m"]["ret60"]


def v26(s: dict) -> bool:
    return all(s["checks"].values())


def elastic(s: dict) -> bool:
    return s["f"]["elastic"]


def market_weak(s: dict) -> bool:
    return not s["m"]["aboveMa60"]


VARIANTS: dict[str, tuple[Callable[[dict], bool], str]] = {
    "A v2.6 · 持有10日": (v26, "hold10"),
    "A1 v2.6 · 40日+止损": (v26, "stop40"),
    "A2 v2.6 · 60日+止损": (v26, "stop60"),
    "B v2.6+弹性": (lambda s: v26(s) and elastic(s), "stop40"),
    "M v2.6+大盘在MA60下": (lambda s: v26(s) and market_weak(s), "stop40"),
    "MB v2.6+弹性+大盘在MA60下": (lambda s: v26(s) and elastic(s) and market_weak(s), "stop40"),
    "R v2.6+60日强于中证1000": (lambda s: v26(s) and s["f"]["rs60"] > 0, "stop40"),
    "U v2.6+上涨日量>=下跌日量": (lambda s: v26(s) and s["f"]["upDownVol20"] >= 1.0, "stop40"),
    "P 仅价格结构": (lambda s: True, "stop40"),
    "PB 仅价格结构+弹性": (elastic, "stop40"),
}


PERIODS = (
    ("2023-07 ~ 2024-12", lambda d: d < "2025-01"),
    ("2025-01 ~ 2026-06", lambda d: "2025-01" <= d < "2026-07"),
    ("2026-07 起（最新）", lambda d: d >= "2026-07"),
)


def select(signals: list[dict], predicate: Callable[[dict], bool], exit_name: str,
           index: dict[str, float]) -> list[dict]:
    trades = []
    last: dict[str, int] = {}
    for s in signals:
        if s["m"] is None or not predicate(s):
            continue
        if s["i"] - last.get(s["code"], -GAP) < GAP:
            continue
        o = s["out"][exit_name]
        if o["exitDate"] not in index:
            continue
        last[s["code"]] = s["i"]
        bench = index[o["exitDate"]] / index[s["date"]] - 1
        trades.append({**o, "excess": o["ret"] - bench, "date": s["date"], "code": s["code"]})
    return trades


def stats(all_trades: list[dict]) -> dict[str, Any]:
    trades = [t for t in all_trades if t.get("complete", True)]
    pending = [t for t in all_trades if not t.get("complete", True)]
    open_part = {"open": len(pending), "openExcess": statistics.mean(t["excess"] for t in pending) if pending else None}
    if not trades:
        return {"n": 0, **open_part}
    rets = [t["ret"] for t in trades]
    by_month: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_month[t["date"][:7]].append(t["excess"])
    month_means = {m: statistics.mean(v) for m, v in by_month.items()}
    total = sum(t["excess"] for t in trades)
    top3 = sorted(by_month, key=lambda m: -sum(by_month[m]))[:3]
    without_top3 = [t["excess"] for t in trades if t["date"][:7] not in top3]
    return {
        "n": len(trades),
        "mean": statistics.mean(rets),
        "median": statistics.median(rets),
        "win": sum(r > 0 for r in rets) / len(rets),
        "excess": total / len(trades),
        "launch": sum(t["maxUp"] >= 0.15 for t in trades) / len(trades),
        "stopped": sum(t["stopped"] for t in trades) / len(trades),
        "months": len(month_means),
        "posMonths": sum(v > 0 for v in month_means.values()) / len(month_means),
        "medianMonth": statistics.median(month_means.values()),
        "exTop3": statistics.mean(without_top3) if without_top3 else 0.0,
        **open_part,
    }


HEADER = [
    "| 方案 | 信号 | 平均 | 中位 | 盈利 | 超额 | 涨≥15% | 止损 | 月数 | 超额为正月 | 月超额中位 | 去掉最好3个月的超额 | 未完结（浮动超额） |",
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
]


def row(name: str, st: dict[str, Any]) -> str:
    pending = f"{st.get('open', 0)}" + (f"（{st['openExcess']:+.1%}）" if st.get("openExcess") is not None else "")
    if not st["n"]:
        return f"| {name} | 0 | | | | | | | | | | | {pending} |"
    return (
        f"| {name} | {st['n']} | {st['mean']:+.1%} | {st['median']:+.1%} | {st['win']:.0%} | {st['excess']:+.1%} | "
        f"{st['launch']:.0%} | {st['stopped']:.0%} | {st['months']} | {st['posMonths']:.0%} | "
        f"{st['medianMonth']:+.1%} | {st['exTop3']:+.1%} | {pending} |"
    )


def load_signals(args: argparse.Namespace) -> list[dict]:
    if args.signals.exists() and not args.rescan:
        return json.loads(args.signals.read_text(encoding="utf-8"))
    kline_dir = args.kline_dir or (LONG_KLINE_DIR if LONG_KLINE_DIR.exists() else max((ROOT / "output" / "cache" / "kline").iterdir()))
    paths = sorted(kline_dir.glob("*.json"))
    signals: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
        for result in executor.map(scan_stock, paths, chunksize=20):
            signals.extend(result)
    signals.sort(key=lambda s: (s["code"], s["i"]))
    args.signals.parent.mkdir(parents=True, exist_ok=True)
    args.signals.write_text(json.dumps(signals, ensure_ascii=False), encoding="utf-8")
    return signals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kline-dir", type=Path, help="Directory of cached {code}.json K-lines")
    parser.add_argument("--signals", type=Path, default=ROOT / "output" / "backtest" / "signals.json")
    parser.add_argument("--rescan", action="store_true", help="Rebuild the signal cache from K-lines")
    parser.add_argument("--start", default="2023-07-01")
    parser.add_argument("--exclude-month", action="append", default=["2024-09"],
                        help="Signal months to drop (default: the 2024-09 market-wide rally)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "backtest" / "bottom-base-variants.md")
    args = parser.parse_args()

    index = benchmark_index()
    signals = [
        s for s in load_signals(args)
        if s["date"] >= args.start and s["date"][:7] not in args.exclude_month
    ]
    annotate_market(signals, index)

    lines = [
        f"# 底部横盘方案回测（信号 {args.start} 起，剔除 {', '.join(args.exclude_month)}，基准中证1000）", "",
        "入场：信号次日开盘。`持有10日`=第10日收盘离场；`40/60日+止损`=收盘跌破信号前60日低点5%离场，否则到期收盘离场。",
        "同股20个交易日内只取首个信号。\"去掉最好3个月的超额\"用来检查结果是否只靠少数行情月份。",
        "持有期未走完且未止损的信号不计入统计，单列为“未完结”，按最新收盘价计算浮动超额。", "",
        "## 总体", "", *HEADER,
    ]
    results = {name: select(signals, pred, exit_name, index) for name, (pred, exit_name) in VARIANTS.items()}
    lines += [row(name, stats(trades)) for name, trades in results.items()]
    for period, test in PERIODS:
        lines += ["", f"## {period}", "", *HEADER]
        lines += [row(name, stats([t for t in trades if test(t["date"])])) for name, trades in results.items()]
    lines += ["", "局限：仅含当前在市股票（无退市股，偏乐观）；未回放财务、公告与行业观察；收益未扣交易成本；信号在时间上高度相关。"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
