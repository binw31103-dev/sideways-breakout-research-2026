#!/usr/bin/env python3
"""Paired signal-close/next-open outcomes; no tuning or historical risk-gate reconstruction."""
from __future__ import annotations

import argparse
from collections import defaultdict, Counter
import datetime as dt
import hashlib
from functools import lru_cache
import json
from pathlib import Path
import statistics as st
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/research")]
from kline_units import load_payload
from backtest_bottom_base import _curl_json, v26
from underwater_cross import macd_lines, classify

MODELS = {"v13": ("平台初破", 20, False), "v26": ("底部横盘", 40, True),
          "ucross": ("水下金叉", 40, False), "zaxis": ("零轴金叉", 20, False)}
PERIODS = [("全部", "2023-07-01", "9999"), ("前段", "2023-07-01", "2025-01-01"),
           ("后段", "2025-01-01", "2026-07-01"), ("新段", "2026-07-01", "9999")]


def paired_outcome(rows: list[dict], signal: int, days: int, use_stop: bool, index: dict) -> dict | None:
    if signal < 59 or signal + 1 >= len(rows):
        return None
    stop = min(r["low"] for r in rows[signal - 59:signal + 1]) * 0.95
    end = min(signal + days, len(rows) - 1)
    stopped = False
    if use_stop:
        for k in range(signal + 1, end + 1):
            if rows[k]["close"] < stop:
                end, stopped = k, True
                break
    if not stopped and end < signal + days:
        return None  # never mix unfinished observations with matured outcomes
    start, next_bar, final = rows[signal], rows[signal + 1], rows[end]
    if min(start["close"], next_bar["open"], final["close"]) <= 0:
        return None
    gap = next_bar["open"] / start["close"] - 1
    close_ret = final["close"] / start["close"] - 1
    open_ret = final["close"] / next_bar["open"] - 1
    result = {"date": start["date"], "entryDate": next_bar["date"], "exitDate": final["date"],
              "gap": gap, "signalCloseReturn": close_ret, "nextOpenReturn": open_ret,
              "pairedDifference": open_ret - close_ret, "stopped": stopped,
              "conversionError": abs(open_ret - ((1 + close_ret) / (1 + gap) - 1))}
    if all(date in index for date in [start["date"], next_bar["date"], final["date"]]):
        base, entry, last = index[start["date"]], index[next_bar["date"]], index[final["date"]]
        if base["close"] > 0 and entry["open"] > 0:
            close_bench = last["close"] / base["close"] - 1
            open_bench = last["close"] / entry["open"] - 1
            result.update(signalCloseBenchmark=close_bench, nextOpenBenchmark=open_bench,
                          signalCloseExcess=close_ret - close_bench,
                          nextOpenExcess=open_ret - open_bench,
                          nextOpenExcessLegacyBenchmark=open_ret - close_bench)
    return result


def fetch_benchmark(cache: Path) -> dict:
    if cache.exists():
        return json.loads(cache.read_text())["rows"]
    em = _curl_json("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000852"
                   "&fields1=f1&fields2=f51,f52,f53&klt=101&fqt=0&beg=20220101&end=20500101") or {}
    sina = _curl_json("https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData?"
                     "symbol=sh000852&scale=240&ma=no&datalen=1023") or {}
    a = {k.split(",")[0]: {"open": float(k.split(",")[1]), "close": float(k.split(",")[2])}
         for k in ((em.get("data") or {}).get("klines") or [])}
    b = {r["day"]: {"open": float(r["open"]), "close": float(r["close"])}
         for r in ((sina.get("result") or {}).get("data") or [])}
    if not a and not b:
        raise RuntimeError("CSI1000 daily open and close unavailable")
    bad = [d for d in set(a) & set(b) if any(abs(a[d][field] / b[d][field] - 1) > 0.001 for field in ["open", "close"])]
    if len(bad) > 3:
        raise RuntimeError(f"index sources disagree: {bad[:3]}")
    payload = {"retrievedAt": dt.datetime.now().astimezone().isoformat(),
               "sourceCounts": {"eastmoney": len(a), "sina": len(b)}, "rows": {**b, **a}}
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("x") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return payload["rows"]


def load_signal_pairs() -> dict:
    """Reuse existing technical event caches; these are not a new point-in-time universe."""
    directory = ROOT / "output/backtest"
    v13_events = json.loads((directory / "v13_signals.json").read_text())
    v26_events = json.loads((directory / "signals.json").read_text())
    pairs = {"v13": {(s["code"], s["date"]) for s in v13_events},
             "v26": {(s["code"], s["date"]) for s in v26_events if v26(s)}}
    return pairs


