#!/usr/bin/env python3
"""Screen A-share candidates that are still inside the v1.3 red zone.

The target is the final-washout / early-stabilization area highlighted in the
four reference charts. The later confirmed rise is validation, not the target
stage. Output is a research watchlist only.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from screen_latest_30 import (
    CTX,
    elasticity_metrics,
    ema,
    fetch_financials,
    fetch_kline,
    fetch_sector_snapshot,
    get_json,
    get_json_with_curl,
    normalize_industry,
    rsi,
    sector_lookup,
    weekly,
)
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_MD = ROOT / "v2.3核心市值严格池.md"
OUTPUT_JSON = ROOT / "output" / "red-zone-v2.3-core-strict.json"

COUNT = 5
MAX_PER_INDUSTRY = 2
DEFAULT_WORKERS = 16

MARKET_POOL_DEFAULTS = {
    "core": {
        "minTotalMarketCap": 5_000_000_000,
        "minFloatMarketCap": 3_000_000_000,
        "minAverageAmount20": 50_000_000,
        "requireLeaderQuality": True,
    },
    "elastic": {
        "minTotalMarketCap": 3_000_000_000,
        "minFloatMarketCap": 2_000_000_000,
        "minAverageAmount20": 30_000_000,
        "requireLeaderQuality": True,
    },
    "any": {
        "minTotalMarketCap": 0,
        "minFloatMarketCap": 0,
        "minAverageAmount20": 0,
        "requireLeaderQuality": False,
    },
}


def moving_average(values: list[float], period: int, end: int | None = None) -> float:
    sample = values[:end] if end is not None else values
    return sum(sample[-period:]) / period


def align_quote_volume(rows: list[dict], quote: dict) -> dict:
    """Align a closing snapshot's volume unit with the historical K-line source."""
    reference = statistics.median(row["volume"] for row in rows[-20:] if row["volume"] > 0)
    raw_volume = float(quote["volume"])
    candidates = (raw_volume, raw_volume / 100, raw_volume * 100)
    aligned_volume = min(
        candidates,
        key=lambda value: abs(math.log(max(value, 1) / max(reference, 1))),
    )
    return {**quote, "volume": aligned_volume}


def macd_metrics(values: list[float]) -> dict:
    fast = ema(values, 12)
    slow = ema(values, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]

    cross_age = None
    for index in range(len(values) - 1, max(1, len(values) - 11), -1):
        if dif[index - 1] < dea[index - 1] and dif[index] >= dea[index]:
            if dif[index] < 0 and dea[index] < 0:
                cross_age = len(values) - 1 - index
            break

    gap = dif[-1] - dea[-1]
    old_gap = dif[-4] - dea[-4]
    improving = hist[-1] > hist[-4] and dif[-1] >= dif[-4]
    near_cross = gap < 0 and abs(gap) <= max(abs(old_gap) * 0.8, 1e-9)
    underwater = dif[-1] < 0 and dea[-1] < 0

    if underwater and cross_age is not None and cross_age <= 5:
        state = "水下刚金叉"
    elif underwater and improving and near_cross:
        state = "水下接近金叉"
    elif underwater and improving:
        state = "水下低位拐头"
    elif underwater:
        state = "水下弱势"
    else:
        state = "非水下"

    return {
        "dif": dif[-1],
        "dea": dea[-1],
        "hist": hist[-1],
        "hist4": hist[-4],
        "crossAge": cross_age,
        "underwater": underwater,
        "improving": improving,
        "state": state,
    }


def consecutive_above_5_10(closes: list[float]) -> int:
    count = 0
    for end in range(len(closes), 30, -1):
        close = closes[end - 1]
        if close >= moving_average(closes, 5, end) and close >= moving_average(closes, 10, end):
            count += 1
        else:
            break
    return count


def normalized_slope(values: list[float]) -> float:
    """Return the least-squares slope as percent of the sample average per day."""
    if len(values) < 2:
        return 0.0
    x_mean = (len(values) - 1) / 2
    y_mean = sum(values) / len(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    if denominator == 0 or y_mean == 0:
        return 0.0
    numerator = sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values))
    return numerator / denominator / y_mean * 100


def detect_secondary_confirmation(rows: list[dict]) -> dict:
    sample = rows[-60:]
    local_lows = []
    for index in range(2, len(sample) - 2):
        window = sample[index - 2:index + 3]
        if sample[index]["low"] <= min(row["low"] for row in window):
            local_lows.append(index)

    best_pair = None
    for first_pos, first_index in enumerate(local_lows):
        for second_index in local_lows[first_pos + 1:]:
            separation = second_index - first_index
            second_age = len(sample) - 1 - second_index
            if not (5 <= separation <= 30) or second_age > 25:
                continue
            first_low = sample[first_index]["low"]
            second_low = sample[second_index]["low"]
            ratio = second_low / first_low
            rebound_high = max(row["high"] for row in sample[first_index:second_index + 1])
            rebound_between = (rebound_high / max(first_low, second_low) - 1) * 100
            post_low = min(row["low"] for row in sample[second_index:])
            post_break = (post_low / second_low - 1) * 100
            if 0.96 <= ratio <= 1.05 and rebound_between >= 3.5 and post_break >= -2.0:
                pair = {
                    "confirmed": True,
                    "firstDate": sample[first_index]["date"],
                    "secondDate": sample[second_index]["date"],
                    "firstLow": first_low,
                    "secondLow": second_low,
                    "separationDays": separation,
                    "secondAge": second_age,
                    "secondVsFirst": round((ratio - 1) * 100, 1),
                    "reboundBetween": round(rebound_between, 1),
                    "postBreak": round(post_break, 1),
                }
                pair_quality = rebound_between - abs((ratio - 1) * 100) - second_age * 0.1
                if best_pair is None or pair_quality > best_pair["pairQuality"]:
                    pair["secondIndex"] = second_index
                    pair["pairQuality"] = pair_quality
                    best_pair = pair
    if best_pair:
        best_pair.pop("secondIndex", None)
        best_pair.pop("pairQuality", None)
        return best_pair
    return {
        "confirmed": False,
        "firstDate": None,
        "secondDate": None,
        "firstLow": None,
        "secondLow": None,
        "separationDays": None,
        "secondAge": None,
        "secondVsFirst": None,
        "reboundBetween": None,
        "postBreak": None,
    }


def sector_gate(
    industry: str,
    code: str,
    snapshots: dict[str, dict],
    quotes: dict[str, dict] | None = None,
    require_leader_quality: bool = False,
) -> dict | None:
    snapshot = sector_lookup(industry, snapshots)
    if not snapshot:
        return None
    leader_name = str(snapshot.get("f128") or "")
    leader_code = str(snapshot.get("f140") or "")
    leader_gain = float(snapshot.get("f136") or 0)
    board_change = float(snapshot.get("f3") or 0)
    rising = int(snapshot.get("f104") or 0)
    falling = int(snapshot.get("f105") or 0)
    breadth = rising / max(rising + falling, 1)
    if leader_code == code or "ST" in leader_name.upper() or "退" in leader_name:
        return None
    if leader_gain < 3.0:
        return None
    # Sector activation is an independent hard switch. A single emotional
    # leader is insufficient when the rest of the board is still broadly weak.
    if board_change <= 0 and breadth < 0.55:
        return None
    leader_quote = (quotes or {}).get(leader_code, {})
    leader_amount = float(leader_quote.get("amount") or 0)
    leader_market_cap = float(leader_quote.get("totalMarketCap") or 0)
    broad_activation = board_change >= 0.8 and breadth >= 0.65
    liquid_leader = leader_amount >= 100_000_000 and leader_market_cap >= 3_000_000_000
    if require_leader_quality and not (liquid_leader or broad_activation):
        return None
    return {
        "leader": leader_name,
        "leaderCode": leader_code,
        "leaderGain": leader_gain,
        "boardChange": board_change,
        "breadth": breadth,
        "leaderAmount": leader_amount,
        "leaderMarketCap": leader_market_cap,
        "leaderQuality": "满足" if liquid_leader or broad_activation else "观察",
    }


