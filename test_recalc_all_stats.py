"""recalc_all.py 三态统计与 review.generate_report 分母修复的专项测试。

覆盖：
1. recalc_all 不再从 review 导入不存在的 get_result_label，且返回类型为中文标签；
2. 半场/全场比分 → HDA 编码转换正确（不会出现“胜胜”这类错误编码）；
3. 含非法编码的整条预测返回 None，不计入有效场次；
4. 全合法预测的命中与未命中均计入有效场次；
5. 半场赛果缺失时返回 None，而不是 False；
6. generate_report 的分母只使用有效场次数；
7. 旧式 stats 缺少 *_valid 时明确报告“不可计算”，不回退为总场次。

所有数据在内存构造，process_date 的读写目录被重定向到临时目录，
不修改任何正式 CSV / JSON / 报告。

运行：
    python3 -m unittest test_recalc_all_stats -v
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import recalc_all
import review
from lottery_rules import cn_to_code, get_result_label


DATE = "2026-10-10"
MIXED = "HH / HP / DH"
LEGAL = "HH / DH / DA"


def _analysis(num, hafu, spf="主胜", rq="让-1平", ttg="1球", crs="1:0"):
    return {
        "编号": num,
        "联赛": "测试联赛",
        "信心指数": "5",
        "胜平负推荐": spf,
        "让球推荐": rq,
        "总进球推荐": ttg,
        "半全场推荐": hafu,
        "比分推荐": crs,
    }


def _api(num, half_raw, full_raw, win_flag):
    return {
        "match_num": num,
        "home": "A队",
        "away": "B队",
        "goal_line": "-1",
        "half_score_raw": half_raw,
        "full_score_raw": full_raw,
        "win_flag": win_flag,
        "league": "测试联赛",
    }


def _run_process_date():
    """以固定样本运行 process_date，输出重定向到临时目录。"""
    tecai = {
        "001": {"league": "测试联赛", "goal_line": "-1"},
        "002": {"league": "测试联赛", "goal_line": "-1"},
        "003": {"league": "测试联赛", "goal_line": "-1"},
        "004": {"league": "测试联赛", "goal_line": "-1"},
    }
    analysis_rows = [
        # 混合合法/非法编码，实际 AA：整条无效
        _analysis("001", MIXED, spf="客胜", rq="让-1客胜", crs="0:1"),
        # 全合法，实际 DH：命中
        _analysis("002", LEGAL, spf="主胜", rq="让-1平", crs="1:0"),
        # 全合法，实际 AA：未命中
        _analysis("003", LEGAL, spf="客胜", rq="让-1客胜", crs="0:1"),
        # 半场赛果无法解析：不可判定
        _analysis("004", LEGAL, spf="主胜", rq="让-1平", crs="1:0"),
    ]
    api_results = [
        _api("001", "0:1", "0:1", "A"),
        _api("002", "0:0", "1:0", "H"),
        _api("003", "0:1", "0:1", "A"),
        _api("004", "无效场次", "1:0", "H"),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(recalc_all, "OUTPUT_DIR", tmp), \
             mock.patch.object(recalc_all, "load_tecai_data",
                               return_value=(tecai, analysis_rows)), \
             mock.patch.object(recalc_all, "fetch_tecai_results",
                               return_value=api_results):
            results, stats = recalc_all.process_date(DATE)

        report_path = os.path.join(tmp, f"竞彩复盘_{DATE}.txt")
        with open(report_path, encoding="utf-8") as f:
            report_text = f.read()
        daily_path = os.path.join(tmp, f"每日记录_{DATE}.json")
        with open(daily_path, encoding="utf-8") as f:
            daily = json.load(f)

    return results, stats, report_text, daily


class TestImportContract(unittest.TestCase):
    """1. 导入来源与返回类型。"""

    def test_get_result_label_comes_from_lottery_rules(self):
        self.assertIs(recalc_all.get_result_label, get_result_label)
        self.assertFalse(hasattr(review, "get_result_label"))

    def test_get_result_label_returns_chinese_label(self):
        self.assertEqual(get_result_label(2, 0), "主胜")
        self.assertEqual(get_result_label(1, 1), "平")
        self.assertEqual(get_result_label(0, 1), "客胜")

    def test_score_to_hafu_code_uses_cn_to_code(self):
        # 半场/全场分别转码后拼接，不能出现“胜胜”这类中文双字编码
        half_code = cn_to_code(get_result_label(1, 0))
        full_code = cn_to_code(get_result_label(1, 0))
        self.assertEqual(f"{half_code}{full_code}", "HH")
        self.assertNotEqual(f"{half_code}{full_code}", "胜胜")

        self.assertEqual(
            f"{cn_to_code(get_result_label(0, 0))}{cn_to_code(get_result_label(1, 0))}",
            "DH",
        )


class TestCheckHafuThreeState(unittest.TestCase):
    """3/4/5. check_hafu 的三态返回。"""

    def test_mixed_invalid_returns_none(self):
        self.assertEqual(review.check_hafu(MIXED, "H", "H"), (None, "HH"))
        self.assertEqual(review.check_hafu(MIXED, "A", "A"), (None, "AA"))
        self.assertEqual(review.check_hafu(MIXED, "D", "H"), (None, "DH"))

    def test_legal_hit_and_miss(self):
        self.assertEqual(review.check_hafu(LEGAL, "D", "H"), (True, "DH"))
        self.assertEqual(review.check_hafu(LEGAL, "A", "A"), (False, "AA"))

    def test_unknown_half_score_returns_none(self):
        self.assertEqual(review.check_hafu(LEGAL, "?", "H"), (None, "?H"))
        self.assertEqual(review.check_hafu(MIXED, "?", "H"), (None, "?H"))


class TestProcessDateStats(unittest.TestCase):
    """2/3/4/5/6. recalc_all 的统计与报告分母。"""

    @classmethod
    def setUpClass(cls):
        cls.results, cls.stats, cls.report_text, cls.daily = _run_process_date()

    def test_valid_counters_present_for_all_plays(self):
        for key in ("spf", "rq", "ttg", "hafu", "crs"):
            self.assertIn(f"{key}_valid", self.stats)
            self.assertIn(f"{key}_hit", self.stats)

    def test_hafu_three_state_counters(self):
        # 001 混合非法 → None；004 半场缺失 → None：均不进有效场次
        self.assertEqual(self.stats["total"], 4)
        self.assertEqual(self.stats["hafu_valid"], 2)
        self.assertEqual(self.stats["hafu_hit"], 1)

    def test_other_plays_all_valid(self):
        for key in ("spf", "rq", "ttg", "crs"):
            self.assertEqual(self.stats[f"{key}_valid"], 4)
            self.assertEqual(self.stats[f"{key}_hit"], 4)

    def test_report_denominator_uses_valid_count(self):
        self.assertIn("半全场命中：1/2", self.report_text)
        self.assertNotIn("半全场命中：1/4", self.report_text)
        self.assertIn("🏆 综合命中率：17/18", self.report_text)

    def test_daily_record_keeps_none_not_false(self):
        by_num = {m["编号"]: m for m in self.daily["场次"]}
        self.assertIsNone(by_num["001"]["命中"]["半全场"])
        self.assertIs(by_num["002"]["命中"]["半全场"], True)
        self.assertIs(by_num["003"]["命中"]["半全场"], False)
        self.assertIsNone(by_num["004"]["命中"]["半全场"])
        # 半场无法解析时只记 "-"，不拼出错误的半场比分
        self.assertEqual(by_num["004"]["赛果"]["半场"], "-")


class TestGenerateReportDenominator(unittest.TestCase):
    """6/7. generate_report 对分母缺失的处理。"""

    def _stats(self, with_valid):
        stats = {"total": 4, "spf_hit": 0, "rq_hit": 0, "ttg_hit": 0,
                 "hafu_hit": 0, "crs_hit": 0}
        if with_valid:
            stats.update({"spf_valid": 4, "rq_valid": 4, "ttg_valid": 4,
                          "hafu_valid": 2, "crs_valid": 4})
        return stats

    def test_uses_valid_denominator(self):
        lines = review.generate_report(DATE, [], self._stats(True))
        self.assertIn("  半全场命中：0/2  (0%)", lines)
        self.assertIn("  胜平负命中：0/4  (0%)", lines)

    def test_missing_valid_is_reported_not_fallback_to_total(self):
        lines = review.generate_report(DATE, [], self._stats(False))
        joined = "\n".join(lines)
        self.assertIn("半全场命中：不可计算（缺少有效场次统计）", joined)
        self.assertNotIn("半全场命中：0/4", joined)  # 不把 total 当分母
        self.assertNotIn("综合命中率", joined)

    def test_partial_valid_does_not_print_comprehensive(self):
        stats = self._stats(True)
        del stats["hafu_valid"]
        joined = "\n".join(review.generate_report(DATE, [], stats))
        self.assertNotIn("综合命中率", joined)
        self.assertIn("半全场命中：不可计算（缺少有效场次统计）", joined)


if __name__ == "__main__":
    unittest.main()
