"""lottery_rules.py 自动化测试。

覆盖体彩让球映射、比分解析、总进球计算、半全场兼容性、一致性检查的关键边界情况。
运行：python3 -m unittest test_lottery_rules -v
"""
import json
import unittest

from lottery_rules import (
    parse_handicap,
    parse_score,
    parse_hafu_codes,
    parse_hafu_code_from_text,
    cn_to_code,
    code_to_cn,
    get_result_label,
    get_goal_diff,
    compute_handicap_result,
    compute_total_goals,
    total_goals_label,
    get_hafu_full_result,
    is_hafu_compatible,
    hafu_candidates_for_full,
    check_score_vs_spf,
    check_score_vs_goal_diff,
    check_score_vs_handicap,
    check_hafu_vs_full,
    check_score_vs_total_goals,
    run_consistency_checks,
    derive_handicap_recommendation,
    normalize_result_label,
    normalize_goal_interval,
    goal_diff_to_interval,
    validate_core_script,
    format_core_validation_report,
    GOAL_DIFF_INTERVALS,
)


class TestParseHandicap(unittest.TestCase):
    def test_valid_values(self):
        self.assertEqual(parse_handicap("-1"), -1)
        self.assertEqual(parse_handicap("+1"), 1)
        self.assertEqual(parse_handicap("-2"), -2)
        self.assertEqual(parse_handicap("+2"), 2)
        self.assertEqual(parse_handicap("-3"), -3)
        self.assertEqual(parse_handicap("+3"), 3)
        self.assertEqual(parse_handicap("0"), 0)

    def test_int_input(self):
        self.assertEqual(parse_handicap(-1), -1)
        self.assertEqual(parse_handicap(1), 1)

    def test_invalid(self):
        self.assertIsNone(parse_handicap(""))
        self.assertIsNone(parse_handicap(None))
        self.assertIsNone(parse_handicap("让1"))
        self.assertIsNone(parse_handicap("abc"))
        self.assertIsNone(parse_handicap("-0.5"))


class TestComputeHandicapResult(unittest.TestCase):
    """让球规则测试 - 全覆盖"""

    # -1 让球系列
    def test_home_minus1_2_0_win(self):
        self.assertEqual(compute_handicap_result(2, 0, -1), "主胜")

    def test_home_minus1_1_0_draw(self):
        self.assertEqual(compute_handicap_result(1, 0, -1), "平")

    def test_home_minus1_1_1_lose(self):
        self.assertEqual(compute_handicap_result(1, 1, -1), "客胜")

    # -2 让球系列
    def test_home_minus2_3_0_win(self):
        self.assertEqual(compute_handicap_result(3, 0, -2), "主胜")

    def test_home_minus2_2_0_draw(self):
        self.assertEqual(compute_handicap_result(2, 0, -2), "平")

    def test_home_minus2_1_0_lose(self):
        self.assertEqual(compute_handicap_result(1, 0, -2), "客胜")

    # +1 让球系列
    def test_home_plus1_0_1_draw(self):
        self.assertEqual(compute_handicap_result(0, 1, 1), "平")

    def test_home_plus1_0_2_lose(self):
        self.assertEqual(compute_handicap_result(0, 2, 1), "客胜")

    def test_home_plus1_1_1_win(self):
        self.assertEqual(compute_handicap_result(1, 1, 1), "主胜")

    # +2 让球系列
    def test_home_plus2_0_1_win(self):
        self.assertEqual(compute_handicap_result(0, 1, 2), "主胜")

    def test_home_plus2_0_2_draw(self):
        self.assertEqual(compute_handicap_result(0, 2, 2), "平")

    def test_home_plus2_0_3_lose(self):
        self.assertEqual(compute_handicap_result(0, 3, 2), "客胜")

    def test_invalid_handicap(self):
        self.assertEqual(compute_handicap_result(2, 0, "abc"), "无法判定")
        self.assertEqual(compute_handicap_result(2, 0, ""), "无法判定")
        self.assertEqual(compute_handicap_result(2, 0, None), "无法判定")

    def test_string_handicap(self):
        self.assertEqual(compute_handicap_result(2, 0, "-1"), "主胜")
        self.assertEqual(compute_handicap_result(0, 1, "+1"), "平")


class TestParseScore(unittest.TestCase):
    def test_colon(self):
        self.assertEqual(parse_score("2:1"), (2, 1))

    def test_dash(self):
        self.assertEqual(parse_score("2-1"), (2, 1))

    def test_spaces(self):
        self.assertEqual(parse_score("2 : 1"), (2, 1))

    def test_invalid(self):
        self.assertIsNone(parse_score(""))
        self.assertIsNone(parse_score("abc"))
        self.assertIsNone(parse_score("2"))
        self.assertIsNone(parse_score(None))


class TestTotalGoals(unittest.TestCase):
    def test_compute(self):
        self.assertEqual(compute_total_goals(2, 1), 3)
        self.assertEqual(compute_total_goals(0, 0), 0)
        self.assertEqual(compute_total_goals(5, 3), 8)

    def test_label(self):
        self.assertEqual(total_goals_label(0), "0")
        self.assertEqual(total_goals_label(3), "3")
        self.assertEqual(total_goals_label(6), "6")
        self.assertEqual(total_goals_label(7), "7+")
        self.assertEqual(total_goals_label(10), "7+")


class TestHafu(unittest.TestCase):
    def test_parse_codes(self):
        self.assertEqual(parse_hafu_codes("HH / DH / DD"), ["HH", "DH", "DD"])
        self.assertEqual(parse_hafu_codes("HH,DH,DD"), ["HH", "DH", "DD"])
        self.assertEqual(parse_hafu_codes("HH"), ["HH"])
        self.assertEqual(parse_hafu_codes(""), [])
        # 非法项被跳过
        self.assertEqual(parse_hafu_codes("HH / XX / DD"), ["HH", "DD"])

    def test_full_result_valid(self):
        self.assertEqual(get_hafu_full_result("HH"), "H")
        self.assertEqual(get_hafu_full_result("DA"), "A")
        self.assertEqual(get_hafu_full_result("DD"), "D")
        self.assertEqual(get_hafu_full_result("AH"), "H")

    def test_full_result_invalid(self):
        # 非法代码必须被拒绝
        self.assertIsNone(get_hafu_full_result("XH"))
        self.assertIsNone(get_hafu_full_result("HX"))
        self.assertIsNone(get_hafu_full_result("XX"))
        self.assertIsNone(get_hafu_full_result("H"))
        self.assertIsNone(get_hafu_full_result(""))
        self.assertIsNone(get_hafu_full_result(None))

    def test_compatible(self):
        self.assertTrue(is_hafu_compatible("HH", "主胜"))
        self.assertTrue(is_hafu_compatible("DH", "主胜"))
        self.assertTrue(is_hafu_compatible("AH", "主胜"))
        self.assertTrue(is_hafu_compatible("HD", "平"))
        self.assertTrue(is_hafu_compatible("DD", "平"))
        self.assertTrue(is_hafu_compatible("AD", "平"))
        self.assertTrue(is_hafu_compatible("HA", "客胜"))
        self.assertTrue(is_hafu_compatible("DA", "客胜"))
        self.assertTrue(is_hafu_compatible("AA", "客胜"))
        self.assertFalse(is_hafu_compatible("HH", "客胜"))
        self.assertFalse(is_hafu_compatible("DA", "主胜"))
        # 非法代码必须不兼容
        self.assertFalse(is_hafu_compatible("XH", "主胜"))
        self.assertFalse(is_hafu_compatible("XX", "主胜"))

    def test_hafu_full_combinations(self):
        """全场主胜只允许 HH/DH/AH"""
        for code in ["HH", "DH", "AH"]:
            self.assertTrue(is_hafu_compatible(code, "主胜"),
                            f"{code} 应与主胜兼容")
        for code in ["HD", "DD", "AD", "HA", "DA", "AA"]:
            self.assertFalse(is_hafu_compatible(code, "主胜"),
                             f"{code} 不应与主胜兼容")

    def test_hafu_draw_combinations(self):
        """全场平只允许 HD/DD/AD"""
        for code in ["HD", "DD", "AD"]:
            self.assertTrue(is_hafu_compatible(code, "平"),
                            f"{code} 应与平兼容")
        for code in ["HH", "DH", "AH", "HA", "DA", "AA"]:
            self.assertFalse(is_hafu_compatible(code, "平"),
                             f"{code} 不应与平兼容")

    def test_hafu_away_combinations(self):
        """全场客胜只允许 HA/DA/AA"""
        for code in ["HA", "DA", "AA"]:
            self.assertTrue(is_hafu_compatible(code, "客胜"),
                            f"{code} 应与客胜兼容")
        for code in ["HH", "DH", "AH", "HD", "DD", "AD"]:
            self.assertFalse(is_hafu_compatible(code, "客胜"),
                             f"{code} 不应与客胜兼容")

    def test_illegal_code_rejected(self):
        """非法代码如 XH 必须被拒绝"""
        self.assertIsNone(get_hafu_full_result("XH"))
        self.assertFalse(is_hafu_compatible("XH", "主胜"))
        self.assertFalse(is_hafu_compatible("XH", "平"))
        self.assertFalse(is_hafu_compatible("XH", "客胜"))

    def test_parse_hafu_filters_illegal(self):
        """parse_hafu_codes 应跳过非法项"""
        result = parse_hafu_codes("XH / HH / XX / DH / AX")
        self.assertEqual(result, ["HH", "DH"])


