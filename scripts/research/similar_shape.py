#!/usr/bin/env python3
"""Find stocks shaped like 国电南瑞 600406 in Oct 2026, and check how that shape did before.

Template (600406 at 2026-10-23): fell 22% from a 120-day high to a low 21 days ago, then
climbed steadily (+7% in 20 days, largest daily gain 2.4%, 14 of 20 days up), MA5 > MA10
> MA20 > MA30 with MA30 flattening, daily MACD DIF crossed zero within the last 15 days,
volume 5/20 1.3. The rule below widens each number to a band; it was fixed before
scanning and is not tuned on results.

Usage:
  python3 scripts/research/similar_shape.py scan --as-of 2026-10-26   # fresh K-lines, list matches
  python3 scripts/research/similar_shape.py history                   # base rate on kline_long
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from screen_red_zone_5 import macd_metrics  # noqa: E402

TEMPLATE = {"drop": -0.223, "riseLow": 0.082, "lowAge": 21, "gain20": 0.074, "maxDay20": 0.024,
            "upDays20": 14, "ma30Slope": -0.0008, "vol5v20": 1.31}
SCALE = {"drop": 0.08, "riseLow": 0.04, "lowAge": 10, "gain20": 0.04, "maxDay20": 0.015,
         "upDays20": 3, "ma30Slope": 0.01, "vol5v20": 0.35}


def ma(c: list[float], n: int, end: int) -> float:
    return sum(c[end - n + 1:end + 1]) / n


def shape(rows: list[dict], i: int) -> dict | None:
    """Features at bar i, or None if the bar does not match the rule."""
    if i < 140:
        return None
    c = [r["close"] for r in rows[: i + 1]]
    hi_k = max(range(i - 119, i + 1), key=lambda k: rows[k]["high"])
    lo_k = min(range(i - 59, i + 1), key=lambda k: rows[k]["low"])
    if lo_k <= hi_k:
        return None
    high, low = rows[hi_k]["high"], rows[lo_k]["low"]
    f = {
        "drop": low / high - 1,
        "riseLow": c[i] / low - 1,
        "fromHigh": c[i] / high - 1,
        "lowAge": i - lo_k,
        "gain20": c[i] / c[i - 20] - 1,
    }
    gains = [c[k] / c[k - 1] - 1 for k in range(i - 19, i + 1)]
    f["maxDay20"], f["upDays20"] = max(gains), sum(g > 0 for g in gains)
    m5, m10, m20, m30 = (ma(c, n, i) for n in (5, 10, 20, 30))
    f["ma30Slope"] = m30 / ma(c, 30, i - 10) - 1
    vols = [r["volume"] for r in rows[: i + 1]]
    f["vol5v20"] = (sum(vols[-5:]) / 5) / max(sum(vols[-20:]) / 20, 1)
    rule = (
        -0.40 <= f["drop"] <= -0.12
        and 10 <= f["lowAge"] <= 40
        and 0.04 <= f["riseLow"] <= 0.15
        and f["fromHigh"] <= -0.08
        and m5 > m10 > m20 and c[i] > m30 and m20 >= m30 * 0.99 and f["ma30Slope"] >= -0.005
        and 0.03 <= f["gain20"] <= 0.12 and f["maxDay20"] < 0.05 and f["upDays20"] >= 11
        and 0.9 <= f["vol5v20"] <= 2.0
    )
    if not rule:
        return None
    dif_now = macd_metrics(c)["dif"]
    dif_past = [macd_metrics(c[: len(c) - back])["dif"] for back in (5, 10, 15)]
    if not (dif_now > 0 and min(dif_past) < 0):
        return None
    f["dif"] = dif_now
    f["distance"] = sum(abs(f[k] - TEMPLATE[k]) / SCALE[k] for k in TEMPLATE) / len(TEMPLATE)
    return f


def fetch_rows(code: str, as_of: str) -> tuple[str, list[dict]]:
    """kline_long (proportional, anchored at its last bar) plus newer unadjusted Sina bars.
    Appending raw bars is exact unless an ex-date falls after the dataset's last bar; such
    stocks are skipped rather than mixed."""
    from backtest_bottom_base import LONG_KLINE_DIR
    from screen_latest_30 import get_json_with_curl
    path = LONG_KLINE_DIR / f"{code}.json"
    if not path.exists():
        return code, []
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    last = rows[-1]["date"]
    if any(last < e["date"] <= as_of for e in DIVIDENDS.get(code, [])):
        return code, []
    symbol = ("sh" if code.startswith("6") else "sz") + code
    try:
        data = (get_json_with_curl("https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData?"
                                   f"symbol={symbol}&scale=240&ma=no&datalen=8").get("result") or {}).get("data") or []
    except Exception:
        return code, []
    by_date = {r["day"]: r for r in data}
    if last in by_date and abs(float(by_date[last]["close"]) / rows[-1]["close"] - 1) > 0.001:
        return code, []  # raw and dataset disagree on the overlap day: unmodelled event
    for d in sorted(by_date):
        if last < d <= as_of:
            r = by_date[d]
            rows.append({"date": d, "open": float(r["open"]), "close": float(r["close"]), "high": float(r["high"]),
                         "low": float(r["low"]), "volume": float(r["volume"])})
    return code, rows


DIVIDENDS: dict[str, list[dict]] = {}


def scan(as_of: str, workers: int) -> None:
    from screen_latest_30 import fetch_financials
    from screen_red_zone_5 import fetch_tencent_daily_quotes
    global DIVIDENDS
    DIVIDENDS = json.loads((ROOT / "output" / "cache" / "dividends.json").read_text(encoding="utf-8"))
    names = fetch_financials(include_financial_yellow=True)
    quotes = fetch_tencent_daily_quotes(sorted(names))
    matches, missing = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for n, (code, rows) in enumerate(executor.map(lambda c: fetch_rows(c, as_of), sorted(names)), 1):
            if n % 1000 == 0:
                print(f"{n} done, missing {missing}", flush=True)
            if not rows or rows[-1]["date"] != as_of:
                missing += 1
                continue
            f = shape(rows, len(rows) - 1)
            q = quotes.get(code) or {}
            cap = float(q.get("totalMarketCap") or 0)
            amount20 = sum(r["close"] * r["volume"] for r in rows[-20:]) / 20
            # Core pool as in the daily job (Sina volume is in shares).
            if f and cap >= 5e9 and amount20 >= 5e7:
                info = names[code]
                matches.append({"code": code, "name": info.get("SECURITY_NAME_ABBR"), "industry": info.get("BOARD_NAME"),
                                "close": rows[-1]["close"], "cap": cap, **f})
    matches.sort(key=lambda m: m["distance"])
    out = ROOT / "output" / "daily" / as_of / "similar-600406.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(matches, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"as of {as_of}: scanned {len(names) - missing}, missing {missing}, matches {len(matches)} -> {out}")
    for m in matches:
        print(f"{m['code']} {m['name']}｜{m['industry']}｜收{m['close']:.2f}｜市值{m['cap'] / 1e8:.0f}亿｜前跌{m['drop']:+.0%}｜"
              f"低点{m['lowAge']}日前｜距低+{m['riseLow']:.1%}｜距前高{m['fromHigh']:+.0%}｜20日{m['gain20']:+.1%}｜"
              f"最大单日{m['maxDay20']:.1%}｜阳{m['upDays20']}/20｜量比{m['vol5v20']:.2f}｜相似距离{m['distance']:.2f}")


def scan_history_file(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    if any(r["low"] <= 0 or r["close"] <= 0 or r["open"] <= 0 for r in rows):
        return []
    out, last = [], -99
    for i in range(140, len(rows) - 1):
        if i - last < 20:
            continue
        f = shape(rows, i)
        if f:
            last = i
            entry = rows[i + 1]["open"]
            res = {"code": path.stem, "date": rows[i]["date"]}
            for days in (10, 20, 40):
                k = i + days
                if k < len(rows):
                    res[f"r{days}"], res[f"d{days}"] = rows[k]["close"] / entry - 1, rows[k]["date"]
            out.append(res)
    return out


def history() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from backtest_bottom_base import LONG_KLINE_DIR, benchmark_index
    index = benchmark_index()
    hits: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(scan_history_file, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            hits.extend(result)
    periods = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"), ("2026-07起", "2026-07", "9999"))
    print(f"历史信号 {len(hits)} 个（同股20日内只取首个），相对中证1000超额：均值 / 中位 / 跑赢比例 / 样本")
    for days in (10, 20, 40):
        cells = []
        for name, lo, hi in periods:
            ex = [h[f"r{days}"] - (index[h[f"d{days}"]] / index[h["date"]] - 1) for h in hits
                  if lo <= h["date"] < hi and f"r{days}" in h and h["date"] in index and h[f"d{days}"] in index
                  and not h["date"].startswith("2024-09")]
            by_month = defaultdict(list)
            cells.append(f"{name} {st.mean(ex):+.1%} / {st.median(ex):+.1%} / {sum(e > 0 for e in ex) / len(ex):.0%} / {len(ex)}" if ex else f"{name} n/a")
        print(f"持有{days}日：" + "｜".join(cells))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--as-of", required=True)
    s.add_argument("--workers", type=int, default=3)
    sub.add_parser("history")
    args = parser.parse_args()
    if args.cmd == "scan":
        scan(args.as_of, args.workers)
    else:
        history()


if __name__ == "__main__":
    main()
