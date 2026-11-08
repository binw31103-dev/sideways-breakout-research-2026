from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from screen_v25_recent_edge import sector_observation_status  # noqa: E402


class V25SectorObservationTest(unittest.TestCase):
    def test_persistent_sector_divergence_is_observation_not_rejection(self) -> None:
        status = sector_observation_status(
            {"currentPass": False},
            {"available": True, "proxyPass": True},
        )
        self.assertEqual(status, "观察（近5日已启动、今日分歧）")

    def test_current_activation_without_persistence_is_observation(self) -> None:
        status = sector_observation_status(
            {"currentPass": True},
            {"available": True, "proxyPass": False},
        )
        self.assertEqual(status, "观察（今日启动、持续性不足）")

    def test_current_and_persistent_activation_is_satisfied(self) -> None:
        status = sector_observation_status(
            {"currentPass": True},
            {"available": True, "proxyPass": True},
        )
        self.assertEqual(status, "满足")

    def test_missing_sector_data_is_review_pending(self) -> None:
        status = sector_observation_status(
            {"currentPass": False},
            {"available": False, "proxyPass": False},
        )
        self.assertEqual(status, "数据待复核")


if __name__ == "__main__":
    unittest.main()