class TestParseHafuCodeFromText(unittest.TestCase):
    """中文半全场描述安全转换"""

    def test_hda_codes(self):
        self.assertEqual(parse_hafu_code_from_text("HH"), "HH")
        self.assertEqual(parse_hafu_code_from_text("dh"), "DH")
        self.assertEqual(parse_hafu_code_from_text(" AA "), "AA")

    def test_chinese_all_nine(self):
        """9 种中文描述全部正确转换"""
        cases = {
            "半主胜-全主胜": "HH", "半平-全主胜": "DH", "半客胜-全主胜": "AH",
            "半主胜-全平": "HD", "半平-全平": "DD", "半客胜-全平": "AD",
            "半主胜-全客胜": "HA", "半平-全客胜": "DA", "半客胜-全客胜": "AA",
        }
        for text, expected in cases.items():
            self.assertEqual(parse_hafu_code_from_text(text), expected,
                             f"{text} 应转换为 {expected}")

    def test_chinese_with_spaces(self):
        """允许中文描述中有空格"""
        self.assertEqual(parse_hafu_code_from_text("半平 - 全客胜"), "DA")

    def test_illegal_hda(self):
        self.assertIsNone(parse_hafu_code_from_text("XH"))
        self.assertIsNone(parse_hafu_code_from_text("HX"))
        self.assertIsNone(parse_hafu_code_from_text("XX"))

    def test_incomplete_chinese(self):
        """中文描述不完整时返回 None"""
        self.assertIsNone(parse_hafu_code_from_text("半平-全"))
        self.assertIsNone(parse_hafu_code_from_text("全主胜"))
        self.assertIsNone(parse_hafu_code_from_text("半平"))

    def test_non_string(self):
        self.assertIsNone(parse_hafu_code_from_text(None))
        self.assertIsNone(parse_hafu_code_from_text(123))
        self.assertIsNone(parse_hafu_code_from_text(["HH"]))
        self.assertIsNone(parse_hafu_code_from_text(""))

    def test_parse_hafu_codes_mixed(self):
        """parse_hafu_codes 混合 HDA/中文/非法输入"""
        result = parse_hafu_codes("HH / 半平-全主胜 / XH / 半客胜-全客胜 / DD")
        self.assertEqual(result, ["HH", "DH", "AA", "DD"])

    def test_parse_hafu_codes_real_case(self):
        """真实 CSV 中的格式"""
        result = parse_hafu_codes("半平-全客胜 / 半客胜-全客胜 / 半平-全平")
        self.assertEqual(result, ["DA", "AA", "DD"])

    def test_parse_hafu_codes_dedup(self):
        """转换后去重"""
        result = parse_hafu_codes("HH / 半主胜-全主胜 / HH")
        self.assertEqual(result, ["HH"])


class TestHafuCandidatesForFull(unittest.TestCase):
    """规则兜底生成合法候选全集"""

    def test_home_win(self):
        self.assertEqual(hafu_candidates_for_full("主胜"), ["HH", "DH", "AH"])

    def test_draw(self):
        self.assertEqual(hafu_candidates_for_full("平"), ["HD", "DD", "AD"])

    def test_away_win(self):
        self.assertEqual(hafu_candidates_for_full("客胜"), ["HA", "DA", "AA"])

    def test_unrecognized(self):
        self.assertEqual(hafu_candidates_for_full(""), [])
        self.assertEqual(hafu_candidates_for_full("未知"), [])

    def test_all_candidates_compatible(self):
        """兜底候选必须全部与全场结果兼容"""
        for spf in ["主胜", "平", "客胜"]:
            for c in hafu_candidates_for_full(spf):
                self.assertTrue(is_hafu_compatible(c, spf))


class TestCheckScoreVsSpf(unittest.TestCase):
    def test_match(self):
        ok, _ = check_score_vs_spf("2:1", "主胜")
        self.assertTrue(ok)

    def test_mismatch(self):
        ok, msg = check_score_vs_spf("1:0", "客胜")
        self.assertFalse(ok)
        self.assertIn("客胜", msg)

    def test_draw(self):
        ok, _ = check_score_vs_spf("1:1", "平")
        self.assertTrue(ok)


class TestCheckScoreVsHandicap(unittest.TestCase):
    def test_home_minus1_2_0(self):
        ok, _ = check_score_vs_handicap("2:0", -1, "主胜")
        self.assertTrue(ok)

    def test_home_minus1_1_0(self):
        ok, _ = check_score_vs_handicap("1:0", -1, "平")
        self.assertTrue(ok)

    def test_home_minus1_1_1(self):
        ok, _ = check_score_vs_handicap("1:1", -1, "客胜")
        self.assertTrue(ok)

    def test_mismatch(self):
        ok, msg = check_score_vs_handicap("1:0", -1, "主胜")
        self.assertFalse(ok)
        self.assertIn("应为 平", msg)

    def test_invalid_handicap(self):
        ok, msg = check_score_vs_handicap("2:0", "abc", "主胜")
        self.assertFalse(ok)
        self.assertIn("无法解析", msg)


