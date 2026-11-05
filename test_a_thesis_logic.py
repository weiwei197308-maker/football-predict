"""「中国体育彩票足球分析 A 思路」新增逻辑的自动化测试。

覆盖本轮实际写入运行路径的 A 思路关键逻辑：
- 赔率隐含概率（市场倾向，仅辅助）；
- 让球临界净胜球 = -让球数；
- 净胜球区间 → 让球后可能方向的推导；
- analyze_handicap_line 的临界线 / 走盘风险 / 胜平负与让球方向分离判定；
- post_process_prediction 将临界线分析并入正式报告与让球理由。

运行：python3 -m unittest test_a_thesis_logic -v
"""
import sys
import types
import unittest

from lottery_rules import (
    implied_probabilities,
    rank_outcomes_by_implied_prob,
    handicap_critical_goal_diff,
    interval_net_goal_range,
    interval_handicap_directions,
    analyze_handicap_line,
)

# analyze.py 依赖本项目的 football_history；测试环境可不可用，缺失时用桩替代。
if "football_history" not in sys.modules:
    _stub = types.ModuleType("football_history")
    _stub.build_history_for_match = lambda home, away: {"available": False}
    sys.modules["football_history"] = _stub

import analyze  # noqa: E402


class TestImpliedProbabilities(unittest.TestCase):
    def test_probabilities_sum_to_one(self):
        probs = implied_probabilities({"主胜": 2.0, "平": 4.0, "客胜": 4.0})
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=6)

    def test_ranking_lowest_odds_first(self):
        ranked = rank_outcomes_by_implied_prob({"主胜": 2.0, "平": 3.0, "客胜": 5.0})
        self.assertEqual(ranked[0][0], "主胜")
        self.assertEqual(ranked[-1][0], "客胜")

    def test_invalid_and_non_positive_skipped(self):
        self.assertEqual(implied_probabilities({"主胜": "abc", "平": None, "客胜": 0, "x": "-1"}), {})

    def test_non_dict_returns_empty(self):
        self.assertEqual(implied_probabilities(None), {})
        self.assertEqual(rank_outcomes_by_implied_prob([1, 2]), [])


class TestHandicapCriticalGoalDiff(unittest.TestCase):
    def test_critical_is_negated_handicap(self):
        self.assertEqual(handicap_critical_goal_diff("-1"), 1)
        self.assertEqual(handicap_critical_goal_diff("+1"), -1)
        self.assertEqual(handicap_critical_goal_diff("-2"), 2)

    def test_invalid_returns_none(self):
        self.assertIsNone(handicap_critical_goal_diff("让1"))
        self.assertIsNone(handicap_critical_goal_diff(None))


class TestIntervalHandicapDirections(unittest.TestCase):
    def test_home_win1_minus1_is_draw(self):
        self.assertEqual(interval_handicap_directions("主胜1", "-1"), ["平"])

    def test_home_win2plus_minus1_is_home_win(self):
        self.assertEqual(interval_handicap_directions("主胜2+", "-1"), ["主胜"])

    def test_away_win2plus_plus1_is_away_win(self):
        self.assertEqual(interval_handicap_directions("客胜2+", "+1"), ["客胜"])

    def test_draw_plus1_is_home_win(self):
        self.assertEqual(interval_handicap_directions("平", "+1"), ["主胜"])

    def test_big_handicap_spans_multiple_directions(self):
        # 主胜2+（净胜 2..8）让 3 球：净胜 2→客胜、3→平、≥4→主胜
        self.assertEqual(set(interval_handicap_directions("主胜2+", "-3")), {"客胜", "平", "主胜"})

    def test_range_mapping_and_unknown(self):
        self.assertEqual(interval_net_goal_range("主胜1"), (1, 1))
        self.assertEqual(interval_net_goal_range("客胜1"), (-1, -1))
        self.assertIsNone(interval_net_goal_range("主队大胜"))

    def test_invalid_handicap_returns_empty(self):
        self.assertEqual(interval_handicap_directions("主胜1", ""), [])