def fetch_eastmoney_daily_quotes() -> dict[str, dict]:
    def number(row: dict, field: str, fallback: float) -> float:
        value = row.get(field)
        return fallback if value in (None, "-") else float(value)

    quotes: dict[str, dict] = {}
    for page in range(1, 70):
        params = {
            "pn": page,
            "pz": 100,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f2,f15,f16,f17,f5,f6,f8,f18,f20,f21,f124",
        }
        url = "https://push2delay.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
        payload = None
        for attempt in range(3):
            try:
                payload = get_json(url) if attempt == 0 else get_json_with_curl(url)
                break
            except Exception:
                continue
        if payload is None:
            # A failed page must not be mistaken for the last page: otherwise the market
            # snapshot silently stops after page 1 (on 2026-11-02 this returned 100 stocks
            # and v2.6 picked nothing). Signal it so the caller falls back to Tencent.
            raise ValueError(f"Eastmoney quote snapshot page {page} unavailable")
        rows = (((payload or {}).get("data") or {}).get("diff") or [])
        for row in rows:
            code = str(row.get("f12") or "")
            timestamp = row.get("f124")
            if len(code) != 6 or not timestamp or row.get("f2") in (None, "-"):
                continue
            close = float(row["f2"])
            previous_close = number(row, "f18", close)
            quotes[code] = {
                "date": dt.datetime.fromtimestamp(int(timestamp)).date().isoformat(),
                "open": number(row, "f17", previous_close),
                "prevClose": previous_close,
                "close": close,
                "high": number(row, "f15", close),
                "low": number(row, "f16", close),
                "volume": number(row, "f5", 0) * 100,
                "amount": number(row, "f6", 0),
                "turnoverRate": number(row, "f8", 0),
                "totalMarketCap": number(row, "f20", 0),
                "floatMarketCap": number(row, "f21", 0),
                "source": "东方财富收盘快照",
            }
        if len(rows) < 100:
            break
    return quotes


SINA_QUOTE_BATCH = 80