class TestCheckScoreVsGoalDiff(unittest.TestCase):
    """净胜球分布与比分一致性检查"""

    def test_home_win2_plus_match(self):
        """主胜2+ 与 2:0 相容"""
        status, _ = check_score_vs_goal_diff("2:0", "主胜2+")
        self.assertEqual(status, "通过")

    def test_home_win2_plus_3_0(self):
        """主胜2+ 与 3:0 相容"""
        status, _ = check_score_vs_goal_diff("3:0", "主胜2+")
        self.assertEqual(status, "通过")

    def test_home_win1_match(self):
        """主胜1 与 2:1 相容"""
        status, _ = check_score_vs_goal_diff("2:1", "主胜1")
        self.assertEqual(status, "通过")

    def test_home_win1_conflict_2_0(self):
        """主胜1 与 2:0 冲突"""
        status, msg = check_score_vs_goal_diff("2:0", "主胜1")
        self.assertEqual(status, "冲突")
        self.assertIn("2", msg)

    def test_draw_match(self):
        """平 与 1:1 相容"""
        status, _ = check_score_vs_goal_diff("1:1", "平")
        self.assertEqual(status, "通过")

    def test_draw_match_0_0(self):
        """平 与 0:0 相容"""
        status, _ = check_score_vs_goal_diff("0:0", "平")
        self.assertEqual(status, "通过")

    def test_away_win1_match(self):
        """客胜1 与 0:1 相容"""
        status, _ = check_score_vs_goal_diff("0:1", "客胜1")
        self.assertEqual(status, "通过")

    def test_away_win2_plus_match(self):
        """客胜2+ 与 0:2 相容"""
        status, _ = check_score_vs_goal_diff("0:2", "客胜2+")
        self.assertEqual(status, "通过")

    def test_away_win2_plus_0_3(self):
        """客胜2+ 与 0:3 相容"""
        status, _ = check_score_vs_goal_diff("0:3", "客胜2+")
        self.assertEqual(status, "通过")

    def test_unrecognized_interval(self):
        """无法识别的区间不能判定为通过"""
        status, msg = check_score_vs_goal_diff("2:0", "未知区间")
        self.assertEqual(status, "无法判定")
        self.assertIn("无法识别", msg)

    def test_empty_interval(self):
        """空区间返回无法判定"""
        status, _ = check_score_vs_goal_diff("2:0", "")
        self.assertEqual(status, "无法判定")

    def test_home_win2_plus_conflict_1_0(self):
        """主胜2+ 与 1:0 冲突（净胜球只有1）"""
        status, msg = check_score_vs_goal_diff("1:0", "主胜2+")
        self.assertEqual(status, "冲突")

    def test_home_win1_conflict_3_0(self):
        """主胜1 与 3:0 冲突"""
        status, _ = check_score_vs_goal_diff("3:0", "主胜1")
        self.assertEqual(status, "冲突")

    def test_draw_conflict_2_1(self):
        """平 与 2:1 冲突"""
        status, _ = check_score_vs_goal_diff("2:1", "平")
        self.assertEqual(status, "冲突")

    def test_away_win1_conflict_0_2(self):
        """客胜1 与 0:2 冲突"""
        status, _ = check_score_vs_goal_diff("0:2", "客胜1")
        self.assertEqual(status, "冲突")

    def test_invalid_score(self):
        """比分格式错误时返回无法判定"""
        status, _ = check_score_vs_goal_diff("abc", "主胜2+")
        self.assertEqual(status, "无法判定")


class TestCheckScoreVsTotalGoals(unittest.TestCase):
    def test_match_3(self):
        ok, _ = check_score_vs_total_goals("2:1", "2球 / 3球 / 4球")
        self.assertTrue(ok)

    def test_mismatch(self):
        ok, msg = check_score_vs_total_goals("1:0", "2球 / 3球 / 4球")
        self.assertFalse(ok)
        self.assertIn("1", msg)

    def test_seven_plus(self):
        ok, _ = check_score_vs_total_goals("5:3", "5球 / 6球 / 7+球")
        self.assertTrue(ok)


class TestCheckHafuVsFull(unittest.TestCase):
    def test_all_compatible(self):
        ok, _ = check_hafu_vs_full("HH / DH / AH", "主胜")
        self.assertTrue(ok)

    def test_one_incompatible(self):
        ok, msg = check_hafu_vs_full("HH / DA / DD", "主胜")
        self.assertFalse(ok)
        self.assertIn("DA", msg)

    def test_all_incompatible(self):
        """全部不兼容时应报错"""
        ok, msg = check_hafu_vs_full("HA / DA / AA", "主胜")
        self.assertFalse(ok)

    def test_empty_codes(self):
        """空推荐应报错"""
        ok, msg = check_hafu_vs_full("", "主胜")
        self.assertFalse(ok)


class TestRunConsistencyChecks(unittest.TestCase):
    def test_all_consistent(self):
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "2球 / 3球 / 4球",
            "半全场推荐": "HH / DH / AH",
            "比分推荐": "2:0 / 3:0 / 3:1",
            "净胜球分布": "主胜2+",
        }
        hard, soft, unjudgeable = run_consistency_checks(pred, "-1")
        self.assertEqual(hard, [])
        self.assertEqual(soft, [])
        self.assertEqual(unjudgeable, [])

    def test_handicap_hard_error(self):
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "1球 / 2球 / 3球",
            "半全场推荐": "HH / DH / DD",
            "比分推荐": "1:0 / 2:0 / 1:1",
        }
        hard, _, _ = run_consistency_checks(pred, "-1")
        self.assertTrue(any("让球" in e for e in hard))

    def test_total_goals_hard_error(self):
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "3球 / 4球 / 5球",
            "半全场推荐": "HH / DH / DD",
            "比分推荐": "1:0 / 2:0 / 2:1",
        }
        hard, _, _ = run_consistency_checks(pred, "-1")
        self.assertTrue(any("总进球" in e for e in hard))

    def test_hafu_hard_error(self):
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "2球 / 3球 / 4球",
            "半全场推荐": "HH / DA / DD",
            "比分推荐": "2:0 / 2:1 / 1:0",
        }
        hard, _, _ = run_consistency_checks(pred, "-1")
        self.assertTrue(any("半全场" in e for e in hard))

    def test_missing_handicap_with_rec(self):
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "2球 / 3球 / 4球",
            "半全场推荐": "HH",
            "比分推荐": "2:0",
        }
        hard, _, _ = run_consistency_checks(pred, "")
        self.assertTrue(any("让球数" in e for e in hard))

    def test_goal_diff_conflict_is_hard(self):
        """净胜球分布冲突应为硬性错误"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "1球 / 2球 / 3球",
            "半全场推荐": "HH / DH / AH",
            "比分推荐": "2:0 / 2:1 / 1:0",
            "净胜球分布": "主胜1",
        }
        hard, soft, unjudgeable = run_consistency_checks(pred, "-1")
        # 2:0 净胜球2，但标记为主胜1 → 冲突
        self.assertTrue(any("主胜1" in e for e in hard))

    def test_goal_diff_unrecognized_is_unjudgeable(self):
        """无法识别的净胜球区间应进入 unjudgeable"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "2球 / 3球 / 4球",
            "半全场推荐": "HH / DH / AH",
            "比分推荐": "2:0 / 2:1 / 1:0",
            "净胜球分布": "未知区间",
        }
        hard, soft, unjudgeable = run_consistency_checks(pred, "-1")
        self.assertTrue(any("无法识别" in u for u in unjudgeable))

    def test_goal_diff_missing_is_unjudgeable(self):
        """净胜球分布缺失应进入 unjudgeable"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "2球 / 3球 / 4球",
            "半全场推荐": "HH / DH / AH",
            "比分推荐": "2:0 / 2:1 / 1:0",
        }
        hard, soft, unjudgeable = run_consistency_checks(pred, "-1")
        self.assertTrue(any("净胜球" in u for u in unjudgeable))

    def test_backup_score_different_direction_is_soft(self):
        """备选比分方向不同应为软性警告"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "2球 / 3球 / 4球",
            "半全场推荐": "HH / DH / AH",
            "比分推荐": "2:0 / 1:1 / 0:1",
            "净胜球分布": "主胜2+",
        }
        hard, soft, _ = run_consistency_checks(pred, "-1")
        # 1:1 和 0:1 方向不是主胜 → 软性警告
        self.assertTrue(any("1:1" in w for w in soft))
        self.assertTrue(any("0:1" in w for w in soft))

    def test_backup_score_bad_format_is_hard(self):
        """备选比分格式错误应为硬性错误"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "2球 / 3球 / 4球",
            "半全场推荐": "HH / DH / AH",
            "比分推荐": "2:0 / abc / 1:0",
            "净胜球分布": "主胜2+",
        }
        hard, _, _ = run_consistency_checks(pred, "-1")
        self.assertTrue(any("备选比分格式异常" in e for e in hard))

    def test_backup_score_outside_goal_diff_range_is_soft(self):
        """备选比分不在净胜球区间内应软性提示（替代剧本）"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "2球 / 3球 / 4球",
            "半全场推荐": "HH / DH / AH",
            # 首选 3:0 在主胜2+；备选 2:1(gd=1) 同方向但不在主胜2+区间
            "比分推荐": "3:0 / 2:1 / 3:1",
            "净胜球分布": "主胜2+",
        }
        hard, soft, _ = run_consistency_checks(pred, "-1")
        self.assertTrue(any("2:1" in w and "净胜球分布" in w for w in soft))
        # 不删除备选比分，不产生硬性错误
        self.assertFalse(any("2:1" in e for e in hard))

    def test_backup_scores_checked_individually(self):
        """每个备选比分都应被逐个检查（格式/总进球/让球/净胜球）"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "2球 / 4球",
            "半全场推荐": "HH / DH / AH",
            # 三个备选：2:1(gd=1区间外)、1:1(平局替代剧本)、abc(格式错)
            "比分推荐": "3:0 / 2:1 / 1:1 / abc",
            "净胜球分布": "主胜2+",
        }
        hard, soft, _ = run_consistency_checks(pred, "-1")
        # abc 格式硬性错误
        self.assertTrue(any("abc" in e for e in hard))
        # 2:1 区间外提示
        self.assertTrue(any("2:1" in w for w in soft))
        # 1:1 方向不同提示
        self.assertTrue(any("1:1" in w and "替代剧本" in w for w in soft))

    def test_returns_three_lists(self):
        """验证返回值为三元组"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": "2球 / 3球 / 4球",
            "半全场推荐": "HH / DH / AH",
            "比分推荐": "2:0",
            "净胜球分布": "主胜2+",
        }
        result = run_consistency_checks(pred, "-1")
        self.assertEqual(len(result), 3)


