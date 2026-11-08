#!/usr/bin/env python3
"""S 组（首次深水 + 二次仍在水下）的补充检验。

已经有结论的是：S 组 20 日同日相对 +1.21，三段 +1.12 / +0.37 / +2.01。
但那个结论有四个没回答的问题，这个脚本逐个回答：

  1. 它是不是只是「短期反转」的另一个写法？
     做法：按近 20 日涨幅分组（分成 5 档），在**每一档内部**再比 S 与非 S。
     如果 S 的优势在每档内部都还在，说明它有独立信息；
     如果只在跌幅最大那一档有效，那它就是反转因子，不该单独用。

  2. 它是不是只是「贴着 60 日低点」的另一种写法？
     做法：同上，按距 60 日低点分档。

  3. 买了之后路径长什么样？先摸 +10% 还是先跌 -5%？
     这个决定能不能拿住，比平均收益重要得多。

  4. 同一天出现很多 S 信号时，怎么挑？
     做法：只在有多只 S 信号的交易日内部比较，看哪个特征能分出好坏。

另外检验参数敏感性：深水阈值 2% / 3% / 5%，确认 3% 不是挑出来的。

用法：python3 scripts/research/second_cross_supplement.py
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from second_cross_variants import collect  # noqa: E402
from screen_latest_30 import ema  # noqa: E402

from backtest_bottom_base import LONG_KLINE_DIR  # noqa: E402

PERIODS = (("前段", "2023-07", "2025-01"), ("后段", "2025-01", "2026-07"), ("新段", "2026-07", "9999"))


def load_industry() -> dict[str, str]:
    path = ROOT / "output" / "cache" / "financials-all.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for code, item in raw.items():
        board = (item or {}).get("BOARD_NAME") or ""
        out[code] = board.split("-")[0] if board else "未知"
    return out


def is_s(e: dict, deep: float = 3.0) -> bool:
    return e["dif1"] <= -deep and e["dif2"] <= -deep


def quintile(sample: list[dict], key: str, bins: int = 5) -> list[tuple[float, float]]:
    values = sorted(e[key] for e in sample)
    if len(values) < bins * 20:
        return []
    edges = [values[min(len(values) - 1, int(len(values) * i / bins))] for i in range(bins + 1)]
    edges[0], edges[-1] = -1e9, 1e9
    return [(edges[i], edges[i + 1]) for i in range(bins)]


def matched_control(events: list[dict], key: str, label: str, fmt: str) -> list[str]:
    """在 key 的每一档内部比较 S 与非 S，验证 S 不是 key 的代理变量。"""
    lines = [f"### 按{label}分档后，S 组与非 S 组（持有 20 日，同日相对）", "",
             f"| {label} | S 组 | 非 S 组 | 差 | 样本 S / 非S |", "| --- | --- | --- | --- | --- |"]
    base = [e for e in events if "rel20" in e]
    for lo, hi in quintile(base, key):
        in_bin = [e for e in base if lo <= e[key] < hi]
        s = [e for e in in_bin if is_s(e)]
        o = [e for e in in_bin if not is_s(e)]
        if len(s) < 30 or len(o) < 30:
            lines.append(f"| {fmt.format(lo, hi)} | n{len(s)} | n{len(o)} | — | — |")
            continue
        a, b = st.mean(e["rel20"] for e in s), st.mean(e["rel20"] for e in o)
        lines.append(f"| {fmt.format(lo, hi)} | {a:+.2f} | {b:+.2f} | {a - b:+.2f} | {len(s)} / {len(o)} |")
    lines.append("")
    return lines


def path_analysis(events: list[dict]) -> list[str]:
    """先摸 +10% 还是先跌 -5%：决定能不能拿住。"""
    lines = ["### 20 日内的路径顺序（次日开盘入场）", "",
             "| 组 | 样本 | 先到 +10% | 先到 -5% | 都未触及 |", "| --- | --- | --- | --- | --- |"]
    for name, test in (("S 组（首次深水 + 二次仍在水下）", lambda e: is_s(e)),
                       ("首次深水，二次已浮上", lambda e: e["dif1"] <= -3 and e["dif2"] > -3),
                       ("牧原式（首次贴近 0 轴）", lambda e: abs(e["dif1"]) <= 1),
                       ("全部二次金叉", lambda e: True)):
        sample = [e for e in events if test(e) and "path" in e]
        if len(sample) < 100:
            lines.append(f"| {name} | n{len(sample)} | — | — | — |")
            continue
        first = [e["path"] for e in sample]
        up = sum(1 for p in first if p == "up") / len(sample)
        dn = sum(1 for p in first if p == "down") / len(sample)
        lines.append(f"| {name} | {len(sample)} | {up:.0%} | {dn:.0%} | {1 - up - dn:.0%} |")
    lines.append("")
    return lines


def path_scan(file: Path) -> list[dict]:
    """重新走一遍，记录入场后先触到 +10% 还是 -5%。"""
    try:
        rows = json.loads(file.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return []
    if len(rows) < 260 or any(r["close"] <= 0 or r["open"] <= 0 or r["low"] <= 0 for r in rows):
        return []
    close = [r["close"] for r in rows]
    volume = [r["volume"] for r in rows]
    fast, slow = ema(close, 12), ema(close, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    crosses = [k for k in range(1, len(rows)) if dif[k - 1] < dea[k - 1] and dif[k] >= dea[k]]
    deaths = [k for k in range(1, len(rows)) if dif[k - 1] > dea[k - 1] and dif[k] <= dea[k]]
    out = []
    for pos in range(1, len(crosses)):
        k = crosses[pos]
        if k < 120 or k + 21 >= len(rows):
            continue
        j = crosses[pos - 1]
        if not (2 <= k - j <= 20):
            continue
        if not any(j < d < k for d in deaths) or min(dif[j:k + 1]) >= 0:
            continue
        entry = rows[k + 1]["open"]
        path = "none"
        for t in range(k + 1, k + 21):
            hit_up = rows[t]["high"] / entry - 1 >= 0.10
            hit_dn = rows[t]["low"] / entry - 1 <= -0.05
            if hit_up and hit_dn:
                path = "both"
                break
            if hit_up:
                path = "up"
                break
            if hit_dn:
                path = "down"
                break
        out.append({"code": file.stem, "date": rows[k]["date"], "path": path,
                    "dif1": dif[j] / close[j] * 100, "dif2": dif[k] / close[k] * 100})
    return out


def rank_analysis(events: list[dict], industry: dict[str, str]) -> list[str]:
    """同一天有多只 S 信号时，哪个特征能分出好坏。"""
    lines = ["### 同日排序：只在同一天有多只 S 信号的交易日内部比较", ""]
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if is_s(e) and "rel20" in e:
            by_day[e["date"]].append(e)
    multi = {d: v for d, v in by_day.items() if len(v) >= 2}
    if len(multi) < 50:
        lines += ["同日多只样本不足，跳过。", ""]
        return lines
    # 每个特征：当天排序后，取最高那组的超额减去当天均值
    features = {
        "RSI 低": lambda e: -e["rsi"],
        "距低点近": lambda e: -e["dist_low"],
        "20日跌得多": lambda e: -e["ret20"],
        "二次比首次深": lambda e: e["dif2"] - e["dif1"],
        "回撤小": lambda e: -e["dip"],
        "成交额大": lambda e: e["amt20_yi"],
        "间隔短": lambda e: -e["gap"],
    }
    lines += ["| 排序特征 | 前 1/3 相对当天均值 | 后 1/3 相对当天均值 | 差 | 天数 |", "| --- | --- | --- | --- | --- |"]
    for name, key in features.items():
        top, bot, days = [], [], 0
        for d, items in multi.items():
            if len(items) < 3:
                continue
            mean = st.mean(e["rel20"] for e in items)
            ordered = sorted(items, key=key, reverse=True)
            third = max(1, len(ordered) // 3)
            top.append(st.mean(e["rel20"] for e in ordered[:third]) - mean)
            bot.append(st.mean(e["rel20"] for e in ordered[-third:]) - mean)
            days += 1
        if days < 30:
            lines.append(f"| {name} | — | — | — | {days} |")
            continue
        lines.append(f"| {name} | {st.mean(top):+.2f} | {st.mean(bot):+.2f} | "
                     f"{st.mean(top) - st.mean(bot):+.2f} | {days} |")
    lines.append("")
    # 行业集中度
    counts: dict[str, int] = defaultdict(int)
    for e in events:
        if is_s(e):
            counts[industry.get(e["code"], "未知")] += 1
    total = sum(counts.values())
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    lines += ["### S 组的行业集中度（前 8 名）", "", "| 行业 | 占比 |", "| --- | --- |"]
    for name, n in top:
        lines.append(f"| {name} | {n / total:.1%}（{n}） |")
    lines.append("")
    return lines


def sensitivity(events: list[dict]) -> list[str]:
    lines = ["### 深水阈值敏感性（决定 S 组定义的唯一参数）", "",
             "| 阈值 | 20日同日相对 | 前段 | 后段 | 新段 | 样本 |", "| --- | --- | --- | --- | --- | --- |"]
    for deep in (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0):
        sample = [e for e in events if e["dif1"] <= -deep and e["dif2"] <= -deep and "rel20" in e]
        if len(sample) < 50:
            lines.append(f"| {deep}% | n{len(sample)} | — | — | — | — |")
            continue
        cells = []
        for _, lo, hi in PERIODS:
            part = [e for e in sample if lo <= e["date"] < hi]
            cells.append(f"{st.mean(e['rel20'] for e in part):+.2f}" if len(part) >= 20 else f"n{len(part)}")
        lines.append(f"| 全深度 | {deep:.1f}% 以下 | {st.mean(e['rel20'] for e in sample):+.2f} | "
                     + " | ".join(cells) + f" | {len(sample)} |")
    # 只按首次深，不要求二次仍深
    lines.append("")
    lines.append("| 定义 | 20日同日相对 | 前段 | 后段 | 新段 | 样本 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for deep in (2.0, 3.0, 5.0):
        sample = [e for e in events if e["dif1"] <= -deep and "rel20" in e]
        if len(sample) < 50:
            continue
        cells = []
        for _, lo, hi in PERIODS:
            part = [e for e in sample if lo <= e["date"] < hi]
            cells.append(f"{st.mean(e['rel20'] for e in part):+.2f}" if len(part) >= 20 else f"n{len(part)}")
        lines.append(f"| 只有首次 ≤ -{deep:.0f}%（不限二次） | {st.mean(e['rel20'] for e in sample):+.2f} | "
                     + " | ".join(cells) + f" | {len(sample)} |")
    lines.append("")
    return lines


def main() -> None:
    import concurrent.futures
    events = collect()
    paths: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
        for result in executor.map(path_scan, sorted(LONG_KLINE_DIR.glob("*.json")), chunksize=20):
            paths.extend(result)
    path_of = {(e["code"], e["date"]): e["path"] for e in paths}
    for e in events:
        e["path"] = path_of.get((e["code"], e["date"]))
    events = [e for e in events if e.get("path")]
    industry = load_industry()

    print(f"事件 {len(events)} 个（带路径标签）\n")
    print("## 一、S 组是不是「短期反转」的另一个写法\n")
    print("\n".join(matched_control(events, "ret20", "近 20 日涨幅",
                                    "{:.1f}% ~ {:.1f}%")))
    print("## 二、S 组是不是「贴着底部」的另一种写法\n")
    print("\n".join(matched_control(events, "dist_low", "距 60 日低点",
                                    "{:+.1f}% ~ {:+.1f}%")))
    print("## 三、路径：先摸 +10% 还是先跌 -5%\n")
    print("\n".join(path_analysis(events)))
    print("## 四、同日排序与行业集中度\n")
    print("\n".join(rank_analysis(events, industry)))
    print("## 五、参数敏感性\n")
    print("\n".join(sensitivity(events)))


if __name__ == "__main__":
    main()