def fetch_sina_daily_quotes(codes: list[str]) -> dict[str, dict]:
    """Third source: Sina batch quotes (unadjusted, fine for one day's bar and caps)."""
    def batch(chunk: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        raw = subprocess.run(
            ["curl", "-sS", "-m", "15", "-H", "User-Agent: Mozilla/5.0", "-H", "Referer: https://finance.sina.com.cn",
             "https://hq.sinajs.cn/list=" + ",".join(("sh" if c.startswith("6") else "sz") + c for c in chunk)],
            capture_output=True,
        ).stdout
        for line in raw.decode("gbk", errors="replace").splitlines():
            if '"' not in line or "=" not in line:
                continue
            code = line.split("=")[0][-6:]
            f = line.split('"')[1].split(",")
            if len(f) < 32 or code not in chunk or not f[3] or float(f[3]) <= 0:
                continue
            close, prev = float(f[3]), float(f[2])
            out[code] = {
                "date": f[30], "prevClose": prev, "open": float(f[1]), "close": close,
                "high": float(f[4]), "low": float(f[5]), "volume": float(f[8]), "amount": float(f[9]),
                "turnoverRate": 0.0, "totalMarketCap": 0.0, "floatMarketCap": 0.0,
                "source": "新浪收盘快照",
            }
        return out
    batches = [codes[i:i + SINA_QUOTE_BATCH] for i in range(0, len(codes), SINA_QUOTE_BATCH)]
    quotes: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for part in executor.map(batch, batches):
            quotes.update(part)
    return quotes


TENCENT_QUOTE_BATCH = 60


def parse_tencent_quote_line(line: str) -> tuple[str, dict] | None:
    """Parse one `v_sh600000="1~名称~600000~..."` line from qt.gtimg.cn."""
    if '="' not in line:
        return None
    fields = line.split('="', 1)[1].rstrip('";\n').split("~")
    if len(fields) < 46:
        return None
    code = fields[2]
    try:
        close = float(fields[3])
        previous_close = float(fields[4] or close)
        volume_lots = float(fields[6] or 0)
    except ValueError:
        return None
    # Suspended or not yet traded: mirror the Eastmoney snapshot, which reports "-".
    if len(code) != 6 or close <= 0 or volume_lots <= 0 or len(fields[30]) < 8:
        return None

    def number(index: int, fallback: float) -> float:
        try:
            return float(fields[index])
        except (ValueError, IndexError):
            return fallback

    # Field 35 is "price/volume/amount(yuan)". Volume is in lots for most boards
    # but in shares for STAR Market, so pick the unit that matches the amount.
    amount = number(37, 0) * 10_000
    deal = fields[35].split("/")
    if len(deal) == 3:
        try:
            amount = float(deal[2]) or amount
        except ValueError:
            pass
    volume = volume_lots * 100
    if amount > 0 and abs(amount / (volume_lots * close) - 1) < abs(amount / (volume_lots * 100 * close) - 1):
        volume = volume_lots

    stamp = fields[30]
    return code, {
        "date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}",
        "open": number(5, previous_close),
        "prevClose": previous_close,
        "close": close,
        "high": number(33, close),
        "low": number(34, close),
        "volume": volume,
        "amount": amount,
        "turnoverRate": number(38, 0),
        "totalMarketCap": number(45, 0) * 100_000_000,
        "floatMarketCap": number(44, 0) * 100_000_000,
        "source": "腾讯收盘快照",
    }


def fetch_tencent_quote_batch(codes: list[str]) -> dict[str, dict]:
    symbols = ",".join(("sh" if code.startswith("6") else "sz") + code for code in codes)
    url = "https://qt.gtimg.cn/q=" + symbols
    raw = b""
    for attempt in range(3):
        try:
            if attempt == 0:
                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(request, timeout=15, context=CTX).read()
            else:
                raw = subprocess.run(
                    ["curl", "-sS", "--connect-timeout", "8", "--max-time", "20", url],
                    check=True,
                    capture_output=True,
                    timeout=30,
                ).stdout
            break
        except Exception:
            continue
    quotes: dict[str, dict] = {}
    for line in raw.decode("gbk", errors="ignore").split(";"):
        parsed = parse_tencent_quote_line(line.strip())
        if parsed:
            quotes[parsed[0]] = parsed[1]
    return quotes


def fetch_tencent_daily_quotes(codes: list[str]) -> dict[str, dict]:
    batches = [codes[index:index + TENCENT_QUOTE_BATCH] for index in range(0, len(codes), TENCENT_QUOTE_BATCH)]
    quotes: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for batch_quotes in executor.map(fetch_tencent_quote_batch, batches):
            quotes.update(batch_quotes)
    return quotes


def fetch_daily_quotes(codes: Iterable[str] | None = None) -> dict[str, dict]:
    """Closing snapshot from Eastmoney, falling back to Tencent batch quotes.

    Raises instead of returning an empty snapshot, so a dead data source fails
    the run rather than producing a report that looks like "no candidates".
    """
    intraday = os.environ.get("SIDEWAYS_INTRADAY_DIR")
    if intraday:
        # Cross-checked multi-platform bars written by scripts/intraday_screen.py.
        return json.loads((Path(intraday) / "quotes.json").read_text(encoding="utf-8"))
    quotes = {}
    try:
        quotes = fetch_eastmoney_daily_quotes()
    except Exception as exc:
        print(f"[quotes] Eastmoney snapshot failed: {exc}", file=sys.stderr, flush=True)
    # Eastmoney's list endpoint occasionally serves one partial page; treat a snapshot that
    # covers less than half the universe as a failure so the Tencent fallback runs.
    if codes is not None and len(quotes) < 0.5 * len(set(codes)):
        quotes = {}
    wanted = sorted(set(codes)) if codes is not None else []
    for name, fetch in (("Tencent", fetch_tencent_daily_quotes), ("Sina", fetch_sina_daily_quotes)):
        if quotes and (not wanted or len(quotes) >= 0.8 * len(wanted)):
            break
        if not wanted:
            break
        print(f"[quotes] {name} fallback for {len(wanted) - len(quotes)} stocks", file=sys.stderr, flush=True)
        try:
            extra = fetch(wanted)
        except Exception as exc:
            print(f"[quotes] {name} failed: {exc}", file=sys.stderr, flush=True)
            continue
        # Keep Eastmoney rows; fill whatever the other sources cover.
        quotes = {**extra, **quotes}
    if not quotes:
        raise RuntimeError("daily quote snapshot is empty: Eastmoney, Tencent and Sina all unavailable")
    if wanted and len(quotes) < 0.8 * len(wanted):
        raise RuntimeError(f"quote snapshot covers only {len(quotes)}/{len(wanted)} stocks")
    save_quote_snapshot(quotes)
    return quotes


QUOTE_SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "output" / "cache" / "quotes"


def save_quote_snapshot(quotes: dict[str, dict], keep: int = 15) -> None:
    """Keep each day's closing snapshot. At 17:05 the cached Tencent K-line usually still
    ends the day before, so the 14:45 run next day fills that day's bar from here."""
    dates = [str(q.get("date") or "") for q in quotes.values()]
    day = max(set(dates), key=dates.count) if dates else ""
    if len(quotes) < 1000 or not day or dt.datetime.now().time() < dt.time(15, 5):
        return
    QUOTE_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (QUOTE_SNAPSHOT_DIR / f"{day}.json").write_text(
        json.dumps({c: q for c, q in quotes.items() if q.get("date") == day}, ensure_ascii=False), encoding="utf-8")
    for old in sorted(QUOTE_SNAPSHOT_DIR.glob("*.json"))[:-keep]:
        old.unlink()


def truncate_rows_as_of(rows: list[dict], as_of: str) -> list[dict]:
    """Drop K-line rows after `as_of` so a past trading day can be re-screened."""
    return [row for row in rows if row["date"] <= as_of]


def historical_quote(quote: dict, rows: list[dict]) -> dict:
    """Approximate a past snapshot: today's market caps scaled by the as-of close.

    Share counts are assumed unchanged over the backfill window.
    """
    if not quote or not rows or not quote.get("close"):
        return {}
    ratio = rows[-1]["close"] / float(quote["close"])
    return {
        **rows[-1],
        "totalMarketCap": float(quote.get("totalMarketCap") or 0) * ratio,
        "floatMarketCap": float(quote.get("floatMarketCap") or 0) * ratio,
        "source": "回溯（腾讯日K+当前股本）",
    }


def ensure_as_of_trading_day(as_of: str) -> None:
    rows, _ = fetch_kline("600000")
    if not any(row["date"] == as_of for row in rows):
        raise SystemExit(f"--as-of {as_of} is not a trading day in the K-line history")


def ensure_scan_health(total: int, failures: list[dict], max_failure_ratio: float = 0.3) -> None:
    """Abort when most per-stock fetches failed, instead of writing an empty report."""
    if total and len(failures) / total > max_failure_ratio:
        sample = failures[0].get("error") if failures else ""
        raise RuntimeError(
            f"data fetch failed for {len(failures)}/{total} stocks "
            f"(>{max_failure_ratio:.0%}); first error: {sample}"
        )


def fetch_financial_detail(code: str) -> dict:
    columns = (
        "SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE_NAME,PARENTNETPROFIT,"
        "KCFJCXSYJLR,NETCASH_OPERATE_PK,ZCFZL,INTEREST_DEBT_RATIO,"
        "PARENTNETPROFITTZ,KCFJCXSYJLRTZ,MGJYXJJE"
    )
    params = {
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": columns,
        "filter": f'(SECURITY_CODE="{code}")',
        "pageNumber": 1,
        "pageSize": 1,
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
    rows = ((get_json(url).get("result") or {}).get("data") or [])
    return rows[0] if rows else {}


def classify_announcement_rows(rows: list[dict]) -> dict[str, list[dict]]:
    risk_pattern = re.compile("减持|质押|立案|处罚|诉讼|违规|问询|警示|担保|冻结|控制权|审计意见|退市")
    severe_pattern = re.compile(
        "立案调查|立案告知|行政处罚|退市风险警示|终止上市|司法冻结|"
        "无法表示意见|否定意见|控制权.{0,8}(变更|不稳定)"
    )
    event_pattern = re.compile("可转债|关联交易|发行股份购买资产|重大资产|同业竞争")
    administrative_pattern = re.compile("管理办法|决策制度|制度修订|专项说明|核查意见|承诺函")
    completed_pattern = re.compile("未减持|提前终止|期限届满|实施完毕|实施结果")
    financing_inquiry_pattern = re.compile(
        "(?:可转债|再融资|向特定对象发行|发行股份).{0,30}(?:问询|回复|说明)|"
        "(?:问询|回复|说明).{0,30}(?:可转债|再融资|向特定对象发行|发行股份)"
    )
    severe_risks = []
    risks = []
    events = []
    seen = set()
    for row in rows:
        title = str(row.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        item = {"date": str(row.get("notice_date") or "")[:10], "title": title}
        if severe_pattern.search(title):
            severe_risks.append(item)
        elif financing_inquiry_pattern.search(title) or administrative_pattern.search(title) or completed_pattern.search(title):
            events.append(item)
        elif risk_pattern.search(title):
            risks.append(item)
        elif event_pattern.search(title):
            events.append(item)
    return {"severeRisk": severe_risks[:5], "risk": risks[:5], "events": events[:5]}


def fetch_risk_announcements(code: str) -> dict[str, list[dict]]:
    url = (
        "https://np-anotice-stock.eastmoney.com/api/security/ann?"
        + urllib.parse.urlencode(
            {
                "sr": -1,
                "page_size": 100,
                "page_index": 1,
                "ann_type": "A",
                "client_source": "web",
                "stock_list": code,
            }
        )
    )
    rows = ((get_json(url).get("data") or {}).get("list") or [])
    return classify_announcement_rows(rows)


def enrich_financial_risk(candidate: dict) -> dict:
    detail = fetch_financial_detail(candidate["code"])
    announcement_groups = fetch_risk_announcements(candidate["code"])
    severe_announcements = announcement_groups["severeRisk"]
    announcements = announcement_groups["risk"]
    events = announcement_groups["events"]
    parent_profit = float(detail.get("PARENTNETPROFIT") or candidate["profit"] or 0)
    deduct_profit = detail.get("KCFJCXSYJLR")
    operating_cash = detail.get("NETCASH_OPERATE_PK")
    debt_ratio = detail.get("ZCFZL")
    profit_yoy = detail.get("PARENTNETPROFITTZ")
    deduct_yoy = detail.get("KCFJCXSYJLRTZ")
    financial_industry = any(keyword in candidate["industry"] for keyword in ("银行", "保险", "证券", "期货", "金融"))

    financial_notes = []
    if parent_profit < 0:
        financial_state = "观察"
        financial_notes.append("归母净利润为负，财务黄灯")
    elif deduct_profit is None:
        financial_state = "观察"
        financial_notes.append("扣非净利润数据缺失")
    elif float(deduct_profit) <= 0:
        financial_state = "观察"
        financial_notes.append("扣非净利润为负，财务黄灯")
    else:
        financial_state = "通过"
        if operating_cash is not None and float(operating_cash) < 0:
            financial_state = "观察"
            financial_notes.append("经营现金流为负")
        if profit_yoy is not None and float(profit_yoy) < -30:
            financial_state = "观察"
            financial_notes.append("归母净利润同比下降超过30%")
        if deduct_yoy is not None and float(deduct_yoy) < -30:
            financial_state = "观察"
            financial_notes.append("扣非净利润同比下降超过30%")
        if debt_ratio is not None and float(debt_ratio) > 75 and not financial_industry:
            financial_state = "观察"
            financial_notes.append("资产负债率超过75%")
        if parent_profit < 50_000_000:
            financial_state = "观察"
            financial_notes.append("利润规模低于0.50亿元")
    if not financial_notes:
        financial_notes.append("扣非利润、现金流和杠杆未触发当前预警")

    risk_state = "不通过" if severe_announcements else "观察" if announcements else "基础通过"
    risk_notes = [item["title"] for item in (severe_announcements + announcements)[:3]] or ["近100条公告标题未匹配主要风险关键词"]
    candidate["financialState"] = financial_state
    candidate["riskState"] = risk_state
    candidate["conditionChecks"]["financial"] = financial_state
    candidate["conditionChecks"]["risk"] = risk_state
    candidate["financialRisk"] = {
        "reportPeriod": str(detail.get("REPORT_DATE_NAME") or candidate["profitPeriod"]),
        "parentProfit": parent_profit,
        "deductProfit": float(deduct_profit) if deduct_profit is not None else None,
        "operatingCashFlow": float(operating_cash) if operating_cash is not None else None,
        "debtRatio": round(float(debt_ratio), 1) if debt_ratio is not None else None,
        "interestDebtRatio": round(float(detail.get("INTEREST_DEBT_RATIO")), 1) if detail.get("INTEREST_DEBT_RATIO") is not None else None,
        "profitYoy": round(float(profit_yoy), 1) if profit_yoy is not None else None,
        "deductProfitYoy": round(float(deduct_yoy), 1) if deduct_yoy is not None else None,
        "financialNotes": financial_notes,
        "severeRiskAnnouncements": severe_announcements,
        "riskAnnouncements": announcements,
        "eventAnnouncements": events,
        "riskNotes": risk_notes,
        "eventNotes": [item["title"] for item in events[:3]] or ["未匹配需单列的普通公司事项"],
    }
    return candidate


def analyze(
    code: str,
    financial: dict,
    snapshots: dict[str, dict],
    quotes: dict[str, dict],
    diagnostics: list[str] | None = None,
    diagnostic_context: dict | None = None,
    max_condition_failures: int = 0,
    market_limits: dict | None = None,
) -> dict | None:
    failure_log = diagnostics if diagnostics is not None else []

    def reject(reason: str, relaxable: bool = True) -> bool:
        failure_log.append(reason)
        return not relaxable or len(failure_log) > max_condition_failures

    rows, source = fetch_kline(code)
    if diagnostic_context is not None:
        diagnostic_context["source"] = source
    quote = quotes.get(code)
    kline_volume_to_shares = 1.0
    if quote and rows[-1]["volume"] > 0 and quote["volume"] > 0:
        observed_ratio = quote["volume"] / rows[-1]["volume"]
        kline_volume_to_shares = min(
            (0.01, 1.0, 100.0),
            key=lambda value: abs(math.log(max(observed_ratio, 1e-9) / value)),
        )
    if quote and quote["date"] >= rows[-1]["date"]:
        quote = align_quote_volume(rows, quote)
        if quote["date"] == rows[-1]["date"]:
            rows[-1] = quote
        else:
            rows.append(quote)
        source += "+" + quote.get("source", "东方财富收盘快照")
        if diagnostic_context is not None:
            diagnostic_context["source"] = source
    if len(rows) < 260:
        if reject("日K历史不足260日", relaxable=False):
            return None
    closes = [row["close"] for row in rows]
    close = closes[-1]
    ma = {period: moving_average(closes, period) for period in (5, 10, 15, 30, 60, 120)}

    # The current candle may sit at the right edge of the red box, but it must
    # not have progressed into a sustained MA15/30 confirmation trend.
    above5 = close >= ma[5]
    above10 = close >= ma[10]
    ma5_previous = moving_average(closes, 5, len(closes) - 2)
    ma5_flattening = ma[5] >= ma5_previous * 0.995
    if not above5 and (close < ma[5] * 0.99 or not ma5_flattening):
        if reject("MA5仍快速下压或价格明显低于MA5"):
            return None
    above_days = consecutive_above_5_10(closes) if above5 and above10 else 0
    if above_days > 5:
        if reject("连续站上MA5/MA10超过5日"):
            return None
    if close >= ma[30] or close > ma[15] * 1.01:
        if reject("已经站上MA30或明显站上MA15"):
            return None

    recent60 = rows[-60:]
    low_offset = min(range(len(recent60)), key=lambda index: recent60[index]["low"])
    recent_low = recent60[low_offset]["low"]
    low_age = len(recent60) - 1 - low_offset
    rise_from_recent_low = (close / recent_low - 1) * 100
    if not (5 <= low_age <= 35) or rise_from_recent_low > 8.0:
        if reject("近期低点年龄或距低点涨幅不满足"):
            return None

    one_year = rows[-250:]
    year_low = min(row["low"] for row in one_year)
    rise_from_year_low = (close / year_low - 1) * 100
    if rise_from_year_low > 15.0:
        if reject("距一年低点超过15%"):
            return None

    change5 = (close / closes[-6] - 1) * 100
    change10 = (close / closes[-11] - 1) * 100
    change20 = (close / closes[-21] - 1) * 100
    change60 = (close / closes[-61] - 1) * 100
    if change5 > 5.0 or change10 > 6.0 or change20 > 7.0:
        if reject("5/10/20日涨幅超过红框上限"):
            return None
    max_daily_gain10 = max(
        (closes[index] / closes[index - 1] - 1) * 100
        for index in range(len(closes) - 9, len(closes))
    )
    if max_daily_gain10 >= 7.5:
        if reject("近10日出现明显大阳线式启动"):
            return None

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
    platform_evidence_count = sum(platform_evidence.values())
    stable_platform = platform_evidence_count >= 5 and range10 <= 14 and close_range10 <= 9
    secondary_with_context = (
        secondary["confirmed"]
        and platform_evidence_count >= 3
        and close_range10 <= 10
        and close_slope10 >= -0.70
    )
    if range10 > 14 or range20 > 28:
        if reject("10日或20日波动区间过宽"):
            return None
    if not secondary_with_context and not stable_platform:
        if reject("底部平台和二次确认均不满足"):
            return None

    daily_rsi_series = rsi(closes)
    weekly_rows = weekly(rows)
    weekly_closes = [row["close"] for row in weekly_rows]
    weekly_rsi_series = rsi(weekly_closes)
    daily_rsi = daily_rsi_series[-1]
    weekly_rsi = weekly_rsi_series[-1]
    if daily_rsi is None or weekly_rsi is None:
        if reject("RSI数据不足", relaxable=False):
            return None
    if not (28.0 <= daily_rsi <= 45.0):
        if reject(f"日RSI {daily_rsi:.2f} 不在28-45"):
            return None
    if not (24.0 <= weekly_rsi <= 42.0):
        if reject(f"周RSI {weekly_rsi:.2f} 不在24-42"):
            return None
    daily_rsi_window = [value for value in daily_rsi_series[-10:] if value is not None]
    weekly_rsi_window = [value for value in weekly_rsi_series[-6:] if value is not None]
    daily_rsi_change3 = daily_rsi - daily_rsi_series[-4]
    weekly_rsi_change3 = weekly_rsi - weekly_rsi_series[-4]
    rsi_trend_state = (
        "日周同步抬升"
        if daily_rsi_change3 >= 0 and weekly_rsi_change3 >= 0
        else "日线先抬升"
        if daily_rsi_change3 >= 0
        else "周线抬升、日线回踩"
        if weekly_rsi_change3 >= 0
        else "日周仍回落"
    )

    daily_macd = macd_metrics(closes)
    weekly_macd = macd_metrics(weekly_closes)
    if daily_macd["state"] not in {"水下刚金叉", "水下接近金叉", "水下低位拐头"}:
        if reject("日MACD不是水下修复状态"):
            return None
    if not weekly_macd["underwater"]:
        if reject("周MACD已不在水下"):
            return None
    # Weekly deterioration is allowed only when daily repair is very fresh.
    if not weekly_macd["improving"] and daily_macd["state"] != "水下刚金叉":
        if reject("周MACD未改善且日MACD不是刚金叉"):
            return None

    industry = str(financial.get("BOARD_NAME") or "待分类")
    sector = sector_gate(
        industry,
        code,
        snapshots,
        quotes,
        bool((market_limits or {}).get("requireLeaderQuality")),
    )
    if not sector:
        if reject("行业启动条件不满足或板块快照缺失", relaxable=False):
            return None

    volume5 = sum(row["volume"] for row in rows[-5:]) / 5
    volume20 = sum(row["volume"] for row in rows[-20:]) / 20
    volume_ratio = volume5 / max(volume20, 1)
    median_volume5 = statistics.median(row["volume"] for row in rows[-5:])
    median_volume20 = statistics.median(row["volume"] for row in rows[-20:])
    median_volume_ratio = median_volume5 / max(median_volume20, 1)
    latest = rows[-1]
    latest_range = max(latest["high"] - latest["low"], 1e-9)
    latest_upper_wick = latest["high"] - max(latest["open"], latest["close"])
    probe_volume = (
        latest["close"] >= latest["open"]
        and latest["volume"] / max(volume20, 1) <= 1.35
        and latest_upper_wick / latest_range <= 0.45
    )
    if volume_ratio > 1.0 or (median_volume_ratio > 1.05 and not probe_volume):
        if reject(
            f"近5日量能未收缩（均量比{volume_ratio:.2f}，中位量比{median_volume_ratio:.2f}）"
        ):
            return None
    previous5_volume = sum(row["volume"] for row in rows[-10:-5]) / 5
    recent_to_previous_volume = volume5 / max(previous5_volume, 1)
    up_volumes = [row["volume"] for row in rows[-10:] if row["close"] >= row["open"]]
    down_volumes = [row["volume"] for row in rows[-10:] if row["close"] < row["open"]]
    down_up_volume_ratio = (
        (sum(down_volumes) / len(down_volumes)) / max(sum(up_volumes) / len(up_volumes), 1)
        if up_volumes and down_volumes
        else None
    )
    volume_state = "明显收缩" if volume_ratio <= 0.80 and median_volume_ratio <= 0.90 else "温和收缩"
    low_index = len(rows) - 60 + low_offset
    start = max(20, low_index - 2)
    end = min(len(rows), low_index + 3)
    washout_volume = max(row["volume"] for row in rows[start:end])
    prior_volume = sum(row["volume"] for row in rows[start - 20:start]) / 20
    washout_ratio = washout_volume / max(prior_volume, 1)

    ma30_slope10 = (ma[30] / moving_average(closes, 30, len(closes) - 10) - 1) * 100
    elasticity = elasticity_metrics(code, rows)
    average_amount20 = sum(
        row["close"] * row["volume"] * kline_volume_to_shares
        for row in rows[-20:]
    ) / 20
    total_market_cap = float((quote or {}).get("totalMarketCap") or 0)
    float_market_cap = float((quote or {}).get("floatMarketCap") or 0)
    limits = market_limits or MARKET_POOL_DEFAULTS["any"]
    if total_market_cap < limits["minTotalMarketCap"]:
        if reject(
            f"总市值{total_market_cap / 100_000_000:.1f}亿低于"
            f"{limits['minTotalMarketCap'] / 100_000_000:.1f}亿",
            relaxable=False,
        ):
            return None
    if float_market_cap < limits["minFloatMarketCap"]:
        if reject(
            f"流通市值{float_market_cap / 100_000_000:.1f}亿低于"
            f"{limits['minFloatMarketCap'] / 100_000_000:.1f}亿",
            relaxable=False,
        ):
            return None
    if average_amount20 < limits["minAverageAmount20"]:
        if reject(
            f"20日估算均额{average_amount20 / 100_000_000:.2f}亿低于"
            f"{limits['minAverageAmount20'] / 100_000_000:.2f}亿",
            relaxable=False,
        ):
            return None
    market_pool_class = (
        "核心市值池"
        if total_market_cap >= 5_000_000_000
        and float_market_cap >= 3_000_000_000
        and average_amount20 >= 50_000_000
        else "弹性市值池"
        if total_market_cap >= 3_000_000_000
        and float_market_cap >= 2_000_000_000
        and average_amount20 >= 30_000_000
        else "微小市值/低流动性"
    )
    if limits.get("pool") == "elastic" and market_pool_class == "核心市值池":
        if reject("已归入核心市值池，不在弹性池重复输出", relaxable=False):
            return None

    rsi_lifting = daily_rsi_series[-4] is not None and daily_rsi >= daily_rsi_series[-4]
    manual_precheck = {
        "lowNoLongerBreaking": low_age >= 5 and floor_drift >= -2.5,
        "notSingleCandleRebound": max_daily_gain10 < 7.5 and abs(change5) <= 5.0,
        "platformFormed": stable_platform or secondary["confirmed"],
        "secondaryLowValid": secondary["confirmed"],
        "ma5Flattening": ma5_flattening,
        "rsiLowAndLifting": daily_rsi <= 45 and rsi_lifting,
        "macdUnderwaterRepair": daily_macd["state"] in {"水下刚金叉", "水下接近金叉", "水下低位拐头"},
        "volumeContracted": volume_ratio <= 1.0 and (median_volume_ratio <= 1.05 or probe_volume),
        "sectorHasBreadth": sector["boardChange"] > 0 or sector["breadth"] >= 0.55,
    }

    condition_checks = {
        "lowPosition": "满足",
        "notStarted": "满足",
        "platform": "满足",
        "volume": "满足",
        "rsi": "满足",
        "macd": "满足",
        "maStage": "满足",
        "sector": "满足",
        "marketCap": "满足" if limits["minTotalMarketCap"] or limits["minFloatMarketCap"] else "未启用",
        "liquidity": "满足" if limits["minAverageAmount20"] else "未启用",
    }
    for reason in failure_log:
        if "RSI" in reason:
            condition_checks["rsi"] = "不满足"
        elif "MACD" in reason:
            condition_checks["macd"] = "不满足"
        elif "量能" in reason:
            condition_checks["volume"] = "不满足"
        elif "平台" in reason or "波动区间" in reason:
            condition_checks["platform"] = "不满足"
        elif "MA" in reason:
            condition_checks["maStage"] = "不满足"
        elif "低点" in reason:
            condition_checks["lowPosition"] = "不满足"
        elif "涨幅" in reason or "大阳线" in reason:
            condition_checks["notStarted"] = "不满足"

    return {
        "code": code,
        "name": str(financial.get("SECURITY_NAME_ABBR") or code),
        "industry": industry,
        "date": rows[-1]["date"],
        "close": close,
        "market": {
            "totalMarketCap": round(total_market_cap, 2),
            "floatMarketCap": round(float_market_cap, 2),
            "amount": round(float((quote or {}).get("amount") or 0), 2),
            "averageAmount20": round(average_amount20, 2),
            "turnoverRate": round(float((quote or {}).get("turnoverRate") or 0), 2),
            "pool": market_pool_class,
        },
        "source": source,
        "profit": float(financial.get("PARENT_NETPROFIT") or 0),
        "profitPeriod": str(financial.get("QDATE") or ""),
        "conditionVerdict": "满足量化条件" if not failure_log else f"边界观察（{len(failure_log)}项不满足）",
        "failedConditions": list(failure_log),
        "conditionChecks": condition_checks,
        "lowAge": low_age,
        "aboveDays": above_days,
        "above5": above5,
        "above10": above10,
        "riseRecentLow": round(rise_from_recent_low, 1),
        "riseYearLow": round(rise_from_year_low, 1),
        "change5": round(change5, 1),
        "change10": round(change10, 1),
        "change20": round(change20, 1),
        "change60": round(change60, 1),
        "dailyRsi": round(float(daily_rsi), 1),
        "weeklyRsi": round(float(weekly_rsi), 1),
        "dailyRsiMin10": round(min(daily_rsi_window), 1),
        "weeklyRsiMin6": round(min(weekly_rsi_window), 1),
        "dailyRsiChange3": round(daily_rsi_change3, 1),
        "weeklyRsiChange3": round(weekly_rsi_change3, 1),
        "rsiTrendState": rsi_trend_state,
        "dailyMacd": daily_macd,
        "weeklyMacd": weekly_macd,
        "below15": close < ma[15],
        "below30": close < ma[30],
        "maDistance15": round((close / ma[15] - 1) * 100, 1),
        "maDistance30": round((close / ma[30] - 1) * 100, 1),
        "ma30Slope10": round(ma30_slope10, 2),
        "range10": round(range10, 1),
        "range20": round(range20, 1),
        "closeRange10": round(close_range10, 1),
        "closeSlope10": round(close_slope10, 2),
        "floorDrift": round(floor_drift, 1),
        "lowTouchCount15": low_touch_count15,
        "platformEvidence": platform_evidence,
        "platformEvidenceCount": platform_evidence_count,
        "stablePlatform": stable_platform,
        "secondaryWithContext": secondary_with_context,
        "secondaryConfirmation": secondary,
        "volumeRatio5v20": round(volume_ratio, 2),
        "medianVolumeRatio5v20": round(median_volume_ratio, 2),
        "volumeRatio5vPrevious5": round(recent_to_previous_volume, 2),
        "downUpVolumeRatio10": round(down_up_volume_ratio, 2) if down_up_volume_ratio is not None else None,
        "probeVolume": probe_volume,
        "volumeState": volume_state,
        "washoutVolumeRatio": round(washout_ratio, 2),
        "manualPrecheck": manual_precheck,
        "manualReviewVerdict": "待人工复核",
        "sector": sector,
        "elasticity": elasticity,
        "sparkline": [
            {
                "date": row["date"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }
            for row in rows[-90:]
        ],
    }


def select_diversified(candidates: list[dict], count: int = COUNT, max_per_industry: int = MAX_PER_INDUSTRY) -> list[dict]:
    selected = []
    industries: defaultdict[str, int] = defaultdict(int)
    for candidate in candidates:
        industry = normalize_industry(candidate["industry"])
        if industries[industry] >= max_per_industry:
            continue
        selected.append(candidate)
        industries[industry] += 1
        if len(selected) == count:
            break
    if len(selected) < count:
        selected_codes = {candidate["code"] for candidate in selected}
        for candidate in candidates:
            if candidate["code"] in selected_codes:
                continue
            selected.append(candidate)
            if len(selected) == count:
                break
    return selected


def apply_manual_reviews(candidates: list[dict], review_path: Path | None) -> None:
    if review_path is None:
        return
    reviews = json.loads(review_path.read_text(encoding="utf-8"))
    for candidate in candidates:
        review = reviews.get(candidate["code"])
        if not review:
            continue
        verdict = review.get("verdict", "待人工复核")
        if verdict not in {"通过", "观察", "剔除", "待人工复核"}:
            raise ValueError(f"invalid manual review verdict for {candidate['code']}: {verdict}")
        candidate["manualReviewVerdict"] = verdict
        candidate["manualReviewReason"] = str(review.get("reason") or "")


def render_markdown(
    selected: list[dict],
    scanned: int,
    passed: int,
    title: str = "v2.3 核心市值严格池（条件制）",
    max_condition_failures: int = 0,
    market_limits: dict | None = None,
) -> str:
    lines = [
        f"# {title}",
        "",
        "本轮把四张参考图的红框视为候选股票的当前阶段；红框之后的上涨仅用于验证形态，不作为筛选目标。",
        "",
        "本文件只用于选股框架研究和观察池跟踪，不构成个股推荐、买卖建议或交易指令。",
        "",
        (
            f"本报告为边界观察模式：最多允许{max_condition_failures}项量化条件不满足，失败项必须明确展示。"
            if max_condition_failures
            else "本报告为严格模式：量化硬条件必须全部满足。"
        ),
        "",
        "## 硬条件",
        "",
        "- 最新归母净利润不少于0.10亿元、扣非净利润为正；经营现金流、负债率和利润同比单独分级。",
        "- 风险开关检查ST/退市名称，并扫描近100条公告中的减持、质押、担保、问询、诉讼、立案和处罚关键词。",
        "- 距近60日下杀低点不超过8%，低点形成5至35个交易日；距一年低点不超过15%。",
        "- 近20日涨幅不超过7%，近10日不超过6%，近5日不超过5%，近10日没有大阳线式启动。",
        "- 必须形成低位平台或有效二次确认：平台由波动收缩、收盘聚拢、低位反复触碰、斜率止跌和底部不再下移等多项证据共同确认。",
        "- 二次低点间隔5至30日，第二低点相对第一低点允许-4%至+5%，两低点之间须有至少3.5%反弹，之后不得再次有效跌破。",
        "- 近5日均量不得高于20日均量，中位量也须收缩；仅允许无明显长上影的首次温和试探量。",
        "- 红框核心区允许尚未完全站上MA5/MA10，但价格必须贴近MA5且MA5停止快速下压；红框右边缘站上MA5/MA10不超过5个交易日。",
        "- 仍在MA30下方，且不得明显站上MA15。",
        "- 日RSI 28至45，周RSI 24至42。",
        "- 日MACD处于水下刚金叉、接近金叉或低位拐头；周MACD仍在水下。",
        "- 行业已有另一只领涨股，领涨幅至少3%；同时板块翻红或上涨宽度达到55%。",
        "- 行业启动是独立条件，不与个股技术条件相互抵消。",
        (
            f"- 市值流动性条件：总市值不少于{market_limits['minTotalMarketCap'] / 100_000_000:.0f}亿、"
            f"流通市值不少于{market_limits['minFloatMarketCap'] / 100_000_000:.0f}亿、"
            f"20日估算日均成交额不少于{market_limits['minAverageAmount20'] / 100_000_000:.2f}亿。"
            if market_limits and market_limits["minTotalMarketCap"]
            else "- 市值流动性条件未启用，仅用于展示和分类。"
        ),
        "- 不计算总分或复核分；任一硬条件不满足即不进入严格池。",
        "",
        f"扫描盈利A股 {scanned} 只，当前模式保留 {passed} 只，按行业优先分散后展示 {len(selected)} 只。",
        "",
        "## 条件检查表",
        "",
        "| 股票 | 低位 | 未明显启动 | 平台/二次确认 | 量能收缩 | RSI | MACD | 均线阶段 | 行业启动 | 市值 | 流动性 | 财务 | 风险 | 人工结论 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in selected:
        checks = item["conditionChecks"]
        lines.append(
            f"| {item['name']} {item['code']} | {checks['lowPosition']} | {checks['notStarted']} | "
            f"{checks['platform']} | {checks['volume']} | {checks['rsi']} | {checks['macd']} | "
            f"{checks['maStage']} | {checks['sector']} | {checks.get('marketCap', '未启用')} | "
            f"{checks.get('liquidity', '未启用')} | {checks['financial']} | {checks['risk']} | "
            f"{item['manualReviewVerdict']} |"
        )

    lines.extend([
        "",
        "## 条件依据",
        "",
        "| 股票 | 数据日 | 市值/20日均额 | 距低点/距今 | 5/10/20日 | 日/周RSI | MA与MACD | 底部结构 | 板块拉升标的 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for item in selected:
        sector = item["sector"]
        stage = f"站5/10第{item['aboveDays']}日" if item["aboveDays"] else "红框核心·未全站5/10"
        ma_state = f"{stage}；MA30 {item['maDistance30']:+.1f}%；{item['dailyMacd']['state']}"
        secondary_text = "二次确认" if item["secondaryConfirmation"]["confirmed"] else "平台确认"
        structure = (
            f"{secondary_text}；平台证据{item['platformEvidenceCount']}/6；10日{item['range10']:.1f}%；"
            f"低位触碰{item['lowTouchCount15']}次；{item['volumeState']} {item['volumeRatio5v20']:.2f}"
        )
        lines.append(
            f"| {item['name']} {item['code']} | {item['date']} | "
            f"{item.get('market', {}).get('totalMarketCap', 0) / 100_000_000:.1f}亿/"
            f"{item.get('market', {}).get('averageAmount20', 0) / 100_000_000:.2f}亿 | "
            f"{item['riseRecentLow']:.1f}%/{item['lowAge']}日 | "
            f"{item['change5']:+.1f}%/{item['change10']:+.1f}%/{item['change20']:+.1f}% | "
            f"{item['dailyRsi']:.1f}/{item['weeklyRsi']:.1f} | {ma_state} | {structure} | "
            f"{sector['leader']} {sector['leaderGain']:+.2f}% |"
        )

    lines.extend([
        "",
        "## 逐只观察理由与风险",
        "",
    ])
    for item in selected:
        sector = item["sector"]
        financial = item["financialRisk"]
        secondary = item["secondaryConfirmation"]
        secondary_detail = (
            f"二次确认成立：{secondary['firstDate']}与{secondary['secondDate']}相隔{secondary['separationDays']}日，"
            f"第二低点相对第一低点{secondary['secondVsFirst']:+.1f}%"
            if secondary["confirmed"]
            else "未识别到标准双低点，但低位平台、波动收缩和多次触碰满足平台确认条件"
        )
        precheck_labels = {
            "lowNoLongerBreaking": "停止创新低",
            "notSingleCandleRebound": "不是单阳反抽",
            "platformFormed": "平台或二次确认成立",
            "secondaryLowValid": "标准二次低点成立",
            "ma5Flattening": "MA5走平",
            "rsiLowAndLifting": "RSI低位抬升",
            "macdUnderwaterRepair": "MACD水下改善",
            "volumeContracted": "成交量收缩",
            "sectorHasBreadth": "板块存在扩散",
        }
        pending_checks = [label for key, label in precheck_labels.items() if not item["manualPrecheck"].get(key)]
        cash_text = "缺失" if financial["operatingCashFlow"] is None else f"{financial['operatingCashFlow'] / 100_000_000:.2f}亿元"
        deduct_text = "缺失" if financial["deductProfit"] is None else f"{financial['deductProfit'] / 100_000_000:.2f}亿元"
        manual_reason = item.get("manualReviewReason", "").rstrip("。；")
        failed_detail = "、".join(item.get("failedConditions") or [])
        lines.extend([
            f"### {item['name']} {item['code']}",
            "",
            f"- 红框对应：距下杀低点{item['riseRecentLow']:.1f}%，低点形成{item['lowAge']}日；"
            + (
                f"站上MA5/10仅{item['aboveDays']}日，仍在MA30下方。"
                if item["aboveDays"]
                else "尚未完全站上MA5/10，但价格贴近MA5且短均线下压减缓，仍在MA30下方。"
            ),
            f"- 指标阶段：日RSI {item['dailyRsi']:.1f}、周RSI {item['weeklyRsi']:.1f}；"
            f"近10日日RSI最低{item['dailyRsiMin10']:.1f}，3日变化{item['dailyRsiChange3']:+.1f}；"
            f"近6周周RSI最低{item['weeklyRsiMin6']:.1f}，3周变化{item['weeklyRsiChange3']:+.1f}，"
            f"当前为{item['rsiTrendState']}；日MACD为{item['dailyMacd']['state']}，周MACD仍在水下。",
            f"- 条件结论：{item['conditionVerdict']}"
            f"{'；不满足项：' + failed_detail if failed_detail else ''}。",
            f"- 底部结构：{secondary_detail}；10日振幅{item['range10']:.1f}%，"
            f"10日收盘区间{item['closeRange10']:.1f}%，收盘斜率{item['closeSlope10']:+.2f}%/日，"
            f"MA30近10日变化{item['ma30Slope10']:+.2f}%，"
            f"底部漂移{item['floorDrift']:+.1f}%，最近15日低位触碰{item['lowTouchCount15']}次，"
            f"平台条件满足{item['platformEvidenceCount']}/6项。",
            f"- 板块开关：{item['industry']}领涨股为{sector['leader']}，当日{sector['leaderGain']:+.2f}%；"
            f"板块涨跌{sector['boardChange']:+.2f}%，上涨宽度{sector['breadth']:.0%}。",
            f"- 市值流动性：总市值{item.get('market', {}).get('totalMarketCap', 0) / 100_000_000:.1f}亿元、"
            f"流通市值{item.get('market', {}).get('floatMarketCap', 0) / 100_000_000:.1f}亿元、"
            f"20日估算日均成交额{item.get('market', {}).get('averageAmount20', 0) / 100_000_000:.2f}亿元，"
            f"归类为{item.get('market', {}).get('pool', '待分类')}。",
            f"- 量价观察：低点附近最大量相对前20日均量{item['washoutVolumeRatio']:.2f}倍，"
            f"近5日均量/20日均量{item['volumeRatio5v20']:.2f}，中位量比{item['medianVolumeRatio5v20']:.2f}，"
            f"近5日/前5日{item['volumeRatio5vPrevious5']:.2f}；状态为{item['volumeState']}"
            f"{'，允许首次温和试探量' if item['probeVolume'] else ''}。",
            f"- 财务开关（{item['financialState']}）：{financial['reportPeriod']}归母净利润"
            f"{financial['parentProfit'] / 100_000_000:.2f}亿元、扣非净利润{deduct_text}、经营现金流{cash_text}、"
            f"资产负债率{financial['debtRatio']}%；{'；'.join(financial['financialNotes'])}。",
            f"- 风险开关（{item['riskState']}）：{'；'.join(financial['riskNotes'])}。",
            f"- 普通公司事项：{'；'.join(financial.get('eventNotes') or ['无'])}。",
            f"- 自动条件检查：{'9项均满足' if not pending_checks else '仍需人工确认：' + '、'.join(pending_checks)}；"
            f"当前人工结论为“{item['manualReviewVerdict']}”"
            f"{'；' + manual_reason if manual_reason else ''}。",
            "- 人工看图必须给出通过/观察/剔除：依次核对是否停止创新低、是否不是单阳反抽、平台宽度是否稳定、二次低点是否有效、MA5是否走平、RSI是否低位抬升、MACD是否水下改善、缩量是否持续、板块是否并非单日脉冲。",
            "- 主要风险：仍属于低位弱修复，MA15/MA30压力、再次下探和行业强度衰减均可能使形态失效。",
            "",
        ])
    lines.extend([
        "## 数据边界",
        "",
        "- 行情采用腾讯前复权日K，失败时回退新浪日K。",
        "- 财务采用东方财富最新主要财务指标；公告风险采用东方财富最近100条公告标题分类扫描。",
        "- 管理办法、制度修订、普通关联交易和已终止减持等事项单独展示，不再自动等同于实质风险。",
        "- 公告标题扫描不能代替阅读全文、F10、股权质押明细、解禁计划和监管数据库核验。",
        "- 板块和领涨股采用东方财富当日快照，只能证明当日存在拉升标的，不能证明行业趋势已经形成。",
    ])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=COUNT)
    parser.add_argument("--exclude-codes", default="")
    parser.add_argument("--output", type=Path, default=OUTPUT_MD)
    parser.add_argument("--json-output", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--title", default="v2.3 核心市值严格池（条件制）")
    parser.add_argument("--max-per-industry", type=int, default=MAX_PER_INDUSTRY)
    parser.add_argument("--manual-review", type=Path)
    parser.add_argument("--max-condition-failures", type=int, default=0)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--market-pool", choices=("core", "elastic", "any"), default="core")
    parser.add_argument("--min-total-market-cap-yi", type=float)
    parser.add_argument("--min-float-market-cap-yi", type=float)
    parser.add_argument("--min-average-amount20-yi", type=float)
    return parser.parse_args()


def resolve_market_limits(args: argparse.Namespace) -> dict:
    limits = dict(MARKET_POOL_DEFAULTS[args.market_pool])
    if args.min_total_market_cap_yi is not None:
        limits["minTotalMarketCap"] = args.min_total_market_cap_yi * 100_000_000
    if args.min_float_market_cap_yi is not None:
        limits["minFloatMarketCap"] = args.min_float_market_cap_yi * 100_000_000
    if args.min_average_amount20_yi is not None:
        limits["minAverageAmount20"] = args.min_average_amount20_yi * 100_000_000
    limits["pool"] = args.market_pool
    return limits


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    market_limits = resolve_market_limits(args)
    excluded_codes = {code.strip() for code in args.exclude_codes.split(",") if code.strip()}
    financials = fetch_financials()
    snapshots = fetch_sector_snapshot()
    quotes = fetch_daily_quotes(financials.keys())
    candidates = []
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                analyze,
                code,
                financial,
                snapshots,
                quotes,
                None,
                None,
                args.max_condition_failures,
                market_limits,
            ): (code, financial)
            for code, financial in financials.items()
            if code not in excluded_codes
        }
        for future in concurrent.futures.as_completed(futures):
            code, financial = futures[future]
            try:
                candidate = future.result()
                if candidate:
                    candidates.append(candidate)
            except Exception as exc:
                failures.append({"code": code, "error": str(exc)})

    enriched_candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, 12)) as executor:
        futures = {executor.submit(enrich_financial_risk, item): item for item in candidates}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                enriched = future.result()
                if enriched["financialState"] != "不通过" and enriched["riskState"] != "不通过":
                    enriched_candidates.append(enriched)
            except Exception as exc:
                failures.append({"code": item["code"], "error": f"financial-risk: {exc}"})
    candidates = enriched_candidates

    apply_manual_reviews(candidates, args.manual_review)
    for candidate in candidates:
        if candidate.get("failedConditions") and candidate["manualReviewVerdict"] == "通过":
            candidate["manualReviewVerdict"] = "观察"
            candidate["manualReviewReason"] = (
                candidate.get("manualReviewReason") or "存在量化条件不满足项"
            )
    verdict_order = {"通过": 0, "观察": 1, "待人工复核": 2, "剔除": 3}
    candidates.sort(
        key=lambda item: (
            verdict_order[item["manualReviewVerdict"]],
            normalize_industry(item["industry"]),
            item["code"],
        )
    )
    eligible_candidates = [item for item in candidates if item["manualReviewVerdict"] != "剔除"]
    selected = select_diversified(eligible_candidates, args.count, args.max_per_industry)
    if len(selected) < args.count:
        print(f"warning: requested {args.count}, current conditions produced {len(selected)} candidates")

    args.output.write_text(
        render_markdown(
            selected,
            len(financials),
            len(candidates),
            args.title,
            args.max_condition_failures,
            market_limits,
        ),
        encoding="utf-8",
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(
            {
                "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "filters": {
                    "maxConditionFailures": args.max_condition_failures,
                    "marketLimits": market_limits,
                    "maxPerIndustry": args.max_per_industry,
                    "workers": args.workers,
                    "excludedCodes": sorted(excluded_codes),
                },
                "selected": selected,
                "allCandidates": candidates,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {args.output}")
    print(f"wrote {args.json_output}")
    print(f"financials={len(financials)} strict_pass={len(candidates)} failures={len(failures)}")
    for index, item in enumerate(selected, 1):
        print(
            index,
            item["name"],
            item["code"],
            item["industry"],
            item["riseRecentLow"],
            item["dailyRsi"],
            item["weeklyRsi"],
            item["dailyMacd"]["state"],
            item["sector"]["leader"],
        )


if __name__ == "__main__":
    main()