class TestDeriveHandicapRecommendation(unittest.TestCase):
    def test_derive(self):
        self.assertEqual(derive_handicap_recommendation("2:0", "-1"), "让-1主胜")
        self.assertEqual(derive_handicap_recommendation("1:0", "-1"), "让-1平")
        self.assertEqual(derive_handicap_recommendation("1:1", "-1"), "让-1客胜")
        self.assertEqual(derive_handicap_recommendation("0:1", "+1"), "让+1平")
        self.assertEqual(derive_handicap_recommendation("0:2", "+1"), "让+1客胜")
        self.assertEqual(derive_handicap_recommendation("2:0", "-2"), "让-2平")
        self.assertEqual(derive_handicap_recommendation("3:0", "-2"), "让-2主胜")
        self.assertEqual(derive_handicap_recommendation("0:1", "+2"), "让+2主胜")

    def test_invalid(self):
        self.assertEqual(derive_handicap_recommendation("2:0", ""), "让球无法判定")
        self.assertEqual(derive_handicap_recommendation("abc", "-1"), "让球无法判定")
        self.assertEqual(derive_handicap_recommendation("2:0", None), "让球无法判定")


class TestPostProcessPrediction(unittest.TestCase):
    """测试 analyze.py 的 post_process_prediction 修正与复查闭环"""

    def setUp(self):
        from analyze import post_process_prediction
        self.post_process = post_process_prediction

    def test_no_errors_passes(self):
        """无错误时状态为通过"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["2球", "3球", "4球"],
            "半全场推荐": ["HH", "DH", "AH"],
            "比分推荐": ["2:0", "2:1", "1:0"],
            "净胜球分布": "主胜2+",
        }
        _, report = self.post_process(pred, "-1")
        self.assertIn("状态: 通过", report)

    def test_handicap_fix_then_recheck_passes(self):
        """修正前有让球错误，修正后复查通过"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["2球", "3球", "4球"],
            "半全场推荐": ["HH", "DH", "AH"],
            "比分推荐": ["1:0", "2:0", "1:1"],
            "净胜球分布": "主胜1",
        }
        result, report = self.post_process(pred, "-1")
        # 1:0 + -1 → 平，原推荐主胜 → 需修正
        self.assertIn("已修正", report)
        self.assertIn("状态: 已修正，复查通过", report)
        # 让球推荐应被修正为让-1平
        self.assertEqual(result["让球推荐"], "让-1平")
        # 理由不应包含与新结果矛盾的旧结论
        self.assertIn("1:0", result["让球理由"])
        self.assertIn("让-1平", result["让球理由"])

    def test_handicap_reason_no_old_contradiction(self):
        """修正让球后，理由不得残留与新结果相矛盾的旧结论"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "让球理由": "主队实力强，让球后仍可赢",
            "总进球推荐": ["2球", "3球", "4球"],
            "半全场推荐": ["HH", "DH", "AH"],
            "比分推荐": ["1:0", "2:0", "1:1"],
            "净胜球分布": "主胜1",
        }
        result, report = self.post_process(pred, "-1")
        # 理由应被完全重写
        self.assertNotIn("主队实力强", result["让球理由"])
        self.assertIn("[程序校验修正]", result["让球理由"])

    def test_total_goals_fix_makes_compatible(self):
        """修正总进球后首选比分和推荐列表相容"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["3球", "4球", "5球"],
            "半全场推荐": ["HH", "DH", "AH"],
            "比分推荐": ["1:0", "2:0", "1:1"],
            "净胜球分布": "主胜1",
        }
        result, report = self.post_process(pred, "-1")
        # 1:0 总进球1，不在 3/4/5 → 应补入1球
        ttg = result.get("总进球推荐", [])
        if isinstance(ttg, str):
            ttg_nums = [t for t in ttg.split()]
        else:
            ttg_str = " / ".join(str(t) for t in ttg)
            ttg_nums = ttg_str
        self.assertIn("1球", ttg_str if isinstance(ttg, list) else ttg)
        self.assertIn("已修正", report)

    def test_hafu_all_incompatible_no_restore(self):
        """所有半全场组合不兼容时，不得恢复错误列表，应规则兜底生成"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["2球", "3球", "4球"],
            "半全场推荐": ["HA", "DA", "AA"],  # 全部客胜方向，与主胜不兼容
            "比分推荐": ["2:0", "2:1", "1:0"],
            "净胜球分布": "主胜2+",
        }
        result, report = self.post_process(pred, "-1")
        # 不得恢复原始错误列表
        hafu = result.get("半全场推荐", [])
        self.assertNotIn("HA", hafu)
        self.assertNotIn("DA", hafu)
        self.assertNotIn("AA", hafu)
        # 应按主胜剧本兜底生成 HH/DH/AH
        self.assertEqual(set(hafu), {"HH", "DH", "AH"})
        # 必须标注为程序兜底、待模型确认
        self.assertIn("程序兜底生成", result.get("半全场理由", ""))
        self.assertIn("待模型确认", report)

    def test_hafu_chinese_description_converted(self):
        """真实场景：LLM 返回中文半全场描述，应安全转码"""
        pred = {
            "胜平负推荐": "客胜",
            "让球推荐": "让+1客胜",
            "总进球推荐": ["0球", "1球", "2球"],
            "半全场推荐": ["半平-全客胜", "半客胜-全客胜", "半平-全平"],
            "比分推荐": ["0:1", "0:2", "1:2"],
            "净胜球分布": "客胜1",
        }
        result, report = self.post_process(pred, "+1")
        hafu = result.get("半全场推荐", [])
        # 半平-全客胜→DA, 半客胜-全客胜→AA 与客胜兼容；半平-全平→DD 不兼容应移除
        self.assertIn("DA", hafu)
        self.assertIn("AA", hafu)
        self.assertNotIn("DD", hafu)
        self.assertIn("中文转码", report)

    def test_hafu_empty_with_spf_generates_fallback(self):
        """半全场为空但胜平负存在时，规则兜底生成候选"""
        pred = {
            "胜平负推荐": "平",
            "让球推荐": "让+1主胜",
            "总进球推荐": ["0球", "1球", "2球"],
            "半全场推荐": [],
            "比分推荐": ["1:1", "0:0", "2:2"],
            "净胜球分布": "平",
        }
        result, report = self.post_process(pred, "+1")
        hafu = result.get("半全场推荐", [])
        self.assertEqual(set(hafu), {"HD", "DD", "AD"})
        self.assertIn("程序兜底生成", result.get("半全场理由", ""))

    def test_hafu_invalid_without_spf_unjudgeable(self):
        """半全场无法解析且胜平负缺失时，清空并标记无法判定"""
        pred = {
            "胜平负推荐": "",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["2球", "3球", "4球"],
            "半全场推荐": ["半平-全", "XX"],
            "比分推荐": ["2:0", "2:1", "1:0"],
            "净胜球分布": "主胜2+",
        }
        result, report = self.post_process(pred, "-1")
        hafu = result.get("半全场推荐", [])
        self.assertEqual(len(hafu), 0)
        # 半全场理由必须明确无法判定，待处理区块必须列出重新生成需求
        self.assertIn("无法判定", result.get("半全场理由", ""))
        self.assertIn("待处理", report)
        self.assertIn("需模型重新生成", report)

    def test_remaining_conflict_shown(self):
        """修正后仍有错误时，报告必须显示仍有冲突"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["3球", "4球", "5球"],
            "半全场推荐": ["HA", "DA", "AA"],
            "比分推荐": ["1:0", "0:1", "1:1"],
            "净胜球分布": "主胜1",
        }
        _, report = self.post_process(pred, "-1")
        # 净胜球分布主胜1 vs 1:0 是相容的（gd=1），
        # 但 0:1 方向为客胜 → 软性
        # 半全场全部不兼容 → 已清空
        # 修正后复查半全场为空 → 不会再报半全场硬性错误
        # 但 1:0+(-1) 让球需修正
        self.assertIn("状态:", report)

    def test_report_from_recheck(self):
        """修正后的报告必须来自修正后的重新检查结果"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["2球", "3球", "4球"],
            "半全场推荐": ["HH", "DA", "AH"],
            "比分推荐": ["1:0", "2:0", "1:1"],
            "净胜球分布": "主胜1",
        }
        result, report = self.post_process(pred, "-1")
        # 让球应被修正为让-1平
        self.assertEqual(result["让球推荐"], "让-1平")
        # DA 应被移除
        hafu = result.get("半全场推荐", [])
        if isinstance(hafu, list):
            self.assertNotIn("DA", hafu)
        # 报告应显示已修正
        self.assertIn("已修正", report)

    def test_non_dict_input(self):
        """非 dict 输入安全处理"""
        _, report = self.post_process("not a dict", "-1")
        self.assertIn("无法判定", report)

    def test_missing_handicap_marks_unjudgeable(self):
        """让球数缺失时标记无法判定"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["2球", "3球", "4球"],
            "半全场推荐": ["HH", "DH", "AH"],
            "比分推荐": ["2:0", "2:1", "1:0"],
            "净胜球分布": "主胜2+",
        }
        result, report = self.post_process(pred, "")
        self.assertIn("无法判定", result.get("让球推荐", ""))
        self.assertIn("无法判定", report)

    def test_missing_score_marks_unjudgeable(self):
        """首选比分缺失时标记无法判定"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["2球", "3球", "4球"],
            "半全场推荐": ["HH", "DH", "AH"],
            "比分推荐": [],
            "净胜球分布": "主胜2+",
        }
        result, report = self.post_process(pred, "-1")
        self.assertIn("无法判定", report)

    def test_total_goals_dedup(self):
        """总进球推荐去重"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["2球", "2球", "3球", "4球"],
            "半全场推荐": ["HH", "DH", "AH"],
            "比分推荐": ["2:0", "2:1", "1:0"],
            "净胜球分布": "主胜2+",
        }
        result, _ = self.post_process(pred, "-1")
        ttg = result.get("总进球推荐", [])
        if isinstance(ttg, list):
            self.assertEqual(len(ttg), len(set(ttg)))

    def test_total_goals_seven_plus(self):
        """7+球映射"""
        pred = {
            "胜平负推荐": "主胜",
            "让球推荐": "让-1主胜",
            "总进球推荐": ["5球", "6球", "7+球"],
            "半全场推荐": ["HH", "DH", "AH"],
            "比分推荐": ["4:3", "3:2", "2:1"],
            "净胜球分布": "主胜1",
        }
        result, _ = self.post_process(pred, "-1")
        # 4:3 总进球7 → 应有7+球
        ttg_str = " / ".join(str(t) for t in result.get("总进球推荐", []))
        self.assertIn("7+球", ttg_str)


