#!/usr/bin/env python3
"""Which stock features rank v2.6 / v1.3 candidates, judged walk-forward.

Rules fixed before looking at results (see 横盘选股最优方案-2026-10-22.md):
- Features and their expected direction come from A-share literature (Liu, Stambaugh &
  Yuan 2019 CH-3/CH-4; MAX/lottery studies; short-term reversal) plus the project's own
  earlier candidates. Nothing is added after seeing results.
- Target: excess return vs CSI 1000, demeaned within the signal month, so market timing
  does not count as stock selection.
- A feature is kept only if, on 2023-07~2024-12 alone, its mean monthly rank IC has the
  literature sign (or, without a prior, any sign) and |IC| >= 0.01. The composite is the
  equal-weight average of within-month rank percentiles of kept features (no fitted weights).
- 2025-01~2026-06 and 2026-07+ are only used to test.

Usage: python3 scripts/research/factor_study.py [--model v26|v13] [--target stop40|hold10]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backtest_bottom_base import GAP, LONG_KLINE_DIR, annotate_market, benchmark_index, v26  # noqa: E402
from bottom_base_context import literature_features  # noqa: E402
from combo_v13_v26 import fundamentals_tier0  # noqa: E402

FUND = ROOT / "output" / "cache" / "fundamentals"
QUOTES = ROOT / "output" / "cache" / "quotes_2026-10-22.json"
PERIODS = (("前段 2023-07~2024-12", "2023-07", "2025-01"),
           ("后段 2025-01~2026-06", "2025-01", "2026-07"),
           ("新段 2026-07~", "2026-07", "9999"))
TRAIN = PERIODS[0]

# name: expected sign of IC (+1 higher is better, -1 lower is better, 0 no prior)
FEATURES = {
    "abnTurn": -1,      # 20d / 250d volume: CH-4 PMO, low abnormal turnover outperforms
    "turn20": -1,       # 20d turnover level: low turnover outperforms
    "ret20": -1,        # short-term reversal
    "max20": -1,        # MAX / lottery: big recent daily jump underperforms
    "vol60": -1,        # low volatility
    "logCap": -1,       # size: small outperforms
    "ep": +1,           # value: TTM earnings / market cap
    "profitYoy": +1,    # earnings momentum
    "fundTier0": +1,    # project: earnings inflection, or improving + holders falling
    "holderChg": -1,    # project: falling holder count = concentration
    "bigUp250": 0,      # project said more is better; MAX literature says the opposite
    "price": -1,        # project: low price better
    "drawdown250": 0,   # project: depth below 1y high, mixed earlier evidence
}


@lru_cache(maxsize=None)
def rows_of(code: str) -> list[dict]:
    return json.loads((LONG_KLINE_DIR / f"{code}.json").read_text(encoding="utf-8"))["rows"]


def load_fundamentals():
    perf: dict[str, dict[str, tuple[str, float | None, float | None]]] = defaultdict(dict)
    for r in json.loads((FUND / "perf.json").read_text(encoding="utf-8")):
        if r.get("NOTICE_DATE"):
            q, n = r["REPORTDATE"][:10], r["NOTICE_DATE"][:10]
            if q not in perf[r["SECURITY_CODE"]] or n < perf[r["SECURITY_CODE"]][q][0]:
                perf[r["SECURITY_CODE"]][q] = (n, r.get("PARENT_NETPROFIT"), r.get("SJLTZ"))
    hold: dict[str, list[tuple[str, str, float | None]]] = defaultdict(list)
    for r in json.loads((FUND / "holders.json").read_text(encoding="utf-8")):
        if r.get("HOLD_NOTICE_DATE"):
            hold[r["SECURITY_CODE"]].append((r["END_DATE"][:10], r["HOLD_NOTICE_DATE"][:10], r.get("HOLDER_NUM_RATIO")))

    def at(code: str, date: str) -> dict:
        known = {q: v for q, v in perf.get(code, {}).items() if v[0] < date}
        out: dict = {}
        if known:
            q = max(known)
            out["profitYoy"] = known[q][2]
            np_q = known[q][1]
            if isinstance(np_q, (int, float)):
                if q.endswith("12-31"):
                    out["ttm"] = np_q
                else:
                    prev_annual = known.get(f"{int(q[:4]) - 1}-12-31", (None, None))[1]
                    prev_same = known.get(f"{int(q[:4]) - 1}{q[4:]}", (None, None))[1]
                    if isinstance(prev_annual, (int, float)) and isinstance(prev_same, (int, float)):
                        out["ttm"] = np_q + prev_annual - prev_same
        hs = sorted((e, v) for e, n, v in hold.get(code, []) if n < date)
        if hs and isinstance(hs[-1][1], (int, float)):
            out["holderChg"] = hs[-1][1]
        return out
    return at


def features(s: dict, fund_at, tier0, shares: dict[str, tuple[float, float]]) -> dict:
    """Same feature code as the live screen (bottom_base_context.literature_features)."""
    rows, i = rows_of(s["code"]), s["i"]
    window = rows[: i + 1]
    total_shares, float_shares = shares.get(s["code"], (0.0, 0.0))
    fa = fund_at(s["code"], s["date"]) if total_shares else {}
    trend = {"tier": 0 if tier0(s["code"], s["date"]) else 2, "profitYoy": fa.get("profitYoy"),
             "holderChange": fa.get("holderChg"), "ttmProfit": fa.get("ttm")}
    close = window[-1]["close"]
    f = literature_features(window, 1.0, close * total_shares, close * float_shares, trend)
    closes = [r["close"] for r in window]
    f["bigUp250"] = float(sum(1 for k in range(len(closes) - 250, len(closes)) if closes[k] / closes[k - 1] - 1 >= 0.095))
    f["drawdown250"] = closes[-1] / max(r["high"] for r in window[-250:]) - 1
    return f


def rank_pct(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda k: values[k])
    out = [0.0] * len(values)
    k = 0
    while k < len(order):
        j = k
        while j + 1 < len(order) and values[order[j + 1]] == values[order[k]]:
            j += 1
        for m in range(k, j + 1):
            out[order[m]] = (k + j) / 2 / max(len(values) - 1, 1)
        k = j + 1
    return out


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 8:
        return None
    rx, ry = rank_pct(x), rank_pct(y)
    mx, my = st.mean(rx), st.mean(ry)
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return None if not sx or not sy else sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy)


def monthly_ic(trades: list[dict], key, lo: str, hi: str) -> tuple[float, float, int]:
    by_month: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if lo <= t["date"] < hi:
            by_month[t["date"][:7]].append(t)
    ics = []
    for items in by_month.values():
        pairs = [(key(t), t["excess"]) for t in items if key(t) is not None]
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            ics.append(ic)
    if len(ics) < 2:
        return float("nan"), float("nan"), len(ics)
    return st.mean(ics), st.mean(ics) / (st.stdev(ics) / math.sqrt(len(ics))), len(ics)


def build_trades(signals: list[dict], target: str, index: dict[str, float]) -> list[dict]:
    trades, last = [], {}
    for s in sorted(signals, key=lambda s: (s["code"], s["i"])):
        if s.get("m") is None or s["i"] - last.get(s["code"], -GAP) < GAP:
            continue
        o = s["out"][target]
        if not o.get("complete", True) or o["exitDate"] not in index:
            continue
        last[s["code"]] = s["i"]
        trades.append({"code": s["code"], "date": s["date"], "s": s,
                       "excess": o["ret"] - (index[o["exitDate"]] / index[s["date"]] - 1)})
    return trades


def add_composite(trades: list[dict], kept: dict[str, int], name: str) -> None:
    by_month: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_month[t["date"][:7]].append(t)
    for items in by_month.values():
        parts = defaultdict(list)
        for feat, sign in kept.items():
            idx = [k for k, t in enumerate(items) if feat in t["f"]]
            if len(idx) < 2:
                continue
            ranks = rank_pct([items[k]["f"][feat] * sign for k in idx])
            for k, r in zip(idx, ranks):
                parts[k].append(r)
        for k, t in enumerate(items):
            t["f"][name] = st.mean(parts[k]) if parts[k] else 0.5


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=("v26", "v13"), default="v26")
    parser.add_argument("--target", choices=("stop40", "hold10"), default="stop40")
    parser.add_argument("--save", type=Path)
    parser.add_argument("--prior-only", action="store_true",
                        help="Composite = every feature with a literature sign, signs from literature, no data selection")
    args = parser.parse_args()

    keep = lambda s: s["date"] >= "2023-07-01" and not s["date"].startswith("2024-09")  # noqa: E731
    index = benchmark_index()
    path = ROOT / "output" / "backtest" / ("signals.json" if args.model == "v26" else "v13_signals.json")
    signals = [s for s in json.loads(path.read_text(encoding="utf-8")) if keep(s)]
    if args.model == "v26":
        signals = [s for s in signals if v26(s)]
    annotate_market(signals, index)
    quotes = json.loads(QUOTES.read_text(encoding="utf-8"))
    shares = {c: (q["totalMarketCap"] / q["close"], q["floatMarketCap"] / q["close"])
              for c, q in quotes.items() if q.get("close") and q.get("totalMarketCap")}
    fund_at, tier0 = load_fundamentals(), fundamentals_tier0()
    trades = build_trades(signals, args.target, index)
    for t in trades:
        t["f"] = features(t["s"], fund_at, tier0, shares)

    print(f"# 特征检验 · {args.model} · {args.target} · 已完结样本 {len(trades)}\n")
    print("同月秩相关 IC（正=数值越大越好），括号内为 t 值；判定只用前段\n")
    print(f"| 特征 | 文献方向 | {' | '.join(p[0] for p in PERIODS)} | 前段判定 |")
    print("| --- | --- |" + " ---: |" * len(PERIODS) + " --- |")
    kept: dict[str, int] = {}
    for feat, prior in FEATURES.items():
        cells = []
        for name, lo, hi in PERIODS:
            ic, t, n = monthly_ic(trades, lambda x: x["f"].get(feat), lo, hi)
            cells.append(f"{ic:+.3f}（{t:+.1f}）" if n >= 2 else "n/a")
        ic0, _, _ = monthly_ic(trades, lambda x: x["f"].get(feat), TRAIN[1], TRAIN[2])
        sign = 1 if ic0 > 0 else -1
        ok = abs(ic0) >= 0.01 and (prior == 0 or prior == sign)
        if ok:
            kept[feat] = sign
        verdict = f"保留（{'越大越好' if sign > 0 else '越小越好'}）" if ok else "不用"
        print(f"| {feat} | {'+' if prior > 0 else '-' if prior < 0 else '无'} | {' | '.join(cells)} | {verdict} |")

    if args.prior_only:
        kept = {feat: prior for feat, prior in FEATURES.items() if prior}
    add_composite(trades, kept, "composite")
    baseline = lambda x: (-x["f"].get("fundTier0", 0) * 1000 + -min(x["f"].get("bigUp250", 0), 3) * 10  # noqa: E731
                          + min(x["f"].get("price", 0), 9.99) / 1000)
    print(f"\n合成（等权，保留 {len(kept)} 个：{', '.join(kept)}）\n")
    print(f"| 排序 | {' | '.join(p[0] for p in PERIODS)} |")
    print("| --- |" + " ---: |" * len(PERIODS))
    for label, key in (("合成分（越大越好）", lambda x: x["f"]["composite"]),
                       ("现行排序（基本面→大涨次数→低价，取负）", lambda x: -baseline(x))):
        cells = []
        for name, lo, hi in PERIODS:
            ic, t, n = monthly_ic(trades, key, lo, hi)
            cells.append(f"{ic:+.3f}（{t:+.1f}）")
        print(f"| {label} | {' | '.join(cells)} |")

    print("\n合成分五分组：同月去均值超额（第5组=合成分最高）\n")
    print(f"| 组 | {' | '.join(p[0] for p in PERIODS)} |")
    print("| --- |" + " ---: |" * len(PERIODS))
    by_month = defaultdict(list)
    for t in trades:
        by_month[t["date"][:7]].append(t)
    for t in trades:
        items = by_month[t["date"][:7]]
        t["dm"] = t["excess"] - st.mean(x["excess"] for x in items)
        rk = rank_pct([x["f"]["composite"] for x in items])
        t["q"] = min(int(rk[items.index(t)] * 5), 4) + 1
    for q in range(1, 6):
        cells = []
        for name, lo, hi in PERIODS:
            v = [t["dm"] for t in trades if t["q"] == q and lo <= t["date"] < hi]
            cells.append(f"{st.mean(v):+.2%}（{len(v)}）" if v else "n/a")
        print(f"| Q{q} | {' | '.join(cells)} |")
    if args.save:
        args.save.write_text(json.dumps({"kept": kept}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
