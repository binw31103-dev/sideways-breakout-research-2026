#!/usr/bin/env python3
"""Build a long, proportionally adjusted daily K-line set for backtests, checked across sources.

Why: Tencent's and Eastmoney's "qfq" series are *subtractive* (price minus later cash
dividends). That inflates older percentage moves -- e.g. 000019 on 2022-01-12 shows +5.26%
instead of the real +4.54% -- which skews RSI, distance-from-low, stop levels and backtest
returns, most of all in the early years and for cheap, high-dividend stocks. Sina's
K-line is unadjusted. None of them is usable as-is for a multi-year backtest.

How:
1. Unadjusted daily bars: Sina (1023 bars, from 2022-08) first, Tencent unadjusted
   (two 900-bar segments, both hosts) as the fallback. Tencent and Eastmoney's K-line hosts
   WAF-block bulk downloads, so neither is a safe single source.
2. Adjustment: Sina's proportional factors (finance.sina.com.cn qfq.js), which follow the
   exchange reference prices and include special dividends and rights issues.
3. Check A (every stock, no extra source): an adjusted daily move beyond the price limit
   (10% main board, 20% ChiNext/STAR, plus a margin) means a corporate action we did not
   model (e.g. a rights issue). Such stocks are marked "有异常跳空"; backtests skip them.
   Optional (`--tencent-check`): Tencent qfq shifts on its own ex-dates, which must match
   the dividend table.
4. Check B (every stock): an independent rebuild from the Eastmoney datacenter dividend
   table (ex-date, cash, bonus/transfer; reference price (prev - cash) / (1 + bonus)).
   Daily multipliers must agree within 0.3%, else "复权来源不一致". The dividend table is
   known to miss some special dividends (600519 2022-12, 2023-12), so Sina wins; the flag
   lets backtests be rerun without the disputed stocks. Without Sina factors the dividend
   rebuild is used and the stock is marked "仅分红表".
5. Check C (`--verify N`): BaoStock daily returns for a random sample. BaoStock is slow
   (~5 s per stock) and blacklists parallel logins, so use one process and a small N.

Output: output/cache/kline_long/{code}.json and output/cache/kline_long_check.json.
Usage: python3 scripts/research/build_kline_dataset.py [--workers 4] [--verify 150]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import subprocess
import time
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "cache" / "kline_long"
CHECK_PATH = ROOT / "output" / "cache" / "kline_long_check.json"
DIVIDENDS_PATH = ROOT / "output" / "cache" / "dividends.json"
START = "2022-01-01"
SPLIT = ("2024-06-30", "2024-03-01")  # end of segment 1, start of segment 2 (overlap checks the join)


def curl_json(url: str, attempts: int = 3) -> dict | None:
    for attempt in range(attempts):
        out = subprocess.run(
            ["curl", "-sS", "-m", "20", "-H", "User-Agent: Mozilla/5.0", url],
            capture_output=True, text=True,
        ).stdout
        try:
            return json.loads(out.lstrip("﻿"))
        except ValueError:
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_dividends() -> dict[str, list[dict]]:
    """All implemented distributions with an ex-date since START, keyed by stock code."""
    if DIVIDENDS_PATH.exists() and DIVIDENDS_PATH.stat().st_mtime > time.time() - 86400:
        return json.loads(DIVIDENDS_PATH.read_text(encoding="utf-8"))
    rows, page = [], 1
    while True:
        params = {
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "SECURITY_CODE,EX_DIVIDEND_DATE,PRETAX_BONUS_RMB,BONUS_IT_RATIO,ASSIGN_PROGRESS",
            "filter": f"(EX_DIVIDEND_DATE>='{START}')",
            "pageSize": 500, "pageNumber": page, "sortColumns": "EX_DIVIDEND_DATE", "sortTypes": 1,
        }
        result = (curl_json("https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)) or {}).get("result")
        if result is None:
            raise RuntimeError(f"dividend table page {page} failed")
        rows += result.get("data") or []
        if page >= (result.get("pages") or 0):
            break
        page += 1
        time.sleep(0.2)
    by_code: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        if not r.get("EX_DIVIDEND_DATE"):
            continue
        date = r["EX_DIVIDEND_DATE"][:10]
        # One record per ex-date; duplicate rows for the same date carry the same terms.
        by_code[r["SECURITY_CODE"]][date] = {
            "date": date,
            "cash": float(r.get("PRETAX_BONUS_RMB") or 0) / 10,
            "bonus": float(r.get("BONUS_IT_RATIO") or 0) / 10,
        }
    out = {code: sorted(v.values(), key=lambda e: e["date"]) for code, v in by_code.items()}
    DIVIDENDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DIVIDENDS_PATH.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def tencent_segment(code: str, start: str, end: str, fq: str) -> list[list]:
    symbol = ("sh" if code.startswith("6") else "sz") + code
    for host in ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn"):
        payload = curl_json(f"https://{host}/appstock/app/fqkline/get?param={symbol},day,{start},{end},900,{fq}", attempts=2)
        node = (((payload or {}).get("data") or {}).get(symbol)) or {}
        if node:
            return [r for r in (node.get(f"{fq}day") or node.get("day") or []) if len(r) >= 6]
    return []


def tencent_series(code: str, fq: str) -> tuple[list[list], str]:
    today = dt.date.today().isoformat()
    old, new = tencent_segment(code, START, SPLIT[0], fq), tencent_segment(code, SPLIT[1], today, fq)
    if not new:
        return [], "empty"
    if not old or old[-1][0] < new[0][0]:
        return new, "recent segment only"
    overlap = {r[0]: float(r[2]) for r in old if r[0] >= new[0][0]}
    if any(r[0] in overlap and abs(float(r[2]) / overlap[r[0]] - 1) > 0.002 for r in new):
        return new, "segments disagree, kept recent only"
    return [r for r in old if r[0] < new[0][0]] + new, "ok"


def sina_raw(code: str) -> list[list]:
    symbol = ("sh" if code.startswith("6") else "sz") + code
    payload = curl_json("https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData?"
                        f"symbol={symbol}&scale=240&ma=no&datalen=1023")
    data = (((payload or {}).get("result") or {}).get("data")) or []
    return [[r["day"], r["open"], r["close"], r["high"], r["low"], r["volume"]] for r in data]


def sina_factors(code: str) -> list[tuple[str, float]] | None:
    """[(ex_date, factor)], newest first; qfq = raw / factor of the latest ex_date <= day."""
    symbol = ("sh" if code.startswith("6") else "sz") + code
    out = subprocess.run(["curl", "-sS", "-m", "20", "-H", "User-Agent: Mozilla/5.0",
                          f"https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js"],
                         capture_output=True, text=True).stdout
    start = out.find("{")
    try:
        data = json.loads(out[start:out.rfind("}") + 1])["data"]
    except (ValueError, KeyError):
        return None
    return sorted(((d["d"], float(d["f"])) for d in data), reverse=True)


def sina_adjusted(raw: list[list], factors: list[tuple[str, float]]) -> list[dict]:
    """raw / factor(day), renormalised so the latest bar is unadjusted."""
    def f(day: str) -> float:
        return next((x for d, x in factors if d <= day), factors[-1][1])
    base = f(raw[-1][0])
    return [
        {"date": r[0], "open": float(r[1]) * base / f(r[0]), "close": float(r[2]) * base / f(r[0]),
         "high": float(r[3]) * base / f(r[0]), "low": float(r[4]) * base / f(r[0]), "volume": float(r[5])}
        for r in raw
    ]


def disagreement(a: list[dict], b: list[dict]) -> list[str]:
    return [x["date"] for x, y in zip(a, b) if abs(x["close"] / y["close"] - 1) > 0.003]


def limit_breaks(code: str, rows: list[dict]) -> list[str]:
    """Adjusted moves no real trading day can produce (IPO first days excluded)."""
    cap = 0.21 if code.startswith(("300", "301", "688", "689")) else 0.11
    return [
        cur["date"] for k, (prev, cur) in enumerate(zip(rows, rows[1:]), 1)
        if k > 5 and abs(cur["close"] / prev["close"] - 1) > cap
    ]


def proportional(raw: list[list], events: list[dict]) -> tuple[list[dict], list[str]]:
    """Chain true daily returns (reference price on ex-dates) back from the latest close."""
    dates = [r[0] for r in raw]
    factors = [1.0] * len(raw)  # true return / raw return, from bar k-1 to bar k
    applied: list[str] = []
    pending: dict | None = None
    e = 0
    for k in range(1, len(raw)):
        # An ex-date can fall on a suspended day; apply it on the next traded bar.
        while e < len(events) and events[e]["date"] <= dates[k]:
            if events[e]["date"] > dates[k - 1]:
                ev = events[e]
                pending = ev if pending is None else {
                    "cash": pending["cash"] + ev["cash"],
                    "bonus": (1 + pending["bonus"]) * (1 + ev["bonus"]) - 1,
                }
            e += 1
        if pending:
            prev = float(raw[k - 1][2])
            ref = (prev - pending["cash"]) / (1 + pending["bonus"])
            if ref > 0:
                factors[k] = prev / ref
                applied.append(dates[k])
            pending = None
    m = [1.0] * len(raw)  # adjusted = raw * m[k]; anchored at the latest bar
    for k in range(len(raw) - 1, 0, -1):
        m[k - 1] = m[k] / factors[k]
    rows = [
        {"date": r[0], "open": float(r[1]) * m[k], "close": float(r[2]) * m[k], "high": float(r[3]) * m[k],
         "low": float(r[4]) * m[k], "volume": float(r[5])}
        for k, r in enumerate(raw)
    ]
    return rows, applied


def qfq_shift_days(raw: list[list], qfq: list[list]) -> list[str]:
    """Days on which Tencent's raw-minus-qfq gap changes, i.e. Tencent's own ex-dates."""
    q = {r[0]: float(r[2]) for r in qfq}
    days, prev_gap = [], None
    for r in raw:
        if r[0] not in q:
            continue
        gap = float(r[2]) - q[r[0]]
        if prev_gap is not None and abs(gap - prev_gap) > max(0.011, 0.002 * float(r[2])):
            days.append(r[0])
        prev_gap = gap
    return days


def build(code: str, events: list[dict], refresh: bool, tencent_check: bool) -> dict:
    path = OUT / f"{code}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))["check"]
    raw, source = sina_raw(code), "新浪不复权"
    if len(raw) < 300:
        raw, note = tencent_series(code, "")
        source = f"腾讯不复权（{note}）"
    check: dict = {"code": code, "source": source, "rows": len(raw)}
    if len(raw) < 300:
        check["status"] = "失败"
        return check
    by_dividends, applied = proportional(raw, events)
    factors = sina_factors(code)
    rows = sina_adjusted(raw, factors) if factors else by_dividends
    mismatch = disagreement(rows, by_dividends) if factors else []
    breaks = limit_breaks(code, rows)
    check.update(dividendExDates=applied, limitBreaks=breaks, first=rows[0]["date"], last=rows[-1]["date"],
                 sourceMismatchDays=len(mismatch), firstMismatch=mismatch[0] if mismatch else None)
    if breaks:
        check["status"] = "有异常跳空"
    elif not factors:
        check["status"] = "仅分红表"
    elif mismatch:
        check["status"] = "复权来源不一致"
    else:
        check["status"] = "通过"
    if tencent_check:
        qfq, _ = tencent_series(code, "qfq")
        unexplained = [d for d in qfq_shift_days(raw, qfq) if d not in applied] if qfq else None
        check["tencentUnexplained"] = unexplained
    if any(r["low"] <= 0 or r["close"] <= 0 for r in rows):
        check["status"] += "/含非正价格"
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    adjust = "新浪复权因子" if factors else "东财分红表"
    tmp.write_text(json.dumps({"source": f"{source}+{adjust} 比例前复权", "check": check, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return check


def compare(a: list[dict], b: list[dict]) -> dict:
    ca, cb = {r["date"]: r["close"] for r in a}, {r["date"]: r["close"] for r in b}
    common = sorted(set(ca) & set(cb))
    diffs = [abs(ca[c] / ca[p] - cb[c] / cb[p]) for p, c in zip(common, common[1:]) if min(ca[p], cb[p]) > 0]
    return {"commonDays": len(common), "coverage": len(common) / max(len(ca), 1),
            "badDays": sum(d > 0.003 for d in diffs), "maxDiff": max(diffs, default=0.0)}


def verify_sample(codes: list[str], n: int, seed: int = 7) -> dict:
    """Independent check against BaoStock's proportional qfq, daily close-to-close returns."""
    import contextlib
    import io
    import random
    import baostock as bs
    sample = random.Random(seed).sample(codes, min(n, len(codes)))
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
    results = []
    try:
        for code in sample:
            ours = json.loads((OUT / f"{code}.json").read_text(encoding="utf-8"))["rows"]
            rs = bs.query_history_k_data_plus(("sh." if code.startswith("6") else "sz.") + code, "date,close",
                                              start_date=ours[0]["date"], frequency="d", adjustflag="2")
            theirs = []
            while rs.error_code == "0" and rs.next():
                d, c = rs.get_row_data()
                if c:
                    theirs.append({"date": d, "close": float(c)})
            if theirs:
                results.append({"code": code, **compare(ours, theirs)})
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            bs.logout()
    days = sum(r["commonDays"] for r in results)
    return {
        "sampled": len(results),
        "comparedDays": days,
        "badDayShare": sum(r["badDays"] for r in results) / max(days, 1),
        "stocksClean": sum(r["badDays"] == 0 for r in results),
        "stocksWithBadDays": [r for r in results if r["badDays"] > 0],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codes-from", type=Path, help="Directory of {code}.json whose names define the universe")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--codes", nargs="*")
    parser.add_argument("--verify", type=int, default=0, help="BaoStock-check this many random stocks")
    parser.add_argument("--tencent-check", action="store_true", help="Also match ex-dates against Tencent qfq")
    args = parser.parse_args()
    codes_dir = args.codes_from or max((ROOT / "output" / "cache" / "kline").iterdir())
    codes = args.codes or sorted(p.stem for p in codes_dir.glob("*.json"))[: args.limit]
    dividends = fetch_dividends()
    print(f"dividend events: {sum(len(v) for v in dividends.values())} for {len(dividends)} stocks", flush=True)
    checks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = executor.map(lambda c: build(c, dividends.get(c, []), args.refresh, args.tencent_check), codes)
        for n, check in enumerate(jobs, 1):
            checks.append(check)
            if n % 500 == 0:
                print(n, Counter(c["status"] for c in checks).most_common(), flush=True)
    summary = {
        "builtAt": dt.datetime.now().isoformat(timespec="seconds"),
        "stocks": len(checks),
        "status": dict(Counter(c["status"] for c in checks)),
        "sources": dict(Counter(c.get("source") for c in checks)),
        "lastDate": dict(Counter(c.get("last") for c in checks).most_common(3)),
        "flagged": [{k: c.get(k) for k in ("code", "status", "limitBreaks", "sourceMismatchDays",
                                          "firstMismatch", "tencentUnexplained")}
                    for c in checks if not c["status"].startswith("通过")],
    }
    if args.verify:
        summary["baostock"] = verify_sample([c["code"] for c in checks if c["status"] != "失败"], args.verify)
    CHECK_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    brief = {k: v for k, v in summary.items() if k != "flagged"}
    brief["flaggedCount"] = len(summary["flagged"])
    print(json.dumps(brief, ensure_ascii=False, indent=1)[:5000])


if __name__ == "__main__":
    main()
