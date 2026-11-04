"""阶段十九·第一项：复盘输入无效时禁止覆盖既有记录的隔离测试。

覆盖 review.run_review 的三道输入保护：
- 分析 CSV 不存在 → 报错并停止（不取赛果、不写任何文件）
- 分析 CSV 存在但无数据行 → 报错并停止
- CSV 有行但全部无有效预测 / 无匹配赛果 → 停止写入，保留既有产物

全部文件操作限定在 tempfile.TemporaryDirectory() 内；fetch_tecai_results 与
call_deepseek_for_attribution 全部 mock，不访问网络、不调用真实 API。
运行：python3 -m unittest test_review_input_guard -v
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import review

DATE = "2026-10-27"
CSV_HEADER = ("编号,联赛,对阵,胜平负推荐,让球推荐,总进球推荐,"
              "半全场推荐,比分推荐,信心指数,半全场来源")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _raw_match(num="测试001"):
    """体彩原始赛程 JSON 的最小合法条目（load_tecai_data 读取的字段）。"""
    return {
        "matchNumStr": num,
        "hhad": {"goalLine": "-1"},
        "homeTeamAllName": "A队",
        "awayTeamAllName": "B队",
        "leagueAbbName": "测试联赛",
        "matchTime": "2026-10-27 20:00",
    }


def _api_result(num="测试001"):
    """模拟体彩 API 赛果：A队 2:0 B队（半场 1:0）。"""
    return {
        "match_num": num,
        "home": "A队",
        "away": "B队",
        "goal_line": "-1",
        "full_score_raw": "2:0",
        "half_score_raw": "1:0",
        "win_flag": "H",
        "league": "测试联赛",
    }


class _ReviewSandbox(unittest.TestCase):
    """在临时目录中运行复盘入口，隔离项目真实 output/。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.out = os.path.join(self.tmp, "output")
        os.makedirs(self.out, exist_ok=True)
        self.daily_path = os.path.join(self.out, f"每日记录_{DATE}.json")
        self.report_path = os.path.join(self.out, f"竞彩复盘_{DATE}.txt")
        self.csv_path = os.path.join(self.out, f"竞彩分析_{DATE}.csv")
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    # ---- 夹具 ----
    def write_raw(self, payload=None):
        with open(os.path.join(self.out, f"竞彩足球_{DATE}_raw.json"),
                  "w", encoding="utf-8") as f:
            json.dump([_raw_match()] if payload is None else payload,
                      f, ensure_ascii=False)

    def write_csv(self, content):
        with open(self.csv_path, "w", encoding="utf-8-sig") as f:
            f.write(content)

    def seed_existing(self):
        """预置已有的每日记录与复盘报告，用于断言“未被覆盖”。"""
        with open(self.daily_path, "w", encoding="utf-8") as f:
            json.dump({"复盘日期": DATE, "命中统计": {"total": 1},
                       "场次": [{"编号": "旧001", "联赛": "旧联赛"}]},
                      f, ensure_ascii=False)
        with open(self.report_path, "w", encoding="utf-8") as f:
            f.write("旧复盘报告内容-不得覆盖")

    def snapshot_outputs(self):
        return {
            "daily": _read(self.daily_path),
            "report": _read(self.report_path),
            "files": sorted(os.listdir(self.out)),
        }

    def run_review_mocked(self, api_results):
        with mock.patch.object(review, "fetch_tecai_results",
                               return_value=api_results) as fetched, \
             mock.patch.object(review, "call_deepseek_for_attribution",
                               return_value=None):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                review.run_review(DATE)
        return buf.getvalue(), fetched


class TestMissingAnalysisCsv(_ReviewSandbox):
    """场景 A：原始赛程 JSON 存在、分析 CSV 不存在。"""

    def test_stops_before_fetch_and_keeps_existing_files(self):
        self.write_raw()
        self.seed_existing()
        before = self.snapshot_outputs()

        out, fetched = self.run_review_mocked([_api_result()])

        # 明确报告“缺文件”并停止
        self.assertIn("找不到", out)
        self.assertIn(f"竞彩分析_{DATE}.csv", out)
        self.assertIn("本次复盘已停止", out)
        # 输入无效时不得继续访问赛果接口
        fetched.assert_not_called()
        # 既有每日记录、复盘报告逐字不变
        self.assertEqual(self.snapshot_outputs(), before)

    def test_missing_csv_does_not_create_new_artifacts(self):
        self.write_raw()
        before = sorted(os.listdir(self.out))

        out, _ = self.run_review_mocked([_api_result()])

        self.assertIn("本次复盘已停止", out)
        self.assertEqual(sorted(os.listdir(self.out)), before)


