#!/usr/bin/env python3
"""Run the daily unified stock model, archive factors, track performance, and notify Feishu."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DAILY_ROOT = ROOT / "output" / "daily"
TRACKING_PATH = DAILY_ROOT / "tracking.json"
sys.path.insert(0, str(ROOT / "scripts"))

from bottom_base_context import (  # noqa: E402
    TRACK_DAYS, TRACK_DAYS_V13, follow_up, profile_text, report_due, trend_text,
)
# Low-position underwater golden cross (600406 pattern): observed 40 days without the base-break
# stop, because most such cases dip below the low before resolving (underwater-cross-cases.md).
TRACK_DAYS_UCROSS = 40
KEYCHAIN_SERVICE = "codex-sideways-feishu-webhook"
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 600


def run_command(command: list[str]) -> None:
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}")


def run_optional_command(command: list[str]) -> None:
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        raise RuntimeError(f"optional command failed ({result.returncode}): {' '.join(command)}")


def retry_operation(
    label: str,
    operation: Any,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
) -> Any:
    attempts = max(1, int(attempts))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            print(f"[retry] {label} failed ({attempt}/{attempts}): {exc}", file=sys.stderr, flush=True)
            if attempt == attempts:
                break
            print(
                f"[retry] {label}: waiting {delay_seconds}s before reconnecting",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(max(0, int(delay_seconds)))
    assert last_error is not None
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last_error}") from last_error


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def update_tracking(models: list[tuple[str, dict[str, Any]]], date: str) -> dict[str, Any]:
    tracking = load_json(TRACKING_PATH) if TRACKING_PATH.exists() else {"version": 1, "records": {}}
    records = tracking.setdefault("records", {})
    for model_key, payload in models:
        for item in payload.get("selected", []):
            key = f"{model_key}:{item['code']}"
            close = float(item["close"])
            record = records.setdefault(key, {
                "model": model_key,
                "code": item["code"],
                "name": item["name"],
                "firstSelectedDate": date,
                "firstClose": close,
                "selectedCount": 0,
                "history": [],
            })
            event = {"date": date, "close": close, "state": item.get("state")}
            # Keyed by date so backfilled days slot into place in any run order.
            history = {entry["date"]: entry for entry in record.get("history", [])}
            history[date] = event
            record["history"] = [history[key] for key in sorted(history)]
            record["firstSelectedDate"] = record["history"][0]["date"]
            record["firstClose"] = record["history"][0]["close"]
            record["selectedCount"] = len(record["history"])
            record["lastSelectedDate"] = record["history"][-1]["date"]
            record["lastClose"] = record["history"][-1]["close"]
            if item.get("featured"):
                record["featured"] = True
            change = (close / float(record["firstClose"]) - 1) * 100
            item["tracking"] = {
                "firstSelectedDate": record["firstSelectedDate"],
                "firstClose": record["firstClose"],
                "changeSinceFirst": change,
                "selectedCount": record["selectedCount"],
            }
    tracking["updatedAt"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    save_json(TRACKING_PATH, tracking)
    return tracking


def refresh_follow_ups(tracking: dict[str, Any], date: str) -> dict[str, list[dict[str, Any]]]:
    """Recompute the follow-up of recent picks as of `date`: v2.6 40 days with the base-break
    stop, v1.3 20 days without a stop."""
    horizon = (dt.date.fromisoformat(date) - dt.timedelta(days=TRACK_DAYS * 2)).isoformat()
    by_model: dict[str, list[dict[str, Any]]] = {"v13": [], "v26": [], "ucross": []}
    for record in tracking.get("records", {}).values():
        if record.get("model") not in by_model or not horizon <= record["firstSelectedDate"] <= date:
            continue
        try:
            if record["model"] == "v13":
                record["followUp"] = follow_up(record, date, TRACK_DAYS_V13, use_stop=False)
            elif record["model"] == "ucross":
                record["followUp"] = follow_up(record, date, TRACK_DAYS_UCROSS, use_stop=False)
            else:
                record["followUp"] = follow_up(record, date)
        except Exception as exc:
            record["followUp"] = {"status": "数据待复核", "final": False, "error": str(exc)}
        by_model[record["model"]].append(record)
    save_json(TRACKING_PATH, tracking)
    return {model: sorted(rows, key=lambda r: r["firstSelectedDate"]) for model, rows in by_model.items()}


def annotate_report_due(lists: list[list[dict[str, Any]]], date: str) -> None:
    """Add a display-only "report due" tag to the day's picks (needs a few API calls)."""
    codes = sorted({item["code"] for items in lists for item in items})
    if not codes:
        return
    results: dict[str, Any] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        for code, due in zip(codes, executor.map(lambda c: report_due(c, date), codes)):
            results[code] = due
    for items in lists:
        for item in items:
            item["reportDue"] = results.get(item["code"])


