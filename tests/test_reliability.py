from __future__ import annotations

import copy
import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/research")]
import bottom_base_context as context
import preopen_check as preopen
import run_daily_screen_and_notify as daily
import screen_reliability as reliability
import screen_red_zone_5 as red
import screen_latest_30 as data
import screen_v26_unified as v26
import underwater_cross
import zero_axis_live


def rows(count: int = 300) -> list[dict]:
    day = dt.date(2025, 1, 1)
    result = []
    while len(result) < count:
        if day.weekday() < 5:
            result.append({"date": day.isoformat(), "open": 10.0, "close": 10.0,
                           "high": 10.1, "low": 9.9, "volume": 1e7})
        day += dt.timedelta(days=1)
    return result


class TrackingEpisodesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(daily, "TRACKING_PATH", Path(self.tmp.name) / "tracking.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def pick(self):
        return {"selected": [{"code": "600000", "name": "样本", "close": 10.0}]}

    def test_finished_episode_is_archived_and_reselection_starts_fresh(self):
        old = daily.update_tracking([("v26", self.pick())], "2026-08-31")
        record = old["records"]["v26:600000"]
        record["followUp"] = {"final": True, "status": "破位失效", "statusDate": "2026-09-10"}
        daily.save_json(daily.TRACKING_PATH, old)
        result = daily.update_tracking([("v26", self.pick())], "2026-11-04")
        self.assertEqual(result["records"]["v26:600000"]["firstSelectedDate"], "2026-11-04")
        self.assertEqual(result["records"]["v26:600000@2026-08-31"], record)
        again = daily.update_tracking([("v26", self.pick())], "2026-11-04")
        self.assertEqual(len(again["records"]), 2)
        self.assertEqual(again["records"]["v26:600000"]["selectedCount"], 1)

    def test_active_episode_keeps_first_date(self):
        daily.update_tracking([("v13", self.pick())], "2026-11-02")
        with mock.patch.object(daily, "record_follow_up", return_value={"final": False, "status": "跟踪中"}):
            result = daily.update_tracking([("v13", self.pick())], "2026-11-03")
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"]["v13:600000"]["firstSelectedDate"], "2026-11-02")

    def test_stale_active_status_is_recomputed_before_rollover(self):
        daily.update_tracking([("v13", self.pick())], "2026-09-01")
        with mock.patch.object(daily, "record_follow_up", return_value={
            "final": True, "status": "到期", "statusDate": "2026-09-29"
        }) as follow:
            result = daily.update_tracking([("v13", self.pick())], "2026-11-03")
        follow.assert_called_once()
        self.assertEqual(len(result["records"]), 2)

    def test_expiry_day_still_belongs_to_previous_episode(self):
        daily.update_tracking([("v13", self.pick())], "2026-10-01")
        with mock.patch.object(daily, "record_follow_up", return_value={
            "final": True, "status": "到期", "statusDate": "2026-10-29"
        }):
            result = daily.update_tracking([("v13", self.pick())], "2026-10-29")
        self.assertEqual(len(result["records"]), 1)

    def test_backfill_event_is_idempotent_and_does_not_roll_over(self):
        daily.update_tracking([("v13", self.pick())], "2026-11-03")
        with mock.patch.object(daily, "record_follow_up", side_effect=AssertionError("should not fetch")):
            result = daily.update_tracking([("v13", self.pick())], "2026-11-02")
            result = daily.update_tracking([("v13", self.pick())], "2026-11-02")
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"]["v13:600000"]["selectedCount"], 2)


class ScanHealthTest(unittest.TestCase):
    def test_valid_no_match_is_success_but_download_failure_is_failed(self):
        hits, clean = reliability.scan_candidates(["600000"], lambda _: None)
        _, failed = reliability.scan_candidates(["600000"], mock.Mock(side_effect=RuntimeError("offline")))
        self.assertEqual(hits, [])
        self.assertEqual(clean["status"], "success")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("offline", failed["failures"][0]["error"])

    def test_partial_scan_reports_failure_without_changing_valid_hits(self):
        def one(code):
            if code == "0":
                raise RuntimeError("offline")
            return {"code": code}
        hits, scan = reliability.scan_candidates(["0", "1", "2", "3"], one)
        self.assertEqual(scan["status"], "partial")
        self.assertEqual({h["code"] for h in hits}, {"1", "2", "3"})

    def test_subprocess_failure_cannot_reuse_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zaxis.json"
            daily.save_json(path, {"dataThrough": "2026-11-06", "selected": [{"code": "600000"}]})
            with mock.patch.object(daily, "run_command", side_effect=RuntimeError("process failed")):
                payload = daily.run_observation_scan(["python3", "fake.py"], path, "2026-11-06", "test")
            self.assertEqual(payload["selected"], [])
            self.assertEqual(daily.load_json(path)["scan"]["status"], "failed")

    def test_failed_scan_is_explicit_in_card_and_report(self):
        failed = {"selected": [], "scan": {"status": "failed", "expected": 100, "succeeded": 0, "failed": 100}}
        card = str(daily.build_feishu_card("2026-11-06", {"selected": []}, {"selected": []}, {}, failed, failed))
        report = daily.render_combined_report("2026-11-06", {"selected": []}, {"selected": []}, {}, failed, failed)
        for text in [card, report]:
            self.assertIn("扫描失败", text)
            self.assertNotIn("今日没有新出现的水下金叉", text)
            self.assertNotIn("今日没有新出现的零轴金叉", text)

    def test_wrong_date_scan_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zaxis.json"
            def command(args):
                daily.save_json(Path(args[-1]), {"dataThrough": "2026-11-05", "selected": [],
                                                "scan": {"status": "success"}})
            with mock.patch.object(daily, "run_command", side_effect=command):
                payload = daily.run_observation_scan(["python3", "fake.py"], path, "2026-11-06", "test")
            self.assertEqual(payload["scan"]["status"], "failed")

    def test_backfill_keeps_live_files_and_external_sync_untouched(self):
        args = argparse.Namespace(resend_date=None, date="2026-11-06", backfill=True,
            allow_stale=False, send=True, retry_attempts=1, retry_delay_seconds=0)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(daily, "DAILY_ROOT", Path(directory)), \
                mock.patch.object(daily, "TRACKING_PATH", Path(directory) / "tracking.json"), \
                mock.patch.object(daily, "parse_args", return_value=args), \
                mock.patch.object(daily, "annotate_report_due"), \
                mock.patch.object(daily, "intraday_check") as consistency, \
                mock.patch.object(data, "prune_kline_cache") as prune, \
                mock.patch.object(daily, "run_optional_command") as sync, \
                mock.patch.object(daily, "send_feishu") as send:
            live_path = Path(directory) / "tracking.json"
            live_path.write_text('{"version":1,"records":{}}')
            before = live_path.read_bytes()
            def command(cmd):
                path = Path(cmd[cmd.index("--json-output") + 1])
                daily.save_json(path, {"dataThrough": args.date, "selected": [], "scan": {"status": "success"}})
            with mock.patch.object(daily, "run_command", side_effect=command):
                daily.main()
            self.assertEqual(live_path.read_bytes(), before)
            consistency.assert_not_called()
            prune.assert_not_called()
            sync.assert_not_called()
            send.assert_not_called()
            outputs = list(Path(directory).glob("backfill-*/combined.json"))
            self.assertEqual(len(outputs), 1)
            self.assertEqual(daily.load_json(outputs[0])["replayMode"], "approximate_current_fundamentals")


class CommonRiskTest(unittest.TestCase):
    def test_severe_risk_rejected_yellow_kept_missing_quarantined(self):
        hits = [{"code": x, "industry": "测试"} for x in ["bad", "yellow", "missing"]]
        def risk(item):
            if item["code"] == "missing":
                raise RuntimeError("no risk data")
            return {**item, "financialState": "观察", "riskState": "不通过" if item["code"] == "bad" else "观察"}
        scan = {"status": "success"}
        with mock.patch.object(red, "enrich_financial_risk", side_effect=risk):
            kept = reliability.filter_observation_risks(hits, {x["code"]: {} for x in hits}, scan)
        self.assertEqual([x["code"] for x in kept], ["yellow"])
        self.assertEqual(scan["riskRejected"][0]["code"], "bad")
        self.assertEqual(scan["riskFailures"][0]["code"], "missing")
        self.assertEqual(scan["status"], "partial")

    def test_malformed_announcement_is_not_a_clean_risk_check(self):
        with mock.patch.object(red, "get_json", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "announcement response"):
                red.fetch_risk_announcements("600000")
            with self.assertRaisesRegex(RuntimeError, "financial detail"):
                red.fetch_financial_detail("600000")
        with mock.patch.object(red, "get_json", return_value={"data": {"list": []}}):
            self.assertEqual(red.fetch_risk_announcements("600000")["severeRisk"], [])

    def test_both_cross_scanners_use_the_existing_risk_gate(self):
        bars = rows()
        date = bars[-1]["date"]
        financials = {"600000": {"SECURITY_NAME_ABBR": "样本", "BOARD_NAME": "测试", "PARENT_NETPROFIT": 1e8}}
        quotes = {"600000": {"totalMarketCap": 1e10, "floatMarketCap": 5e9}}
        for module in [underwater_cross, zero_axis_live]:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory, \
                    mock.patch.object(data, "fetch_financials", return_value=financials), \
                    mock.patch.object(red, "fetch_daily_quotes", return_value=quotes), \
                    mock.patch.object(v26, "prepare_rows", return_value=(copy.deepcopy(bars), "fixture", 1)), \
                    mock.patch.object(context, "fundamental_trend", return_value={"tier": 2}), \
                    mock.patch.object(underwater_cross, "macd_lines", return_value=([-0.1] * 300, [-0.11] * 300, [299])), \
                    mock.patch.object(underwater_cross, "classify", return_value={"G": True}), \
                    mock.patch.object(data, "ema", side_effect=[[10.0] * 300, [10.1] * 300, [-0.09] * 299 + [-0.11]]), \
                    mock.patch.object(red, "enrich_financial_risk", side_effect=lambda x: {
                        **x, "financialState": "通过", "riskState": "不通过"
                    }) as gate:
                path = Path(directory) / "result.json"
                if module is underwater_cross:
                    module.live(date, 1, path)
                else:
                    module.live(date, path)
                output = json.loads(path.read_text())
                gate.assert_called_once()
                self.assertEqual(output["selected"], [])
                self.assertEqual(len(output["scan"]["riskRejected"]), 1)


class QuoteFreshnessTest(unittest.TestCase):
    def test_each_quote_is_checked_for_date_time_and_open(self):
        report = {"v13": {"selected": [{"code": code, "name": code, "close": 10} for code in "ABCDE"]}}
        quotes = {
            "A": {"date": "2026-11-06", "time": "09:25", "open": 10.1, "prevClose": 10},
            "B": {"date": "2026-11-05", "time": "09:25", "open": 10.1, "prevClose": 10},
            "C": {"date": "2026-11-06", "time": "09:20", "open": 10.1, "prevClose": 10},
            "D": {"date": "2026-11-06", "time": "09:25", "open": 0, "price": 10.1, "prevClose": 10},
            "E": {"date": "2026-11-06", "time": "09:30", "open": 10.1, "prevClose": 10},
        }
        result = preopen.build("2026-11-05", report, quotes, "2026-11-06", "09:26")
        self.assertIsNotNone(result["rows"][0]["gap"])
        self.assertTrue(all(r["gap"] is None for r in result["rows"][1:]))

    def test_unfinished_followup_with_stale_kline_is_pending(self):
        bars = rows(70)
        record = {"code": "600000", "firstSelectedDate": bars[59]["date"]}
        with mock.patch.object(context, "fetch_kline", return_value=(bars, "fixture")), \
                mock.patch.object(context, "extend_to", side_effect=lambda code, history, day: history):
            result = context.follow_up(record, "2026-11-06")
        self.assertEqual(result["status"], "数据待复核")


class MetadataTest(unittest.TestCase):
    def test_overlap_counts_stocks_separately_from_signals(self):
        result = reliability.signal_overlap({"ucross": {"selected": [{"code": "600000"}]},
                                              "zaxis": {"selected": [{"code": "600000"}]}})
        self.assertEqual(result["signalCount"], 2)
        self.assertEqual(result["uniqueStockCount"], 1)

    def test_missing_factor_coverage_does_not_change_score(self):
        features = {"price": 10.0}
        before = context.literature_score(features)
        coverage = context.literature_coverage(features)
        self.assertLess(coverage["available"], coverage["total"])
        self.assertEqual(before, context.literature_score(features))


if __name__ == "__main__":
    unittest.main()