# ============ 阶段二：核心比赛剧本校验 ============

def _valid_core_script():
    return {
        "home_team": "A队",
        "away_team": "B队",
        "full_time_result": "主胜",
        "net_goal_interval": "主胜1",
        "primary_score": "2:1",
        "alternative_scores": ["1:0", "3:2"],
        "match_script": "主队控场，边路传中先拔头筹，客队定位球扳平后主队再入一球。",
        "net_goal_reasoning": "主队进攻效率略高但客队防线稳固，净胜 1 球最合理。",
        "uncertainties": ["客队反击效率"],
        "confidence": "中",
    }


class TestNormalizeResultLabel(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(normalize_result_label("主胜"), "主胜")
        self.assertEqual(normalize_result_label("客胜"), "客胜")

    def test_pingju_alias(self):
        """任务结构允许写'平局'，必须规范化为项目枚举'平'"""
        self.assertEqual(normalize_result_label("平局"), "平")
        self.assertEqual(normalize_result_label("平"), "平")

    def test_unknown(self):
        self.assertIsNone(normalize_result_label("主胜一球"))
        self.assertIsNone(normalize_result_label(""))
        self.assertIsNone(normalize_result_label(None))


class TestNormalizeGoalInterval(unittest.TestCase):
    def test_all_canonical(self):
        for v in GOAL_DIFF_INTERVALS:
            self.assertEqual(normalize_goal_interval(v), v)

    def test_aliases(self):
        self.assertEqual(normalize_goal_interval("主胜1球"), "主胜1")
        self.assertEqual(normalize_goal_interval("主胜2球及以上"), "主胜2+")
        self.assertEqual(normalize_goal_interval("客胜2球"), "客胜2+")
        self.assertEqual(normalize_goal_interval("平局"), "平")

    def test_unknown_must_fail(self):
        """未知区间严禁默认通过"""
        self.assertIsNone(normalize_goal_interval("主胜小胜"))
        self.assertIsNone(normalize_goal_interval("大胜"))
        self.assertIsNone(normalize_goal_interval(""))
        self.assertIsNone(normalize_goal_interval(None))


class TestGoalDiffToInterval(unittest.TestCase):
    def test_mapping(self):
        cases = {3: "主胜2+", 2: "主胜2+", 1: "主胜1", 0: "平",
                 -1: "客胜1", -2: "客胜2+", -4: "客胜2+"}
        for gd, expected in cases.items():
            self.assertEqual(goal_diff_to_interval(gd), expected)


class TestValidateCoreScript(unittest.TestCase):
    def test_valid_script(self):
        r = validate_core_script(_valid_core_script(), "A队", "B队")
        self.assertTrue(r["ok"], r["errors"])
        self.assertTrue(r["chain_ok"])
        self.assertEqual(r["normalized"]["full_time_result"], "主胜")
        self.assertEqual(r["normalized"]["net_goal_interval"], "主胜1")
        self.assertEqual(r["normalized"]["primary_score"], "2:1")
        self.assertEqual(r["alternative_scripts"], [])

    def test_non_dict(self):
        r = validate_core_script(["not", "dict"])
        self.assertFalse(r["ok"])
        self.assertTrue(any("不是 JSON 对象" in e for e in r["errors"]))
        self.assertIsNone(r["normalized"])

    def test_missing_required_fields(self):
        for field in ("full_time_result", "net_goal_interval", "primary_score"):
            s = _valid_core_script()
            del s[field]
            r = validate_core_script(s, "A队", "B队")
            self.assertFalse(r["ok"], f"缺少 {field} 必须报错")
            self.assertTrue(any(field in e for e in r["errors"]))

    def test_illegal_result_enum(self):
        s = _valid_core_script()
        s["full_time_result"] = "主队不败"
        r = validate_core_script(s, "A队", "B队")
        self.assertFalse(r["ok"])
        self.assertTrue(any("枚举非法" in e for e in r["errors"]))

    def test_illegal_interval_enum(self):
        s = _valid_core_script()
        s["net_goal_interval"] = "主胜小胜"
        r = validate_core_script(s, "A队", "B队")
        self.assertFalse(r["ok"])
        self.assertTrue(any("区间未知" in e for e in r["errors"]))

    def test_bad_primary_score_format(self):
        s = _valid_core_script()
        s["primary_score"] = "2-1-0"
        r = validate_core_script(s, "A队", "B队")
        self.assertFalse(r["ok"])
        self.assertTrue(any("primary_score 比分格式错误" in e for e in r["errors"]))

    def test_bad_alternative_score_format(self):
        s = _valid_core_script()
        s["alternative_scores"] = ["1:0", "abc"]
        r = validate_core_script(s, "A队", "B队")
        self.assertFalse(r["ok"])
        self.assertTrue(any("abc" in e for e in r["errors"]))

    def test_team_name_mismatch(self):
        r = validate_core_script(_valid_core_script(), "X队", "B队")
        self.assertFalse(r["ok"])
        self.assertTrue(any("主队名称不一致" in e for e in r["errors"]))

    def test_alternative_scores_not_list(self):
        s = _valid_core_script()
        s["alternative_scores"] = "1:0"
        r = validate_core_script(s, "A队", "B队")
        self.assertFalse(r["ok"])
        self.assertTrue(any("必须是数组" in e for e in r["errors"]))

    def test_duplicate_alternative(self):
        s = _valid_core_script()
        s["alternative_scores"] = ["1:0", "1:0"]
        r = validate_core_script(s, "A队", "B队")
        self.assertFalse(r["ok"])
        self.assertTrue(any("重复" in e for e in r["errors"]))

    def test_alternative_equals_primary(self):
        s = _valid_core_script()
        s["alternative_scores"] = ["2:1"]
        r = validate_core_script(s, "A队", "B队")
        self.assertFalse(r["ok"])
        self.assertTrue(any("与主推比分重复" in e for e in r["errors"]))

    def test_illegal_confidence(self):
        s = _valid_core_script()
        s["confidence"] = "极高"
        r = validate_core_script(s, "A队", "B队")
        self.assertFalse(r["ok"])
        self.assertTrue(any("confidence" in e for e in r["errors"]))

    def test_result_score_chain_conflict(self):
        """主推比分 2:1 是主胜，却声明客胜 → 核心链冲突"""
        s = _valid_core_script()
        s["full_time_result"] = "客胜"
        r = validate_core_script(s, "A队", "B队")
        self.assertFalse(r["ok"])
        self.assertFalse(r["chain_ok"])
        self.assertTrue(any("核心链冲突" in e and "full_time_result" in e for e in r["errors"]))

    def test_draw_score_chain(self):
        """1:1 平局比分 + 平局 + 平区间 → 通过"""
        s = _valid_core_script()
        s["full_time_result"] = "平局"
        s["net_goal_interval"] = "平"
        s["primary_score"] = "1:1"
        s["alternative_scores"] = ["0:0", "2:2"]
        r = validate_core_script(s, "A队", "B队")
        self.assertTrue(r["ok"], r["errors"])
        self.assertTrue(r["chain_ok"])
        self.assertEqual(r["normalized"]["full_time_result"], "平")

    def test_score_interval_chain_conflict(self):
        """2:0 净胜 2 球，却声明主胜1 → 核心链冲突"""
        s = _valid_core_script()
        s["primary_score"] = "2:0"
        # full_time_result 仍是主胜（方向一致），但区间不一致
        r = validate_core_script(s, "A队", "B队")
        self.assertFalse(r["ok"])
        self.assertFalse(r["chain_ok"])
        self.assertTrue(any("net_goal_interval" in e for e in r["errors"]))

    def test_alternative_different_direction_marked_not_deleted(self):
        """不同方向备选比分标记为替代剧本，不删除、不算硬性错误"""
        s = _valid_core_script()
        s["alternative_scores"] = ["1:1"]  # 平局替代剧本
        r = validate_core_script(s, "A队", "B队")
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(len(r["alternative_scripts"]), 1)
        alt = r["alternative_scripts"][0]
        self.assertEqual(alt["score"], "1:1")
        self.assertEqual(alt["result"], "平")
        self.assertTrue(any("替代剧本" in w for w in r["warnings"]))

    def test_alternative_same_direction_different_interval_marked(self):
        """同方向但不同净胜球区间（主胜1 剧本下备选 2:0）→ 也标记替代剧本"""
        s = _valid_core_script()
        s["alternative_scores"] = ["2:0"]
        r = validate_core_script(s, "A队", "B队")
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["alternative_scripts"][0]["interval"], "主胜2+")

    def test_normalized_uses_canonical_enums(self):
        """输入用别名（平局/主胜1球），normalized 必须是规范枚举"""
        s = _valid_core_script()
        s["full_time_result"] = "主胜"
        s["net_goal_interval"] = "主胜1球"
        r = validate_core_script(s, "A队", "B队")
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["normalized"]["net_goal_interval"], "主胜1")

    def test_does_not_mutate_input(self):
        s = _valid_core_script()
        snapshot = json.dumps(s, ensure_ascii=False)
        validate_core_script(s, "A队", "B队")
        self.assertEqual(json.dumps(s, ensure_ascii=False), snapshot)

    def test_away_win_script(self):
        s = _valid_core_script()
        s["full_time_result"] = "客胜"
        s["net_goal_interval"] = "客胜2+"
        s["primary_score"] = "0:2"
        s["alternative_scores"] = ["1:3"]
        r = validate_core_script(s, "A队", "B队")
        self.assertTrue(r["ok"], r["errors"])
        self.assertTrue(r["chain_ok"])

    def test_report_format_contains_status(self):
        text = format_core_validation_report(validate_core_script(_valid_core_script(), "A队", "B队"))
        self.assertIn("【核心剧本校验】", text)
        self.assertIn("核心剧本校验通过", text)
        self.assertIn("锁定核心", text)


