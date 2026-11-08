from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import screen_red_zone_5  # noqa: E402
from screen_red_zone_5 import ensure_scan_health, fetch_daily_quotes, parse_tencent_quote_line  # noqa: E402


def tencent_line(symbol: str, code: str, close: str, volume: str, deal: str, stamp: str = "20261016161456") -> str:
    fields = [""] * 50
    fields[1], fields[2], fields[3], fields[4], fields[5], fields[6] = "名称", code, close, "9.64", "9.60", volume
    fields[30], fields[33], fields[34], fields[35] = stamp, "9.79", "9.53", deal
    fields[37], fields[38], fields[44], fields[45] = "89616", "0.28", "3240.66", "3240.66"
    return f'v_{symbol}="' + "~".join(fields) + '"'


class TencentQuoteParseTest(unittest.TestCase):
    def test_main_board_volume_is_converted_from_lots(self) -> None:
        code, quote = parse_tencent_quote_line(
            tencent_line("sh600000", "600000", "9.73", "922942", "9.73/922942/896160848")
        )
        self.assertEqual(code, "600000")
        self.assertEqual(quote["date"], "2026-10-16")
        self.assertEqual(quote["volume"], 92294200)
        self.assertEqual(quote["amount"], 896160848)
        self.assertAlmostEqual(quote["totalMarketCap"], 324_066_000_000)
        self.assertEqual(quote["source"], "腾讯收盘快照")

    def test_star_market_volume_is_already_in_shares(self) -> None:
        _, quote = parse_tencent_quote_line(
            tencent_line("sh688128", "688128", "20.47", "1956271", "20.47/1956271/40106818")
        )
        self.assertEqual(quote["volume"], 1956271)

    def test_untraded_stock_is_skipped(self) -> None:
        self.assertIsNone(parse_tencent_quote_line(tencent_line("sz000001", "000001", "11.50", "0", "")))


class DailyQuotesFallbackTest(unittest.TestCase):
    def test_falls_back_to_tencent_when_eastmoney_empty(self) -> None:
        with mock.patch.object(screen_red_zone_5, "fetch_eastmoney_daily_quotes", return_value={}), \
                mock.patch.object(screen_red_zone_5, "fetch_tencent_daily_quotes", return_value={"600000": {"date": "2026-10-16"}}) as tencent:
            quotes = fetch_daily_quotes(["600000"])
        tencent.assert_called_once_with(["600000"])
        self.assertIn("600000", quotes)

    def test_raises_when_all_sources_empty(self) -> None:
        with mock.patch.object(screen_red_zone_5, "fetch_eastmoney_daily_quotes", return_value={}), \
                mock.patch.object(screen_red_zone_5, "fetch_tencent_daily_quotes", return_value={}):
            with self.assertRaises(RuntimeError):
                fetch_daily_quotes(["600000"])


class ScanHealthTest(unittest.TestCase):
    def test_mass_failure_aborts(self) -> None:
        with self.assertRaises(RuntimeError):
            ensure_scan_health(100, [{"code": str(i), "error": "x"} for i in range(31)])

    def test_normal_failure_rate_passes(self) -> None:
        ensure_scan_health(5030, [{"code": str(i), "error": "x"} for i in range(70)])


class AsOfBackfillTest(unittest.TestCase):
    ROWS = [
        {"date": "2026-10-14", "open": 1, "close": 10.0, "high": 1, "low": 1, "volume": 100},
        {"date": "2026-10-15", "open": 1, "close": 9.0, "high": 1, "low": 1, "volume": 100},
        {"date": "2026-10-16", "open": 1, "close": 12.0, "high": 1, "low": 1, "volume": 100},
    ]

    def test_prepare_rows_truncates_in_both_screeners(self) -> None:
        import screen_v25_recent_edge
        import screen_v26_unified

        for module in (screen_v26_unified, screen_v25_recent_edge):
            with self.subTest(module=module.__name__), \
                    mock.patch.object(module, "AS_OF", "2026-10-15"), \
                    mock.patch.object(module, "fetch_kline", return_value=([dict(row) for row in self.ROWS], "腾讯前复权日K")):
                # K-line volume in lots, snapshot volume in shares: the scale must survive backfill.
                rows, source, scale = module.prepare_rows("600000", {"600000": {"date": "2026-10-16", "volume": 10_000}})
            self.assertEqual(rows[-1]["date"], "2026-10-15")
            self.assertEqual(scale, 100.0)
            self.assertIn("回溯", source)

    def test_historical_quote_scales_market_cap_to_as_of_close(self) -> None:
        quote = {"close": 12.0, "totalMarketCap": 12e9, "floatMarketCap": 6e9}
        past = screen_red_zone_5.historical_quote(quote, self.ROWS[:2])
        self.assertEqual(past["date"], "2026-10-15")
        self.assertAlmostEqual(past["totalMarketCap"], 9e9)
        self.assertAlmostEqual(past["floatMarketCap"], 4.5e9)


class KlineCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        import screen_latest_30

        self.module = screen_latest_30
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(screen_latest_30, "KLINE_CACHE_ROOT", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_second_call_reads_cache(self) -> None:
        rows = [{"date": "2000-01-03", "close": 1.0}]
        with mock.patch.object(self.module, "fetch_kline_uncached", return_value=(rows, "腾讯前复权日K")) as fetch:
            self.module.fetch_kline("600000")
            cached_rows, source = self.module.fetch_kline("600000")
        fetch.assert_called_once()
        self.assertEqual(cached_rows, rows)
        self.assertEqual(source, "腾讯前复权日K")

    def test_prune_removes_old_days_only(self) -> None:
        import datetime as dt

        today = dt.date(2026, 10, 16)
        for day in ("2026-10-10", "2026-10-15"):
            (Path(self.tmp.name) / day).mkdir()
            (Path(self.tmp.name) / day / "600000.json").write_text("{}")
        self.module.prune_kline_cache(today)
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), ["2026-10-15"])


class FollowUpTest(unittest.TestCase):
    @staticmethod
    def rows(closes: list[float]) -> list[dict]:
        import datetime as dt

        start = dt.date(2026, 1, 1)
        return [
            {"date": (start + dt.timedelta(days=k)).isoformat(), "open": c, "close": c, "high": c, "low": c, "volume": 1}
            for k, c in enumerate(closes)
        ]

    def run_follow_up(self, after: list[float], as_of_index: int | None = None) -> dict:
        import bottom_base_context

        rows = self.rows([10.0] * 60 + after)
        as_of = rows[as_of_index if as_of_index is not None else -1]["date"]
        record = {"code": "600000", "firstSelectedDate": rows[59]["date"]}
        with mock.patch.object(bottom_base_context, "fetch_kline", return_value=(rows, "t")):
            return bottom_base_context.follow_up(record, as_of)

    def test_break_below_pre_selection_low_is_final(self) -> None:
        result = self.run_follow_up([10.2, 9.4, 11.0])
        self.assertEqual(result["status"], "破位失效")
        self.assertTrue(result["final"])
        self.assertEqual(result["days"], 2)

    def test_launch_is_flagged_but_still_tracked(self) -> None:
        result = self.run_follow_up([10.5, 11.6])
        self.assertEqual(result["status"], "启动中")
        self.assertFalse(result["final"])

    def test_expires_after_track_days(self) -> None:
        result = self.run_follow_up([10.1] * 45)
        self.assertEqual(result["status"], "到期")
        self.assertEqual(result["days"], 40)

    def test_as_of_ignores_later_rows(self) -> None:
        result = self.run_follow_up([10.1, 10.2, 9.0], as_of_index=61)
        self.assertEqual(result["status"], "跟踪中")


class FundamentalTrendTest(unittest.TestCase):
    def test_inflection_uses_only_reports_announced_before_date(self) -> None:
        import bottom_base_context

        reports = [
            {"REPORTDATE": "2026-06-30", "NOTICE_DATE": "2026-08-20", "SJLTZ": 50.0, "YSTZ": 5.0},  # after as_of
            {"REPORTDATE": "2026-03-31", "NOTICE_DATE": "2026-04-25", "SJLTZ": 12.0, "YSTZ": 3.0},
            {"REPORTDATE": "2025-12-31", "NOTICE_DATE": "2026-03-20", "SJLTZ": -8.0, "YSTZ": 1.0},
        ]
        holders = [
            {"END_DATE": "2026-03-31", "HOLD_NOTICE_DATE": "2026-04-25", "HOLDER_NUM_RATIO": -3.0},
            {"END_DATE": "2025-12-31", "HOLD_NOTICE_DATE": "2026-03-20", "HOLDER_NUM_RATIO": -2.0},
        ]

        def fake(url: str, timeout: int = 20) -> dict:
            return {"result": {"data": reports if "RPT_LICO_FN_CPD" in url else holders}}

        with mock.patch.object(bottom_base_context, "get_json", side_effect=fake):
            trend = bottom_base_context.fundamental_trend("600000", "2026-06-01")
        self.assertEqual(trend["reportDate"], "2026-03-31")
        self.assertTrue(trend["inflection"])
        self.assertTrue(trend["holdersDown2"])
        self.assertEqual(trend["tier"], 0)
        self.assertIn("净利拐点/户数连降", bottom_base_context.trend_text(trend))


if __name__ == "__main__":
    unittest.main()
