from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_daily_screen_and_notify as daily  # noqa: E402


class DailyNotifyTest(unittest.TestCase):
    def test_retry_operation_reconnects_after_failure(self) -> None:
        calls = []

        def flaky_operation() -> str:
            calls.append(len(calls) + 1)
            if len(calls) < 3:
                raise RuntimeError("temporary network failure")
            return "ok"

        result = daily.retry_operation(
            "test operation",
            flaky_operation,
            attempts=3,
            delay_seconds=0,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls, [1, 2, 3])

    def test_retry_operation_reports_final_failure(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "failed after 2 attempts"):
            daily.retry_operation(
                "test operation",
                lambda: (_ for _ in ()).throw(RuntimeError("offline")),
                attempts=2,
                delay_seconds=0,
            )

    def sample_item(self) -> dict:
        return {
            "code": "000001",
            "name": "样本",
            "state": "首次启动确认",
            "date": "2026-07-30",
            "close": 10.0,
            "industry": "测试行业",
            "riseRecentLow": 10,
            "change5": 2,
            "change10": 3,
            "change20": 4,
            "dailyRsi": 50,
            "weeklyRsi": 40,
            "maDistance15": 2,
            "maDistance30": 3,
            "volumeRatio5v20": 0.9,
            "dailyMacd": {"state": "水下刚金叉"},
            "sector": {"leader": "领涨样本", "leaderGain": 5},
            "sectorPersistence": {"activeDays5": 3},
            "conditionChecks": {"sector": "观察（近5日已启动、今日分歧）"},
            "market": {"totalMarketCap": 10_000_000_000, "averageAmount20": 100_000_000},
            "elasticity": {"maxStageGain1y": 40, "gain30Count": 1},
            "financialState": "通过",
            "riskState": "基础通过",
            "financialRisk": {"riskNotes": ["无明显事项"]},
        }

    def test_tracking_update_is_idempotent_for_same_date(self) -> None:
        original = daily.TRACKING_PATH
        with tempfile.TemporaryDirectory() as directory:
            daily.TRACKING_PATH = Path(directory) / "tracking.json"
            payload = {"selected": [self.sample_item()]}
            daily.update_tracking([("v13", payload)], "2026-07-30")
            daily.update_tracking([("v13", payload)], "2026-07-30")
            record = next(iter(daily.load_json(daily.TRACKING_PATH)["records"].values()))
            self.assertEqual(record["selectedCount"], 1)
            self.assertEqual(len(record["history"]), 1)
        daily.TRACKING_PATH = original

    def test_card_puts_featured_v13_first_and_keeps_v26_pool(self) -> None:
        featured = {**self.sample_item(), "code": "600001", "name": "主推样本", "featured": True,
                    "breakoutDate": "2026-07-29", "currentAbovePlatformPct": 2, "breakoutVolumeRatio": 1.2, "daysAbove1530": 1,
                    "fundamentalTrend": {"tier": 0, "inflection": True, "improving": True, "holdersDown2": False}}
        other = {**self.sample_item(), "code": "600002", "name": "观察样本", "fundamentalTrend": {"tier": 2}}
        pool = {**self.sample_item(), "code": "000003", "name": "底部样本"}
        card = daily.build_feishu_card(
            "2026-07-30",
            {"selected": [pool]},
            {"selected": [featured, other]},
            {"v13": [], "v26": []},
        )
        serialized = str(card)
        self.assertLess(serialized.index("主推样本"), serialized.index("观察样本"))
        self.assertLess(serialized.index("观察样本"), serialized.index("底部样本"))
        self.assertIn("主推1只", serialized)
        self.assertIn("底部观察池（v2.6，观察、不作入场依据）", serialized)
        self.assertNotIn("hook/", serialized)

    def test_card_reports_empty_sections(self) -> None:
        serialized = str(daily.build_feishu_card("2026-08-11", {"selected": []}, {"selected": []}, {"v13": [], "v26": []}))
        self.assertIn("今日没有满足主推条件的股票", serialized)
        self.assertIn("今日没有股票满足底部观察条件", serialized)
        self.assertIn("主推 0只｜其他启动观察 0只｜底部观察池 0只", serialized)

    def test_card_shows_regime_status_and_follow_up(self) -> None:
        record = {
            "code": "600000", "name": "浦发银行", "firstSelectedDate": "2026-09-01", "featured": True,
            "followUp": {"status": "启动中", "days": 12, "change": 0.16, "maxUp": 0.18, "stopDistance": 0.25, "final": False},
        }
        regime = {"available": True, "weak": False, "label": "中证1000 在MA120上方（+3.0%）｜近20日+5.0%"}
        serialized = str(daily.build_feishu_card("2026-09-17", {"selected": [], "marketRegime": regime}, {"selected": []},
                                                 {"v13": [record], "v26": []}))
        self.assertIn("谨慎：中证1000在MA120上方", serialized)
        self.assertIn("启动中｜★浦发银行 600000", serialized)
        self.assertIn("第12/40日", serialized)


if __name__ == "__main__":
    unittest.main()