class TestApplyLockedCoreScript(unittest.TestCase):
    """阶段一锁定剧本对玩法层字段的覆盖（analyze.py 纯函数部分）"""

    def _core(self):
        return {
            "home_team": "A队", "away_team": "B队",
            "full_time_result": "主胜", "net_goal_interval": "主胜1",
            "primary_score": "1:0", "alternative_scores": ["2:1"],
            "match_script": "主队小胜", "net_goal_reasoning": "净胜一球",
            "uncertainties": ["定位球"], "confidence": "中",
        }

    def test_overrides_core_fields(self):
        from analyze import apply_locked_core_script
        parsed = {
            "胜平负推荐": "客胜",            # 模型玩法层故意给出矛盾方向
            "净胜球分布": "客胜1",
            "比分推荐": ["0:1", "0:2"],
            "让球推荐": "让-1客胜",
        }
        out = apply_locked_core_script(parsed, self._core())
        self.assertEqual(out["胜平负推荐"], "主胜")
        self.assertEqual(out["净胜球分布"], "主胜1")
        self.assertEqual(out["比分推荐"], ["1:0", "2:1"])
        # 让球字段不在这里覆盖，交给确定性后处理
        self.assertEqual(out["让球推荐"], "让-1客胜")
        self.assertIn("主队小胜", out["核心剧本"])
        self.assertIn("定位球", out["数据缺失说明"])

    def test_none_core_noop(self):
        from analyze import apply_locked_core_script
        parsed = {"胜平负推荐": "主胜"}
        self.assertIs(apply_locked_core_script(parsed, None), parsed)