def intraday_check(date: str, lists: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    """Compare the 14:45 pre-screen with the close and append to the running log, so the
    preview's reliability is measured on real days (scripts/intraday_screen.py)."""
    path = ROOT / "output" / "intraday" / date / "combined.json"
    if not path.exists():
        return None
    pre = json.loads(path.read_text(encoding="utf-8"))
    if pre.get("stamp") == "收盘回放":
        return None
    result: dict[str, Any] = {"date": date, "stamp": pre.get("stamp")}
    for key, items in lists.items():
        a = {i["code"] for i in pre.get("selected", {}).get(key, [])}
        b = {i["code"] for i in items}
        result[key] = {"preview": len(a), "close": len(b), "both": len(a & b),
                       "droppedAtClose": sorted(a - b), "newAtClose": sorted(b - a)}
    log = ROOT / "output" / "intraday" / "consistency.jsonl"
    seen = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    if not any(x.get("date") == date for x in seen):
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def intraday_check_text(check: dict[str, Any] | None) -> str:
    if not check:
        return ""
    names = {"v13": "主推", "ucross": "水下金叉", "v26": "底部池"}
    parts = [f"{names[k]} 预选{v['preview']}/收盘{v['close']}/一致{v['both']}" for k, v in check.items() if k in names]
    return f"14:45预选核对（{check.get('stamp')}）：" + "；".join(parts)


def regime_status(regime: dict[str, Any]) -> str:
    if not regime.get("available"):
        return "大盘环境待核"
    return "仅作参考：回测中大盘均线状态不能稳定区分好坏时期，不作为开关；收益主要取决于入选后大盘走势"


def follow_up_card_lines(records: list[dict[str, Any]], date: str) -> list[str]:
    """Compact card version: counts, launches, near-stop names and recent endings."""
    lines = follow_up_lines(records, date)
    summary, detail = lines[0], lines[1:]
    launched = [line for line in detail if line.startswith("启动中")]
    near_stop = [
        f"{r['name']}({r['followUp']['stopDistance']:+.1%})"
        for r in records
        if r["followUp"]["status"] == "跟踪中" and r["followUp"].get("useStop", True)
        and "stopDistance" in r["followUp"] and r["followUp"]["stopDistance"] <= 0.015
    ]
    ended = [line.split("｜")[1] for line in detail if line.startswith(("破位失效", "到期"))]
    out = [summary, *launched]
    if near_stop:
        out.append("接近破位(≤1.5%)：" + "、".join(near_stop))
    if ended:
        out.append("近7日结束：" + "、".join(ended))
    out.append("完整跟踪表见每日 report.md")
    return out


def follow_up_lines(records: list[dict[str, Any]], date: str) -> list[str]:
    active = [r for r in records if r["followUp"]["status"] in ("跟踪中", "启动中")]
    recent_start = (dt.date.fromisoformat(date) - dt.timedelta(days=7)).isoformat()
    ended = [r for r in records if r["followUp"].get("final") and r["followUp"].get("statusDate", "") >= recent_start]
    lines = [f"跟踪中 {sum(r['followUp']['status'] == '跟踪中' for r in active)}只｜启动中 {sum(r['followUp']['status'] == '启动中' for r in active)}只｜近7日结束 {len(ended)}只"]
    for r in active + ended:
        f = r["followUp"]
        if "change" not in f:
            continue
        stop_text = f"｜距破位{f['stopDistance']:+.1%}" if f.get("useStop", True) else ""
        lines.append(
            f"{f['status']}｜{'★' if r.get('featured') else ''}{r['name']} {r['code']}｜首次 {r['firstSelectedDate']}｜"
            f"第{f['days']}/{f.get('trackDays', TRACK_DAYS)}日｜现{f['change']:+.1%}｜最高{f['maxUp']:+.1%}{stop_text}"
        )
    return lines


def yi(value: Any) -> str:
    try:
        return f"{float(value) / 100_000_000:.1f}亿"
    except (TypeError, ValueError):
        return "待核"


def pct(value: Any, digits: int = 1, signed: bool = False) -> str:
    try:
        number = float(value)
        return f"{number:+.{digits}f}%" if signed else f"{number:.{digits}f}%"
    except (TypeError, ValueError):
        return "待核"


def short_text(value: Any, limit: int = 54) -> str:
    text = str(value or "无明显事项").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def risk_summary(item: dict[str, Any]) -> str:
    financial = item.get("financialRisk") or {}
    notes = financial.get("riskNotes") or financial.get("financialNotes") or []
    return short_text(notes[0] if notes else "无明显事项")


def common_factor_lines(item: dict[str, Any]) -> list[str]:
    tracking = item.get("tracking") or {}
    sector = item.get("sector") or {}
    persistence = item.get("sectorPersistence") or {}
    sector_status = (item.get("conditionChecks") or {}).get("sector", "待核")
    market = item.get("market") or {}
    elasticity = item.get("elasticity") or {}
    macd = item.get("dailyMacd") or {}
    return [
        (
            f"行业 {item.get('industry', '待分类')}｜收盘 {float(item.get('close', 0)):.2f}｜"
            f"首次 {tracking.get('firstSelectedDate', item.get('date'))} {pct(tracking.get('changeSinceFirst', 0), signed=True)}"
        ),
        (
            f"低点+{pct(item.get('riseRecentLow'))}｜5/10/20日 "
            f"{pct(item.get('change5'), signed=True)}/{pct(item.get('change10'), signed=True)}/{pct(item.get('change20'), signed=True)}"
        ),
        (
            f"RSI 日/周 {float(item.get('dailyRsi', 0)):.1f}/{float(item.get('weeklyRsi', 0)):.1f}｜"
            f"MA5/10 {pct(item.get('maDistance5'), signed=True)}/{pct(item.get('maDistance10'), signed=True)}｜"
            f"MA15/30 {pct(item.get('maDistance15'), signed=True)}/{pct(item.get('maDistance30'), signed=True)}｜"
            f"{macd.get('state', 'MACD待核')}"
        ),
        (
            f"量5/20 {float(item.get('volumeRatio5v20', 0)):.2f}｜行业 {sector_status}｜扩散 {persistence.get('activeDays5', 0)}/5｜"
            f"领涨 {sector.get('leader', '待核')} {pct(sector.get('leaderGain'), signed=True)}"
        ),
        (
            f"市值 {yi(market.get('totalMarketCap'))}｜20日均额 {yi(market.get('averageAmount20'))}｜"
            f"弹性 年内最大{pct(elasticity.get('maxStageGain1y'))}/30%段{elasticity.get('gain30Count', 0)}次"
        ),
        profile_text(item.get("baseProfile")),
        trend_text(item.get("fundamentalTrend")) + (
            f"｜文献合成分 {float(item['literatureScore']):.0%}" if item.get("literatureScore") is not None else ""
        ),
        (
            f"财务 {item.get('financialState', '待核')}｜风险 {item.get('riskState', '待核')}｜"
            f"{risk_summary(item)}"
            + (f"｜⚠ 财报 {item['reportDue']['date']} 披露（{item['reportDue']['days']}天后）" if item.get("reportDue") else "")
        ),
    ]


def stock_block(item: dict[str, Any], index: int, model_key: str) -> str:
    title = f"**{index}. {item['name']} {item['code']}｜{item.get('state', '')}**"
    lines = common_factor_lines(item)
    if model_key == "v13":
        lines.insert(4, (
            f"突破 {item.get('breakoutDate', '待核')}｜平台上方{pct(item.get('currentAbovePlatformPct'), signed=True)}｜"
            f"突破量比 {float(item.get('breakoutVolumeRatio', 0)):.2f}｜站上15/30 {item.get('daysAbove1530', 0)}日"
        ))
    return title + "\n" + "\n".join(lines)


def featured_split(v13: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items = v13.get("selected", [])
    return [i for i in items if i.get("featured")], [i for i in items if not i.get("featured")]


def brief_line(item: dict[str, Any]) -> str:
    trend = item.get("fundamentalTrend") or {}
    tags = "/".join(t for t, on in (("净利改善", trend.get("improving")), ("户数连降", trend.get("holdersDown2"))) if on) or "基本面未改善"
    return (
        f"{item['name']} {item['code']}｜{item.get('state', '')}｜{item.get('industry', '')}｜"
        f"收盘 {float(item.get('close', 0)):.2f}｜距低点+{pct(item.get('riseRecentLow'))}｜{tags}"
    )


def ucross_line(item: dict[str, Any]) -> str:
    trend = item.get("fundamentalTrend") or {}
    yoy = trend.get("profitYoy")
    tier_text = {0: "基本面向好", 1: "基本面改善其一"}.get(trend.get("tier"), "基本面未改善")
    return (
        f"{item['name']} {item['code']}｜{item.get('industry', '')}｜金叉 {item['crossDate']}｜收盘 {float(item['close']):.2f}｜"
        f"低点 {item['lowDate']} {float(item['low']):.2f}（距低+{item['riseLow']:.1%}）｜前期跌{item['drop']:+.0%}｜"
        f"{tier_text}｜净利同比 {'待核' if yoy is None else f'{yoy:+.0f}%'}"
        + ("｜已再次死叉" if item.get("deadCrossSince") else "")
    )


UCROSS_NOTE = (
    "价格在60日低点5%内、前期跌≥12%、底背离、此前20日无金叉。历史上约一半能走出来，"
    "同月内并不比其他股票稳定更好；结果主要看大盘。观察40日，不设破位。"
)


def build_feishu_card(
    date: str,
    unified: dict[str, Any],
    v13: dict[str, Any],
    follow_ups: dict[str, list[dict[str, Any]]],
    ucross: dict[str, Any] | None = None,
) -> dict[str, Any]:
    regime = unified.get("marketRegime") or v13.get("marketRegime") or {}
    featured, others = featured_split(v13)
    pool = unified.get("selected", [])
    elements: list[dict[str, Any]] = [{
        "tag": "markdown",
        "content": (
            f"**数据日：{date}**｜腾讯前复权K线（新浪+复权因子备源） + 东方财富财务/公告\n"
            f"**大盘：{regime.get('label', '待核')}**\n{regime_status(regime)}\n"
            f"主推 {len(featured)}只｜底部观察池 {len(pool)}只"
            + (f"\n{intraday_check_text((ucross or {}).get('intradayCheck'))}" if (ucross or {}).get("intradayCheck") else "")
        ),
    }]
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": f"### 主推：右侧启动（{TRACK_DAYS_V13}日观察）\n刚突破底部平台；排序：基本面向好优先。回测中持有{TRACK_DAYS_V13}日优于更长持有。"})
    if featured:
        for index, item in enumerate(featured, 1):
            elements.append({"tag": "markdown", "content": stock_block(item, index, "v13")})
    else:
        elements.append({"tag": "markdown", "content": "今日没有满足主推条件的股票。"})
    if others:
        elements.append({"tag": "markdown", "content": "### 其他右侧启动观察（基本面未达主推）\n" + "\n".join(brief_line(i) for i in others)})
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": "### 底部观察池（v2.6，观察、不作入场依据）"})
    if pool:
        for index, item in enumerate(pool, 1):
            elements.append({"tag": "markdown", "content": stock_block(item, index, "v26")})
    else:
        elements.append({"tag": "markdown", "content": "今日没有股票满足底部观察条件。"})
    ucross_items = (ucross or {}).get("selected", [])
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": (
        f"### 低位水下金叉观察（国电南瑞式，{len(ucross_items)}只）\n{UCROSS_NOTE}\n"
        + ("\n".join(ucross_line(i) for i in ucross_items) if ucross_items else "今日没有新出现的低位水下金叉。")
    )})
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": (
        "### 跟踪（★=主推）\n"
        f"**右侧启动（{TRACK_DAYS_V13}日，不设破位）**\n" + "\n".join(follow_up_card_lines(follow_ups.get("v13", []), date))
        + f"\n**底部观察池（{TRACK_DAYS}日，跌破入选前60日低点5%即失效）**\n" + "\n".join(follow_up_card_lines(follow_ups.get("v26", []), date))
        + f"\n**低位水下金叉（{TRACK_DAYS_UCROSS}日，不设破位）**\n" + "\n".join(follow_up_card_lines(follow_ups.get("ucross", []), date))
    )})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "仅用于观察池研究，不构成投资建议；财务和公告事项仍需核验正文。回测中没有任何规则能稳定跑赢中证1000，收益主要取决于大盘。"}],
    })
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "template": "red" if featured else "blue",
                "title": {"tag": "plain_text", "content": f"A股底部启动观察｜{date}｜主推{len(featured)}只"},
            },
            "elements": elements,
        },
    }


