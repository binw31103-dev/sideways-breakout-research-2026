#!/usr/bin/env python3
"""Build a fresh 30-stock A-share sideways-breakout research pool.

The screen starts from the current market rather than historical project
reports. It is a research/watchlist workflow and never emits trade actions.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import math
import os
import re
import ssl
import subprocess
import threading
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_JSON = ROOT / "src" / "data" / "stock-data.json"
OUTPUT_MD = ROOT / "最新要求重新筛选30只-v1.9.md"
LEGACY_CODES = ROOT / "notes" / "legacy-stock-codes-v1.8.txt"
CTX = ssl._create_unverified_context()
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

COUNT = 30
MAX_PER_INDUSTRY = 3
MIN_PARENT_PROFIT = 10_000_000
MAX_RISE_FROM_LOW = 25.0
MAX_20D_GAIN = 12.0
MAX_60D_RANGE = 55.0
WEEKLY_RSI_RANGE = (28.0, 50.0)
DAILY_RSI_RANGE = (35.0, 60.0)


def get_json(url: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(url, headers=HEADERS)
    last_error: Exception | None = None
    for _ in range(2):
        try:
            raw = urllib.request.urlopen(request, timeout=timeout, context=CTX).read().decode("utf-8")
            return json.loads(raw.lstrip("\ufeff"))
        except Exception as exc:
            last_error = exc
    try:
        result = subprocess.run(
            [
                "curl",
                "-sS",
                "--connect-timeout", "8",
                "--max-time", str(timeout),
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout + 10,
        )
        return json.loads(result.stdout.lstrip("\ufeff"))
    except Exception as exc:
        raise RuntimeError(f"request failed after retries: {url}") from (exc if exc else last_error)


def get_json_with_curl(url: str) -> dict:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            result = subprocess.run(
                ["curl", "-sS", "--connect-timeout", "8", "--max-time", "20", url],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return json.loads(result.stdout.lstrip("\ufeff"))
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"curl request failed after retries: {url}") from last_error


FINANCIALS_CACHE = ROOT / "output" / "cache"


def fetch_financials(include_financial_yellow: bool = False) -> dict[str, dict]:
    """Latest-report list. Cached on success; the intraday run reads the cache (the list does
    not change during the day) and any run falls back to it if Eastmoney fails."""
    cache = FINANCIALS_CACHE / f"financials-{'all' if include_financial_yellow else 'profitable'}.json"
    if INTRADAY_DIR and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        records = _fetch_financials(include_financial_yellow)
    except Exception:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        raise
    if records:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return records


def _fetch_financials(include_financial_yellow: bool) -> dict[str, dict]:
    records: dict[str, dict] = {}
    page = 1
    while True:
        params = {
            "reportName": "RPT_LICO_FN_CPD",
            "columns": (
                "SECURITY_CODE,SECURITY_NAME_ABBR,REPORTDATE,QDATE,"
                "PARENT_NETPROFIT,TOTAL_OPERATE_INCOME,BOARD_NAME,SECUCODE"
            ),
            "filter": '(ISNEW="1")',
            "pageNumber": page,
            "pageSize": 500,
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
            "source": "WEB",
            "client": "WEB",
        }
        result = get_json(
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            + urllib.parse.urlencode(params)
        ).get("result") or {}
        rows = result.get("data") or []
        if not rows:
            break
        for row in rows:
            code = str(row.get("SECURITY_CODE") or "")
            name = str(row.get("SECURITY_NAME_ABBR") or "")
            profit = row.get("PARENT_NETPROFIT")
            if len(code) != 6 or code[0] not in "036":
                continue
            if "ST" in name.upper() or "退" in name:
                continue
            if not include_financial_yellow and (profit is None or profit < MIN_PARENT_PROFIT):
                continue
            records[code] = row
        if page >= int(result.get("pages") or page):
            break
        page += 1
    return records


def market_symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


KLINE_CACHE_ROOT = ROOT / "output" / "cache" / "kline"
KLINE_CACHE_KEEP_DAYS = 3
# Writing the cache only after the close keeps intraday bars out of it.
KLINE_CACHE_AFTER = dt.time(15, 10)


def kline_cache_path(code: str, today: dt.date | None = None) -> Path:
    return KLINE_CACHE_ROOT / (today or dt.date.today()).isoformat() / f"{code}.json"


def prune_kline_cache(today: dt.date | None = None) -> None:
    if not KLINE_CACHE_ROOT.exists():
        return
    cutoff = ((today or dt.date.today()) - dt.timedelta(days=KLINE_CACHE_KEEP_DAYS)).isoformat()
    for directory in KLINE_CACHE_ROOT.iterdir():
        if directory.is_dir() and directory.name < cutoff:
            for file in directory.iterdir():
                file.unlink()
            directory.rmdir()


# Intraday mode (scripts/intraday_screen.py): history comes from the last full cache (bars
# up to the previous trading day) and today's bar from cross-checked live quotes, so the
# 14:45 run downloads no K-lines at all.
INTRADAY_DIR = os.environ.get("SIDEWAYS_INTRADAY_DIR")
INTRADAY_DATE = os.environ.get("SIDEWAYS_INTRADAY_DATE") or dt.date.today().isoformat()


def _snapshot(day: str) -> dict[str, dict]:
    path = KLINE_CACHE_ROOT.parent / "quotes" / f"{day}.json"
    if day not in _SNAPSHOTS:
        _SNAPSHOTS[day] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _SNAPSHOTS[day]


_SNAPSHOTS: dict[str, dict[str, dict]] = {}


def intraday_kline(code: str) -> tuple[list[dict], str]:
    """Bars before INTRADAY_DATE: newest daily cache, plus any later days from saved closing
    snapshots (the 17:05 cache often lacks its own day). Must end on the previous trading
    day (SIDEWAYS_INTRADAY_PREV), otherwise the stock is skipped rather than left with a gap."""
    previous = os.environ.get("SIDEWAYS_INTRADAY_PREV", "")
    days = sorted(p for p in KLINE_CACHE_ROOT.iterdir() if p.is_dir() and p.name <= INTRADAY_DATE)
    for directory in reversed(days):
        path = directory / f"{code}.json"
        if not path.exists():
            continue
        rows = [row for row in json.loads(path.read_text(encoding="utf-8"))["rows"] if row["date"] < INTRADAY_DATE]
        if len(rows) < 160:
            continue
        snapshots = sorted(p.stem for p in (KLINE_CACHE_ROOT.parent / "quotes").glob("*.json")) \
            if (KLINE_CACHE_ROOT.parent / "quotes").exists() else []
        filled = []
        for day in snapshots:
            if rows[-1]["date"] < day < INTRADAY_DATE and code in _snapshot(day):
                q = _snapshot(day)[code]
                reference = sorted(r["volume"] for r in rows[-20:])[10] or 1
                unit = min((1.0, 0.01, 100.0), key=lambda f: abs(math.log(max(q["volume"] * f, 1) / reference)))
                rows.append({"date": day, "open": q["open"], "close": q["close"], "high": q["high"],
                             "low": q["low"], "volume": q["volume"] * unit})
                filled.append(day)
        if previous and rows[-1]["date"] != previous:
            raise ValueError(f"cached K-line ends {rows[-1]['date']}, expected {previous}")
        return rows, f"缓存日K（{directory.name}）" + (f"+快照{','.join(filled)}" if filled else "")
    raise ValueError(f"no cached K-line before {INTRADAY_DATE}")


def fetch_kline(code: str) -> tuple[list[dict], str]:
    """Daily K-line, cached per calendar day so every model shares one download."""
    if INTRADAY_DIR:
        return intraday_kline(code)
    path = kline_cache_path(code)
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            return cached["rows"], cached["source"]
        except (ValueError, KeyError):
            pass
    rows, source = fetch_kline_uncached(code)
    if dt.datetime.now().time() >= KLINE_CACHE_AFTER or rows[-1]["date"] < dt.date.today().isoformat():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(json.dumps({"source": source, "rows": rows}, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    return rows, source


def fetch_kline_uncached(code: str) -> tuple[list[dict], str]:
    symbol = market_symbol(code)
    end_date = dt.date.today().isoformat()
    for host in ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn"):
        url = (
            f"https://{host}/appstock/app/fqkline/get?"
            f"param={symbol},day,2023-01-01,{end_date},900,qfq"
        )
        try:
            node = (get_json(url, timeout=12).get("data") or {}).get(symbol) or {}
            source = node.get("qfqday") or node.get("day") or []
            if len(source) >= 160:
                return (
                    [
                        {
                            "date": row[0],
                            "open": float(row[1]),
                            "close": float(row[2]),
                            "high": float(row[3]),
                            "low": float(row[4]),
                            "volume": float(row[5]),
                        }
                        for row in source
                    ],
                    "腾讯前复权日K",
                )
        except Exception:
            continue

    # Sina's K-line is unadjusted: dividends would show up as fake drops and mix badly with
    # Tencent's adjusted bars, so it is only used together with Sina's adjustment factors.
    fallback_url = (
        "https://quotes.sina.cn/cn/api/openapi.php/"
        "CN_MarketDataService.getKLineData?"
        f"symbol={symbol}&scale=240&ma=no&datalen=1023"
    )
    source = (get_json_with_curl(fallback_url).get("result") or {}).get("data") or []
    if len(source) < 160:
        raise ValueError("insufficient kline rows")
    factors = sina_adjust_factors(symbol)

    def factor(day: str) -> float:
        return next((value for start, value in factors if start <= day), factors[-1][1])

    base = factor(source[-1]["day"])
    return (
        [
            {
                "date": row["day"],
                "open": float(row["open"]) * base / factor(row["day"]),
                "close": float(row["close"]) * base / factor(row["day"]),
                "high": float(row["high"]) * base / factor(row["day"]),
                "low": float(row["low"]) * base / factor(row["day"]),
                "volume": float(row["volume"]),
            }
            for row in source
        ],
        "新浪日K+新浪复权因子回退",
    )


def sina_adjust_factors(symbol: str) -> list[tuple[str, float]]:
    """[(ex_date, factor)] newest first; forward-adjusted price = raw / factor of the latest
    ex_date on or before the day. Raises if missing, so unadjusted bars never get used."""
    raw = subprocess.run(
        ["curl", "-sS", "--max-time", "20", "-H", "User-Agent: Mozilla/5.0",
         f"https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js"],
        capture_output=True, text=True,
    ).stdout
    data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])["data"]
    factors = sorted(((item["d"], float(item["f"])) for item in data), reverse=True)
    if not factors:
        raise ValueError("no Sina adjustment factors")
    return factors


def ema(values: list[float], period: int) -> list[float]:
    factor = 2 / (period + 1)
    current = values[0]
    output = []
    for value in values:
        current = value if not output else value * factor + current * (1 - factor)
        output.append(current)
    return output


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    gains: list[float] = []
    losses: list[float] = []
    avg_gain = avg_loss = None
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
        if index < period:
            continue
        if index == period:
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
        else:
            assert avg_gain is not None and avg_loss is not None
            avg_gain = (avg_gain * (period - 1) + gains[-1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[-1]) / period
        output[index] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return output


def weekly(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for row in rows:
        date_value = dt.date.fromisoformat(row["date"])
        year, week, _ = date_value.isocalendar()
        groups[(year, week)].append(row)
    return [
        {
            "date": values[-1]["date"],
            "close": values[-1]["close"],
            "high": max(item["high"] for item in values),
            "low": min(item["low"] for item in values),
            "volume": sum(item["volume"] for item in values),
        }
        for _, values in sorted(groups.items())
    ]


def macd_status(values: list[float]) -> str:
    fast = ema(values, 12)
    slow = ema(values, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    for index in range(max(1, len(dif) - 6), len(dif)):
        if dif[index - 1] < dea[index - 1] and dif[index] >= dea[index] and dif[index] < 0 and dea[index] < 0:
            return "A 水下近6周金叉"
    gap = dif[-1] - dea[-1]
    old_gap = dif[-4] - dea[-4]
    if dif[-1] < 0 and dea[-1] < 0 and dif[-1] < dea[-1] and abs(gap) < abs(old_gap) and hist[-1] > hist[-4]:
        return "B 水下接近金叉"
    if dif[-1] < 0 and dea[-1] < 0 and hist[-1] > hist[-4]:
        return "C 水下绿柱改善"
    if dif[-1] < 0 and dea[-1] < 0:
        return "D 水下弱势"
    return "E 非水下"


def normalize_industry(value: str) -> str:
    return re.sub(r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫⅬⅭⅮⅯⅰⅱⅲⅳⅴⅵⅶⅷⅸⅹⅺⅻ\sⅡⅢ]+$", "", value).replace("Ⅱ", "").replace("Ⅲ", "").strip()


def fetch_sector_snapshot() -> dict[str, dict]:
    if INTRADAY_DIR:
        return {}  # sector context is observation only; skip the extra platform at 14:45
    rows = []
    for page in range(1, 7):
        params = {
            "pn": page,
            "pz": 100,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:90+t:2+f:!50",
            "fields": "f12,f14,f2,f3,f8,f62,f104,f105,f128,f140,f136",
        }
        url = "https://push2delay.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
        payload = None
        for attempt in range(3):
            try:
                payload = get_json(url) if attempt == 0 else get_json_with_curl(url)
                break
            except Exception:
                continue
        page_rows = (((payload or {}).get("data") or {}).get("diff") or [])
        rows.extend(page_rows)
        if len(page_rows) < 100:
            break
    result = {}
    for row in rows:
        name = str(row.get("f14") or "")
        if name:
            result[normalize_industry(name)] = row
    return result


def previous_codes() -> set[str]:
    if not LEGACY_CODES.exists():
        return set()
    return {
        line.split("\t", 1)[0].strip()
        for line in LEGACY_CODES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def limit_threshold(code: str) -> float:
    return 0.19 if code.startswith(("300", "301", "688")) else 0.095


def elasticity_metrics(code: str, rows: list[dict]) -> dict:
    sample = rows[-250:]
    threshold = limit_threshold(code)
    limit_days = 0
    max_limit_streak = 0
    current_streak = 0
    max_stage_gain = 0.0
    gain30_count = 0
    trough = sample[0]["low"]
    wave_armed = True
    peak_after_trigger = 0.0

    for index, row in enumerate(sample):
        if index > 0:
            daily_change = row["close"] / sample[index - 1]["close"] - 1
            if daily_change >= threshold:
                limit_days += 1
                current_streak += 1
                max_limit_streak = max(max_limit_streak, current_streak)
            else:
                current_streak = 0

        trough = min(trough, row["low"])
        gain = row["high"] / trough - 1
        max_stage_gain = max(max_stage_gain, gain)
        if wave_armed and gain >= 0.30:
            gain30_count += 1
            wave_armed = False
            peak_after_trigger = row["high"]
        elif not wave_armed:
            peak_after_trigger = max(peak_after_trigger, row["high"])
            if row["low"] <= peak_after_trigger * 0.82:
                trough = row["low"]
                wave_armed = True

    if max_limit_streak >= 2 or gain30_count >= 2 or max_stage_gain >= 0.70:
        grade = "高"
    elif limit_days >= 1 or gain30_count >= 1 or max_stage_gain >= 0.40:
        grade = "中"
    else:
        grade = "低"
    return {
        "grade": grade,
        "maxStageGain1y": round(max_stage_gain * 100, 1),
        "gain30Count": gain30_count,
        "limitUpDays": limit_days,
        "maxLimitStreak": max_limit_streak,
    }


def sector_lookup(industry: str, snapshots: dict[str, dict]) -> dict | None:
    key = normalize_industry(industry)
    if key in snapshots:
        return snapshots[key]
    aliases = {
        "IT服务": ["IT服务Ⅲ", "软件开发"],
        "电视广播": ["电视广播Ⅲ", "数字媒体"],
        "军工电子": ["军工电子Ⅲ", "航空装备Ⅲ"],
        "汽车零部件": ["汽车零部件Ⅲ"],
        "化学制品": ["化学制品Ⅲ"],
        "其他家电": ["其他家电Ⅲ"],
    }
    for alias in aliases.get(key, []):
        normalized = normalize_industry(alias)
        if normalized in snapshots:
            return snapshots[normalized]
    return None


def sector_state(snapshot: dict | None) -> tuple[str, str]:
    if not snapshot:
        return "待验证", "未匹配到行业快照"
    change = float(snapshot.get("f3") or 0)
    leader_gain = float(snapshot.get("f136") or 0)
    rising = int(snapshot.get("f104") or 0)
    falling = int(snapshot.get("f105") or 0)
    breadth = rising / max(rising + falling, 1)
    if leader_gain >= 5 and (change >= 0.5 or breadth >= 0.6):
        return "通过", f"板块{change:+.2f}%，领涨{leader_gain:+.2f}%，上涨宽度{breadth:.0%}"
    if leader_gain >= 3 or change > 0 or breadth >= 0.55:
        return "观察", f"板块{change:+.2f}%，领涨{leader_gain:+.2f}%，上涨宽度{breadth:.0%}"
    return "不通过", f"板块{change:+.2f}%，领涨{leader_gain:+.2f}%，上涨宽度{breadth:.0%}"


def analyze(code: str, financial: dict, snapshots: dict[str, dict]) -> dict | None:
    rows, source = fetch_kline(code)
    closes = [row["close"] for row in rows]
    weekly_rows = weekly(rows)
    weekly_closes = [row["close"] for row in weekly_rows]
    weekly_rsi_series = rsi(weekly_closes)
    daily_rsi_series = rsi(closes)
    if weekly_rsi_series[-1] is None or daily_rsi_series[-1] is None:
        return None

    close = rows[-1]["close"]
    ma = {period: sum(closes[-period:]) / period for period in [5, 10, 15, 30, 60, 120]}
    if close < ma[5] or close < ma[10]:
        return None
    one_year = rows[-250:]
    three_year = rows[-750:]
    rise_from_low = (close / min(row["low"] for row in one_year) - 1) * 100
    change20 = (close / rows[-21]["close"] - 1) * 100
    change60 = (close / rows[-61]["close"] - 1) * 100
    range60 = (max(row["high"] for row in rows[-60:]) / min(row["low"] for row in rows[-60:]) - 1) * 100
    if rise_from_low > MAX_RISE_FROM_LOW or change20 > MAX_20D_GAIN or range60 > MAX_60D_RANGE:
        return None

    weekly_rsi = float(weekly_rsi_series[-1])
    daily_rsi = float(daily_rsi_series[-1])
    if not (WEEKLY_RSI_RANGE[0] <= weekly_rsi <= WEEKLY_RSI_RANGE[1]):
        return None
    if not (DAILY_RSI_RANGE[0] <= daily_rsi <= DAILY_RSI_RANGE[1]):
        return None
    macd_class = macd_status(weekly_closes)
    if not macd_class.startswith(("A", "B")):
        return None

    rsi4 = weekly_rsi_series[-5] if len(weekly_rsi_series) >= 5 else None
    rsi8 = weekly_rsi_series[-9] if len(weekly_rsi_series) >= 9 else None
    rsi_trend_count = sum(
        1 for old in (rsi4, rsi8) if old is not None and weekly_rsi > float(old)
    )
    if rsi_trend_count == 0 and not macd_class.startswith("A"):
        return None

    industry = str(financial.get("BOARD_NAME") or "待分类")
    snapshot = sector_lookup(industry, snapshots)
    state, sector_evidence = sector_state(snapshot)
    if state in {"不通过", "待验证"}:
        return None

    elasticity = elasticity_metrics(code, rows)
    avg_amount20 = sum(row["close"] * row["volume"] * 100 for row in rows[-20:]) / 20
    volume_ratio = (
        sum(row["volume"] for row in rows[-5:]) / 5
        / max(sum(row["volume"] for row in rows[-20:]) / 20, 1)
    )
    base12w = (
        max(row["high"] for row in weekly_rows[-12:])
        / min(row["low"] for row in weekly_rows[-12:])
        - 1
    ) * 100
    drawdown3y = (close / max(row["high"] for row in three_year) - 1) * 100
    snapshot_leader = str((snapshot or {}).get("f128") or "待验证")
    snapshot_leader_code = str((snapshot or {}).get("f140") or "")
    snapshot_leader_gain = float((snapshot or {}).get("f136") or 0)
    if snapshot_leader_code == code or "ST" in snapshot_leader.upper() or "退" in snapshot_leader:
        return None

    score = 0
    score += 20 if rise_from_low <= 12 else 16 if rise_from_low <= 18 else 12
    score += 15 if 30 <= weekly_rsi <= 42 else 12
    score += 20 if macd_class.startswith("A") else 17
    score += 12 if close >= ma[15] and close >= ma[30] else 9 if close >= ma[15] or close >= ma[30] else 6
    score += 15 if state == "通过" else 9
    score += {"高": 10, "中": 8, "低": 4}[elasticity["grade"]]
    score += 5 if avg_amount20 >= 300_000_000 else 3 if avg_amount20 >= 100_000_000 else 1
    score += 3 if rsi_trend_count == 2 else 2 if rsi_trend_count == 1 else 0
    score += 2 if base12w <= 35 else 1

    if close >= ma[15] and close >= ma[30]:
        category = "v1.3图形确认"
    elif close >= ma[15] or close >= ma[30]:
        category = "5/10早修复·部分确认"
    else:
        category = "5/10早期修复"
    financial_state = "通过" if financial["PARENT_NETPROFIT"] >= 50_000_000 else "观察"
    financial_risk = (
        "最新归母净利润为正；扣非、现金流、质押与减持仍需F10复核"
    )
    reasons = [
        f"距一年低点{rise_from_low:.1f}%，20日涨幅{change20:+.1f}%，尚未脱离低位区",
        f"周RSI {weekly_rsi:.1f}，相比4/8周前改善{rsi_trend_count}项；周MACD为{macd_class}",
        f"已站上MA5/MA10，当前层级为{category}",
        f"{industry}开关{state}：{snapshot_leader} {snapshot_leader_code} {snapshot_leader_gain:+.2f}%",
    ]
    risks = [
        financial_risk,
        "行业强度为盘中/当日快照，需继续验证多日扩散和周线持续性",
    ]
    if not (close >= ma[15] and close >= ma[30]):
        risks.append("尚未完整收复MA15/MA30，早修复失败率高于图形确认层")
    elif volume_ratio < 0.8:
        risks.append("近5日量能低于20日均量，启动意愿仍需观察")
    else:
        risks.append("需人工复核周线二次回踩、放量长上影与上方套牢区")

    return {
        "code": code,
        "name": str(financial.get("SECURITY_NAME_ABBR") or code),
        "industry": industry,
        "date": rows[-1]["date"],
        "close": close,
        "profit": financial["PARENT_NETPROFIT"],
        "profitPeriod": str(financial.get("QDATE") or ""),
        "source": source,
        "score": score,
        "category": category,
        "sectorState": state,
        "sectorEvidence": sector_evidence,
        "sectorLeader": snapshot_leader,
        "sectorLeaderCode": snapshot_leader_code,
        "sectorLeaderGain": snapshot_leader_gain,
        "financialState": financial_state,
        "metrics": {
            "weeklyRsi": round(weekly_rsi, 1),
            "weeklyRsi4w": round(float(rsi4), 1) if rsi4 is not None else None,
            "weeklyRsi8w": round(float(rsi8), 1) if rsi8 is not None else None,
            "dailyRsi": round(daily_rsi, 1),
            "macdClass": macd_class,
            "riseFromYearLow": round(rise_from_low, 1),
            "change20d": round(change20, 1),
            "change60d": round(change60, 1),
            "range60d": round(range60, 1),
            "drawdown3y": round(drawdown3y, 1),
            "base12wRange": round(base12w, 1),
            "averageAmount20d": round(avg_amount20),
            "volumeRatio5v20": round(volume_ratio, 2),
            "above5": close >= ma[5],
            "above10": close >= ma[10],
            "above15": close >= ma[15],
            "above30": close >= ma[30],
            "above60": close >= ma[60],
            "above120": close >= ma[120],
        },
        "elasticity": elasticity,
        "reasons": reasons,
        "risks": risks,
        "action": (
            "观察板块强度能否持续，以及个股能否从MA5/10修复升级为MA15/30图形确认。"
            if category != "v1.3图形确认"
            else "观察站回MA15/30后的回踩是否缩量、周线箱体是否保持。"
        ),
        "sparkline": [{"date": row["date"], "close": round(row["close"], 2)} for row in rows[-80:]],
    }


def select_diversified(candidates: list[dict]) -> list[dict]:
    selected: list[dict] = []
    industry_counts: dict[str, int] = defaultdict(int)
    pass_target = 22
    for candidate in candidates:
        industry = normalize_industry(candidate["industry"])
        if industry_counts[industry] >= MAX_PER_INDUSTRY:
            continue
        if len(selected) < pass_target and candidate["sectorState"] != "通过":
            continue
        selected.append(candidate)
        industry_counts[industry] += 1
        if len(selected) == COUNT:
            return selected
    for candidate in candidates:
        if candidate in selected:
            continue
        industry = normalize_industry(candidate["industry"])
        if industry_counts[industry] >= MAX_PER_INDUSTRY:
            continue
        selected.append(candidate)
        industry_counts[industry] += 1
        if len(selected) == COUNT:
            break
    return selected


def render_markdown(selected: list[dict], scanned: int, passed: int, old_count: int) -> str:
    lines = [
        "# 最新要求重新筛选 30 只观察池 - v1.9",
        "",
        "本轮从当前A股重新计算，不把前面各轮名单合并成候选池。为保证是新找的样本，硬性排除旧看板中的 " + str(old_count) + " 只。",
        "",
        "本文件只用于选票框架研究，不构成个股推荐、买卖建议或交易指令。",
        "",
        "## 本轮硬条件",
        "",
        "- 最新归母净利润 >= 0.10 亿，剔除 ST/退市风险名称；比“只要不亏损”多加一层微利过滤。",
        "- 收盘价站上 MA5 和 MA10；MA15/30 用于再分层，不作硬性统一条件。",
        "- 距一年低点 <= 25%，20日涨幅 <= 12%，60日波动区间 <= 55%。",
        "- 周 RSI 28-50，日 RSI 35-60；周 RSI 对4/8周前至少改善1项，A类水下金叉可例外。",
        "- 周 MACD 只取 A 类水下近6周金叉或 B 类水下接近金叉。",
        "- 行业启动为独立开关：不通过的行业不进入本轮30只；同一行业最多3只。",
        "- 弹性独立记录：过去一年最大阶段涨幅、30%以上波段次数、涨停日数和最高连板。",
        "",
        f"- 扫描盈利A股：{scanned} 只；技术+行业开关通过：{passed} 只；最终分散化保留：{len(selected)} 只。",
        "",
        "## 30 只新样本",
        "",
        "| # | 股票 | 行业 | 数据日 | 收盘 | 距1年低点 | 20日 | 周RSI(4/8周前) | 周MACD | 均线层 | 板块开关/拉升标的 | 弹性 | 归母净利 | 评分 |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | --- | --- | --- | --- | --- | ---: | ---: |",
    ]
    for index, item in enumerate(selected, 1):
        metrics = item["metrics"]
        elasticity = item["elasticity"]
        rsi_history = f"{metrics['weeklyRsi']:.1f} ({metrics['weeklyRsi4w']}/{metrics['weeklyRsi8w']})"
        leader = f"{item['sectorState']} / {item['sectorLeader']} {item['sectorLeaderGain']:+.2f}%"
        elasticity_text = (
            f"{elasticity['grade']}；最大{elasticity['maxStageGain1y']:.0f}%；"
            f"30%波段{elasticity['gain30Count']}次；连板{elasticity['maxLimitStreak']}天"
        )
        lines.append(
            f"| {index} | {item['name']} {item['code']} | {item['industry']} | {item['date']} | {item['close']:.2f} | "
            f"{metrics['riseFromYearLow']:.1f}% | {metrics['change20d']:+.1f}% | {rsi_history} | "
            f"{metrics['macdClass']} | {item['category']} | {leader} | {elasticity_text} | "
            f"{item['profit'] / 100_000_000:.2f}亿 | {item['score']} |"
        )
    lines.extend(
        [
            "",
            "## 分层说明",
            "",
            "- `v1.3图形确认`：已站上 MA15/MA30，图形确认更强，但仍受低位和不过度上涨约束。",
            "- `5/10早修复·部分确认`：站上 MA5/10，只收复 MA15 或 MA30 之一。",
            "- `5/10早期修复`：尚未收复 MA15/30，位置更早，但失败率也更高。",
            "",
            "## 风险边界",
            "",
            "- 归母净利润为正不等于完整财务排雷；扣非净利、现金流、质押、减持、商誉、诉讼与监管处罚仍需F10复核。",
            "- 行业开关使用东方财富板块盘中/当日快照，用于确认“板块已有拉升股”，不代替多日扩散和周线持续性检查。",
            "- 腾讯前复权日K不可用时会回退新浪日K；数据日可能因停牌或源更新节奏不同。",
        ]
    )
    return "\n".join(lines) + "\n"


def build_dashboard(selected: list[dict], failures: list[dict]) -> dict:
    as_of = max((item["date"] for item in selected), default="")
    report_id = "fresh-v1-9-30"
    stocks = []
    for item in selected:
        stocks.append(
            {
                "code": item["code"],
                "name": item["name"],
                "industry": item["industry"],
                "direction": item["industry"],
                "firstSelectedDate": as_of,
                "firstSelectedPrice": round(item["close"], 2),
                "currentDate": item["date"],
                "currentPrice": round(item["close"], 2),
                "performance": 0.0,
                "trackedDays": 0,
                "firstCategory": item["category"],
                "currentCategory": item["category"],
                "sectorState": item["sectorState"],
                "sectorEvidence": item["sectorEvidence"],
                "sectorLeader": item["sectorLeader"],
                "sectorLeaderCode": item["sectorLeaderCode"],
                "sectorLeaderGain": item["sectorLeaderGain"],
                "financialState": item["financialState"],
                "parentNetProfit": item["profit"],
                "profitPeriod": item["profitPeriod"],
                "score": item["score"],
                "elasticity": item["elasticity"],
                "metrics": item["metrics"],
                "reasons": item["reasons"],
                "risks": item["risks"],
                "action": item["action"],
                "appearances": [
                    {
                        "code": item["code"],
                        "name": item["name"],
                        "direction": item["industry"],
                        "date": as_of,
                        "dataDate": item["date"],
                        "label": item["category"],
                        "reportId": report_id,
                        "reportVersion": "v1.9 / 全新30只",
                        "reportFile": OUTPUT_MD.name,
                    }
                ],
                "sparkline": item["sparkline"],
            }
        )
    return {
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "asOf": as_of,
        "methodologyVersion": "v1.9-fresh-30",
        "disclaimer": "本页面用于选票框架研究与观察池跟踪，不构成个股推荐、买卖建议或交易指令。",
        "reports": [
            {
                "id": report_id,
                "version": "v1.9 / 全新30只",
                "file": OUTPUT_MD.name,
                "date": as_of,
                "family": "最新要求重新筛选",
                "summary": "排除旧48只，按盈利、低位、MA5/10、RSI趋势、MACD水下修复、行业拉升标的与弹性重新筛选。",
                "selections": [item["code"] for item in selected],
                "stats": {
                    "count": len(selected),
                    "averagePerformance": 0.0,
                    "positiveCount": 0,
                    "strengthCount": 0,
                },
            }
        ],
        "stocks": stocks,
        "failures": failures,
        "sources": [
            "腾讯前复权日K；失败时回退新浪日K",
            "东方财富最新主要财务指标",
            "东方财富行业板块盘中/当日快照及领涨股",
        ],
    }


def main() -> None:
    old_codes = previous_codes()
    financials = fetch_financials()
    snapshots = fetch_sector_snapshot()
    candidates: list[dict] = []
    failures: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        futures = {
            executor.submit(analyze, code, financial, snapshots): (code, financial)
            for code, financial in financials.items()
            if code not in old_codes
        }
        for future in concurrent.futures.as_completed(futures):
            code, financial = futures[future]
            try:
                candidate = future.result()
                if candidate:
                    candidates.append(candidate)
            except Exception as exc:
                failures.append(
                    {
                        "code": code,
                        "name": financial.get("SECURITY_NAME_ABBR") or code,
                        "error": str(exc),
                    }
                )

    state_rank = {"通过": 2, "观察": 1, "待验证": 0}
    elasticity_rank = {"高": 2, "中": 1, "低": 0}
    candidates.sort(
        key=lambda item: (
            state_rank.get(item["sectorState"], 0),
            item["score"],
            elasticity_rank[item["elasticity"]["grade"]],
            -item["metrics"]["riseFromYearLow"],
        ),
        reverse=True,
    )
    selected = select_diversified(candidates)
    if len(selected) < COUNT:
        raise RuntimeError(f"only selected {len(selected)} candidates")

    OUTPUT_MD.write_text(
        render_markdown(selected, len(financials), len(candidates), len(old_codes)),
        encoding="utf-8",
    )
    OUTPUT_JSON.write_text(
        json.dumps(build_dashboard(selected, failures), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT_MD}")
    print(f"wrote {OUTPUT_JSON} with {len(selected)} fresh stocks; technical/sector pass={len(candidates)}; failures={len(failures)}")


if __name__ == "__main__":
    main()
