#!/usr/bin/env python3
"""Raw returns of what the daily job actually pushed since a date (output/daily/tracking.json).

Entry = open of the trading day after the first selection; return after N trading days =
close / entry - 1 (no benchmark). Trades whose N-day window has not finished are left out
of that column; "至今" is the return to the latest close for every trade. CSI 1000's own
move over the same windows is shown alongside for context, not subtracted.

K-lines: the daily job's cache (Tencent qfq), with any missing recent days appended from
Sina (bottom_base_context.extend_to).

Usage: python3 scripts/research/live_since.py --since 2026-08-01
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

HOLD = (5, 10, 20, 40)
MODELS = (("v13", "右侧启动 v1.3"), ("v26", "底部观察池 v2.6"), ("ucross", "低位水下金叉"))


def stage_of(record: dict) -> str:
    states = [h.get("state") for h in record.get("history", []) if h.get("state")]
    return states[0] if states else ""


def main() -> None:
    from backtest_bottom_base import benchmark_index
    from bottom_base_context import extend_to
    from screen_latest_30 import fetch_kline
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="2026-08-01")
    args = parser.parse_args()
    index = benchmark_index()
    idx_dates = sorted(index)
    records = [r for r in json.loads((ROOT / "output/daily/tracking.json").read_text(encoding="utf-8"))["records"].values()
               if r["firstSelectedDate"] >= args.since and r.get("model") in dict(MODELS)]
    latest = idx_dates[-1]

    def load(code: str) -> tuple[str, list[dict]]:
        try:
            rows, _ = fetch_kline(code)
            return code, extend_to(code, rows, latest)
        except Exception:
            return code, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        prices = dict(executor.map(load, sorted({r["code"] for r in records})))

    trades = []
    for r in records:
        rows = prices.get(r["code"]) or []
        dates = [x["date"] for x in rows]
        if r["firstSelectedDate"] not in dates:
            continue
        i = dates.index(r["firstSelectedDate"])
        if i + 1 >= len(rows):
            continue
        entry = rows[i + 1]["open"]
        t = {"model": r["model"], "code": r["code"], "name": r["name"], "date": r["firstSelectedDate"], "stage": stage_of(r),
             "entry": entry, "now": rows[-1]["close"] / entry - 1, "nowDate": rows[-1]["date"],
             "maxUp": max(x["close"] for x in rows[i + 1:]) / entry - 1, "held": len(rows) - 1 - i}
        k0 = idx_dates.index(r["firstSelectedDate"]) if r["firstSelectedDate"] in index else None
        for h in HOLD:
            if i + h < len(rows):
                t[f"r{h}"] = rows[i + h]["close"] / entry - 1
                if k0 is not None and k0 + h < len(idx_dates):
                    t[f"i{h}"] = index[idx_dates[k0 + h]] / index[r["firstSelectedDate"]] - 1
        if k0 is not None:
            t["iNow"] = index[latest] / index[r["firstSelectedDate"]] - 1
        trades.append(t)

    def row(label: str, xs: list[dict]) -> str:
        cells = []
        for h in HOLD:
            v = [t[f"r{h}"] for t in xs if f"r{h}" in t]
            iv = [t[f"i{h}"] for t in xs if f"i{h}" in t]
            cells.append(f"{st.mean(v):+.1%} / {sum(x > 0 for x in v) / len(v):.0%} / n{len(v)}（指数{st.mean(iv):+.1%}）" if v else "—")
        now = [t["now"] for t in xs]
        inow = [t["iNow"] for t in xs if "iNow" in t]
        return (f"| {label} | {len(xs)} | " + " | ".join(cells)
                + f" | {st.mean(now):+.1%} / {sum(x > 0 for x in now) / len(now):.0%}（指数{st.mean(inow):+.1%}） |")

    lines = [f"# {args.since} 以来实际推送的票：真实涨跌幅（截至 {latest} 收盘）", "",
             "买入 = 首次入选次日开盘价；每格：平均收益 / 赚钱比例 / 已满N日的笔数（括号：同期中证1000涨跌，仅参考）。", "",
             "| 清单 | 笔数 | 5日 | 10日 | 20日 | 40日 | 至今 |", "| --- | ---: | --- | --- | --- | --- | --- |"]
    for key, label in MODELS:
        xs = [t for t in trades if t["model"] == key]
        if not xs:
            continue
        lines.append(row(label, xs))
        if key == "v13":
            for stage in ("强启动确认", "右沿初确认"):
                sub = [t for t in xs if t["stage"] == stage]
                if sub:
                    lines.append(row(f"　└ {stage}", sub))
    by_month = sorted({t["date"][:7] for t in trades})
    lines += ["", "## 按入选月份：至今收益", "", "| 清单 | " + " | ".join(by_month) + " |", "| --- |" + " --- |" * len(by_month)]
    for key, label in MODELS:
        cells = []
        for m in by_month:
            xs = [t for t in trades if t["model"] == key and t["date"][:7] == m]
            cells.append(f"{st.mean(t['now'] for t in xs):+.1%}（{len(xs)}只）" if xs else "—")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    iv = {m: index[latest] / index[min(d for d in idx_dates if d[:7] == m)] - 1 for m in by_month if any(d[:7] == m for d in idx_dates)}
    lines.append("| 中证1000（月初至今） | " + " | ".join(f"{iv[m]:+.1%}" if m in iv else "—" for m in by_month) + " |")
    lines += ["", "## v1.3 强启动确认：逐笔", "", "| 入选 | 股票 | 买入价 | 5日 | 10日 | 20日 | 至今 | 期间最高 |", "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for t in sorted((t for t in trades if t["model"] == "v13" and t["stage"] == "强启动确认"), key=lambda t: t["date"]):
        f = lambda h: f"{t[f'r{h}']:+.1%}" if f"r{h}" in t else "未满"  # noqa: E731
        lines.append(f"| {t['date']} | {t['name']} {t['code']} | {t['entry']:.2f} | {f(5)} | {f(10)} | {f(20)} | {t['now']:+.1%} | {t['maxUp']:+.1%} |")
    out = ROOT / "output" / "backtest" / f"live-since-{args.since}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
