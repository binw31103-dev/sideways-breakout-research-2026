#!/usr/bin/env python3
"""Screen v1.3 right-side startup candidates after a validated bottom base.

The model is separate from v2.5. It now splits into two stages:
right-edge initial confirmation and strong startup confirmation. The early
stage accepts a fresh platform breakout with incomplete MA15/MA30 repair, while
the strong stage keeps the older tighter confirmation. It produces research
candidates only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from screen_latest_30 import elasticity_metrics, fetch_financials, fetch_sector_snapshot, normalize_industry, sector_lookup
import screen_v25_recent_edge
from bottom_base_context import bottom_base_profile, fundamental_trend, market_regime, profile_text, trend_text
from screen_v26_unified import current_sector_observation, sector_observation_status
from screen_red_zone_5 import (
    MARKET_POOL_DEFAULTS,
    enrich_financial_risk,
    ensure_as_of_trading_day,
    ensure_scan_health,
    fetch_daily_quotes,
    historical_quote,
    moving_average,
)
from screen_v25_recent_edge import (
    build_tencent_sector_persistence,
    calculate_metrics,
    candidate_sector_persistence,
    prepare_rows,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "v1.3右侧启动观察池.md"
DEFAULT_JSON = ROOT / "output" / "v1.3-right-start.json"


def consecutive_above_15_30(closes: list[float]) -> int:
    count = 0
    for end in range(len(closes), 35, -1):
        close = closes[end - 1]
        if close >= moving_average(closes, 15, end) and close >= moving_average(closes, 30, end):
            count += 1
        else:
            break
    return count


def find_recent_breakout(rows: list[dict]) -> dict[str, Any] | None:
    closes = [float(row["close"]) for row in rows]
    for index in range(len(rows) - 5, len(rows)):
        if index < 31:
            continue
        prior_closes = closes[index - 20:index]
        platform_upper = max(prior_closes)
        day_close = closes[index]
        if day_close < platform_upper:
            continue
        prior_volume20 = sum(float(row["volume"]) for row in rows[index - 20:index]) / 20
        day = rows[index]
        day_range = max(float(day["high"]) - float(day["low"]), 1e-9)
        upper_wick = float(day["high"]) - max(float(day["open"]), float(day["close"]))
        return {
            "breakoutDate": day["date"],
            "age": len(rows) - 1 - index,
            "platformUpper": platform_upper,
            "breakoutClosePct": (day_close / platform_upper - 1) * 100,
            "currentAbovePlatformPct": (closes[-1] / platform_upper - 1) * 100,
            "breakoutVolumeRatio": float(day["volume"]) / max(prior_volume20, 1),
            "breakoutUpperWickRatio": upper_wick / day_range,
        }
    return None


def right_start_candidate(rows: list[dict]) -> dict[str, Any] | None:
    current = calculate_metrics(rows)
    if current is None:
        return None
    closes = [float(row["close"]) for row in rows]
    breakout = find_recent_breakout(rows)
    if breakout is None:
        return None
    prior_platform = False
    for offset in range(max(1, breakout["age"] + 1), 6):
        historical = calculate_metrics(rows[:-offset])
        if historical and (
            historical["stablePlatform"] or historical["secondaryWithContext"]
        ) and historical["range20"] <= 28:
            prior_platform = True
            break
    if not prior_platform:
        return None

    days_above = consecutive_above_15_30(closes)
    ma15_now = moving_average(closes, 15)
    ma30_now = moving_average(closes, 30)
    ma30_old = moving_average(closes, 30, len(closes) - 10)
    ma30_slope10 = (ma30_now / ma30_old - 1) * 100
    ma15_distance = (current["close"] / ma15_now - 1) * 100
    ma30_distance = (current["close"] / ma30_now - 1) * 100
    daily_gains = [
        (closes[index] / closes[index - 1] - 1) * 100
        for index in range(len(closes) - 9, len(closes))
    ]
    daily_macd = current["dailyMacd"]
    weekly_macd = current["weeklyMacd"]
    right_edge_macd_ok = (
        daily_macd["underwater"]
        and (daily_macd["improving"] or daily_macd["state"] in {"水下刚金叉", "水下接近金叉", "水下低位拐头"})
        and weekly_macd["underwater"]
        and (weekly_macd["improving"] or daily_macd["state"] == "水下刚金叉")
    )
    strong_macd_ok = (
        daily_macd["state"] == "水下刚金叉"
        and (daily_macd["improving"] or (daily_macd["crossAge"] is not None and daily_macd["crossAge"] <= 5))
        and weekly_macd["underwater"]
    )
    early_conditions = {
        "lowPosition": 4 <= current["riseRecentLow"] <= 20 and current["riseYearLow"] <= 30,
        "controlledGain": (
            current["change5"] <= 10
            and current["change10"] <= 15
            and current["change20"] <= 20
            and max(daily_gains) < 10
            and sum(1 for value in daily_gains if value >= 7.5) <= 1
        ),
        "maStage": (
            0 <= days_above <= 4
            and current["close"] <= current["ma"][30] * 1.06
            and ma30_slope10 >= -5
        ),
        "breakout": (
            0 <= breakout["currentAbovePlatformPct"] <= 5
            and 0.80 <= breakout["breakoutVolumeRatio"] <= 2.5
            and breakout["breakoutUpperWickRatio"] <= 0.50
        ),
        "rsi": 38 <= current["dailyRsi"] <= 58 and 32 <= current["weeklyRsi"] <= 48,
        "macd": right_edge_macd_ok,
    }
    strong_conditions = {
        "lowPosition": 8 <= current["riseRecentLow"] <= 22 and current["riseYearLow"] <= 28,
        "controlledGain": (
            current["change5"] <= 12
            and current["change10"] <= 18
            and current["change20"] <= 25
            and max(daily_gains) < 12
            and sum(1 for value in daily_gains if value >= 7.5) <= 1
        ),
        "maStage": (
            1 <= days_above <= 5
            and ma15_distance >= -1.0
            and ma30_distance >= -2.5
            and current["close"] <= current["ma"][30] * 1.08
            and ma30_slope10 >= -2
        ),
        "breakout": (
            0 <= breakout["currentAbovePlatformPct"] <= 6
            and 1.05 <= breakout["breakoutVolumeRatio"] <= 2.5
            and breakout["breakoutUpperWickRatio"] <= 0.45
        ),
        "rsi": 45 <= current["dailyRsi"] <= 62 and 35 <= current["weeklyRsi"] <= 52,
        "macd": strong_macd_ok,
    }
    if not all(early_conditions.values()):
        return None
    stage = "强启动确认" if all(strong_conditions.values()) else "右沿初确认"
    return {
        **current,
        "conditions": strong_conditions if stage == "强启动确认" else early_conditions,
        "stage": stage,
        "daysAbove1530": days_above,
        "ma30Slope10": ma30_slope10,
        "ma15Distance": ma15_distance,
        "ma30Distance": ma30_distance,
        **breakout,
    }


def analyze_code(
    code: str,
    financial: dict[str, Any],
    quotes: dict[str, dict],
    market_limits: dict[str, Any],
    market_date: str,
) -> dict[str, Any] | None:
    rows, source, volume_scale = prepare_rows(code, quotes)
    if not rows or rows[-1]["date"] != market_date:
        return None
    quote = quotes.get(code) or {}
    if screen_v25_recent_edge.AS_OF:
        quote = historical_quote(quote, rows)
    total_cap = float(quote.get("totalMarketCap") or 0)
    float_cap = float(quote.get("floatMarketCap") or 0)
    average_amount20 = sum(
        row["close"] * row["volume"] * volume_scale for row in rows[-20:]
    ) / 20
    if (
        total_cap < market_limits["minTotalMarketCap"]
        or float_cap < market_limits["minFloatMarketCap"]
        or average_amount20 < market_limits["minAverageAmount20"]
    ):
        return None
    metrics = right_start_candidate(rows)
    if metrics is None:
        return None
    return {
        "code": code,
        "name": str(financial.get("SECURITY_NAME_ABBR") or code),
        "industry": str(financial.get("BOARD_NAME") or "待分类"),
        "model": "v1.3右侧启动",
        "state": metrics["stage"],
        "date": metrics["date"],
        "source": source,
        "profit": float(financial.get("PARENT_NETPROFIT") or 0),
        "profitPeriod": str(financial.get("QDATE") or financial.get("REPORTDATE") or ""),
        "market": {
            "totalMarketCap": total_cap,
            "floatMarketCap": float_cap,
            "averageAmount20": average_amount20,
            "pool": "核心市值池",
        },
        **{key: value for key, value in metrics.items() if key != "ma"},
        "stage": metrics["stage"],
        "maDistance15": (metrics["close"] / metrics["ma"][15] - 1) * 100,
        "maDistance30": (metrics["close"] / metrics["ma"][30] - 1) * 100,
        "elasticity": elasticity_metrics(code, rows),
        "baseProfile": bottom_base_profile(rows),
        "sparkline": rows[-90:],
        "manualReviewVerdict": "待人工复核",
        "conditionChecks": {
            "technicalState": "满足",
            "sector": "待复核",
            "financial": "待复核",
            "risk": "待复核",
        },
    }


def render(selected: list[dict], scanned: int, technical_count: int, failures: list[dict], regime: dict[str, Any]) -> str:
    lines = [
        "# v1.3 右侧启动（主推：基本面向好）",
        "",
        f"生成时间：{dt.datetime.now().astimezone().isoformat(timespec='seconds')}。",
        f"行情数据日：{max((item['date'] for item in selected), default='无')}。",
        f"大盘环境：{regime['label']}。{regime.get('note', '')}。",
        "",
        "刚突破底部平台的右侧样本，分右沿初确认与强启动确认两段。基本面向好（净利拐点，或净利改善且股东户数连降）的列为主推；行业扩散只作观察。不计算评分。",
        "",
        f"扫描盈利A股 {scanned} 只，技术候选 {technical_count} 只，财务和风险开关后保留 {len(selected)} 只，其中主推 {sum(1 for item in selected if item.get('featured'))} 只。",
        "",
    ]
    for index, item in enumerate(selected, 1):
        lines.append(
            f"**{index}. {item['name']} {item['code']}｜{item['state']}{'｜主推' if item.get('featured') else ''}**\n"
            f"行业 {item['industry']}｜数据日 {item['date']}｜最近突破 {item['breakoutDate']}｜站上15/30 {item['daysAbove1530']}日\n"
            f"距低点+{item['riseRecentLow']:.1f}%｜5/10/20日 {item['change5']:+.1f}%/{item['change10']:+.1f}%/{item['change20']:+.1f}%\n"
            f"日/周RSI {item['dailyRsi']:.1f}/{item['weeklyRsi']:.1f}｜MA15/30 {item['maDistance15']:+.1f}%/{item['maDistance30']:+.1f}%｜"
            f"MA30斜率 {item['ma30Slope10']:+.1f}%｜{item['dailyMacd']['state']}\n"
            f"平台突破 {item['currentAbovePlatformPct']:+.1f}%｜突破量比 {item['breakoutVolumeRatio']:.2f}｜行业 {item['conditionChecks']['sector']}\n"
            f"{trend_text(item.get('fundamentalTrend'))}\n"
            f"{profile_text(item.get('baseProfile'))}\n"
            f"财务 {item['financialState']}｜风险 {item['riskState']}\n"
        )
    lines.extend([
        "",
        "## 数据边界",
        "",
        "- 个股和板块5日扩散使用腾讯前复权日K；行业成员、财务和公告使用东方财富。",
        "- 行业扩散（最近5日至少3日上涨宽度>=55%且另一只涨幅>=3%）只作观察，不再剔除。",
        "- 回测中，本模型在“基本面向好且中证1000在MA120下方”时通过全部稳健性检查；中证1000在MA120上方时表现明显变差。",
        "- 定位为突破后观察，入选后跟踪40个交易日，跌破入选前60日低点5%即失效。",
        "- 突破量比使用突破日成交量相对突破前20日均量计算。",
        "- 输出是研究观察池，不构成交易建议。",
        f"- 行情或接口失败 {len(failures)} 只，不把数据失败视为条件失败。",
    ])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-per-industry", type=int, default=2)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--as-of", help="Re-screen a past trading day (YYYY-MM-DD) for backfill")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    financials = fetch_financials()
    quotes = fetch_daily_quotes(financials.keys())
    market_date = max((quote["date"] for quote in quotes.values()), default="")
    snapshots = fetch_sector_snapshot()
    if args.as_of:
        ensure_as_of_trading_day(args.as_of)
        screen_v25_recent_edge.AS_OF = market_date = args.as_of
        snapshots = {}
    market_limits = dict(MARKET_POOL_DEFAULTS["core"])
    technical = []
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(analyze_code, code, financial, quotes, market_limits, market_date): (code, financial)
            for code, financial in financials.items()
        }
        for future in concurrent.futures.as_completed(futures):
            code, financial = futures[future]
            try:
                item = future.result()
                if item:
                    technical.append(item)
            except Exception as exc:
                failures.append({"code": code, "error": str(exc)})
    ensure_scan_health(len(financials), failures)

    # Sector spread is an observation label, not a gate: in the backtest it halved the
    # signals without changing excess return (v1.3与v2.6对比-2026-10-18).
    for item in technical:
        snapshot = sector_lookup(item["industry"], snapshots)
        item["sector"] = current_sector_observation(item["industry"], item["code"], snapshots, quotes)
        item["sectorBoardCode"] = str((snapshot or {}).get("f12") or "")
    persistence_by_board = build_tencent_sector_persistence(technical, snapshots, args.workers)
    for item in technical:
        persistence = candidate_sector_persistence(persistence_by_board.get(item["sectorBoardCode"]) or {}, item["code"])
        item["sectorPersistence"] = persistence
        item["conditionChecks"]["sector"] = sector_observation_status(item["sector"], persistence)
    sector_filtered = technical

    enriched = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, 12)) as executor:
        futures = {executor.submit(enrich_financial_risk, item): item for item in sector_filtered}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
                if result["financialState"] != "不通过" and result["riskState"] != "不通过":
                    enriched.append(result)
            except Exception as exc:
                failures.append({"code": item["code"], "error": f"financial-risk: {exc}"})

    regime = market_regime(market_date)
    for item in enriched:
        try:
            item["fundamentalTrend"] = fundamental_trend(item["code"], market_date)
        except Exception as exc:
            item["fundamentalTrend"] = {"tier": 2, "error": str(exc)}
        # Every selected right-side start is featured; fundamentals only order them. The old
        # "tier 0 + weak market" main pick had no usable 2025-01~2026-06 sample and was
        # negative from 2026-07 on rebuilt data (横盘选股最优方案-2026-10-22.md).
        item["featured"] = True
    enriched.sort(key=lambda item: (
        item["fundamentalTrend"]["tier"],
        -min(item["baseProfile"]["bigUpDays250"], 3),
        item["close"],
        item["code"],
    ))
    selected = []
    industry_counts: defaultdict[str, int] = defaultdict(int)
    for item in enriched:
        industry = normalize_industry(item["industry"])
        if industry_counts[industry] >= args.max_per_industry:
            continue
        selected.append(item)
        industry_counts[industry] += 1
        if len(selected) >= args.count:
            break
    if len(selected) < args.count:
        selected_codes = {item["code"] for item in selected}
        for item in enriched:
            if item["code"] in selected_codes:
                continue
            selected.append(item)
            if len(selected) >= args.count:
                break

    args.output.write_text(render(selected, len(financials), len(technical), failures, regime), encoding="utf-8")
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps({
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataThrough": market_date,
        "method": "v1.3-right-side-platform-breakout",
        "marketRegime": regime,
        "selected": selected,
        "allCandidates": enriched,
        "technicalCandidates": technical,
        "failures": failures,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"financials={len(financials)} technical={len(technical)} passed={len(enriched)} selected={len(selected)} failures={len(failures)}")
    print(f"wrote {args.output}")
    print(f"wrote {args.json_output}")
    for index, item in enumerate(selected, 1):
        print(index, item["code"], item["name"], item["industry"], item["daysAbove1530"], item["currentAbovePlatformPct"])


if __name__ == "__main__":
    main()