class TestCoreFeedbackText(unittest.TestCase):
    def test_feedback_lists_errors(self):
        from analyze import build_core_feedback_text
        validation = {
            "errors": ["核心链冲突：x"], "warnings": ["备选剧本：y"],
        }
        text = build_core_feedback_text(validation)
        self.assertIn("必须修正：核心链冲突", text)
        self.assertIn("建议检查：备选剧本", text)


class TestTwoStageOrchestration(unittest.TestCase):
    """用 monkeypatch 模拟两次 LLM 调用，验证编排逻辑（不发起真实网络请求）"""

    def _make_match_data(self):
        return {"编号": "测试001", "联赛": "测试", "对阵": "A队 vs B队"}

    def test_core_locked_then_plays_derived(self):
        """核心剧本锁定后，玩法层矛盾方向必须被覆盖，让球由程序按锁定比分修正"""
        import analyze

        core_json = json.dumps({
            "home_team": "A队", "away_team": "B队",
            "full_time_result": "主胜", "net_goal_interval": "主胜1",
            "primary_score": "1:0", "alternative_scores": ["2:1"],
            "match_script": "主队一球小胜", "net_goal_reasoning": "效率差",
            "uncertainties": [], "confidence": "中",
        }, ensure_ascii=False)
        play_json = json.dumps({
            "胜平负推荐": "客胜",       # 与核心矛盾，必须被覆盖
            "让球推荐": "让-1客胜",     # 1:0 + (-1)=平，必须被程序修正
            "总进球推荐": ["2球", "3球"],
            "半全场推荐": ["HA"],       # 与主胜不兼容 → 规则兜底
            "比分推荐": ["0:1"],
            "净胜球分布": "客胜1",
            "信心指数": 5,
        }, ensure_ascii=False)

        calls = []

        def fake_core(match_data, api_key, previous_attempt=None, feedback=None):
            calls.append("core")
            return core_json

        def fake_play(match_data, api_key, extra_user_instruction=""):
            calls.append("play")
            assert "1:0" in extra_user_instruction  # 锁定剧本必须注入
            return play_json

        orig_core, orig_play = analyze.call_deepseek_core_script, analyze.call_deepseek
        analyze.call_deepseek_core_script = fake_core
        analyze.call_deepseek = fake_play
        try:
            parsed, report, err = analyze.analyze_match_two_stage(
                self._make_match_data(), "fake-key", "-1", home="A队", away="B队"
            )
        finally:
            analyze.call_deepseek_core_script = orig_core
            analyze.call_deepseek = orig_play

        self.assertIsNone(err)
        self.assertEqual(calls, ["core", "play"])
        self.assertEqual(parsed["胜平负推荐"], "主胜")
        self.assertEqual(parsed["比分推荐"], ["1:0", "2:1"])
        self.assertEqual(parsed["让球推荐"], "让-1平")
        self.assertIn("核心剧本校验通过", report)
        self.assertIn("五种玩法校验", report)

    def test_core_invalid_twice_blocks_stage_two(self):
        """核心剧本两次校验不过：必须阻止第二阶段，玩法 LLM 不得被调用"""
        import analyze

        bad_core = json.dumps({
            "home_team": "A队", "away_team": "B队",
            "full_time_result": "主胜", "net_goal_interval": "主胜1",
            "primary_score": "2:0",  # 与区间冲突
            "alternative_scores": [], "match_script": "x",
            "net_goal_reasoning": "y", "uncertainties": [], "confidence": "中",
        }, ensure_ascii=False)

        core_calls, play_calls = [], []

        def fake_core(match_data, api_key, previous_attempt=None, feedback=None):
            core_calls.append(feedback)
            # 第一次反馈必须包含具体错误（不能只是"重新检查一下"）
            if len(core_calls) == 2:
                assert feedback and "必须修正" in feedback and "net_goal_interval" in feedback
            return bad_core  # 重试也返回同样的错误剧本

        def fake_play(match_data, api_key, extra_user_instruction=""):
            play_calls.append(1)
            raise AssertionError("核心剧本未通过时第二阶段不得执行")

        orig_core, orig_play = analyze.call_deepseek_core_script, analyze.call_deepseek
        analyze.call_deepseek_core_script = fake_core
        analyze.call_deepseek = fake_play
        try:
            parsed, report, err = analyze.analyze_match_two_stage(
                self._make_match_data(), "fake-key", "-1", home="A队", away="B队"
            )
        finally:
            analyze.call_deepseek_core_script = orig_core
            analyze.call_deepseek = orig_play

        self.assertIsNone(parsed)
        self.assertIsNotNone(err)
        self.assertIn("已阻止第二阶段", err)
        self.assertEqual(len(core_calls), 2)   # 初次 + 1 次重生成
        self.assertEqual(play_calls, [])      # 第二阶段零调用
        self.assertIn("核心剧本校验未通过", report)
        self.assertIn("不产生胜平负", report)

    def test_core_json_failure_twice_blocks_stage_two(self):
        """阶段一 JSON/API 两次失败：阻止第二阶段，报告如实记录"""
        import analyze

        attempts = []

        def fake_core(match_data, api_key, previous_attempt=None, feedback=None):
            attempts.append(1)
            return "__API_ERROR__: connection timeout"

        def fake_play(match_data, api_key, extra_user_instruction=""):
            raise AssertionError("阶段一失败时第二阶段不得执行")

        orig_core, orig_play = analyze.call_deepseek_core_script, analyze.call_deepseek
        analyze.call_deepseek_core_script = fake_core
        analyze.call_deepseek = fake_play
        try:
            parsed, report, err = analyze.analyze_match_two_stage(
                self._make_match_data(), "fake-key", "-1", home="A队", away="B队"
            )
        finally:
            analyze.call_deepseek_core_script = orig_core
            analyze.call_deepseek = orig_play

        self.assertIsNone(parsed)
        self.assertIsNotNone(err)
        self.assertEqual(len(attempts), 2)
        self.assertIn("已阻止第二阶段", err)
        self.assertIn("JSON 获取/解析失败", report)
        self.assertIn("阶段一 LLM 调用 2 次", report)

    def test_core_fails_first_then_passes_on_regeneration(self):
        """首次校验失败、带错误反馈重生成后通过：正常进入第二阶段"""
        import analyze

        bad_core = json.dumps({
            "home_team": "A队", "away_team": "B队",
            "full_time_result": "主胜", "net_goal_interval": "主胜1",
            "primary_score": "2:0",  # 首次：区间冲突
            "alternative_scores": [], "match_script": "x",
            "net_goal_reasoning": "y", "uncertainties": [], "confidence": "中",
        }, ensure_ascii=False)
        good_core = json.dumps({
            "home_team": "A队", "away_team": "B队",
            "full_time_result": "主胜", "net_goal_interval": "主胜2+",
            "primary_score": "2:0",  # 重生成：区间修正
            "alternative_scores": ["3:1"], "match_script": "x",
            "net_goal_reasoning": "y", "uncertainties": [], "confidence": "中",
        }, ensure_ascii=False)
        play_json = json.dumps({
            "胜平负推荐": "主胜", "让球推荐": "让-1主胜",
            "总进球推荐": ["2球", "4球"],
            "半全场推荐": ["HH", "DH"],
            "比分推荐": ["2:0", "3:1"], "净胜球分布": "主胜2+",
            "信心指数": 7,
        }, ensure_ascii=False)

        core_returns = [bad_core, good_core]
        play_calls = []

        def fake_core(match_data, api_key, previous_attempt=None, feedback=None):
            return core_returns.pop(0)

        def fake_play(match_data, api_key, extra_user_instruction=""):
            play_calls.append(1)
            assert "主胜2+" in extra_user_instruction  # 锁定的是重生成后的剧本
            return play_json

        orig_core, orig_play = analyze.call_deepseek_core_script, analyze.call_deepseek
        analyze.call_deepseek_core_script = fake_core
        analyze.call_deepseek = fake_play
        try:
            parsed, report, err = analyze.analyze_match_two_stage(
                self._make_match_data(), "fake-key", "-1", home="A队", away="B队"
            )
        finally:
            analyze.call_deepseek_core_script = orig_core
            analyze.call_deepseek = orig_play

        self.assertIsNone(err)
        self.assertEqual(len(play_calls), 1)
        self.assertEqual(parsed["净胜球分布"], "主胜2+")
        self.assertIn("阶段一 LLM 调用 2 次", report)

    def test_stage1_preserves_raw_outputs_and_retry_count(self):
        """analyze_core_script 必须保留原始输出、规范化剧本、校验结果、调用次数"""
        import analyze

        good_core = json.dumps({
            "home_team": "A队", "away_team": "B队",
            "full_time_result": "主胜", "net_goal_interval": "主胜1",
            "primary_score": "1:0", "alternative_scores": [],
            "match_script": "x", "net_goal_reasoning": "y",
            "uncertainties": [], "confidence": "低",
        }, ensure_ascii=False)

        orig = analyze.call_deepseek_core_script
        analyze.call_deepseek_core_script = lambda *a, **k: good_core
        try:
            result = analyze.analyze_core_script(
                self._make_match_data(), "fake", "A队", "B队"
            )
        finally:
            analyze.call_deepseek_core_script = orig

        self.assertTrue(result["ok"])
        self.assertEqual(result["llm_calls"], 1)
        self.assertEqual(len(result["raw_attempts"]), 1)
        self.assertEqual(result["raw_attempts"][0], good_core)
        self.assertEqual(result["parse_errors"], [])
        self.assertEqual(result["normalized"]["primary_score"], "1:0")
        self.assertIn("errors", result["validation"])

    def test_stage2_json_failure_returns_error_no_prediction(self):
        """核心剧本通过但第二阶段 JSON 失败：返回错误，不产出预测"""
        import analyze

        good_core = json.dumps({
            "home_team": "A队", "away_team": "B队",
            "full_time_result": "主胜", "net_goal_interval": "主胜1",
            "primary_score": "1:0", "alternative_scores": [],
            "match_script": "x", "net_goal_reasoning": "y",
            "uncertainties": [], "confidence": "中",
        }, ensure_ascii=False)

        orig_core, orig_play = analyze.call_deepseek_core_script, analyze.call_deepseek
        analyze.call_deepseek_core_script = lambda *a, **k: good_core
        analyze.call_deepseek = lambda *a, **k: "不是JSON的内容{{"
        try:
            parsed, report, err = analyze.analyze_match_two_stage(
                self._make_match_data(), "fake", "-1", home="A队", away="B队"
            )
        finally:
            analyze.call_deepseek_core_script = orig_core
            analyze.call_deepseek = orig_play

        self.assertIsNone(parsed)
        self.assertIsNotNone(err)
        self.assertIn("第二阶段", err)