def get_webhook() -> str:
    value = os.environ.get("FEISHU_WEBHOOK_URL")
    if value:
        return value.strip()
    result = subprocess.run(
        ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", KEYCHAIN_SERVICE, "-w"],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Feishu webhook is not configured in environment or macOS Keychain")
    return result.stdout.strip()


def send_feishu(payload: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "--max-time", "30",
            "-X", "POST",
            "-H", "Content-Type: application/json; charset=utf-8",
            "--data-binary", "@-",
            get_webhook(),
        ],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Feishu curl request failed: {result.stderr.strip()}")
    response = json.loads(result.stdout)
    code = response.get("code", response.get("StatusCode", 0))
    if code not in (0, "0", None):
        raise RuntimeError(f"Feishu webhook rejected message: {response}")
    return response


def render_combined_report(
    date: str,
    unified: dict[str, Any],
    v13: dict[str, Any],
    follow_ups: dict[str, list[dict[str, Any]]],
    ucross: dict[str, Any] | None = None,
) -> str:
    regime = unified.get("marketRegime") or v13.get("marketRegime") or {}
    featured, others = featured_split(v13)
    lines = [
        f"# A股底部启动每日观察 {date}",
        "",
        f"- 大盘环境：{regime.get('label', '待核')}。{regime_status(regime)}。",
        f"- 主推（右侧启动，{TRACK_DAYS_V13}日观察）：{len(featured)}只；底部观察池：{len(unified.get('selected', []))}只。",
        "- 不足数量不放宽条件；均线只作为状态和压力字段。",
        *([f"- {intraday_check_text((ucross or {}).get('intradayCheck'))}"] if (ucross or {}).get("intradayCheck") else []),
        "",
        f"## 主推：右侧启动（{TRACK_DAYS_V13}日观察）",
        "",
    ]
    lines += [block for index, item in enumerate(featured, 1) for block in (stock_block(item, index, "v13"), "")] or ["今日没有满足主推条件的股票。", ""]
    lines += ["", "## 底部观察池（v2.6，观察、不作入场依据）", ""]
    lines += [block for index, item in enumerate(unified.get("selected", []), 1) for block in (stock_block(item, index, "v26"), "")] or ["当日无完整满足样本。", ""]
    ucross_items = (ucross or {}).get("selected", [])
    lines += ["## 低位水下金叉观察（国电南瑞式）", "", UCROSS_NOTE, ""]
    lines += [f"- {ucross_line(item)}" for item in ucross_items] or ["- 今日没有新出现的低位水下金叉。"]
    lines.append("")
    for title, key in ((f"右侧启动（{TRACK_DAYS_V13}日，不设破位）", "v13"), (f"底部观察池（{TRACK_DAYS}日，跌破5%失效）", "v26"),
                       (f"低位水下金叉（{TRACK_DAYS_UCROSS}日，不设破位）", "ucross")):
        lines += [f"## 跟踪：{title}（★=主推）", "", *[f"- {line}" for line in follow_up_lines(follow_ups.get(key, []), date)], ""]
    lines.append("本报告只用于观察池研究，不构成投资建议。")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Send the generated card to Feishu")
    parser.add_argument(
        "--resend-date",
        help="Resend an already generated Feishu card from output/daily/YYYY-MM-DD without rerunning the screen",
    )
    parser.add_argument("--allow-stale", action="store_true", help="Allow data dates different from the local date for testing")
    parser.add_argument("--date", help="Expected local date in YYYY-MM-DD format")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Re-screen the past trading day given by --date; skips Bitable sync and never sends",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=DEFAULT_RETRY_ATTEMPTS,
        help="Total attempts for network-backed stages, including the initial attempt",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=int,
        default=DEFAULT_RETRY_DELAY_SECONDS,
        help="Delay between failed attempts; defaults to 600 seconds",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.resend_date:
        resend_date = dt.date.fromisoformat(args.resend_date).isoformat()
        card_path = DAILY_ROOT / resend_date / "feishu-card.json"
        if not card_path.exists():
            raise RuntimeError(f"cannot resend: missing {card_path}")
        response = retry_operation(
            "Feishu webhook resend",
            lambda: send_feishu(load_json(card_path)),
            attempts=args.retry_attempts,
            delay_seconds=args.retry_delay_seconds,
        )
        save_json(DAILY_ROOT / resend_date / "feishu-response.json", response)
        print(f"resent Feishu report for {resend_date}: {response}")
        return

    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now().astimezone().date()
    date = today.isoformat()
    if today.weekday() >= 5:
        print(f"skip: {date} is a weekend")
        return

    if args.backfill and not args.date:
        raise SystemExit("--backfill requires --date")
    as_of_args = ["--as-of", date] if args.backfill else []

    from screen_latest_30 import prune_kline_cache

    prune_kline_cache()
    daily_dir = DAILY_ROOT / date
    daily_dir.mkdir(parents=True, exist_ok=True)
    unified_json = daily_dir / "v26.json"
    unified_md = daily_dir / "v26.md"
    v13_json = daily_dir / "v13.json"
    v13_md = daily_dir / "v13.md"
    retry_operation(
        "v2.6 market screening",
        lambda: run_command([
            sys.executable,
            str(ROOT / "scripts" / "screen_v26_unified.py"),
            "--count", "10",
            "--output", str(unified_md),
            "--json-output", str(unified_json),
            *as_of_args,
        ]),
        attempts=args.retry_attempts,
        delay_seconds=args.retry_delay_seconds,
    )
    retry_operation(
        "v1.3 market screening",
        lambda: run_command([
            sys.executable,
            str(ROOT / "scripts" / "screen_v13_right_start.py"),
            "--count", "5",
            "--output", str(v13_md),
            "--json-output", str(v13_json),
            *as_of_args,
        ]),
        attempts=args.retry_attempts,
        delay_seconds=args.retry_delay_seconds,
    )

    # Observation list; a failure here must not block the main report.
    ucross_json = daily_dir / "ucross.json"
    try:
        run_command([
            sys.executable, str(ROOT / "scripts" / "research" / "underwater_cross.py"), "live",
            "--as-of", date, "--days", "1", "--json-output", str(ucross_json),
        ])
    except Exception as exc:
        print(f"[ucross] skipped: {exc}", file=sys.stderr)
    unified = load_json(unified_json)
    v13 = load_json(v13_json)
    ucross = load_json(ucross_json) if ucross_json.exists() else {"selected": []}
    # A quote snapshot that only covers part of the market shows up as thousands of "data"
    # rejections and an empty pick list (2026-11-02). Refuse to publish that as "0 stocks".
    stages = (unified.get("diagnostics") or {}).get("stageCounts") or {}
    data_rejects = stages.get("data", 0) + stages.get("quote", 0)
    if not unified.get("selected") and data_rejects > 0.5 * len(unified.get("allCandidates") or []) + 0.5 * 5000:
        raise RuntimeError(
            f"v2.6 rejected {data_rejects} stocks for missing data with no picks: "
            f"quote snapshot likely incomplete ({unified.get('diagnostics')})"
        )
    data_date = str(unified.get("dataThrough") or "")
    if not data_date:
        raise RuntimeError("unified model data date is missing")
    v13_data_date = str(v13.get("dataThrough") or "")
    if not v13_data_date:
        raise RuntimeError("v1.3 model data date is missing")
    if data_date != date and not args.allow_stale:
        status = {"status": "skipped_stale", "expectedDate": date, "dataDate": data_date}
        save_json(daily_dir / "status.json", status)
        print(f"skip: expected {date}, latest market data is {data_date}")
        return
    if v13_data_date != date and not args.allow_stale:
        status = {"status": "skipped_stale", "expectedDate": date, "dataDate": v13_data_date, "model": "v13"}
        save_json(daily_dir / "status.json", status)
        print(f"skip: expected {date}, latest v1.3 market data is {v13_data_date}")
        return

    annotate_report_due([unified.get("selected", []), v13.get("selected", []), ucross.get("selected", [])], data_date)
    ucross["intradayCheck"] = intraday_check(data_date, {"v13": v13.get("selected", []), "ucross": ucross.get("selected", []),
                                                          "v26": unified.get("selected", [])})
    tracking = update_tracking([("v26", unified), ("v13", v13), ("ucross", ucross)], data_date)
    follow_ups = refresh_follow_ups(tracking, data_date)
    combined = {
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataThrough": data_date,
        "v26": unified,
        "v13": v13,
        "ucross": ucross,
    }
    save_json(daily_dir / "combined.json", combined)
    (daily_dir / "report.md").write_text(render_combined_report(data_date, unified, v13, follow_ups, ucross), encoding="utf-8")
    card = build_feishu_card(data_date, unified, v13, follow_ups, ucross)
    save_json(daily_dir / "feishu-card.json", card)
    if args.backfill:
        save_json(daily_dir / "status.json", {"status": "backfilled", "dataDate": data_date})
        print(f"backfill complete: {daily_dir}")
        return
    retry_operation(
        "Feishu Bitable sync",
        lambda: run_optional_command([sys.executable, str(ROOT / "scripts" / "sync_feishu_bitable.py")]),
        attempts=args.retry_attempts,
        delay_seconds=args.retry_delay_seconds,
    )

    if args.send:
        response = retry_operation(
            "Feishu webhook send",
            lambda: send_feishu(card),
            attempts=args.retry_attempts,
            delay_seconds=args.retry_delay_seconds,
        )
        save_json(daily_dir / "feishu-response.json", response)
        print(f"sent Feishu report for {data_date}: {response}")
    else:
        print(f"dry-run complete: {daily_dir}")


if __name__ == "__main__":
    main()
