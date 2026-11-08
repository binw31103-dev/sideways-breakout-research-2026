#!/usr/bin/env python3
"""How much of the day is known at 14:45: volume share and price drift to the close.

Samples stocks from the latest K-line cache, pulls one day of minute data split across
Tencent and Sina (no single platform carries the whole sample), and reports
  volShare  cumulative volume at 14:45 / full-day volume
  drift     close / price at 14:45 - 1
The intraday screen uses the volume share to scale a 14:45 bar to a full-day estimate.

Usage: python3 scripts/research/intraday_profile.py [--sample 120]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics as st
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CUTOFF = "14:45"


def curl_json(url: str) -> dict | None:
    out = subprocess.run(["curl", "-sS", "-m", "15", "-H", "User-Agent: Mozilla/5.0", url], capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except ValueError:
        return None


def tencent(code: str) -> tuple[float, float, float, float] | None:
    sym = ("sh" if code.startswith("6") else "sz") + code
    node = (((curl_json(f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={sym}") or {}).get("data") or {}).get(sym) or {})
    rows = ((node.get("data") or {}).get("data")) or []
    pts = [(r.split()[0], float(r.split()[1]), float(r.split()[2])) for r in rows if len(r.split()) >= 3]
    if not pts:
        return None
    cut = [p for p in pts if p[0] <= CUTOFF.replace(":", "")]
    if not cut:
        return None
    return cut[-1][2], pts[-1][2], cut[-1][1], pts[-1][1]  # cumulative volumes, prices


def sina(code: str) -> tuple[float, float, float, float] | None:
    sym = ("sh" if code.startswith("6") else "sz") + code
    data = ((curl_json(f"https://quotes.sina.cn/cn/api/openapi.php/CN_MinlineService.getMinlineData?symbol={sym}") or {})
            .get("result") or {}).get("data") or []
    if not data:
        return None
    total = sum(float(d["v"]) for d in data)
    upto = [d for d in data if d["m"][:5] <= CUTOFF]
    if not upto:
        return None
    return sum(float(d["v"]) for d in upto), total, float(upto[-1]["p"]), float(data[-1]["p"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", type=int, default=120)
    args = parser.parse_args()
    cache = max((ROOT / "output" / "cache" / "kline").iterdir())
    codes = sorted(p.stem for p in cache.glob("*.json"))
    sample = random.Random(1).sample(codes, args.sample)
    shares, drifts, by_source = [], [], {"腾讯": 0, "新浪": 0}
    for n, code in enumerate(sample):
        fetch, label = (tencent, "腾讯") if n % 2 == 0 else (sina, "新浪")
        r = fetch(code) or (sina(code) if label == "腾讯" else tencent(code))
        time.sleep(0.15)
        if not r or r[1] <= 0:
            continue
        by_source[label] += 1
        shares.append(r[0] / r[1])
        drifts.append(r[3] / r[2] - 1)
    shares.sort()
    q = lambda p: shares[int(p * (len(shares) - 1))]  # noqa: E731
    print(f"样本 {len(shares)} 只（{by_source}），数据日期为最近一个交易日")
    print(f"14:45 成交量占全天：中位 {st.median(shares):.1%}，10%分位 {q(0.1):.1%}，90%分位 {q(0.9):.1%}")
    print(f"14:45 → 收盘价格变化：中位 {st.median(drifts):+.2%}，绝对值中位 {st.median(abs(d) for d in drifts):.2%}，"
          f"绝对值>1% 的占 {sum(abs(d) > 0.01 for d in drifts) / len(drifts):.0%}")


if __name__ == "__main__":
    main()
