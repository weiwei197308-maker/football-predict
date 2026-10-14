"""review.py 判定纯函数自动化测试。

覆盖 review.py 中已迁移到 lottery_rules 统一口径的纯函数：
- has_formal_prediction：是否计入命中统计
- get_result_after_handicap：让球胜平负实际结果（委托 lottery_rules.compute_handicap_result）
- check_spf / check_rq / check_ttg / check_crs / check_hafu：五大玩法命中判定

其中 check_hafu 重点关注“中文半全场描述 → HDA 代码”统一解析（消除与对照报告的口径分叉）。
运行：python3 -m unittest test_review -v
"""
import unittest

from review import (
    has_formal_prediction,
    get_result_after_handicap,
    check_spf,
    check_rq,
    check_ttg,
    check_crs,
    check_hafu,
)


class TestHasFormalPrediction(unittest.TestCase):
    """仅当存在非空胜平负正式推荐时才计入命中率分母。"""

    def test_has_recommendation(self):
        self.assertTrue(has_formal_prediction({"胜平负推荐": "主胜"}))

    def test_blank_or_missing(self):
        self.assertFalse(has_formal_prediction({"胜平负推荐": "   "}))
        self.assertFalse(has_formal_prediction({"胜平负推荐": ""}))
        self.assertFalse(has_formal_prediction({}))
        self.assertFalse(has_formal_prediction(None))


class TestGetResultAfterHandicap(unittest.TestCase):
    """让球胜平负实际结果：adjusted = 主队进球 + 让球数 后与客队进球比较。"""

    def _label(self, home, away, goal_line):
        return get_result_after_handicap(home, away, goal_line)[0]

    def test_home_gives(self):
        self.assertEqual(self._label(2, 0, "-1"), "主胜")
        self.assertEqual(self._label(1, 0, "-1"), "平")
        self.assertEqual(self._label(1, 1, "-1"), "客胜")
        self.assertEqual(self._label(3, 0, "-2"), "主胜")
        self.assertEqual(self._label(2, 0, "-2"), "平")
        self.assertEqual(self._label(1, 0, "-2"), "客胜")

    def test_home_receives(self):
        self.assertEqual(self._label(0, 1, "+1"), "平")
        self.assertEqual(self._label(0, 2, "+1"), "客胜")
        self.assertEqual(self._label(1, 1, "+1"), "主胜")

    def test_zero_handicap(self):
        self.assertEqual(self._label(1, 1, "0"), "平")
        self.assertEqual(self._label(2, 1, "0"), "主胜")

    def test_unparseable_handicap_is_undecidable(self):
        """让球数缺失/非法时返回“无法判定”，不按无让球默认处理（避免默认通过）。"""
        for bad in ("", "  ", None, "让1", "abc", "-0.5"):
            self.assertEqual(get_result_after_handicap(2, 1, bad)[0], "无法判定")


class TestCheckSpf(unittest.TestCase):
    def test_hit_and_miss(self):
        self.assertEqual(check_spf("主胜", "主胜"), (True, "主胜"))
        self.assertEqual(check_spf("主胜", "平"), (False, "平"))

    def test_strip(self):
        self.assertTrue(check_spf(" 主胜 ", "主胜")[0])

    def test_empty(self):
        self.assertEqual(check_spf("", "平"), (False, "无预测"))


class TestCheckRq(unittest.TestCase):
    """让球命中：方向由 lottery_rules.extract_handicap_direction 统一提取。"""

    def test_with_handicap_prefix(self):
        self.assertEqual(check_rq("让-1主胜", "主胜"), (True, "主胜"))
        self.assertEqual(check_rq("让-1平", "平"), (True, "平"))
        self.assertEqual(check_rq("让+1客胜", "客胜"), (True, "客胜"))
        self.assertEqual(check_rq("让-2平", "平"), (True, "平"))

    def test_direction_only(self):
        self.assertTrue(check_rq("主胜", "主胜")[0])

    def test_miss(self):
        self.assertEqual(check_rq("让-1主胜", "客胜"), (False, "客胜"))

    def test_empty_pred(self):
        self.assertEqual(check_rq("", "主胜"), (False, "无预测"))

    def test_unrecognizable_pred(self):
        """无方向关键词时判未命中，实际值原样返回。"""
        self.assertEqual(check_rq("xyz", "主胜"), (False, "主胜"))


class TestCheckTtg(unittest.TestCase):
    def test_slash_string(self):
        self.assertEqual(check_ttg("2球 / 3球 / 4球", 3), (True, 3))
        self.assertEqual(check_ttg("2球 / 3球", 1), (False, 1))

    def test_comma_string(self):
        self.assertTrue(check_ttg("2,3", 3)[0])

    def test_seven_plus(self):
        self.assertTrue(check_ttg("7+球", 7)[0])

    def test_list_input(self):
        self.assertTrue(check_ttg(["2球", "3球"], 3)[0])

    def test_empty(self):
        self.assertEqual(check_ttg("", 2), (False, 2))
        self.assertEqual(check_ttg(None, 2), (False, 2))


class TestCheckCrs(unittest.TestCase):
    def test_hit(self):
        self.assertEqual(check_crs("2:0 / 2:1", 2, 1), (True, "2:1"))

    def test_miss(self):
        self.assertEqual(check_crs("2:0 / 2:1", 1, 1), (False, "1:1"))

    def test_comma_and_dash_inputs(self):
        self.assertTrue(check_crs("2:1,3:1", 3, 1)[0])
        self.assertTrue(check_crs("2-1", 2, 1)[0])

    def test_empty_or_garbage(self):
        self.assertEqual(check_crs("", 1, 0), (False, "1:0"))
        self.assertEqual(check_crs("garbage", 2, 1), (False, "2:1"))


class TestCheckHafu(unittest.TestCase):
    """半全场命中：HDA 代码与中文描述统一由 lottery_rules.parse_hafu_codes 解析。"""

    def test_hda_codes(self):
        self.assertEqual(check_hafu("HH / DH", "H", "H"), (True, "HH"))
        self.assertEqual(check_hafu("HH / DH", "A", "A"), (False, "AA"))

    def test_lowercase_codes(self):
        self.assertTrue(check_hafu("hh / dh", "H", "H")[0])

    def test_chinese_description_regression(self):
        """回归：中文半全场描述必须被识别（旧 HDA-only 解析会漏计）。

        取 2026-10-11 真实预测样例：
        '半平-全客胜 / 半客胜-全客胜 / 半平-全平' → [DA, AA, DD]
        """
        pred = "半平-全客胜 / 半客胜-全客胜 / 半平-全平"
        self.assertEqual(check_hafu(pred, "D", "D"), (True, "DD"))
        self.assertEqual(check_hafu(pred, "A", "A"), (True, "AA"))
        self.assertEqual(check_hafu(pred, "D", "A"), (True, "DA"))
        self.assertEqual(check_hafu(pred, "H", "H"), (False, "HH"))

    def test_mixed_codes_and_chinese(self):
        self.assertTrue(check_hafu("HH / 半平-全主胜", "D", "H")[0])

    def test_invalid_items_skipped(self):
        self.assertTrue(check_hafu("XH / HH / XX", "H", "H")[0])

    def test_empty(self):
        self.assertEqual(check_hafu("", "H", "H"), (False, "HH"))


if __name__ == "__main__":
    unittest.main()
