#!/usr/bin/env python3
"""Screen current v2.5 strict triggers and recent-trigger right-edge samples.

The recent-trigger scan replays the strict technical conditions on the previous
three trading bars, then applies the current right-edge limits. Sector history
uses board-index persistence plus current breadth/leader as a documented proxy.
Outputs are research watchlists only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import statistics
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Any

from screen_latest_30 import (
    fetch_financials,
    fetch_kline,
    fetch_sector_snapshot,
    elasticity_metrics,
    get_json,
    get_json_with_curl,
    normalize_industry,
    rsi,
    sector_lookup,
    weekly,
)
from screen_red_zone_5 import (
    MARKET_POOL_DEFAULTS,
    align_quote_volume,
    apply_manual_reviews,
    consecutive_above_5_10,
    detect_secondary_confirmation,
    enrich_financial_risk,
    fetch_daily_quotes,
    truncate_rows_as_of,
    macd_metrics,
    moving_average,
    normalized_slope,
    sector_gate,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "v2.5当前严格与右沿观察池-2026-07-29.md"
DEFAULT_JSON = ROOT / "output" / "red-zone-v2.5-current-and-edge.json"


# Historical re-screen date set by --as-of; None means the live close.
AS_OF: str | None = None


def prepare_rows(code: str, quotes: dict[str, dict]) -> tuple[list[dict], str, float]:
    rows, source = fetch_kline(code)
    quote = quotes.get(code)
    volume_scale = 1.0
    if quote and rows[-1]["volume"] > 0 and quote["volume"] > 0:
        observed_ratio = quote["volume"] / rows[-1]["volume"]
        volume_scale = min(
            (0.01, 1.0, 100.0),
            key=lambda value: abs(math.log(max(observed_ratio, 1e-9) / value)),
        )
    if AS_OF:
        # The K-line volume unit (lots vs shares) is the same on past days, so keep the scale.
        return truncate_rows_as_of(rows, AS_OF), f"{source}（回溯至{AS_OF}）", volume_scale
    if quote and quote["date"] >= rows[-1]["date"]:
        aligned = align_quote_volume(rows, quote)
        if aligned["date"] == rows[-1]["date"]:
            rows[-1] = aligned
        else:
            rows.append(aligned)
        source += "+" + quote.get("source", "东方财富收盘快照")
    return rows, source, volume_scale


def calculate_metrics(rows: list[dict]) -> dict[str, Any] | None:
    if len(rows) < 260:
        return None
    closes = [float(row["close"]) for row in rows]
    close = closes[-1]
    ma = {period: moving_average(closes, period) for period in (5, 10, 15, 30)}
    above5 = close >= ma[5]
    above10 = close >= ma[10]
    ma5_previous = moving_average(closes, 5, len(closes) - 2)
    ma5_flattening = ma[5] >= ma5_previous * 0.995
    above_days = consecutive_above_5_10(closes) if above5 and above10 else 0

    recent60 = rows[-60:]
    low_offset = min(range(len(recent60)), key=lambda index: recent60[index]["low"])
    recent_low = float(recent60[low_offset]["low"])
    low_age = len(recent60) - 1 - low_offset
    rise_recent = (close / recent_low - 1) * 100
    year_low = min(float(row["low"]) for row in rows[-250:])
    rise_year = (close / year_low - 1) * 100
    changes = {
        period: (close / closes[-period - 1] - 1) * 100
        for period in (5, 10, 20)
    }
    max_daily_gain10 = max(
        (closes[index] / closes[index - 1] - 1) * 100
        for index in range(len(closes) - 9, len(closes))
    )

    range10 = (max(row["high"] for row in rows[-10:]) / min(row["low"] for row in rows[-10:]) - 1) * 100
    range20 = (max(row["high"] for row in rows[-20:]) / min(row["low"] for row in rows[-20:]) - 1) * 100
    close_range10 = (max(closes[-10:]) / min(closes[-10:]) - 1) * 100
    low_touch_count15 = sum(1 for row in rows[-15:] if row["low"] <= recent_low * 1.04)
    close_slope10 = normalized_slope(closes[-10:])
    floor_drift = (
        min(row["low"] for row in rows[-5:])
        / min(row["low"] for row in rows[-15:-5])
        - 1
    ) * 100
    secondary = detect_secondary_confirmation(rows)
    platform_evidence = {
        "lowMatured": low_age >= 7,
        "intradayRangeContracted": range10 <= 12.5,
        "closeRangeContracted": close_range10 <= 8.0,
        "repeatedFloorTouches": low_touch_count15 >= 3,
        "closeSlopeStable": close_slope10 >= -0.30,
        "floorNotDriftingDown": floor_drift >= -2.5,
    }
    platform_count = sum(platform_evidence.values())
    stable_platform = platform_count >= 5 and range10 <= 14 and close_range10 <= 9
    secondary_with_context = (
        secondary["confirmed"]
        and platform_count >= 3
        and close_range10 <= 10
        and close_slope10 >= -0.70
    )

    daily_rsi_series = rsi(closes)
    weekly_rows = weekly(rows)
    weekly_closes = [float(row["close"]) for row in weekly_rows]
    weekly_rsi_series = rsi(weekly_closes)
    daily_rsi = daily_rsi_series[-1]
    weekly_rsi = weekly_rsi_series[-1]
    if daily_rsi is None or weekly_rsi is None:
        return None
    daily_macd = macd_metrics(closes)
    weekly_macd = macd_metrics(weekly_closes)

    volume5 = sum(row["volume"] for row in rows[-5:]) / 5
    volume20 = sum(row["volume"] for row in rows[-20:]) / 20
    median_volume5 = statistics.median(row["volume"] for row in rows[-5:])
    median_volume20 = statistics.median(row["volume"] for row in rows[-20:])
    volume_ratio = volume5 / max(volume20, 1)
    median_volume_ratio = median_volume5 / max(median_volume20, 1)
    latest = rows[-1]
    latest_range = max(latest["high"] - latest["low"], 1e-9)
    upper_wick = latest["high"] - max(latest["open"], latest["close"])
    probe_volume = (
        latest["close"] >= latest["open"]
        and latest["volume"] / max(volume20, 1) <= 1.35
        and upper_wick / latest_range <= 0.45
    )

    daily_direction = daily_rsi_series[-4] is not None and daily_rsi >= daily_rsi_series[-4]
    weekly_direction = weekly_rsi_series[-4] is not None and weekly_rsi >= weekly_rsi_series[-4]
    return {
        "date": rows[-1]["date"],
        "close": close,
        "ma": ma,
        "above5": above5,
        "above10": above10,
        "aboveDays": above_days,
        "ma5Flattening": ma5_flattening,
        "lowAge": low_age,
        "riseRecentLow": rise_recent,
        "riseYearLow": rise_year,
        "change5": changes[5],
        "change10": changes[10],
        "change20": changes[20],
        "maxDailyGain10": max_daily_gain10,
        "range10": range10,
        "range20": range20,
        "closeRange10": close_range10,
        "lowTouchCount15": low_touch_count15,
        "closeSlope10": close_slope10,
        "floorDrift": floor_drift,
        "platformEvidence": platform_evidence,
        "platformEvidenceCount": platform_count,
        "stablePlatform": stable_platform,
        "secondaryWithContext": secondary_with_context,
        "secondaryConfirmation": secondary,
        "dailyRsi": daily_rsi,
        "weeklyRsi": weekly_rsi,
        "dailyRsiDirection": daily_direction,
        "weeklyRsiDirection": weekly_direction,
        "dailyMacd": daily_macd,
        "weeklyMacd": weekly_macd,
        "volumeRatio5v20": volume_ratio,
        "medianVolumeRatio5v20": median_volume_ratio,
        "probeVolume": probe_volume,
        "volumeState": "明显收缩" if volume_ratio <= 0.8 and median_volume_ratio <= 0.9 else "温和收缩",
    }


def strict_technical(metrics: dict[str, Any]) -> bool:
    ma = metrics["ma"]
    close = metrics["close"]
    ma5_ok = metrics["above5"] or (close >= ma[5] * 0.99 and metrics["ma5Flattening"])
    ma_stage = (
        ma5_ok
        and metrics["aboveDays"] <= 5
        and close < ma[30]
        and close <= ma[15] * 1.01
    )
    structure = (
        metrics["range10"] <= 14
        and metrics["range20"] <= 28
        and (metrics["stablePlatform"] or metrics["secondaryWithContext"])
    )
    volume = metrics["volumeRatio5v20"] <= 1 and (
        metrics["medianVolumeRatio5v20"] <= 1.05 or metrics["probeVolume"]
    )
    daily_macd = metrics["dailyMacd"]
    weekly_macd = metrics["weeklyMacd"]
    macd = (
        daily_macd["state"] in {"水下刚金叉", "水下接近金叉", "水下低位拐头"}
        and weekly_macd["underwater"]
        and (weekly_macd["improving"] or daily_macd["state"] == "水下刚金叉")
    )
    return all((
        5 <= metrics["lowAge"] <= 35,
        metrics["riseRecentLow"] <= 8,
        metrics["riseYearLow"] <= 15,
        metrics["change5"] <= 5,
        metrics["change10"] <= 6,
        metrics["change20"] <= 7,
        metrics["maxDailyGain10"] < 7.5,
        ma_stage,
        structure,
        volume,
        28 <= metrics["dailyRsi"] <= 45,
        24 <= metrics["weeklyRsi"] <= 42,
        macd,
    ))


def right_edge_current(metrics: dict[str, Any]) -> bool:
    ma = metrics["ma"]
    close = metrics["close"]
    structure = metrics["stablePlatform"] or metrics["secondaryWithContext"]
    volume = metrics["volumeRatio5v20"] <= 1 and (
        metrics["medianVolumeRatio5v20"] <= 1.05 or metrics["probeVolume"]
    )
    direction = (
        metrics["dailyRsiDirection"]
        or metrics["weeklyRsiDirection"]
        or metrics["weeklyMacd"]["improving"]
    )
    return all((
        5 <= metrics["lowAge"] <= 38,
        metrics["riseRecentLow"] <= 10,
        metrics["riseYearLow"] <= 15,
        metrics["change5"] <= 8,
        metrics["change10"] <= 10,
        metrics["change20"] <= 12,
        metrics["maxDailyGain10"] < 7.5,
        close >= ma[5] * 0.99,
        close <= ma[15] * 1.03,
        close <= ma[30] * 1.02,
        structure,
        volume,
        28 <= metrics["dailyRsi"] <= 50,
        24 <= metrics["weeklyRsi"] <= 45,
        metrics["dailyMacd"]["underwater"],
        metrics["dailyMacd"]["improving"],
        metrics["weeklyMacd"]["underwater"],
        direction,
    ))


def fetch_board_members(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    board_code = str(snapshot.get("f12") or "")
    if not board_code:
        return []
    members = []
    for page in range(1, 6):
        params = {
            "pn": page,
            "pz": 500,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": f"b:{board_code}",
            "fields": "f12,f14",
        }
        url = "https://push2delay.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
        payload = None
        for attempt in range(3):
            try:
                payload = get_json(url) if attempt == 0 else get_json_with_curl(url)
                break
            except Exception:
                continue
        rows = (((payload or {}).get("data") or {}).get("diff") or [])
        for row in rows:
            code = str(row.get("f12") or "")
            name = str(row.get("f14") or "")
            if len(code) != 6 or code[0] not in "036" or "ST" in name.upper() or "退" in name:
                continue
            members.append({"code": code, "name": name})
        if len(rows) < 500:
            break
    unique = {item["code"]: item for item in members}
    return list(unique.values())


def fetch_tencent_recent_returns(code: str) -> dict[str, float]:
    symbol = ("sh" if code.startswith("6") else "sz") + code
    end_date = dt.date.today().isoformat()
    url = (
        "https://ifzq.gtimg.cn/appstock/app/fqkline/get?"
        f"param={symbol},day,2026-07-01,{end_date},40,qfq"
    )
    node = (get_json(url, timeout=15).get("data") or {}).get(symbol) or {}
    source = node.get("qfqday") or node.get("day") or []
    closes = [(str(row[0]), float(row[2])) for row in source if len(row) >= 3]
    quote = ((node.get("qt") or {}).get(symbol) or [])
    if len(quote) > 30 and quote[3] not in (None, ""):
        quote_date = str(quote[30])[:8]
        if len(quote_date) == 8:
            quote_date = f"{quote_date[:4]}-{quote_date[4:6]}-{quote_date[6:8]}"
            quote_close = float(quote[3])
            if not closes or quote_date > closes[-1][0]:
                closes.append((quote_date, quote_close))
            elif quote_date == closes[-1][0]:
                closes[-1] = (quote_date, quote_close)
    returns = {}
    for index in range(1, len(closes)):
        previous = closes[index - 1][1]
        if previous > 0:
            returns[closes[index][0]] = (closes[index][1] / previous - 1) * 100
    return returns


def build_tencent_sector_persistence(
    items: list[dict[str, Any]],
    snapshots: dict[str, dict],
    workers: int,
) -> dict[str, dict[str, Any]]:
    board_members: dict[str, list[dict[str, str]]] = {}
    member_names: dict[str, str] = {}
    for item in items:
        snapshot = sector_lookup(item["industry"], snapshots)
        if not snapshot:
            continue
        board_code = str(snapshot.get("f12") or "")
        if board_code not in board_members:
            board_members[board_code] = fetch_board_members(snapshot)
        for member in board_members[board_code]:
            member_names[member["code"]] = member["name"]

    returns_by_code: dict[str, dict[str, float]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, 24)) as executor:
        futures = {
            executor.submit(fetch_tencent_recent_returns, code): code
            for code in member_names
        }
        for future in concurrent.futures.as_completed(futures):
            code = futures[future]
            try:
                returns_by_code[code] = future.result()
            except Exception:
                continue

    results = {}
    for board_code, members in board_members.items():
        all_dates = sorted({
            date
            for member in members
            for date in returns_by_code.get(member["code"], {})
        })[-5:]
        daily = []
        for date in all_dates:
            values = [
                (member, returns_by_code.get(member["code"], {}).get(date))
                for member in members
            ]
            valid = [(member, value) for member, value in values if value is not None]
            if not valid:
                continue
            rising = sum(1 for _, value in valid if value > 0)
            breadth = rising / len(valid)
            leaders = sorted(valid, key=lambda pair: pair[1], reverse=True)[:5]
            leader_member, leader_gain = leaders[0]
            daily.append({
                "date": date,
                "valid": len(valid),
                "rising": rising,
                "breadth": breadth,
                "leaderCode": leader_member["code"],
                "leader": leader_member["name"],
                "leaderGain": leader_gain,
                "leaders": [
                    {"code": member["code"], "name": member["name"], "gain": gain}
                    for member, gain in leaders
                ],
            })
        results[board_code] = {
            "available": len(daily) >= 3,
            "boardCode": board_code,
            "memberCount": len(members),
            "coveredMembers": sum(1 for member in members if member["code"] in returns_by_code),
            "daily": daily,
            "source": "腾讯前复权日K重建板块宽度",
        }
    return results


def candidate_sector_persistence(base: dict[str, Any], candidate_code: str) -> dict[str, Any]:
    daily = []
    for row in base.get("daily", []):
        other = next(
            (leader for leader in row.get("leaders", []) if leader["code"] != candidate_code),
            None,
        )
        active = bool(other) and row["breadth"] >= 0.55 and other["gain"] >= 3
        daily.append({
            **{key: value for key, value in row.items() if key != "leaders"},
            "otherLeaderCode": other["code"] if other else None,
            "otherLeader": other["name"] if other else None,
            "otherLeaderGain": other["gain"] if other else None,
            "active": active,
        })
    active_days = sum(1 for row in daily if row["active"])
    return {
        **{key: value for key, value in base.items() if key != "daily"},
        "daily": daily,
        "activeDays5": active_days,
        "proxyPass": bool(base.get("available")) and active_days >= 2,
    }


def current_sector_observation(
    industry: str,
    candidate_code: str,
    snapshots: dict[str, dict],
    quotes: dict[str, dict],
) -> dict[str, Any]:
    snapshot = sector_lookup(industry, snapshots)
    if not snapshot:
        return {
            "leader": "待确认",
            "leaderCode": "",
            "leaderGain": None,
            "boardChange": None,
            "breadth": None,
            "leaderAmount": None,
            "leaderMarketCap": None,
            "leaderQuality": "待确认",
            "currentPass": False,
            "observation": "行业快照缺失",
        }

    leader_name = str(snapshot.get("f128") or "待确认")
    leader_code = str(snapshot.get("f140") or "")
    leader_gain = float(snapshot.get("f136") or 0)
    board_change = float(snapshot.get("f3") or 0)
    rising = int(snapshot.get("f104") or 0)
    falling = int(snapshot.get("f105") or 0)
    breadth = rising / max(rising + falling, 1)
    leader_quote = quotes.get(leader_code, {})
    leader_amount = float(leader_quote.get("amount") or 0)
    leader_market_cap = float(leader_quote.get("totalMarketCap") or 0)
    broad_activation = board_change >= 0.8 and breadth >= 0.65
    liquid_leader = leader_amount >= 100_000_000 and leader_market_cap >= 3_000_000_000
    current_pass = sector_gate(industry, candidate_code, snapshots, quotes, True) is not None

    reasons = []
    if leader_code == candidate_code:
        reasons.append("领涨股为候选自身")
    if leader_gain < 3:
        reasons.append("另一只领涨股涨幅不足3%")
    if board_change <= 0 and breadth < 0.55:
        reasons.append("当日板块宽度不足")
    if not (liquid_leader or broad_activation):
        reasons.append("领涨质量或多股扩散待确认")
    return {
        "leader": leader_name,
        "leaderCode": leader_code,
        "leaderGain": leader_gain,
        "boardChange": board_change,
        "breadth": breadth,
        "leaderAmount": leader_amount,
        "leaderMarketCap": leader_market_cap,
        "leaderQuality": "满足" if liquid_leader or broad_activation else "观察",
        "currentPass": current_pass,
        "observation": "当日启动满足" if current_pass else "；".join(reasons) or "行业启动待确认",
    }


def sector_observation_status(sector: dict[str, Any], persistence: dict[str, Any]) -> str:
    if persistence.get("proxyPass") and sector.get("currentPass"):
        return "满足"
    if persistence.get("proxyPass"):
        return "观察（近5日已启动、今日分歧）"
    if sector.get("currentPass"):
        return "观察（今日启动、持续性不足）"
    if persistence.get("available"):
        return "观察（板块启动待确认）"
    return "数据待复核"


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

    current = calculate_metrics(rows)
    if current is None:
        return None
    trigger_dates = []
    for offset in (1, 2, 3):
        historical = calculate_metrics(rows[:-offset])
        if historical is not None and strict_technical(historical):
            trigger_dates.append(historical["date"])
    current_strict = strict_technical(current)
    state = "当前严格" if current_strict else "红框右沿" if trigger_dates and right_edge_current(current) else None
    if state is None:
        return None
    return {
        "code": code,
        "name": str(financial.get("SECURITY_NAME_ABBR") or code),
        "industry": str(financial.get("BOARD_NAME") or "待分类"),
        "model": "v2.5红框",
        "date": current["date"],
        "source": source,
        "state": state,
        "triggerDates": trigger_dates,
        "latestTriggerDate": current["date"] if current_strict else max(trigger_dates),
        "profit": float(financial.get("PARENT_NETPROFIT") or 0),
        "profitPeriod": str(financial.get("QDATE") or financial.get("REPORTDATE") or ""),
        "market": {
            "totalMarketCap": total_cap,
            "floatMarketCap": float_cap,
            "averageAmount20": average_amount20,
            "pool": "核心市值池",
        },
        **{key: value for key, value in current.items() if key != "ma"},
        "maDistance15": (current["close"] / current["ma"][15] - 1) * 100,
        "maDistance30": (current["close"] / current["ma"][30] - 1) * 100,
        "elasticity": elasticity_metrics(code, rows),
        "sparkline": rows[-90:],
        "manualReviewVerdict": "待人工复核",
        "conditionChecks": {
            "technicalState": state,
            "sector": "待复核",
            "financial": "待复核",
            "risk": "待复核",
        },
    }


def render(selected: list[dict], scanned: int, technical_count: int, failures: list[dict]) -> str:
    lines = [
        "# v2.5 当前严格与红框右沿观察池",
        "",
        f"生成时间：{dt.datetime.now().astimezone().isoformat(timespec='seconds')}。",
        f"行情数据日：{max((item['date'] for item in selected), default='无')}。",
        "",
        "本轮按 v2.5 扫描当前严格触发和最近三日严格技术触发后的红框右沿，不采用任意失败项放宽，不计算评分，不提供交易指令。",
        "",
        f"扫描盈利A股 {scanned} 只，技术时序候选 {technical_count} 只，完成财务和公告开关后保留 {len(selected)} 只；行业仅作观察，不参与淘汰。",
        "",
        "## 候选汇总",
        "",
        "| 股票 | 当前状态 | 最近触发日 | 行业 | 距低点 | 5/10/20日 | 日/周RSI | MA15/30距离 | 量能5/20 | 行业5日扩散 | 财务 | 风险 | 人工图形 |",
        "| --- | --- | --- | --- | ---: | --- | --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for item in selected:
        persistence = item["sectorPersistence"]
        sector_text = item["conditionChecks"]["sector"]
        lines.append(
            f"| {item['name']} {item['code']} | {item['state']} | {item['latestTriggerDate']} | {item['industry']} | "
            f"{item['riseRecentLow']:.1f}% | {item['change5']:+.1f}%/{item['change10']:+.1f}%/{item['change20']:+.1f}% | "
            f"{item['dailyRsi']:.1f}/{item['weeklyRsi']:.1f} | {item['maDistance15']:+.1f}%/{item['maDistance30']:+.1f}% | "
            f"{item['volumeRatio5v20']:.2f} | {sector_text} | {item['financialState']} | {item['riskState']} | {item['manualReviewVerdict']} |"
        )
    lines.extend(["", "## 逐只依据", ""])
    for item in selected:
        sector = item["sector"]
        persistence = item["sectorPersistence"]
        financial = item["financialRisk"]
        leader_gain_text = (
            f"{sector['leaderGain']:+.2f}%"
            if sector.get("leaderGain") is not None
            else "待核"
        )
        breadth_text = (
            f"{sector['breadth']:.0%}"
            if sector.get("breadth") is not None
            else "待核"
        )
        persistence_text = (
            f"腾讯成分股K线显示近5日有 {persistence['activeDays5']} 日同时满足上涨宽度和另一只领涨股"
            if persistence.get("available")
            else "腾讯成分股K线覆盖不足，持续性待补数据"
        )
        lines.extend([
            f"### {item['name']} {item['code']}",
            "",
            f"- 状态：{item['state']}；最近技术严格触发日 {item['latestTriggerDate']}，当前距低点 {item['riseRecentLow']:.1f}%。",
            f"- 指标：日/周 RSI {item['dailyRsi']:.1f}/{item['weeklyRsi']:.1f}；日 MACD {item['dailyMacd']['state']}；MA15/MA30 距离 {item['maDistance15']:+.1f}%/{item['maDistance30']:+.1f}%。",
            f"- 结构量能：平台证据 {item['platformEvidenceCount']}/6；10日振幅 {item['range10']:.1f}%；5/20日均量比 {item['volumeRatio5v20']:.2f}。",
            f"- 行业观察（{item['conditionChecks']['sector']}）：当前领涨股 {sector['leader']} "
            f"{leader_gain_text}；板块上涨宽度 {breadth_text}；{persistence_text}。",
            f"- 财务（{item['financialState']}）：{'；'.join(financial['financialNotes'])}。",
            f"- 风险（{item['riskState']}）：{'；'.join(financial['riskNotes'])}。",
            "- 观察项：人工核对是否仍映射在红框或右沿、是否存在单阳反抽、上影抛压、平台破坏以及概念板块是否同步扩散。",
            "",
        ])
    lines.extend([
        "## 数据边界",
        "",
        "- 最近三日触发和板块成员最近5日涨跌均使用腾讯前复权日K；行业成分名单、财务和公告使用东方财富。",
        "- 行业启动仅作观察，不淘汰技术候选；最近5日扩散、当日宽度和另一只领涨股用于描述板块状态。",
        "- 行业持续性观察口径为最近5个交易日中至少2日同时满足上涨宽度>=55%，并存在另一只涨幅>=3%的股票。",
        "- 概念板块持续性尚未自动加入，本轮使用行业路径；人工看图和概念复核仍具有否决权。",
        "- 公告风险来自最近100条公告标题扫描，命中或未命中都不能替代公告正文核验。",
        f"- 行情或接口失败 {len(failures)} 只，不把数据失败视为条件不满足。",
        "- 若最终不足10只，表示当前条件下没有足够样本，不降低条件补足。",
    ])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--max-per-industry", type=int, default=2)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--manual-review", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    financials = fetch_financials()
    quotes = fetch_daily_quotes(financials.keys())
    market_date = max((quote["date"] for quote in quotes.values()), default="")
    snapshots = fetch_sector_snapshot()
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

    sector_observed = []
    for item in technical:
        snapshot = sector_lookup(item["industry"], snapshots)
        item["sector"] = current_sector_observation(
            item["industry"], item["code"], snapshots, quotes
        )
        item["sectorBoardCode"] = str((snapshot or {}).get("f12") or "")
        sector_observed.append(item)

    persistence_by_board = build_tencent_sector_persistence(sector_observed, snapshots, args.workers)
    for item in sector_observed:
        base = persistence_by_board.get(item["sectorBoardCode"])
        persistence = candidate_sector_persistence(base or {}, item["code"])
        item["sectorPersistence"] = persistence
        item["conditionChecks"]["sector"] = sector_observation_status(
            item["sector"], persistence
        )

    enriched = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, 12)) as executor:
        futures = {executor.submit(enrich_financial_risk, item): item for item in sector_observed}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
                if result["financialState"] != "不通过" and result["riskState"] != "不通过":
                    enriched.append(result)
            except Exception as exc:
                failures.append({"code": item["code"], "error": f"financial-risk: {exc}"})

    state_order = {"当前严格": 0, "红框右沿": 1}
    apply_manual_reviews(enriched, args.manual_review)
    enriched.sort(key=lambda item: (state_order[item["state"]], normalize_industry(item["industry"]), item["code"]))
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

    args.output.write_text(render(selected, len(financials), len(technical), failures), encoding="utf-8")
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps({
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataThrough": market_date,
        "method": "v2.5-current-strict-and-previous-3-bars-right-edge",
        "selected": selected,
        "allCandidates": enriched,
        "technicalCandidates": technical,
        "failures": failures,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"financials={len(financials)} technical={len(technical)} financialRiskPassed={len(enriched)} selected={len(selected)} failures={len(failures)}")
    print(f"wrote {args.output}")
    print(f"wrote {args.json_output}")
    for index, item in enumerate(selected, 1):
        print(index, item["code"], item["name"], item["state"], item["industry"], item["latestTriggerDate"])


if __name__ == "__main__":
    main()
