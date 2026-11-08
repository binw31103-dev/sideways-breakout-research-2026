#!/usr/bin/env python3
"""MA / RSI 的最终判定表：按项目自己的标准逐条筛。

标准（沿用 v2.7 文档的口径）：
  1. 三段同向（2023-07~2024-12 / 2025-01~2026-06 / 2026-07 起）
  2. 相对同日全池均值的增量 > 0.3 个百分点
  3. 换参数后方向不变
  4. 只在池子 ≥100 只的交易日计算（避免 2024-02 那种 7 只股票的小池主导结果）
  5. t 值用逐日等权（不是样本加权）

前几轮踩过的坑，这个脚本全部回避：
  - 样本加权会被大池日主导 → 改逐日等权
  - 逐日等权会被小池日主导 → 限制池子 ≥100 只
  - 持仓重叠会高估 t → 报告里注明，且只看方向不看 t 的绝对值

用法：python3 scripts/research/ma_rsi_verdict_table.py
"""

from __future__ import annotations

import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from ma_rsi_verdict import demean, load  # noqa: E402

PERIODS = (("前段", "2023-07", "2025-01"), ("后段", "2025-01", "2026-07"), ("新段", "2026-07", "9999"))
MIN_POOL = 100


def main() -> None:
    events = load()
    by_day = demean(events)
    big = {d: items for d, items in by_day.items()
           if len([x for x in items if "y20" in x]) >= MIN_POOL}
    print(f"逐日样本 {len(events)}，含 y20 的交易日 {len(big)}"
          f"（总 {len(by_day)}，剔除池子<{MIN_POOL}只的 {len(by_day) - len(big)} 天）\n")

    def run(sub_cost, test, h=20):
        per = []
        for _d, items in sub_cost.items():
            s = [x for x in items if f"y{h}" in x and test(x)]
            if len(s) < 2:
                continue
            per.append(st.mean(x[f"y{h}"] for x in s))
        if len(per) < 20:
            return None
        m = st.mean(per)
        sd = st.stdev(per) if len(per) > 1 else 0
        return m * 100, (m / (sd / math.sqrt(len(per))) if sd else 0), len(per)

    # 特征清单：只用能算的、有经济含义的
    features = {
        "RSI14 < 30": lambda e: e["rsi"] < 30,
        "RSI14 < 35": lambda e: e["rsi"] < 35,
        "RSI14 28~45（v2.6 现行）": lambda e: 28 <= e["rsi"] <= 45,
        "RSI14 30~45（中段）": lambda e: 30 <= e["rsi"] < 45,
        "RSI14 > 51（最热 1/10）": lambda e: e["rsi"] > 51,
        "距60日低点 ≤ 3%": lambda e: e["distLow"] <= 0.03,
        "距60日低点 3~6%": lambda e: 0.03 < e["distLow"] <= 0.06,
        "距60日低点 6~10%": lambda e: 0.06 < e["distLow"] <= 0.10,
        "20日跌幅 < -15%": lambda e: e["ret20"] < -0.15,
        "20日跌幅 -15~-5%": lambda e: -0.15 <= e["ret20"] < -0.05,
        "20日涨幅 > 0": lambda e: e["ret20"] > 0,
        "v2.6 形态内": lambda e: e["inV26"],
        "非 v2.6 形态": lambda e: not e["inV26"],
        "RSI<30 且 距低点≤5%": lambda e: e["rsi"] < 30 and e["distLow"] <= 0.05,
        "RSI<35 且 距低点≤5%": lambda e: e["rsi"] < 35 and e["distLow"] <= 0.05,
    }

    print("## 判定表（20 日超额，逐日等权，池≥100只）\n")
    print("| 特征 | 全部 | 前段 | 后段 | 新段 | 三段同向 | 全部>0.3pp | 通过 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    passed = []
    for name, test in features.items():
        overall = run(big, test)
        cells = [f"{overall[0]:+.3f}（t{overall[1]:+.1f}）" if overall else "n/a"]
        signs = []
        for _l, lo, hi in PERIODS:
            sub = {d: v for d, v in big.items() if lo <= d < hi}
            r = run(sub, test)
            if r is None:
                cells.append("n/a")
                signs.append(None)
            else:
                cells.append(f"{r[0]:+.3f}（t{r[1]:+.1f}）")
                signs.append(r[0] > 0)
        valid = [s for s in signs if s is not None]
        same_dir = len(set(valid)) == 1 and len(valid) == 3
        big_enough = overall is not None and abs(overall[0]) > 0.3
        ok = same_dir and big_enough
        if ok:
            passed.append(name)
        print(f"| {name} | " + " | ".join(cells) +
              f" | {'是' if same_dir else f'否（{sum(valid)}/3）'} | "
              f"{'是' if big_enough else '否'} | {'**通过**' if ok else '不通过'} |")
    print()

    print("## 结论\n")
    if passed:
        print(f"通过全部标准的特征：{'、'.join(passed)}")
    else:
        print("**没有任何一个 MA / RSI 特征通过全部标准。**")

    # 补充：现行 RSI 条件的方向在多少比例的日子是负的
    print("\n### 现行 RSI 28~45 条件的逐日方向分布\n")
    for label, lo, hi in (("全部", "0000", "9999"),) + PERIODS:
        sub = {d: v for d, v in big.items() if lo <= d < hi}
        per = []
        for _d, items in sub.items():
            s = [x for x in items if "y20" in x and 28 <= x["rsi"] <= 45]
            if len(s) >= 2:
                per.append(st.mean(x["y20"] for x in s))
        if len(per) < 20:
            print(f"- {label}：样本不足（{len(per)} 日）")
            continue
        neg = sum(1 for v in per if v < 0) / len(per)
        print(f"- {label}：{len(per)} 日，其中 {neg:.0%} 的日子为负，均值 {st.mean(per) * 100:+.3f}%")


if __name__ == "__main__":
    main()
