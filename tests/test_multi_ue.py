import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "lab"), str(ROOT / "sim")]
from common import DEFAULTS, extend_ues, load_site, terminals
from realtime import RealtimeControl


class MultiUETest(unittest.TestCase):
    def test_extend_legacy_site_without_rotating_credentials(self):
        data = DEFAULTS | dict(imsi="001010000000001", k="ab" * 16, opc="cd" * 16,
                               db_password="a" * 16, db_root_password="b" * 16)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site.json"
            path.write_text(json.dumps(data))
            original = terminals(load_site(path))
            extend_ues(data, 2)
            saved = copy.deepcopy(data)
            extend_ues(data, 2)
            self.assertEqual(saved, data)
            path.write_text(json.dumps(data))
            roster = terminals(load_site(path))
            self.assertEqual(original, roster[:1])
            self.assertEqual([t["slot"] for t in roster], [0, 1])
            self.assertEqual(roster[1]["namespace"], roster[0]["namespace"] + "-2")
            with self.assertRaises(ValueError):
                extend_ues(data, 1)
            data["additional_ues"][0]["imsi"] = data["imsi"]
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                load_site(path)

    def test_late_wakeup_skips_slots_and_holds_last_gripper(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            path = Path(directory) / "control.jsonl"
            clock = RealtimeControl(path)
            self.addCleanup(clock.close)
            clock.target = 1_000_000_000
            with patch("realtime.time.monotonic_ns", side_effect=[
                    990_000_000, 1_109_000_000, 1_109_001_000, 1_120_000_000]), patch("realtime.time.sleep"):
                clock.release()
                clock.step(Mock(), [0.1] * 6 + [1.0], "policy")
            tick = json.loads(path.read_text())
            self.assertEqual(tick["missed_releases"], 2)
            self.assertEqual(tick["release_ns"], 1_100_000_000)
            self.assertTrue(tick["finished_in_period"])
            self.assertEqual(clock.hold(), [0.] * 6 + [1.0])


if __name__ == "__main__":
    unittest.main()
