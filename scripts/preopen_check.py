#!/usr/bin/env python3
"""Pre-open check: compare last evening's picks with this morning's call-auction price.

Runs at 09:26 on trading days, after the 09:25 auction result is public, and sends a short
Feishu card. It does not re-screen anything; it only reads the latest report and one batch
quote request (Tencent, Sina as fallback).

Why: every model buys at the next open. Across all three lists (2023-07 ~ 2026-10, about
10 400 trades, output/backtest/gap-buckets.md), the 20-day excess vs CSI 1000 by opening
gap was: low open 1-3% +3.6%, within +/-1% +1.4%, high open 1-2% -1.0%, 2-3% -3.2%,
>3% -1.0%. So a high open of 2% or more is flagged as chasing, 1-2% as caution.

Usage:
  python3 scripts/preopen_check.py --send            # scheduled run
  python3 scripts/preopen_check.py --force           # outside the time window, dry run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DAILY_ROOT = ROOT / "output" / "daily"
sys.path.insert(0, str(ROOT / "scripts"))

SKIP_GAP = 0.02     # high open >= 2%: historical 20-day excess -3.2% (2-3%) / -1.0% (>3%)
CAUTION_GAP = 0.01  # high open 1-2%: -1.0%, median -2.5%
WINDOW = (dt.time(9, 25), dt.time(9, 50))  # launchd runs missed jobs on wake; don't send stale checks
LISTS = (("v13", "主推：右侧启动"), ("ucross", "低位水下金叉"), ("v26", "底部观察池（观察）"))


def latest_report(before: str) -> tuple[str, dict[str, Any]] | None:
    for directory in sorted((p for p in DAILY_ROOT.iterdir() if p.is_dir() and p.name[:4].isdigit()), reverse=True):
        if directory.name < before and (directory / "combined.json").exists():
            return directory.name, json.loads((directory / "combined.json").read_text(encoding="utf-8"))
    return None


def curl(url: str, referer: str | None = None) -> str:
    cmd = ["curl", "-sS", "-m", "10", "-H", "User-Agent: Mozilla/5.0"]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    raw = subprocess.run(cmd + [url], capture_output=True).stdout
    return raw.decode("gbk", errors="replace")


def symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def quotes_tencent(codes: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for line in curl("https://qt.gtimg.cn/q=" + ",".join(symbol(c) for c in codes)).splitlines():
        f = line.split("~")
        if len(f) > 30 and f[2] in codes:
            stamp = f[30]
            out[f[2]] = {"price": float(f[3] or 0), "prevClose": float(f[4] or 0), "open": float(f[5] or 0),
                         "date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}", "time": f"{stamp[8:10]}:{stamp[10:12]}",
                         "source": "腾讯"}
    return out


def quotes_sina(codes: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    text = curl("https://hq.sinajs.cn/list=" + ",".join(symbol(c) for c in codes), "https://finance.sina.com.cn")
    for line in text.splitlines():
        if '"' not in line:
            continue
        code = line.split("=")[0][-6:]
        f = line.split('"')[1].split(",")
        if len(f) > 31 and code in codes:
            out[code] = {"price": float(f[3] or 0), "prevClose": float(f[2] or 0), "open": float(f[1] or 0),
                         "date": f[30], "time": f[31][:5], "source": "新浪"}
    return out


def fetch_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    quotes = {}
    try:
        quotes = quotes_tencent(codes)
    except Exception as exc:
        print(f"[preopen] Tencent quotes failed: {exc}", file=sys.stderr)
    missing = [c for c in codes if c not in quotes]
    if missing:
        try:
            quotes.update(quotes_sina(missing))
        except Exception as exc:
            print(f"[preopen] Sina quotes failed: {exc}", file=sys.stderr)
    return quotes


def verdict(gap: float) -> str:
    if gap >= SKIP_GAP:
        return "放弃（高开追高）"
    if gap >= CAUTION_GAP:
        return "谨慎（偏高）"
    if gap <= -0.01:
        return "按计划（低开，先看有无利空）"
    return "按计划"


def build(signal_date: str, report: dict[str, Any], quotes: dict[str, dict[str, Any]], today: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for key, title in LISTS:
        for item in (report.get(key) or {}).get("selected", []):
            q = quotes.get(item["code"]) or {}
            auction = q.get("open") or q.get("price") or 0
            prev = q.get("prevClose") or float(item.get("close") or 0)
            gap = auction / prev - 1 if auction and prev else None
            rows.append({"list": key, "title": title, "code": item["code"], "name": item["name"],
                         "state": item.get("state", ""), "prevClose": prev, "auction": auction, "gap": gap,
                         "verdict": verdict(gap) if gap is not None else "行情缺失"})
    return {"date": today, "signalDate": signal_date, "rows": rows,
            "source": "/".join(sorted({q.get("source", "") for q in quotes.values()})) or "无"}


def card(result: dict[str, Any]) -> dict[str, Any]:
    rows = result["rows"]
    skip = sum(r["verdict"].startswith("放弃") for r in rows)
    caution = sum(r["verdict"].startswith("谨慎") for r in rows)
    elements = [{"tag": "markdown", "content": (
        f"**{result['signalDate']} 收盘选出，{result['date']} 集合竞价对比**｜行情：{result['source']}\n"
        f"高开≥2% 放弃；高开1~2% 谨慎；其余按计划。依据：三类清单约1万笔回测，"
        f"高开2~3%的20日超额-3.2%，高开1~2% -1.0%，低开1~3% +3.6%。"
    )}]
    for key, title in LISTS:
        items = [r for r in rows if r["list"] == key]
        if not items:
            continue
        lines = [f"### {title}（{len(items)}只）"]
        for r in sorted(items, key=lambda r: -(r["gap"] if r["gap"] is not None else -9)):
            gap = f"{r['gap']:+.2%}" if r["gap"] is not None else "待核"
            mark = "🔴 " if r["verdict"].startswith("放弃") else "🟡 " if r["verdict"].startswith("谨慎") else ""
            lines.append(f"{mark}{r['name']} {r['code']}｜昨收 {r['prevClose']:.2f}｜竞价 {r['auction']:.2f}（{gap}）｜{r['verdict']}")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "仅用于观察池研究，不构成投资建议。竞价价格到9:30前仍可能变化。"}]})
    return {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange" if skip else "blue",
                   "title": {"tag": "plain_text", "content": f"开盘前检查｜{result['date']}｜放弃{skip}只·谨慎{caution}只"}},
        "elements": elements}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--force", action="store_true", help="Run outside 09:25-09:50 (testing)")
    args = parser.parse_args()
    now = dt.datetime.now()
    today = now.date().isoformat()
    if now.weekday() >= 5:
        print(f"skip: {today} is a weekend")
        return
    if not args.force and not (WINDOW[0] <= now.time() <= WINDOW[1]):
        print(f"skip: {now:%H:%M} is outside the pre-open window")
        return
    found = latest_report(today)
    if not found:
        print("skip: no previous report")
        return
    signal_date, report = found
    codes = sorted({i["code"] for key, _ in LISTS for i in (report.get(key) or {}).get("selected", [])})
    if not codes:
        print(f"skip: {signal_date} report has no picks")
        return
    quotes = fetch_quotes(codes)
    if not quotes or not any(q.get("date") == today for q in quotes.values()):
        print(f"skip: no quotes dated {today} (holiday or feed down)")
        return
    result = build(signal_date, report, quotes, today)
    out_dir = DAILY_ROOT / today
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = card(result)
    (out_dir / "preopen.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "preopen-card.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in result["rows"]:
        gap = f"{r['gap']:+.2%}" if r["gap"] is not None else "n/a"
        print(f"{r['list']:6s} {r['code']} {r['name']}｜昨收 {r['prevClose']:.2f}｜竞价 {r['auction']:.2f}（{gap}）｜{r['verdict']}")
    if args.send:
        from run_daily_screen_and_notify import retry_operation, send_feishu
        response = retry_operation("Feishu pre-open send", lambda: send_feishu(payload), attempts=3, delay_seconds=20)
        (out_dir / "preopen-response.json").write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        print(f"sent pre-open check for {today}: {response}")


if __name__ == "__main__":
    main()
