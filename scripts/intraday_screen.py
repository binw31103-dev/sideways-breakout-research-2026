#!/usr/bin/env python3
"""14:45 intraday pre-screen: the three daily lists on a live bar, sent to Feishu ~14:50.

Data, so that no single platform carries the run:
  history     yesterday's K-line cache from the 17:05 job (no downloads at 14:45)
  today's bar Tencent + Sina live quotes, cross-checked; Eastmoney only as a backup
              (scripts/multi_source_quotes.py); volume scaled by 1/0.926 to a full-day estimate
  financials  cached Eastmoney list; per-candidate fundamentals/announcements as usual
The screens themselves are the unchanged production scripts, run with SIDEWAYS_INTRADAY_DIR
set (see screen_latest_30.fetch_kline / screen_red_zone_5.fetch_daily_quotes).

This is a preview. The 17:05 run on the close is the record: tracking is only updated
there, and it reports how many 14:45 picks were confirmed (output/intraday/consistency.jsonl).
On a 160-stock sample, the price moved a median 0.6% between 14:45 and the close and 18% of
stocks moved more than 1%, so picks near a threshold can flip.

Usage:
  python3 scripts/intraday_screen.py --send                       # scheduled 14:45 run
  python3 scripts/intraday_screen.py --simulate 2026-10-30        # replay a past day with its closing quotes
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

INTRADAY_ROOT = ROOT / "output" / "intraday"
WINDOW = (dt.time(14, 40), dt.time(14, 56))
VOLUME_SHARE_1445 = 0.926
LISTS = (("v13", "主推：右侧启动"), ("ucross", "低位水下金叉"), ("v26", "底部观察池（观察）"))


def run(cmd: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr, end="")
        raise RuntimeError(f"failed: {' '.join(cmd[1:3])}")


def previous_trading_day(date: str) -> str:
    """Latest day before `date` that is in the K-line cache or has a saved closing snapshot."""
    root = ROOT / "output" / "cache"
    days = {p.stem for p in (root / "quotes").glob("*.json")} if (root / "quotes").exists() else set()
    for directory in sorted((root / "kline").iterdir()):
        if directory.is_dir() and directory.name <= date:
            sample = sorted(directory.glob("*.json"))[:200]
            for path in sample:
                days.update(r["date"] for r in json.loads(path.read_text(encoding="utf-8"))["rows"][-5:])
    return max(d for d in days if d < date)


def all_codes() -> list[str]:
    from screen_latest_30 import fetch_financials
    return sorted(fetch_financials(include_financial_yellow=True))


def line(item: dict[str, Any], quotes: dict[str, Any]) -> str:
    q = quotes.get(item["code"]) or {}
    change = q["close"] / q["prevClose"] - 1 if q.get("prevClose") else None
    trend = item.get("fundamentalTrend") or {}
    tier = {0: "基本面向好", 1: "基本面改善其一"}.get(trend.get("tier"), "")
    flag = "" if q.get("check") == "双源一致" else f"｜⚠{q.get('check', '行情待核')}"
    return (f"{item['name']} {item['code']}｜{item.get('state', '')}｜{item.get('industry', '')}｜现价 {q.get('close', 0):.2f}"
            + (f"（今日{change:+.2%}）" if change is not None else "") + (f"｜{tier}" if tier else "") + flag)


def build_card(date: str, stamp: str, lists: dict[str, list[dict]], quotes: dict[str, Any], summary: dict[str, Any]) -> dict:
    counts = summary["counts"]
    elements = [{"tag": "markdown", "content": (
        f"**{date} {stamp} 盘中数据预选**｜行情：{'、'.join(f'{k}{v}只' for k, v in summary['perSource'].items())}；"
        f"双源一致 {counts['双源一致']}、单源 {counts['单源']}、不一致剔除 {counts['不一致剔除']}\n"
        f"成交量按14:45约占全天{VOLUME_SHARE_1445:.0%}折算。**以17:05收盘版为准**：14:45到收盘价格中位变动约0.6%，"
        f"约两成股票变动超过1%，临界的票可能变化。"
    )}]
    for key, title in LISTS:
        items = lists.get(key, [])
        body = "\n".join(line(i, quotes) for i in items) if items else "暂无"
        elements.append({"tag": "markdown", "content": f"### {title}（{len(items)}只）\n{body}"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "仅用于观察池研究，不构成投资建议。盘中预选未写入跟踪台账。"}]})
    return {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"template": "purple",
                   "title": {"tag": "plain_text", "content": f"尾盘预选｜{date} {stamp}｜主推{len(lists.get('v13', []))}只"}},
        "elements": elements}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--force", action="store_true", help="Run outside 14:40-14:56 (testing)")
    parser.add_argument("--simulate", help="Replay this past trading day: cache before it + its closing quotes")
    args = parser.parse_args()
    now = dt.datetime.now()
    date = args.simulate or now.date().isoformat()
    if not args.simulate:
        if now.weekday() >= 5:
            print(f"skip: {date} is a weekend")
            return
        if not args.force and not (WINDOW[0] <= now.time() <= WINDOW[1]):
            print(f"skip: {now:%H:%M} is outside the intraday window")
            return
    out_dir = INTRADAY_ROOT / date
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    from multi_source_quotes import merged_quotes
    codes = all_codes()
    share = 1.0 if args.simulate else VOLUME_SHARE_1445
    quotes, summary = merged_quotes(codes, date, share)
    print(f"[intraday] quotes {summary}")
    if len(quotes) < 0.5 * len(codes):
        (out_dir / "status.json").write_text(json.dumps({"status": "skipped_no_quotes", **summary}, ensure_ascii=False), encoding="utf-8")
        print(f"skip: only {len(quotes)}/{len(codes)} stocks have a live bar dated {date} (holiday or feeds down)")
        return
    (out_dir / "quotes.json").write_text(json.dumps(quotes, ensure_ascii=False), encoding="utf-8")
    (out_dir / "quotes-check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    env = {**os.environ, "SIDEWAYS_INTRADAY_DIR": str(out_dir), "SIDEWAYS_INTRADAY_DATE": date,
           "SIDEWAYS_INTRADAY_PREV": previous_trading_day(date)}
    print(f"[intraday] previous trading day {env['SIDEWAYS_INTRADAY_PREV']}")
    py = sys.executable
    run([py, str(ROOT / "scripts" / "screen_v13_right_start.py"), "--count", "5",
         "--output", str(out_dir / "v13.md"), "--json-output", str(out_dir / "v13.json")], env)
    run([py, str(ROOT / "scripts" / "research" / "underwater_cross.py"), "live", "--as-of", date, "--days", "1",
         "--json-output", str(out_dir / "ucross.json")], env)
    run([py, str(ROOT / "scripts" / "screen_v26_unified.py"), "--count", "10",
         "--output", str(out_dir / "v26.md"), "--json-output", str(out_dir / "v26.json")], env)

    lists = {key: json.loads((out_dir / f"{key}.json").read_text(encoding="utf-8")).get("selected", []) for key, _ in LISTS}
    stamp = "收盘回放" if args.simulate else now.strftime("%H:%M")
    combined = {"date": date, "stamp": stamp, "quotes": summary,
                "selected": {k: [{"code": i["code"], "name": i["name"], "state": i.get("state")} for i in v] for k, v in lists.items()},
                "seconds": round(time.time() - started, 1)}
    (out_dir / "combined.json").write_text(json.dumps(combined, ensure_ascii=False, indent=1), encoding="utf-8")
    card = build_card(date, stamp, lists, quotes, summary)
    (out_dir / "feishu-card.json").write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[intraday] done in {combined['seconds']}s: " + "，".join(f"{k} {len(v)}" for k, v in lists.items()))
    if args.send and not args.simulate:
        from run_daily_screen_and_notify import retry_operation, send_feishu
        response = retry_operation("Feishu intraday send", lambda: send_feishu(card), attempts=2, delay_seconds=15)
        (out_dir / "feishu-response.json").write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        print(f"sent intraday pre-screen for {date}: {response}")


if __name__ == "__main__":
    main()
