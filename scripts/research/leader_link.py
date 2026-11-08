#!/usr/bin/env python3
"""Combine the v2.6 bottom list and the underwater-cross (G) list with sector leaders (龙头).

Prior: within an industry, big firms' returns lead small firms' (Hou 2007, RFS, information
diffusion); A-share evidence of momentum spillover across connected firms. Two readings of
"龙头", groups fixed before running:

  自身是龙头      the stock's market cap ranks top 3 in its industry (industry >= 5 members)
  行业龙头走强    the industry's largest stock (other than itself) beat CSI 1000 by >= 5% over
                  the last 20 trading days
  行业有领涨股    some member (cap >= 50亿) gained >= 30% over the last 20 trading days
  行业整体走强    industry median 20-day return beat CSI 1000

Outcome: next-open entry, excess vs CSI 1000 at 20 / 40 days; "同月差" = group minus the
rest of the same list in the same month. Industry = today's BOARD_NAME and caps = today's
share count x close (both are look-ahead approximations).

Usage: python3 scripts/research/leader_link.py
"""

from __future__ import annotations

import bisect
import concurrent.futures
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backtest_bottom_base import GAP, LONG_KLINE_DIR, benchmark_index, v26  # noqa: E402

PERIODS = (("2023-07~2024-12", "2023-07", "2025-01"), ("2025-01~2026-06", "2025-01", "2026-07"),
           ("2026-07起", "2026-07", "9999"))
HOLD = (20, 40)


def load_prices() -> dict[str, tuple[list[str], list[float], list[float]]]:
    out = {}
    for path in LONG_KLINE_DIR.glob("*.json"):
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
        if rows and all(r["close"] > 0 and r["open"] > 0 for r in rows):
            out[path.stem] = ([r["date"] for r in rows], [r["close"] for r in rows], [r["open"] for r in rows])
    return out


def main() -> None:
    import entry_gap as E
    from screen_latest_30 import normalize_industry
    index = benchmark_index()
    idx_dates = sorted(index)
    prices = load_prices()
    pos = {c: {d: k for k, d in enumerate(p[0])} for c, p in prices.items()}
    fin = json.loads((ROOT / "output" / "cache" / "financials-all.json").read_text(encoding="utf-8"))
    shares = json.loads((ROOT / "output" / "cache" / "shares.json").read_text(encoding="utf-8"))
    industry = {c: normalize_industry(str(f.get("BOARD_NAME") or "")) for c, f in fin.items()}
    members: dict[str, list[str]] = defaultdict(list)
    for c, ind in industry.items():
        if ind and c in prices and c in shares:
            members[ind].append(c)

    keep = lambda d: d >= "2023-07-01" and not d.startswith("2024-09")  # noqa: E731
    v26_pairs = [(s["code"], s["date"]) for s in json.loads((ROOT / "output/backtest/signals.json").read_text(encoding="utf-8"))
                 if keep(s["date"]) and v26(s)]
    g: list[tuple[str, str]] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(E.g_crosses, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            g.extend(x for x in result if keep(x[1]))

    cache: dict[tuple[str, str], dict] = {}

    def ret20(code: str, date: str) -> float | None:
        k = pos[code].get(date)
        if k is None or k < 20:
            return None
        closes = prices[code][1]
        return closes[k] / closes[k - 20] - 1

    def sector(ind: str, date: str) -> dict:
        key = (ind, date)
        if key not in cache:
            rows = []
            for c in members.get(ind, []):
                k = pos[c].get(date)
                r = ret20(c, date)
                if k is not None and r is not None:
                    rows.append((prices[c][1][k] * shares[c][0], r, c))
            rows.sort(reverse=True)
            cache[key] = {"rows": rows}
        return cache[key]

    def tags(code: str, date: str) -> dict[str, bool] | None:
        ind = industry.get(code)
        if not ind or code not in shares or date not in index:
            return None
        sec = sector(ind, date)["rows"]
        if len(sec) < 5:
            return None
        k_idx = idx_dates.index(date)
        if k_idx < 20:
            return None
        idx20 = index[date] / index[idx_dates[k_idx - 20]] - 1
        rank = next((n for n, (_, _, c) in enumerate(sec, 1) if c == code), None)
        leader = next(((cap, r) for cap, r, c in sec if c != code), None)
        return {
            "自身是龙头": rank is not None and rank <= 3,
            "行业龙头走强": leader is not None and leader[1] - idx20 >= 0.05,
            "行业有领涨股": any(r >= 0.30 and cap >= 5e9 and c != code for cap, r, c in sec),
            "行业整体走强": st.median(r for _, r, _ in sec) - idx20 > 0,
        }

    def trades(pairs: list[tuple[str, str]]) -> list[dict]:
        out, last = [], {}
        for code, date in sorted(pairs):
            if code not in prices:
                continue
            k = pos[code].get(date)
            if k is None or k - last.get(code, -GAP) < GAP or k + 1 >= len(prices[code][0]):
                continue
            last[code] = k
            t = tags(code, date)
            if t is None:
                continue
            e = {"code": code, "date": date, **t}
            entry = prices[code][2][k + 1]
            for h in HOLD:
                j = k + h
                if j < len(prices[code][0]) and prices[code][0][j] in index:
                    d = prices[code][0][j]
                    e[f"x{h}"] = prices[code][1][j] / entry - 1 - (index[d] / index[date] - 1)
            out.append(e)
        return out

    lines = ["# v2.6 / 水下金叉 × 龙头（比例复权数据，2023-07 起，剔除 2024-09）", "",
             "格式：命中组平均超额（同月差 = 命中组 − 同清单同月未命中组，跑赢比例，样本）。", ""]
    for name, pairs in (("v2.6 底部观察池", v26_pairs), ("低位水下金叉", g)):
        t = trades(pairs)
        lines += [f"## {name}（{len(t)} 笔）", ""]
        for h in HOLD:
            lines += [f"### 持有 {h} 日", "", "| 条件 | 命中率 | " + " | ".join(p[0] for p in PERIODS) + " |",
                      "| --- | ---: |" + " --- |" * len(PERIODS)]
            for cond in ("自身是龙头", "行业龙头走强", "行业有领涨股", "行业整体走强"):
                cells = []
                for _, lo, hi in PERIODS:
                    xs = [e for e in t if lo <= e["date"] < hi and f"x{h}" in e]
                    hit = [e for e in xs if e[cond]]
                    if len(hit) < 15:
                        cells.append(f"n{len(hit)}")
                        continue
                    by_month = defaultdict(lambda: [[], []])
                    for e in xs:
                        by_month[e["date"][:7]][0 if e[cond] else 1].append(e[f"x{h}"])
                    diffs = [(st.mean(a) - st.mean(b), len(a)) for a, b in by_month.values() if a and b]
                    dm = sum(d * n for d, n in diffs) / max(sum(n for _, n in diffs), 1)
                    ex = [e[f"x{h}"] for e in hit]
                    cells.append(f"{st.mean(ex):+.1%}（{dm:+.1%}，{sum(v > 0 for v in ex) / len(ex):.0%}，n{len(hit)}）")
                rate = sum(e[cond] for e in t) / max(len(t), 1)
                lines.append(f"| {cond} | {rate:.0%} | " + " | ".join(cells) + " |")
            lines.append("")
    out = ROOT / "output" / "backtest" / "leader-link.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