class TestAnalyzeHandicapLine(unittest.TestCase):
    def test_small_win_vs_big_win_are_distinguished(self):
        small = analyze_handicap_line("1:0", "主胜1", "-1", spf="主胜")
        self.assertEqual(small["derived"], "让-1平")
        self.assertEqual(small["critical_goal_diff"], 1)
        self.assertEqual(small["spf_vs_handicap"], "分离")

        big = analyze_handicap_line("2:0", "主胜2+", "-1", spf="主胜")
        self.assertEqual(big["derived"], "让-1主胜")
        self.assertEqual(big["spf_vs_handicap"], "一致")
        self.assertFalse(big["walk_line_risk"])

    def test_different_handicaps_change_result_same_score(self):
        self.assertEqual(analyze_handicap_line("2:0", "主胜2+", "-1")["derived"], "让-1主胜")
        self.assertEqual(analyze_handicap_line("2:0", "主胜2+", "-2")["derived"], "让-2平")
        self.assertEqual(analyze_handicap_line("2:0", "主胜2+", "-3")["derived"], "让-3客胜")

    def test_walk_line_risk_when_interval_brackets_critical(self):
        # 主胜2+ 横跨临界净胜球 2（净胜 2→让平、净胜≥3→让胜）
        res = analyze_handicap_line("3:0", "主胜2+", "-2", spf="主胜")
        self.assertTrue(res["walk_line_risk"])
        self.assertIn("走盘", "\n".join(res["notes"]))

    def test_determined_when_interval_equals_critical(self):
        res = analyze_handicap_line("1:0", "主胜1", "-1", spf="主胜")
        self.assertFalse(res["walk_line_risk"])
        self.assertIn("恰落在让球临界净胜球", "\n".join(res["notes"]))

    def test_unjudgeable_when_inputs_missing(self):
        res = analyze_handicap_line("", "主胜1", "")
        self.assertEqual(res["derived"], "让球无法判定")
        self.assertTrue(res["unjudgeable"])

    def test_odds_market_note(self):
        res = analyze_handicap_line(
            "2:0", "主胜2+", "-1", spf="主胜",
            handicap_odds={"主胜": 1.5, "平": 3.0, "客胜": 5.0},
        )
        self.assertEqual(res["market"][0][0], "主胜")
        self.assertIn("让球赔率隐含概率排序", "\n".join(res["notes"]))


def _base_pred(**updates):
    pred = {
        "胜平负推荐": "主胜",
        "让球推荐": "让-1主胜",
        "总进球推荐": ["2球", "3球", "4球"],
        "半全场推荐": ["HH", "DH", "AH"],
        "比分推荐": ["2:0", "2:1", "1:0"],
        "净胜球分布": "主胜2+",
    }
    pred.update(updates)
    return pred


class TestPostProcessIntegration(unittest.TestCase):
    """A 思路逻辑确实参与正式报告生成（post_process_prediction）。"""

    def test_clean_case_report_has_critical_line_block(self):
        result, report = analyze.post_process_prediction(_base_pred(), "-1")
        self.assertIn("状态: 通过", report)
        self.assertIn("让球临界线分析（A 思路）", report)
        self.assertEqual(result["让球推荐"], "让-1主胜")

    def test_clean_case_with_odds_reports_market_note(self):
        match_data = {
            "让球赔率": {"让球数": "-1", "主胜": 1.6, "平": 3.2, "客胜": 4.8},
            "胜平负赔率": {"主胜": 1.9, "平": 3.4, "客胜": 4.0},
        }
        _, report = analyze.post_process_prediction(_base_pred(), "-1", match_data)
        self.assertIn("让球赔率隐含概率排序", report)
        self.assertIn("主胜(", report)
        # "让球数"是让球赔率表里的数值字段，不得被当作赔率计入市场倾向
        self.assertNotIn("让球数(", report)

    def test_spf_and_handicap_separation_is_explained(self):
        pred = _base_pred(
            让球推荐="让-1主胜", 比分推荐=["1:0", "2:0", "1:1"], 净胜球分布="主胜1",
        )
        result, report = analyze.post_process_prediction(pred, "-1")
        self.assertEqual(result["让球推荐"], "让-1平")
        self.assertIn("合理分离", report)
        self.assertIn("让球临界净胜球", result["让球理由"])

    def test_correction_reason_keeps_required_fragments(self):
        pred = _base_pred(让球推荐="让-1主胜", 比分推荐=["1:0", "2:0", "1:1"], 净胜球分布="主胜1")
        result, _ = analyze.post_process_prediction(pred, "-1")
        self.assertIn("1:0", result["让球理由"])
        self.assertIn("让-1平", result["让球理由"])
        self.assertIn("[程序校验修正]", result["让球理由"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
