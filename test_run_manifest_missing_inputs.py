"""run_manifest 输入文件缺失告警的单元测试。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from shadow_experiment import config, run_manifest


def _manifest_with(keys):
    return {"pre_run": {"pre_run_hash": {k: "0" * 64 for k in keys}}}


class DetectMissingInputsTest(unittest.TestCase):
    def test_no_previous_manifest_returns_empty(self):
        self.assertEqual(
            run_manifest._detect_missing_inputs({"a": "1"}, None), [])

    def test_detects_file_present_last_run_but_missing_now(self):
        prev = _manifest_with(["output/影子实验_总进球_2026-11-05.json", "output/x.csv"])
        now = {"output/x.csv": "1"}
        missing = run_manifest._detect_missing_inputs(now, prev)
        self.assertEqual(missing, ["output/影子实验_总进球_2026-11-05.json"])

    def test_ignores_new_files(self):
        prev = _manifest_with(["output/old.json"])
        now = {"output/old.json": "1", "output/new.json": "2"}
        self.assertEqual(run_manifest._detect_missing_inputs(now, prev), [])

    def test_ignores_backup_and_lock_files(self):
        prev = _manifest_with([
            "output/影子实验_让球_2026-11-05.json",
            "output/影子实验_让球_2026-11-05.json.backup.20261106115707",
            "output/影子实验_让球_2026-11-05.json.lock",
        ])
        now = {"output/影子实验_让球_2026-11-05.json": "1"}
        self.assertEqual(run_manifest._detect_missing_inputs(now, prev), [])

    def test_previous_manifest_without_pre_run_hash(self):
        self.assertEqual(
            run_manifest._detect_missing_inputs({"a": "1"}, {}), [])


class LoadPreviousManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = config.RUN_LOGS_DIR
        config.RUN_LOGS_DIR = self.tmp

    def tearDown(self):
        config.RUN_LOGS_DIR = self._orig

    def _write(self, date, obj):
        path = os.path.join(self.tmp, f"run_manifest_{date}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def test_returns_none_when_no_manifest(self):
        self.assertEqual(run_manifest._load_previous_manifest("2026-11-07"),
                         (None, None))

    def test_excludes_current_date_and_picks_latest_other(self):
        self._write("2026-11-05", _manifest_with(["output/a.json"]))
        self._write("2026-11-06", _manifest_with(["output/b.json"]))
        self._write("2026-11-07", _manifest_with(["output/c.json"]))
        path, obj = run_manifest._load_previous_manifest("2026-11-07")
        self.assertIn("run_manifest_2026-11-06.json", path)
        self.assertEqual(obj["pre_run"]["pre_run_hash"], {"output/b.json": "0" * 64})

    def test_broken_json_falls_back_to_none(self):
        path = os.path.join(self.tmp, "run_manifest_2026-11-06.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(run_manifest._load_previous_manifest("2026-11-07"),
                         (None, None))


class PreRunReportWiringTest(unittest.TestCase):
    """pre_run_checks 必须把告警字段写进报告，但不阻断执行。"""

    def test_missing_input_warning_recorded_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "output")
            os.makedirs(out)
            for play in ("总进球", "让球", "半全场"):
                with open(os.path.join(out, f"影子实验_{play}_2026-11-05.json"),
                          "w", encoding="utf-8") as f:
                    json.dump({"场次": []}, f)

            logs = os.path.join(tmp, "logs")
            os.makedirs(logs)
            with open(os.path.join(logs, "run_manifest_2026-11-04.json"),
                      "w", encoding="utf-8") as f:
                json.dump(_manifest_with([
                    "output/影子实验_总进球_2026-11-04.json"
                ]), f)

            with mock.patch.object(run_manifest.config, "RUN_LOGS_DIR", logs), \
                    mock.patch.object(run_manifest, "_PROJECT_ROOT", tmp), \
                    mock.patch.object(run_manifest, "_git_status", return_value=("", "deadbeef")):
                ok, report, _, _ = run_manifest.pre_run_checks("2026-11-05")

        self.assertTrue(ok, "缺失告警不得阻断执行")
        self.assertEqual(
            report["missing_inputs"],
            ["output/影子实验_总进球_2026-11-04.json"])
        self.assertIn("累计统计可能静默减少", report["missing_inputs_warning"])


if __name__ == "__main__":
    unittest.main()
