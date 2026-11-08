#!/usr/bin/env python3
"""Which index (and moving average) works best as the "weak market" switch.

Uses the cached v1.3 / v2.6 signals and point-in-time fundamentals from the
research runs in /tmp/sideways-analysis. Excess is always measured against
CSI 1000 so every regime definition is compared on the same yardstick.
"""

from __future__ import annotations

import glob
import json
import statistics as st
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from backtest_bottom_base import v26  # noqa: E402

DATA = Path("/tmp/sideways-analysis")
INDEXES = {"中证1000": "sh000852", "中证500": "sh000905", "沪深300": "sh000300", "上证指数": "sh000001",
           "创业板指": "sz399006", "国证2000": "sz399303", "中小100": "sz399101"}


def sina(symbol: str) -> dict[str, float]:
    url = ("https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData?"
           f"symbol={symbol}&scale=240&ma=no&datalen=1023")
    raw = subprocess.run(["curl", "-sS", "-m", "30", url], check=True, capture_output=True, text=True).stdout
    return {r["day"]: float(r["close"]) for r in json.loads(raw)["result"]["data"]}


def breadth_series() -> tuple[dict[str, float], dict[str, float]]:
    """Equal-weight market (cumulative median return) and share of stocks above their MA60."""
    rets, above, total = defaultdict(list), defaultdict(int), defaultdict(int)
    for f in glob.glob(str(DATA / "kline" / "*.json")):
        rows = json.load(open(f))["rows"]
        closes = [r["close"] for r in rows]
        for k in range(1, len(rows)):
            if closes[k - 1] > 0:
                rets[rows[k]["date"]].append(closes[k] / closes[k - 1] - 1)
            if k >= 60:
                total[rows[k]["date"]] += 1
                above[rows[k]["date"]] += closes[k] > sum(closes[k - 59:k + 1]) / 60
    level, ew = 1.0, {}
    for d in sorted(rets):
        level *= 1 + st.median(rets[d]); ew[d] = level
    return ew, {d: above[d] / total[d] for d in total if total[d] >= 500}


def regime_fn(series: dict[str, float], ma: int):
    dates = sorted(series); pos = {d: k for k, d in enumerate(dates)}

    def weak(date: str) -> bool | None:
        k = pos.get(date)
        if k is None or k < ma:
            return None
        return series[date] < sum(series[d] for d in dates[k - ma + 1:k + 1]) / ma
    return weak


def load_fundamentals():
    perf = defaultdict(dict)
    for r in json.load(open(DATA / "fund" / "perf.json")):
        if r.get("NOTICE_DATE"):
            q, n = r["REPORTDATE"][:10], r["NOTICE_DATE"][:10]
            if q not in perf[r["SECURITY_CODE"]] or n < perf[r["SECURITY_CODE"]][q][0]:
                perf[r["SECURITY_CODE"]][q] = (n, r.get("SJLTZ"))
    hold = defaultdict(list)
    for r in json.load(open(DATA / "fund" / "holders.json")):
        if r.get("HOLD_NOTICE_DATE"):
            hold[r["SECURITY_CODE"]].append((r["END_DATE"][:10], r["HOLD_NOTICE_DATE"][:10], r.get("HOLDER_NUM_RATIO")))
    num = lambda v: isinstance(v, (int, float))

    def tier0(code: str, date: str) -> bool:
        reps = sorted((q, y) for q, (n, y) in perf.get(code, {}).items() if n < date)
        hs = sorted((e, v) for e, n, v in hold.get(code, []) if n < date)
        y, yp = (reps[-1][1] if reps else None), (reps[-2][1] if len(reps) > 1 else None)
        h, hp = (hs[-1][1] if hs else None), (hs[-2][1] if len(hs) > 1 else None)
        inflection = num(y) and num(yp) and y > 0 >= yp
        improving = num(y) and num(yp) and y > yp
        return bool(inflection or (improving and num(h) and num(hp) and h < 0 and hp < 0))
    return tier0