class TestEmptyAnalysisCsv(_ReviewSandbox):
    """场景 B：CSV 存在但没有有效预测记录。"""

    def test_header_only_csv_is_rejected(self):
        self.write_raw()
        self.write_csv(CSV_HEADER + "\n")
        self.seed_existing()
        before = self.snapshot_outputs()

        out, fetched = self.run_review_mocked([_api_result()])

        self.assertIn("没有任何预测数据行", out)
        self.assertIn("本次复盘已停止", out)
        fetched.assert_not_called()
        self.assertEqual(self.snapshot_outputs(), before)

    def test_rows_without_formal_prediction_do_not_overwrite(self):
        """CSV 有数据行但无正式推荐（核心剧本校验失败行）→ 停在写入前。"""
        self.write_raw()
        self.write_csv(CSV_HEADER + "\n"
                       "测试001,测试联赛,A队 vs B队,,,,,,,核心剧本校验未通过\n")
        self.seed_existing()
        before = self.snapshot_outputs()

        out, fetched = self.run_review_mocked([_api_result()])

        self.assertIn("没有可复盘场次", out)
        fetched.assert_called_once()
        self.assertEqual(self.snapshot_outputs(), before)

    def test_no_matching_api_results_do_not_overwrite(self):
        """CSV 有有效预测但窗口内无匹配赛果 → 同样不得覆盖旧记录。"""
        self.write_raw()
        self.write_csv(CSV_HEADER + "\n"
                       "测试001,测试联赛,A队 vs B队,主胜,让-1主胜,2球,HH,2:0,8,模型原始\n")
        self.seed_existing()
        before = self.snapshot_outputs()

        # 有赛果返回，但编号与本地预测不匹配（不会触发“拿不到赛果”的早退分支）
        out, _ = self.run_review_mocked([_api_result(num="其他999")])

        self.assertIn("没有可复盘场次", out)
        self.assertEqual(self.snapshot_outputs(), before)


class TestValidAnalysisKeepsBehavior(_ReviewSandbox):
    """场景 C：有效 CSV → 原复盘路径与统计口径保持不变。"""

    def test_normal_path_writes_expected_daily_record_and_report(self):
        self.write_raw()
        self.write_csv(CSV_HEADER + "\n"
                       "测试001,测试联赛,A队 vs B队,主胜,让-1主胜,2球,HH,2:0,8,模型原始\n")

        out, fetched = self.run_review_mocked([_api_result()])
        fetched.assert_called_once()

        self.assertNotIn("本次复盘已停止", out)
        self.assertNotIn("没有可复盘场次", out)

        record = json.loads(_read(self.daily_path))
        self.assertEqual(record["复盘日期"], DATE)
        self.assertEqual([m["编号"] for m in record["场次"]], ["测试001"])

        match = record["场次"][0]
        # 五种玩法均命中（赛果 2:0 / 半场 1:0 与预测一致）
        for play in ("胜平负", "让球", "总进球", "半全场", "比分"):
            self.assertIs(match["命中"][play], True, f"{play} 应命中")
        self.assertEqual(match["半全场来源"], "模型原始")
        self.assertEqual(match["半全场状态"], "有效预测")

        stats = record["命中统计"]
        self.assertEqual(stats["total"], 1)
        for key in ("spf_valid", "rq_valid", "ttg_valid", "hafu_valid", "crs_valid"):
            self.assertEqual(stats[key], 1, f"{key} 口径应不变")
        for key in ("spf_hit", "rq_hit", "ttg_hit", "hafu_hit", "crs_hit"):
            self.assertEqual(stats[key], 1, f"{key} 口径应不变")
        self.assertEqual(stats["hafu_invalid"], 0)
        self.assertEqual(stats["hafu_no_prediction"], 0)

        report_text = _read(self.report_path)
        self.assertIn("测试001", report_text)


class TestMissingRawJson(_ReviewSandbox):
    """场景 D：原始赛程 JSON 缺失或为空 → 原有错误处理仍生效且不写文件。"""

    def test_missing_raw_json_keeps_original_handling(self):
        self.write_csv(CSV_HEADER + "\n"
                       "测试001,测试联赛,A队 vs B队,主胜,让-1主胜,2球,HH,2:0,8,模型原始\n")
        self.seed_existing()
        before = self.snapshot_outputs()

        out, fetched = self.run_review_mocked([_api_result()])

        self.assertIn("体彩数据", out)
        self.assertIn("get_matches.py", out)
        fetched.assert_not_called()
        self.assertEqual(self.snapshot_outputs(), before)

    def test_empty_raw_json_treated_as_missing(self):
        self.write_raw(payload=[])
        self.write_csv(CSV_HEADER + "\n"
                       "测试001,测试联赛,A队 vs B队,主胜,让-1主胜,2球,HH,2:0,8,模型原始\n")
        self.seed_existing()
        before = self.snapshot_outputs()

        out, fetched = self.run_review_mocked([_api_result()])

        self.assertIn("体彩数据", out)
        fetched.assert_not_called()
        self.assertEqual(self.snapshot_outputs(), before)


if __name__ == "__main__":
    unittest.main()
