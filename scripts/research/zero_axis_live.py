#!/usr/bin/env python3
"""零轴金叉 live 选股：把"0 轴附近金叉"当作一条观察池规则实盘扫描。

规则写死在 output/backtest/zero-axis-rule-note.md 第六节，不给自己留调参空间：

  形态  日线 DIF<0 且 DEA<0，当日 DIF 上穿 DEA，且 |DIF|/收盘 ≤ 1%
  位置  距 60 日低点 ≤ 8%
  安静  20 日振幅 ≤ 12%，近 5 日涨幅 ≤ 3%，信号日量比 ≤ 1.2
  池子  核心市值池（总市值 ≥50 亿、流通 ≥30 亿、20 日均额 ≥0.5 亿）

跟踪 20 个交易日，不设破位。

研究结论（zero-axis-cross.md / zero-axis-rule-note.md）：
  这是 10~20 日的规则，40 日上优势归零；效果绝大部分来自"正在横盘 + 贴着底部"，
  MACD 只贡献 0.06~0.22 个百分点，按月 t 值全部不显著。
  容量平均每 3 个交易日 1 个信号，中位 1 个、最多 7 个，不需要截取排序。
  预期值 20 日同日相对 +0.53（三段的 +1.81 / +0.12 / +0.12）。

与水下金叉的区别：水下金叉要求低点在 0~10 日前、跌 ≥12%、底背离、此前 20 日无金叉；
零轴金叉不要求这些，只要求"DIF/DEA 都在水下、刚金叉、贴近零轴、位置低、还没动"。
两者会有重叠，同一只票可能同时出现在两个清单里。

用法：
  python3 scripts/research/zero_axis_live.py --as-of 2026-11-06
  python3 scripts/research/zero_axis_live.py --as-of 2026-11-06 --json-output output/daily/2026-11-06/zaxis.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

NEAR_ZERO = 0.01      # |DIF|/收盘 ≤ 1%
MAX_RISE_LOW = 8.0    # 距 60 日低点 ≤ 8%
MAX_RANGE20 = 12.0    # 20 日振幅 ≤ 12%
MAX_CHANGE5 = 3.0     # 近 5 日涨幅 ≤ 3%
MAX_VOLUME_RATIO = 1.2  # 信号日量比 ≤ 1.2


def live(as_of: str, json_output: Path | None = None, days: int = 1) -> list[dict[str, Any]]:
    """Stocks whose 零轴金叉 condition holds on the bar `days-1` bars back to `as_of`.

    Uses the daily job's cache and quote snapshot (no extra downloads) and the same core
    market-cap pool as the other three lists. Observation only."""
    from bottom_base_context import fundamental_trend
    from screen_latest_30 import ema, fetch_financials
    from screen_red_zone_5 import MARKET_POOL_DEFAULTS, fetch_daily_quotes
    from screen_v26_unified import calculate_metrics, prepare_rows
    from screen_reliability import scan_candidates, filter_observation_risks

    names = fetch_financials(include_financial_yellow=True)
    quotes = fetch_daily_quotes(names.keys())
    limits = MARKET_POOL_DEFAULTS["core"]

    def one(code: str) -> dict[str, Any] | None:
        if code not in quotes:
            raise ValueError("missing quote")
        rows, source, volume_scale = prepare_rows(code, quotes)
        rows = [r for r in rows if r["date"] <= as_of]
        if not rows or rows[-1]["date"] != as_of:
            raise ValueError("K-line does not reach requested date")
        if len(rows) < 200:
            return None
        if any(r["low"] <= 0 or r["close"] <= 0 for r in rows):
            raise ValueError("invalid nonpositive price")

        q = quotes.get(code) or {}
        if (float(q.get("totalMarketCap") or 0) < limits["minTotalMarketCap"]
                or float(q.get("floatMarketCap") or 0) < limits["minFloatMarketCap"]):
            return None
        amount20 = sum(r["close"] * r["volume"] * volume_scale for r in rows[-20:]) / 20
        if amount20 < limits["minAverageAmount20"]:
            return None

        closes = [r["close"] for r in rows]
        fast, slow = ema(closes, 12), ema(closes, 26)
        dif = [a - b for a, b in zip(fast, slow)]
        dea = ema(dif, 9)

        hits: list[dict[str, Any]] = []
        for k in range(max(120, len(rows) - days), len(rows)):
            # 形态：当日水下金叉（DIF、DEA 均 <0，DIF 上穿 DEA），且贴近零轴
            if not (dif[k - 1] < dea[k - 1] and dif[k] >= dea[k] and dif[k] < 0 and dea[k] < 0):
                continue
            price = closes[k]
            if price <= 0 or abs(dif[k]) / price > NEAR_ZERO:
                continue

            window = rows[:k + 1]
            metrics = calculate_metrics(window[-260:])
            if metrics is None:
                continue
            # 位置与安静：还没开始动
            if metrics["riseRecentLow"] > MAX_RISE_LOW:
                continue
            if metrics["range20"] > MAX_RANGE20:
                continue
            if metrics["change5"] > MAX_CHANGE5:
                continue
            vol20 = sum(r["volume"] for r in window[-21:-1]) / 20
            if vol20 <= 0 or rows[k]["volume"] / vol20 > MAX_VOLUME_RATIO:
                continue

            low60 = min(r["low"] for r in window[-60:])
            hits.append({
                "code": code,
                "name": str(names[code].get("SECURITY_NAME_ABBR") or code),
                "industry": str(names[code].get("BOARD_NAME") or "待分类"),
                "state": "零轴金叉",
                "totalMarketCap": float(q.get("totalMarketCap") or 0),
                "crossDate": rows[k]["date"],
                "close": price,
                "difPct": dif[k] / price,
                "deaPct": dea[k] / price,
                "low60": low60,
                "riseRecentLow": metrics["riseRecentLow"],
                "range20": metrics["range20"],
                "change5": metrics["change5"],
                "volumeRatio": rows[k]["volume"] / vol20,
                "dailyRsi": metrics["dailyRsi"],
                "weeklyRsi": metrics["weeklyRsi"],
                "source": source,
            })
        if not hits:
            return None
        best = hits[-1]
        best["difSeries"] = {"dif": dif[-1], "dea": dea[-1]}
        return best

    results, diagnostics = scan_candidates(sorted(names), one)
    results = filter_observation_risks(results, names, diagnostics)

    for h in results:
        try:
            h["fundamentalTrend"] = fundamental_trend(h["code"], as_of)
        except Exception as exc:
            h["fundamentalTrend"] = {"tier": 2, "error": str(exc)}
    # 容量小（每 3 个交易日约 1 个），不截取只数，只排序
    results.sort(key=lambda h: (h["fundamentalTrend"].get("tier", 2), -h["totalMarketCap"], h["code"]))

    if json_output:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps({
            "dataThrough": as_of,
            "method": "zero-axis-near-cross",
            "days": days,
            "criteria": {
                "nearZeroPct": NEAR_ZERO * 100,
                "maxRiseLowPct": MAX_RISE_LOW,
                "maxRange20Pct": MAX_RANGE20,
                "maxChange5Pct": MAX_CHANGE5,
                "maxVolumeRatio": MAX_VOLUME_RATIO,
            },
            "selected": results,
            "scan": diagnostics,
            "inputTiming": "K-line as-of; financials and announcements use current available data",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"as of {as_of}: 零轴金叉 {len(results)} 只")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--days", type=int, default=1, help="scan crosses in the last N bars (default 1 = today)")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    live(args.as_of, args.json_output, args.days)


if __name__ == "__main__":
    main()