def trades(signals: list[dict], exit_name: str, bench: dict[str, float]) -> list[dict]:
    out, last = [], {}
    for s in sorted(signals, key=lambda s: (s["code"], s["i"])):
        if s["i"] - last.get(s["code"], -20) < 20:
            continue
        o = s["out"][exit_name]
        if o["exitDate"] not in bench or s["date"] not in bench:
            continue
        last[s["code"]] = s["i"]
        out.append({"code": s["code"], "date": s["date"], "ret": o["ret"],
                    "ex": o["ret"] - (bench[o["exitDate"]] / bench[s["date"]] - 1)})
    return out


def robust(t: list[dict]) -> str:
    if len(t) < 30:
        return f"n={len(t)} 样本太少"
    by_month = defaultdict(list)
    for x in t:
        by_month[x["date"][:7]].append(x["ex"])
    top3 = sorted(by_month, key=lambda m: -sum(by_month[m]))[:3]
    ex3 = [x["ex"] for x in t if x["date"][:7] not in top3]
    halves = [[x["ex"] for x in t if (x["date"] < "2025-01") == early] for early in (True, False)]
    h = "/".join(f"{st.mean(v):+.1%}" if v else "n/a" for v in halves)
    ok = all(v and st.mean(v) > 0 for v in halves) and st.mean(ex3) > 0
    return (f"n={len(t):4d} 超额{st.mean(x['ex'] for x in t):+.2%} 盈利{sum(x['ret'] > 0 for x in t) / len(t):.0%} "
            f"去top3 {st.mean(ex3):+.2%} 前/后 {h} {'✓' if ok else ''}")


def main() -> None:
    csi1000 = sina("sh000852")
    series = {name: sina(code) for name, code in INDEXES.items()}
    ew, breadth = breadth_series()
    series["全市场等权"] = ew
    keep = lambda s: s["date"] >= "2023-07-01" and not s["date"].startswith("2024-09")
    v13 = [s for s in json.load(open(DATA / "v13_signals.json")) if keep(s)]
    v26s = [s for s in json.load(open(DATA / "signals.json")) if keep(s) and v26(s)]
    tier0 = load_fundamentals()
    sets = {
        "v1.3 40日": trades(v13, "stop40", csi1000),
        "v2.6 40日": trades(v26s, "stop40", csi1000),
    }
    for t in sets.values():
        for x in t:
            x["tier0"] = tier0(x["code"], x["date"])
    regimes = {f"{name} MA{ma}": regime_fn(s, ma) for name, s in series.items() for ma in (20, 60, 120)}
    breadth_dates = sorted(breadth)
    regimes["个股站上MA60比例<50%"] = lambda d: (breadth[d] < 0.5) if d in breadth else None
    regimes["个股站上MA60比例<40%"] = lambda d: (breadth[d] < 0.4) if d in breadth else None

    all_days = [d for d in sorted(csi1000) if "2023-07-01" <= d <= "2026-08-14"]
    print("说明：弱势=指数低于其均线；“差值”=弱势组平均超额-强势组平均超额（越大越能区分）；✓=两段都为正且去掉最好3个月仍为正\n")
    for set_name, t in sets.items():
        print(f"===== {set_name}（全部 {robust(t)}）")
        rows = []
        for name, weak in regimes.items():
            w = [x for x in t if weak(x["date"]) is True]
            s = [x for x in t if weak(x["date"]) is False]
            if not w or not s:
                continue
            spread = st.mean(x["ex"] for x in w) - st.mean(x["ex"] for x in s)
            halves = []
            for early in (True, False):
                ww = [x["ex"] for x in w if (x["date"] < "2025-01") == early]
                ss = [x["ex"] for x in s if (x["date"] < "2025-01") == early]
                halves.append(st.mean(ww) - st.mean(ss) if ww and ss else float("nan"))
            weak_days = sum(1 for d in all_days if weak(d)) / len(all_days)
            combo = [x for x in w if x["tier0"]]
            rows.append((spread, name, weak_days, halves, robust(w), robust(combo)))
        for spread, name, weak_days, halves, rw, rc in sorted(rows, reverse=True):
            print(f"{name:22s} 弱势天数{weak_days:4.0%} 差值{spread:+.2%}（前{halves[0]:+.1%}/后{halves[1]:+.1%}）")
            print(f"{'':22s}   弱势组：{rw}")
            print(f"{'':22s}   弱势+基本面向好：{rc}")
        print()


if __name__ == "__main__":
    main()
