#!/usr/bin/env python3
"""Write config/literature_score_reference.json: per-feature quantiles of the literature
features (signed so that higher = better) over 2023-07~2024-12 v2.6 signals only, so the
live score and later backtest periods never see the data they are judged on.

Usage: python3 scripts/research/build_literature_reference.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backtest_bottom_base import v26  # noqa: E402
from bottom_base_context import LITERATURE_SIGNS, REFERENCE_PATH  # noqa: E402
from combo_v13_v26 import fundamentals_tier0  # noqa: E402
from factor_study import QUOTES, features, load_fundamentals  # noqa: E402

N_QUANTILES = 201


def main() -> None:
    quotes = json.loads(QUOTES.read_text(encoding="utf-8"))
    shares = {c: (q["totalMarketCap"] / q["close"], q["floatMarketCap"] / q["close"])
              for c, q in quotes.items() if q.get("close") and q.get("totalMarketCap")}
    fund_at, tier0 = load_fundamentals(), fundamentals_tier0()
    signals = [s for s in json.loads((ROOT / "output/backtest/signals.json").read_text(encoding="utf-8"))
               if "2023-07-01" <= s["date"] < "2025-01-01" and not s["date"].startswith("2024-09") and v26(s)]
    values: dict[str, list[float]] = {name: [] for name in LITERATURE_SIGNS}
    for s in signals:
        f = features(s, fund_at, tier0, shares)
        for name, sign in LITERATURE_SIGNS.items():
            if name in f:
                values[name].append(f[name] * sign)
    quantiles = {}
    for name, vals in values.items():
        vals.sort()
        quantiles[name] = [vals[round(k * (len(vals) - 1) / (N_QUANTILES - 1))] for k in range(N_QUANTILES)] if vals else []
    REFERENCE_PATH.write_text(json.dumps({
        "built": "2023-07~2024-12 v2.6 signals (excl. 2024-09), output/cache/kline_long",
        "signs": LITERATURE_SIGNS,
        "samples": {k: len(v) for k, v in values.items()},
        "quantiles": quantiles,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print({k: len(v) for k, v in values.items()})


if __name__ == "__main__":
    main()
