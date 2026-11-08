from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from screen_v13_right_start import find_recent_breakout  # noqa: E402


class V13RightStartTest(unittest.TestCase):
    def test_breakout_date_does_not_replace_market_date(self) -> None:
        rows = []
        for index in range(40):
            close = 10.0 if index < 35 else 10.2 + (index - 35) * 0.02
            rows.append({
                "date": f"2026-07-{index + 1:02d}",
                "open": close - 0.02,
                "close": close,
                "high": close + 0.05,
                "low": close - 0.05,
                "volume": 100 if index != 35 else 140,
            })
        breakout = find_recent_breakout(rows)
        self.assertIsNotNone(breakout)
        assert breakout is not None
        self.assertIn("breakoutDate", breakout)
        self.assertNotIn("date", breakout)


if __name__ == "__main__":
    unittest.main()
