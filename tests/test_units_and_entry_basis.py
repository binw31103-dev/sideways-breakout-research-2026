from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/research")]
from kline_units import normalize_payload, load_payload
import screen_latest_30 as data
import screen_red_zone_5 as red
from entry_basis_alignment import paired_outcome, sensitivity
import build_kline_dataset as build
import second_cross_live
import entry_basis_alignment


def bars(n=85):
    return [{"date": (dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat(), "open": 10.0,
             "close": 10.0, "high": 10.1, "low": 9.9, "volume": 100.0} for i in range(n)]


class VolumeUnitTest(unittest.TestCase):
    def test_tencent_lots_sina_shares_and_star_shares_match(self):
        daily = bars(1)
        tencent = normalize_payload({"source": "腾讯前复权日K", "rows": daily}, "600000")
        sina_rows = [{**daily[0], "volume": 10000}]
        sina = normalize_payload({"source": "新浪日K+新浪复权因子回退", "rows": sina_rows}, "600000")
        star = normalize_payload({"source": "腾讯前复权日K", "rows": sina_rows}, "688128")
        self.assertEqual(tencent["rows"], sina["rows"])
        self.assertEqual(star["rows"], sina["rows"])
        self.assertEqual(tencent["volumeUnit"], "shares")

    def test_normalization_is_idempotent_and_prices_are_preserved(self):
        original = {"source": "腾讯前复权日K", "rows": bars()}
        normalized = normalize_payload(original, "000001")
        self.assertEqual(normalize_payload(normalized, "000001"), normalized)
        self.assertEqual(original["rows"][0]["volume"], 100)
        for key in ["open", "close", "high", "low", "date"]:
            self.assertEqual(original["rows"][0][key], normalized["rows"][0][key])

    def test_legacy_cache_migrates_once_on_fetch(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(data, "KLINE_CACHE_ROOT", Path(directory)):
            path = data.kline_cache_path("600000")
            path.parent.mkdir()
            path.write_text(json.dumps({"source": "腾讯前复权日K", "rows": bars()}))
            first, _ = data.fetch_kline("600000")
            second, _ = data.fetch_kline("600000")
            self.assertEqual(first, second)
            self.assertEqual(first[0]["volume"], 10000)
            self.assertEqual(json.loads(path.read_text())["volumeUnit"], "shares")

    def test_fresh_download_written_in_shares(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(data, "KLINE_CACHE_ROOT", Path(directory)), \
                mock.patch.object(data, "fetch_kline_uncached", return_value=(bars(), "腾讯前复权日K")):
            result, _ = data.fetch_kline("600000")
            payload = json.loads(data.kline_cache_path("600000").read_text())
            self.assertEqual(result[0]["volume"], 10000)
            self.assertEqual(payload["rows"], result)
            self.assertEqual(payload["sourceVolumeUnit"], "lots")

    def test_real_hundredfold_volume_spike_is_not_rescaled(self):
        quote = {"volume": 1000000}
        result = red.align_quote_volume(bars(), quote)
        self.assertEqual(result["volume"], 1000000)

    def test_unknown_unit_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_payload({"source": "unknown", "rows": bars()}, "600000")

    def test_legacy_research_loader_uses_same_unit_rule(self):
        legacy = {"source": "腾讯前复权日K", "rows": bars(1), "code": "600000"}
        self.assertEqual(second_cross_live.normalise_volume(legacy)[0]["volume"], 10000)
        normalized = normalize_payload(legacy, "600000")
        self.assertEqual(second_cross_live.normalise_volume(normalized)[0]["volume"], 10000)

    def test_long_dataset_tencent_fallback_is_normalized_before_write(self):
        raw = [[r["date"], r["open"], r["close"], r["high"], r["low"], r["volume"]] for r in bars(300)]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(build, "OUT", Path(directory)), \
                mock.patch.object(build, "sina_raw", return_value=[]), \
                mock.patch.object(build, "tencent_series", return_value=(raw, "ok")), \
                mock.patch.object(build, "sina_factors", return_value=[("2000-01-01", 1.0)]):
            build.build("600000", [], False, False)
            payload = load_payload(Path(directory) / "600000.json")
            self.assertEqual(payload["rows"][0]["volume"], 10000)
            self.assertEqual(payload["sourceVolumeUnit"], "lots")


class EntryBasisTest(unittest.TestCase):
    def test_exact_conversion_and_benchmark_use_matching_entry_dates(self):
        history = bars()
        history[60]["open"] = 11
        history[79]["close"] = 12
        index = {r["date"]: {"open": 100.0, "close": 100.0} for r in history}
        index[history[60]["date"]]["open"] = 102
        index[history[79]["date"]]["close"] = 110
        result = paired_outcome(history, 59, 20, False, index)
        self.assertAlmostEqual(result["signalCloseReturn"], .2)
        self.assertAlmostEqual(result["nextOpenReturn"], 12/11-1)
        self.assertLess(result["conversionError"], 1e-14)
        self.assertAlmostEqual(result["nextOpenBenchmark"], 110/102-1)
        self.assertNotEqual(result["nextOpenExcess"], result["nextOpenExcessLegacyBenchmark"])

    def test_same_stop_exit_for_both_entry_bases(self):
        history = bars()
        history[61]["close"] = 9.0
        result = paired_outcome(history, 59, 40, True, {})
        self.assertTrue(result["stopped"])
        self.assertEqual(result["exitDate"], history[61]["date"])

    def test_unfinished_and_no_next_open_are_excluded(self):
        history = bars()
        self.assertIsNone(paired_outcome(history, 59, 40, False, {}))
        self.assertIsNone(paired_outcome(history, 84, 20, False, {}))

    def test_risk_sensitivity_is_explicit_best_worst_deletion(self):
        result = sensitivity([-.2, -.1, 0, .1, .2], .2)
        self.assertEqual(result["excluded"], 1)
        self.assertAlmostEqual(result["removeBestMean"], -.05)
        self.assertAlmostEqual(result["removeWorstMean"], .05)

    def test_event_dedup_matches_existing_model_methodology(self):
        self.assertEqual(entry_basis_alignment.MODELS["v13"][1], 20)
        self.assertEqual(entry_basis_alignment.MODELS["v26"][1], 40)
        self.assertEqual(entry_basis_alignment.MODELS["ucross"][1], 40)
        self.assertEqual(entry_basis_alignment.MODELS["zaxis"][1], 20)


if __name__ == "__main__":
    unittest.main()
