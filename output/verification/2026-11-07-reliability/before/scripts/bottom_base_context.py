"""Observation-only context for the bottom-base models.

None of these values filter stocks. They come from the 2026-10-18 backtest
(`底部横盘回测结论-2026-10-18.md`): the market regime and a stock's elasticity
changed outcomes more than any screening condition, but not stably enough to
become hard rules yet.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import statistics
import subprocess
from pathlib import Path
from typing import Any

import urllib.parse

from screen_latest_30 import fetch_kline, get_json

TRACK_DAYS = 40
# v1.3 right-side starts are judged over 20 trading days with no stop: in 2023-07~2024-12,
# 2025-01~2026-06 and 2026-07+ that beat 40 days + stop, which gave back the gains later
# (横盘选股最优方案-2026-10-22.md).
TRACK_DAYS_V13 = 20
# Close below 95% of the pre-selection 60-day low = base broken. A 2% buffer stopped out
# 60% of backtest signals, many of which later recovered; 5% kept more of the rebounds.
STOP_BUFFER = 0.95
LAUNCH_GAIN = 0.15
BIG_UP_DAY = 0.095


def bottom_base_profile(rows: list[dict]) -> dict[str, Any]:
    """Elasticity of a base at rows[-1]: deeper, more volatile, livelier bases launch more often."""
    i = len(rows) - 1
    window = rows[-250:]
    gains = [rows[k]["close"] / rows[k - 1]["close"] - 1 for k in range(max(1, i - 249), i + 1)]
    up_vol = sum(rows[k]["volume"] for k in range(i - 19, i + 1) if rows[k]["close"] > rows[k - 1]["close"])
    down_vol = sum(rows[k]["volume"] for k in range(i - 19, i + 1) if rows[k]["close"] < rows[k - 1]["close"])
    profile = {
        "drawdown250": rows[i]["close"] / max(r["high"] for r in window) - 1,
        "atr20": statistics.mean((rows[k]["high"] - rows[k]["low"]) / rows[k - 1]["close"] for k in range(i - 19, i + 1)),
        "bigUpDays250": sum(1 for g in gains if g >= BIG_UP_DAY),
        "upDownVol20": up_vol / down_vol if down_vol else 9.0,
    }
    profile["elastic"] = (
        profile["drawdown250"] <= -0.40 and profile["atr20"] >= 0.03 and profile["bigUpDays250"] >= 2
    )
    return profile


def profile_text(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "底部弹性 待核"
    tag = "高弹性" if profile.get("elastic") else "普通"
    return (
        f"底部弹性 {tag}｜回撤{profile['drawdown250']:+.0%}｜振幅{profile['atr20']:.1%}｜"
        f"大涨{profile['bigUpDays250']}次｜涨/跌量{profile['upDownVol20']:.2f}"
    )


# Literature composite: every feature has a direction fixed by A-share studies, not fitted
# (Liu, Stambaugh & Yuan 2019 size/value/abnormal turnover; MAX/lottery and short-term
# reversal studies). Score = mean percentile against 2023-07~2024-12 v2.6 signals
# (config/literature_score_reference.json). Within the v2.6 pool, ranking by
# fundamentals tier then this score beat the old big-up-days/low-price order in all three
# backtest periods (横盘选股最优方案-2026-10-22.md). +1 = higher is better.
LITERATURE_SIGNS = {
    "abnTurn": -1, "turn20": -1, "ret20": -1, "max20": -1, "vol60": -1, "logCap": -1,
    "ep": +1, "profitYoy": +1, "fundTier0": +1, "holderChg": -1, "price": -1,
}
REFERENCE_PATH = Path(__file__).resolve().parents[1] / "config" / "literature_score_reference.json"


def literature_features(rows: list[dict], volume_scale: float, total_cap: float, float_cap: float,
                        trend: dict[str, Any] | None) -> dict[str, float]:
    """Features at rows[-1]. Volume times volume_scale must be shares; caps in yuan."""
    closes = [row["close"] for row in rows]
    volumes = [row["volume"] * volume_scale for row in rows]
    rets = [closes[k] / closes[k - 1] - 1 for k in range(len(closes) - 60, len(closes))]
    features: dict[str, Any] = {
        "abnTurn": (sum(volumes[-20:]) / 20) / max(sum(volumes[-250:]) / 250, 1),
        "ret20": closes[-1] / closes[-21] - 1,
        "max20": max(rets[-20:]),
        "vol60": statistics.pstdev(rets),
        "price": closes[-1],
    }
    if total_cap > 0:
        features["logCap"] = math.log(total_cap)
    if float_cap > 0:
        features["turn20"] = (sum(volumes[-20:]) / 20) / (float_cap / closes[-1])
    return add_trend_features(features, trend, total_cap) if trend is not None else _finite(features)


def add_trend_features(features: dict[str, float], trend: dict[str, Any] | None, total_cap: float) -> dict[str, float]:
    """The fundamentals half of literature_features, for screens that fetch fundamentals later."""
    trend = trend or {}
    out: dict[str, Any] = {
        **features,
        "fundTier0": 1.0 if trend.get("tier") == 0 else 0.0,
        "profitYoy": trend.get("profitYoy"),
        "holderChg": trend.get("holderChange"),
    }
    if total_cap > 0 and isinstance(trend.get("ttmProfit"), (int, float)):
        out["ep"] = trend["ttmProfit"] / total_cap
    return _finite(out)


def _finite(features: dict[str, Any]) -> dict[str, float]:
    return {k: float(v) for k, v in features.items() if isinstance(v, (int, float)) and math.isfinite(v)}


_REFERENCE: dict[str, list[float]] | None = None


def literature_score(features: dict[str, float]) -> float:
    """Mean percentile (0-1, higher = better per literature) against the reference sample."""
    global _REFERENCE
    if _REFERENCE is None:
        _REFERENCE = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["quantiles"]
    parts = []
    for name, sign in LITERATURE_SIGNS.items():
        quantiles = _REFERENCE.get(name)
        if name in features and quantiles:
            parts.append(bisect.bisect_left(quantiles, features[name] * sign) / len(quantiles))
    return statistics.mean(parts) if parts else 0.5


DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get?"


def _latest_rows(report: str, columns: str, code: str, period_field: str, notice_field: str, as_of: str) -> list[dict]:
    params = {
        "reportName": report, "columns": columns, "pageSize": 12, "pageNumber": 1,
        "filter": f'(SECURITY_CODE="{code}")', "sortColumns": period_field, "sortTypes": -1,
    }
    rows = (get_json(DATACENTER + urllib.parse.urlencode(params)).get("result") or {}).get("data") or []
    # Point-in-time: only reports announced before the screen date; first announcement per period.
    by_period: dict[str, dict] = {}
    for row in rows:
        notice = str(row.get(notice_field) or "")[:10]
        period = str(row.get(period_field) or "")[:10]
        if notice and notice < as_of and (period not in by_period or notice < by_period[period]["_notice"]):
            by_period[period] = {**row, "_notice": notice}
    return [by_period[p] for p in sorted(by_period, reverse=True)]


def fundamental_trend(code: str, as_of: str) -> dict[str, Any]:
    """Earnings and holder-count direction. In the backtest (2023-07 ~ 2026-08), within the
    same month, earnings inflection and two straight holder-count declines beat other
    bottom-base signals in both halves (底部横盘深入学习)."""
    reports = _latest_rows("RPT_LICO_FN_CPD", "REPORTDATE,NOTICE_DATE,SJLTZ,YSTZ,PARENT_NETPROFIT", code, "REPORTDATE", "NOTICE_DATE", as_of)
    holders = _latest_rows("RPT_HOLDERNUM_DET", "END_DATE,HOLD_NOTICE_DATE,HOLDER_NUM_RATIO", code, "END_DATE", "HOLD_NOTICE_DATE", as_of)

    def number(row: dict | None, field: str) -> float | None:
        value = (row or {}).get(field)
        return float(value) if isinstance(value, (int, float)) else None

    yoy, yoy_prev = number(reports[0] if reports else None, "SJLTZ"), number(reports[1] if len(reports) > 1 else None, "SJLTZ")
    h, h_prev = number(holders[0] if holders else None, "HOLDER_NUM_RATIO"), number(holders[1] if len(holders) > 1 else None, "HOLDER_NUM_RATIO")
    inflection = yoy is not None and yoy_prev is not None and yoy > 0 >= yoy_prev
    improving = yoy is not None and yoy_prev is not None and yoy > yoy_prev
    holders_down2 = h is not None and h_prev is not None and h < 0 and h_prev < 0
    # Trailing-12-month profit from year-to-date figures: latest + last annual - last year's same period.
    profit_by_period = {str(r.get("REPORTDATE") or "")[:10]: number(r, "PARENT_NETPROFIT") for r in reports}
    ttm = None
    if reports:
        period = str(reports[0].get("REPORTDATE") or "")[:10]
        latest = profit_by_period.get(period)
        if latest is not None and period.endswith("12-31"):
            ttm = latest
        elif latest is not None and period:
            annual = profit_by_period.get(f"{int(period[:4]) - 1}-12-31")
            same = profit_by_period.get(f"{int(period[:4]) - 1}{period[4:]}")
            if annual is not None and same is not None:
                ttm = latest + annual - same
    return {
        "ttmProfit": ttm,
        "reportDate": str((reports[0] if reports else {}).get("REPORTDATE") or "")[:10],
        "profitYoy": yoy,
        "profitYoyPrev": yoy_prev,
        "revenueYoy": number(reports[0] if reports else None, "YSTZ"),
        "holderChange": h,
        "holderChangePrev": h_prev,
        "inflection": inflection,
        "improving": improving,
        "holdersDown2": holders_down2,
        # Ordering tier only, not a score: 0 = inflection or (improving and holders concentrating).
        "tier": 0 if inflection or (improving and holders_down2) else 1 if improving or holders_down2 else 2,
    }


def trend_text(trend: dict[str, Any] | None) -> str:
    if not trend:
        return "基本面趋势 待核"

    def pct(value: float | None) -> str:
        return "待核" if value is None else f"{value:+.0f}%"

    tags = [tag for tag, on in (("净利拐点", trend.get("inflection")), ("净利改善", trend.get("improving") and not trend.get("inflection")),
                                ("户数连降", trend.get("holdersDown2"))) if on]
    return (
        f"基本面趋势 {'/'.join(tags) or '无'}｜净利同比 {pct(trend.get('profitYoyPrev'))}→{pct(trend.get('profitYoy'))}"
        f"（{trend.get('reportDate') or '待核'}）｜股东户数 {pct(trend.get('holderChangePrev'))}→{pct(trend.get('holderChange'))}"
    )


# Primary switch: CSI 1000 vs MA120. Secondary reference: Shanghai Composite vs MA60.
# Chosen in 大盘环境指标对比-2026-10-18: long averages on broad indexes separated good and bad
# periods for both models; MA20 was too noisy.
REGIME_PRIMARY = ("中证1000", "sh000852", 120)
REGIME_SECONDARY = ("上证指数", "sh000001", 60)


def fetch_index(symbol: str) -> list[tuple[str, float]]:
    urls = (
        "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData?"
        f"symbol={symbol}&scale=240&ma=no&datalen=300",
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,300,qfq",
    )
    for url in urls:
        try:
            raw = subprocess.run(["curl", "-sS", "-m", "20", url], check=True, capture_output=True, text=True).stdout
            payload = json.loads(raw)
            if "result" in payload:
                return [(row["day"], float(row["close"])) for row in payload["result"]["data"]]
            node = payload["data"][symbol]
            return [(row[0], float(row[2])) for row in node.get("qfqday") or node.get("day")]
        except Exception:
            continue
    return []


def index_state(name: str, symbol: str, ma: int, as_of: str) -> dict[str, Any] | None:
    closes = [close for date, close in fetch_index(symbol) if date <= as_of]
    if len(closes) < ma + 1:
        return None
    average = statistics.mean(closes[-ma:])
    return {
        "name": name,
        "ma": ma,
        "close": closes[-1],
        "average": average,
        "above": closes[-1] > average,
        "distance": closes[-1] / average - 1,
        "change20": closes[-1] / closes[-21] - 1,
    }


def market_regime(as_of: str) -> dict[str, Any]:
    primary = index_state(*REGIME_PRIMARY, as_of)
    if primary is None:
        return {"available": False, "label": "大盘环境待核", "note": "中证1000行情获取失败"}
    secondary = index_state(*REGIME_SECONDARY, as_of)

    def describe(state: dict[str, Any]) -> str:
        side = "上方" if state["above"] else "下方"
        return f"{state['name']} 在MA{state['ma']}{side}（{state['distance']:+.1%}）｜近20日{state['change20']:+.1%}"

    weak = not primary["above"]
    # Display only. On the rebuilt data the MA120 state did not separate good and bad periods
    # consistently (CSI 1000 was below MA120 on 80% / 13% / 95% of days across the three
    # backtest periods), so it is no longer a "可关注/谨慎" switch.
    note = "中证1000在MA120下方" if weak else "中证1000在MA120上方"
    if secondary and secondary["above"] == primary["above"]:
        note += "；上证指数MA60同向"
    elif secondary:
        note += "；上证指数MA60方向相反，信号偏弱"
    return {
        "available": True,
        "weak": weak,
        "primary": primary,
        "secondary": secondary,
        "label": describe(primary) + (f"；参考 {describe(secondary)}" if secondary else ""),
        "note": note,
    }


_REPORT_CACHE: dict[str, list[dict]] = {}


def report_due(code: str, as_of: str, days: int = 3) -> dict[str, Any] | None:
    """Scheduled disclosure within `days` calendar days of `as_of`, or None.

    Label only. On 2023-07~2026-06 v1.3 signals, selecting a stock 0-3 days before its
    report went out was 1.7% worse at 20 days in the later period but 2.2% better at 10
    days, so no rule was applied (output/backtest/earnings-decline.md). 爱施德 002416
    (picked 2026-10-16, Q3 out 2026-10-17, -19.5% in two weeks) is why the label exists."""
    if code not in _REPORT_CACHE:
        params = {
            "reportName": "RPT_PUBLIC_BS_APPOIN",
            "columns": "REPORT_DATE,APPOINT_PUBLISH_DATE,ACTUAL_PUBLISH_DATE",
            "filter": f'(SECURITY_CODE="{code}")',
            "pageSize": 8, "sortColumns": "APPOINT_PUBLISH_DATE", "sortTypes": -1,
        }
        try:
            _REPORT_CACHE[code] = (get_json(DATACENTER + urllib.parse.urlencode(params)).get("result") or {}).get("data") or []
        except Exception:
            _REPORT_CACHE[code] = []
    today = as_of[:10]
    limit = (dt.date.fromisoformat(today) + dt.timedelta(days=days)).isoformat()
    for row in _REPORT_CACHE[code]:
        appointed = str(row.get("APPOINT_PUBLISH_DATE") or "")[:10]
        # Point in time: what was known on `as_of` is the appointed date; if it was in fact
        # published later, the appointment had still been standing on that day.
        if appointed and today <= appointed <= limit:
            return {"date": appointed, "period": str(row.get("REPORT_DATE") or "")[:10],
                    "days": dt.date.fromisoformat(appointed).toordinal() - dt.date.fromisoformat(today).toordinal()}
    return None


def extend_to(code: str, rows: list[dict], as_of: str) -> list[dict]:
    """Append missing bars up to `as_of` from Sina's unadjusted daily K-line.

    At 17:00 Tencent's daily K-line often still ends on the previous day and gets cached
    like that, which put every follow-up one day behind. Bars after the last cached one can
    be appended raw as long as the stock did not go ex-dividend in between; the overlap day
    must match, otherwise the rows are returned unchanged."""
    if not rows or rows[-1]["date"] >= as_of:
        return rows
    symbol = ("sh" if code.startswith("6") else "sz") + code
    try:
        raw = subprocess.run(
            ["curl", "-sS", "-m", "20", "-H", "User-Agent: Mozilla/5.0",
             "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData?"
             f"symbol={symbol}&scale=240&ma=no&datalen=10"],
            capture_output=True, text=True,
        ).stdout
        data = {r["day"]: r for r in json.loads(raw)["result"]["data"]}
    except Exception:
        return rows
    last = rows[-1]
    if last["date"] not in data or abs(float(data[last["date"]]["close"]) / last["close"] - 1) > 0.002:
        return rows
    extra = [
        {"date": d, "open": float(r["open"]), "close": float(r["close"]), "high": float(r["high"]),
         "low": float(r["low"]), "volume": float(r["volume"])}
        for d, r in sorted(data.items()) if last["date"] < d <= as_of
    ]
    return rows + extra


def follow_up(record: dict[str, Any], as_of: str, track_days: int = TRACK_DAYS, use_stop: bool = True) -> dict[str, Any]:
    """Follow-up for one tracked pick over `track_days`; final once expired or, with
    `use_stop`, once the close breaks the pre-selection 60-day low x STOP_BUFFER."""
    rows, _ = fetch_kline(record["code"])
    rows = extend_to(record["code"], [row for row in rows if row["date"] <= as_of], as_of)
    dates = [row["date"] for row in rows]
    first = record["firstSelectedDate"]
    if first not in dates or dates.index(first) < 59:
        return {"status": "数据待复核", "final": False}
    start = dates.index(first)
    first_close = rows[start]["close"]
    stop = min(row["low"] for row in rows[start - 59:start + 1]) * STOP_BUFFER
    status, final, max_up = "跟踪中", False, 0.0
    end = len(rows) - 1
    for index in range(start + 1, len(rows)):
        max_up = max(max_up, rows[index]["close"] / first_close - 1)
        if use_stop and rows[index]["close"] < stop:
            status, final, end = "破位失效", True, index
            break
        if index - start >= track_days:
            status, final, end = "到期", True, index
            break
    if not final and max_up >= LAUNCH_GAIN:
        status = "启动中"
    last = rows[end]
    status_date = last["date"]
    return {
        "status": status,
        "statusDate": status_date,
        "days": end - start,
        "trackDays": track_days,
        "useStop": use_stop,
        "change": last["close"] / first_close - 1,
        "maxUp": max_up,
        "launched": max_up >= LAUNCH_GAIN,
        "stopPrice": stop,
        "stopDistance": last["close"] / stop - 1,
        "final": final,
    }
