"""Research-only ablations using production v26 checks, without changing live rules."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screen_v26_unified import calculate_metrics, strict_condition_checks


def compare_rules(rows: list[dict]) -> dict | None:
    metrics = calculate_metrics(rows)
    if metrics is None:
        return None
    checks = strict_condition_checks(metrics)
    other = all(value for key, value in checks.items() if key != "dailyRsi_28_45")
    without_week = all(value for key, value in checks.items() if key != "weeklyRsi_24_42")
    return {"current": all(checks.values()),
            "no_daily_rsi_lower": other and metrics["dailyRsi"] <= 45,
            "daily_rsi_upper_50": other and 28 <= metrics["dailyRsi"] < 50,
            "no_weekly_rsi": without_week,
            "checks": checks,
            "dailyRsi": metrics["dailyRsi"], "weeklyRsi": metrics["weeklyRsi"],
            "oldSimplifiedPool": (
                metrics["riseRecentLow"] <= 8
                and (max(r["high"] for r in rows[-20:]) - min(r["low"] for r in rows[-20:])) / rows[-1]["close"] <= 0.28
                and rows[-1]["close"] <= metrics["ma"][15] * 1.03
                and rows[-1]["close"] <= metrics["ma"][30] * 1.03)}