def sensitivity(values: list[float], fraction: float) -> dict:
    """Ex-post best/worst deletion bounds, NOT a causal estimate of the actual risk gate."""
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    remove = int(len(values) * fraction)
    low = ordered[:len(ordered) - remove]
    high = ordered[remove:]
    return {"n": len(values), "excluded": remove, "excludeFraction": fraction,
            "removeBestMean": st.mean(low), "removeWorstMean": st.mean(high),
            "unfilteredMean": st.mean(values)}


def summarize(items: list[dict]) -> dict:
    fields = ["gap", "signalCloseReturn", "nextOpenReturn", "pairedDifference", "signalCloseExcess",
              "nextOpenExcess", "nextOpenExcessLegacyBenchmark"]
    result = {"n": len(items), "benchmarkN": sum("nextOpenExcess" in row for row in items)}
    for field in fields:
        values = [row[field] for row in items if field in row]
        result[field] = {"mean": st.mean(values), "median": st.median(values)} if values else None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--benchmark-json", type=Path, default=ROOT / "output/cache/csi1000-open-close.json")
    parser.add_argument("--historical", action="store_true", help="Also compare existing historical technical event samples")
    parser.add_argument("--live-cache-dir", type=Path, help="Normalized daily cache for actual selected episodes (default newest local daily cache)")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    index = fetch_benchmark(args.benchmark_json)
    skipped = Counter()
    samples = []
    @lru_cache(maxsize=64)
    def history(code):
        path = ROOT / "output/cache/kline_long" / f"{code}.json"
        if not path.exists():
            skipped["missingKline"] += 1
            return []
        return [r for r in load_payload(path)["rows"] if r["date"] <= args.as_of]
    ledger_path = ROOT / "output/daily/tracking.json"
    ledger = json.loads(ledger_path.read_text())
    live_dirs = sorted(p for p in (ROOT / "output/cache/kline").iterdir() if p.is_dir())
    live_cache = args.live_cache_dir or (live_dirs[-1] if live_dirs else None)
    for record in ledger["records"].values():
        model = record["model"]
        if model not in MODELS:
            continue
        live_path = live_cache / f"{record['code']}.json" if live_cache else None
        bars = ([r for r in load_payload(live_path)["rows"] if r["date"] <= args.as_of]
                if live_path and live_path.exists() else history(record["code"]))
        positions = {r["date"]: i for i, r in enumerate(bars)}
        k = positions.get(record["firstSelectedDate"])
        if k is None:
            skipped["liveSignalNotInKline"] += 1
            continue
        outcome = paired_outcome(bars, k, MODELS[model][1], MODELS[model][2], index)
        if outcome:
            samples.append({**outcome, "model": model, "code": record["code"], "cohort": "actual_selected_episodes"})
        else:
            skipped["liveUnfinished"] += 1
    if args.historical:
        pairs = load_signal_pairs()
        pairs.update(ucross=set(), zaxis=set())
        for path in sorted((ROOT / "output/cache/kline_long").glob("*.json")):
            bars = history(path.stem)
            if len(bars) < 260 or any(r["close"] <= 0 or r["low"] <= 0 for r in bars):
                continue
            dif, dea, crosses = macd_lines(bars)
            for k in crosses:
                if k < 259:
                    continue
                date = bars[k]["date"]
                if date < "2023-07-01" or date > args.as_of:
                    continue
                if sum(r["close"] * r["volume"] for r in bars[k-19:k+1]) / 20 < 5e7:
                    continue
                if classify(bars, k, dif, crosses)["G"]:
                    pairs["ucross"].add((path.stem, date))
                price = bars[k]["close"]
                window = bars[k-19:k+1]
                vol20 = sum(r["volume"] for r in bars[k-20:k]) / 20
                if (abs(dif[k]) / price <= 0.01
                        and price / min(r["low"] for r in bars[k-59:k+1]) <= 1.08
                        and max(r["high"] for r in window) / min(r["low"] for r in window) <= 1.12
                        and price / bars[k-5]["close"] <= 1.03
                        and vol20 > 0 and bars[k]["volume"] / vol20 <= 1.2):
                    pairs["zaxis"].add((path.stem, date))
        for model, events in pairs.items():
            previous = {}
            for code, date in sorted(events):
                bars = history(code)
                positions = {r["date"]: i for i, r in enumerate(bars)}
                k = positions.get(date)
                # Preserve the existing daily-portfolio dedup for v13/v26. Underwater and
                # zero-axis papers are signal-event studies, so every qualifying event stays.
                gap = 20 if model in {"v13", "v26"} else 0
                if date < "2023-07-01" or k is None or k - previous.get(code, -gap) < gap:
                    continue
                previous[code] = k
                outcome = paired_outcome(bars, k, MODELS[model][1], MODELS[model][2], index)
                if outcome:
                    samples.append({**outcome, "model": model, "code": code, "cohort": "historical_technical_only"})
    summaries, bounds = [], []
    for cohort in sorted({r["cohort"] for r in samples}):
        for model in MODELS:
            for exclude_september in [False, True]:
                for label, low, high in PERIODS:
                    group = [r for r in samples if r["cohort"] == cohort and r["model"] == model
                             and low <= r["date"] < high and (not exclude_september or not r["date"].startswith("2024-09"))]
                    summaries.append({"cohort": cohort, "model": model, "period": label,
                                      "exclude202409": exclude_september, **summarize(group)})
                    if cohort == "historical_technical_only" and label == "全部" and not exclude_september:
                        values = [r["nextOpenExcess"] for r in group if "nextOpenExcess" in r]
                        bounds.extend({"model": model, **sensitivity(values, f)} for f in [0.0, 0.05, 0.10, 0.20])
    result = {"asOf": args.as_of, "ledgerSha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
              "benchmarkSha256": hashlib.sha256(args.benchmark_json.read_bytes()).hexdigest(),
              "scope": "paired entry-basis conversion; technical-only history has no point-in-time financial/announcement/cap replay",
              "benchmark": "close->close for signal-close; next-date index open->exit close for next-open; legacy excess also retained",
              "costs": "not deducted", "liveCache": str(live_cache), "skipped": dict(skipped),
              "maxConversionError": max((r["conversionError"] for r in samples), default=0),
              "summaries": summaries, "riskGateDeletionBounds": bounds, "samples": samples}
    with (args.output_dir / "entry-basis.json").open("x") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    lines = ["# 收盘观察与次日开盘：入场基准配对核验", "", f"数据截至 {args.as_of}。本次不调整任何筛选、排序或组合条件。", "",
             "同一股票、同一信号日、同一退出日配对。平台突破/底部横盘沿用既有日组合20日信号间隔；水下金叉/零轴金叉保留逐事件研究样本，不新增间隔过滤。底部横盘沿用40日/参考低点×0.95，其余沿用20或40日无破位。",
             "信号收盘是观察起点，17:05生成的信号无法按当日收盘成交。次日开盘收益也未扣费用、滑点与成交限制。", "",
             "绝对收益换算：(1+收盘收益)/(1+次日跳空)-1。应逐笔换算，不能直接减平均跳空。",
             "收盘超额使用指数信号日收盘至退出收盘；开盘超额使用指数下一交易日开盘至退出收盘。", "",
             "历史样本没有回放历史财务/公告、市值和退市股票，且沿用已研究过的信号缓存；以下不是生产策略完整回测，也不是新样本外证据。", "",
             "| 样本 | 策略 | 时段 | 2024-09 | 笔数 | 指数覆盖笔数 | 收盘起算 | 次开起算 | 配对差 | 收盘超额 | 对齐后的次开超额 | 旧基准次开超额 |",
             "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    def percent(cell):
        return f"{cell['mean']:+.2%}" if cell else "—"
    for row in summaries:
        if row["n"] == 0:
            continue
        fields = ["signalCloseReturn", "nextOpenReturn", "pairedDifference", "signalCloseExcess", "nextOpenExcess", "nextOpenExcessLegacyBenchmark"]
        lines.append(f"| {'真实入选批次' if row['cohort']=='actual_selected_episodes' else '历史技术样本'} | {MODELS[row['model']][0]} | {row['period']} | {'排除' if row['exclude202409'] else '包含'} | {row['n']} | {row['benchmarkN']} | " + " | ".join(percent(row[x]) for x in fields) + " |")
    lines += ["", "## 历史风险门槛敏感性", "",
              "无历史风险快照，不能估计门槛实际带来的收益。下表假设门槛剔除5%/10%/20%样本，并分别剔除事后最好或最差的结果，给出选择偏差可能区间。这不是可实现筛选，也不是收益置信区间。", "",
              "| 策略 | 假定剔除比例 | 原均值 | 剔除最好后的均值 | 剔除最差后的均值 |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for row in bounds:
        if row["n"]:
            lines.append(f"| {MODELS[row['model']][0]} | {row['excludeFraction']:.0%} | {row['unfilteredMean']:+.2%} | {row['removeBestMean']:+.2%} | {row['removeWorstMean']:+.2%} |")
    lines += ["", f"逐笔换算最大误差：{result['maxConversionError']:.3g}；完整逐笔、样本数、指数覆盖和排除原因见同目录 JSON。", "",
              "原回测报告未覆盖或重写。财务门槛、入场基准、基准收益必须一起标注，不能将旧超额数字直接视作生产表现。"]
    (args.output_dir / "entry-basis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"samples": len(samples), "maxConversionError": result["maxConversionError"], "skipped": dict(skipped), "output": str(args.output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