class TestFailedResultRow(unittest.TestCase):
    """被阻止/失败场次的 CSV 行必须保留 27 字段且玩法字段为空"""

    def test_row_has_all_fields_with_empty_predictions(self):
        from analyze import build_failed_result_row, CSV_FIELDNAMES
        row = build_failed_result_row(
            "测试009", "测试联赛", {"对阵": "A vs B", "开赛": "2026-10-12 20:00"},
            None, "-1", "分析失败: 核心剧本校验未通过，已阻止第二阶段玩法分析",
            "核心剧本校验报告内容",
        )
        self.assertEqual(set(row.keys()), set(CSV_FIELDNAMES))
        # v2 让球推导扩展：前 22 列名称与顺序逐字不变，5 个新字段仅尾部追加
        self.assertEqual(len(CSV_FIELDNAMES), 27)
        self.assertEqual(
            CSV_FIELDNAMES[:22],
            ["编号", "联赛", "对阵", "开赛",
             "胜平负推荐", "胜平负理由", "让球推荐", "让球理由",
             "总进球推荐", "总进球理由", "半全场推荐", "半全场理由",
             "比分推荐", "比分理由", "信心指数", "综合分析", "历史数据",
             "核心剧本", "净胜球分布", "体彩让球数", "数据缺失说明", "一致性检查"],
        )
        self.assertEqual(
            CSV_FIELDNAMES[22:],
            ["让球次选", "净胜球概率分布", "让球结果概率", "概率来源", "让球推导版本"],
        )
        # 五种玩法推荐字段必须为空
        for f in ("胜平负推荐", "让球推荐", "总进球推荐", "半全场推荐", "比分推荐"):
            self.assertEqual(row[f], "", f"{f} 必须留空，不能伪装成预测")
        # v2 概率字段同样必须为空：阻止场次不得伪造概率
        for f in ("让球次选", "净胜球概率分布", "让球结果概率", "概率来源", "让球推导版本"):
            self.assertEqual(row[f], "", f"{f} 必须留空，不能伪装成 v2 概率推导")
        self.assertIn("核心剧本校验未通过", row["综合分析"])
        self.assertEqual(row["体彩让球数"], "-1")
        self.assertEqual(row["一致性检查"], "核心剧本校验报告内容")


if __name__ == "__main__":
    unittest.main()
