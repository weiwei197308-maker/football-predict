"""300 场观察节点：统计口径与报告渲染测试。"""
import sys
import unittest

sys.path.insert(0, "/Users/mac/Desktop/zucai")

from shadow_experiment import config
from shadow_experiment.evaluate_shadow import (
    binom_two_sided_p,
    compute_node_metrics,
    mcnemar_exact_p,
    paired_diff_ci,
)
from shadow_experiment.report_writer import _node_section_lines


def _ttg_record(i, a_pick, b_pick):
    return {
        "对齐键": {"日期": "2026-01-01", "编号": f"001-{i}"},
        "A盲判": {"状态": "ok", "候选": [a_pick, "3球"], "有序": True},
        "B复核": {"状态": "ok", "候选": [b_pick, "3球"], "有序": True},
        "C正式": {"状态": "ok", "候选": [a_pick, "3球"], "有序": True},
        "D固定": {"状态": "ok", "候选": [a_pick, "3球"], "有序": True},
        "市场前三": ["3球", "2球", "4球"],
        "实际赛果": {"总进球": "2球", "比分": "1:1"},
    }


def _records(n, a_hits, b_hits):
    recs = []
    for i in range(n):
        a_pick = "2球" if i < a_hits else "3球"
        b_pick = "2球" if i < b_hits else "3球"
        recs.append(_ttg_record(i, a_pick, b_pick))
    return recs


class TestStatisticalHelpers(unittest.TestCase):
    def test_binom_known_values(self):
        self.assertAlmostEqual(binom_two_sided_p(1, 10), 0.021484375, places=6)
        self.assertAlmostEqual(binom_two_sided_p(0, 10), 0.001953125, places=6)
        self.assertAlmostEqual(binom_two_sided_p(5, 10), 1.0, places=6)
        self.assertEqual(binom_two_sided_p(0, 0), 1.0)

    def test_mcnemar_is_symmetric(self):
        self.assertAlmostEqual(mcnemar_exact_p(1, 9), mcnemar_exact_p(9, 1))
        self.assertAlmostEqual(mcnemar_exact_p(1, 9), 0.021484375, places=6)

    def test_paired_diff_ci_widens_with_smaller_n(self):
        _, lo_small, hi_small = paired_diff_ci(3, 1, 10)
        _, lo_big, hi_big = paired_diff_ci(30, 10, 100)
        self.assertGreater(hi_small - lo_small, hi_big - lo_big)


class TestComputeNodeMetrics(unittest.TestCase):
    def test_not_reached_below_threshold(self):
        node = compute_node_metrics(_records(100, 40, 35), config.PLAY_TTG)
        self.assertFalse(node["已到达"])
        self.assertEqual(node["配对数"], 100)
        self.assertIn("未达阈值", node["结论"])

    def test_reached_and_significant_lower(self):
        node = compute_node_metrics(_records(300, 100, 80), config.PLAY_TTG)
        self.assertTrue(node["已到达"])
        self.assertEqual(node["配对数"], 300)
        self.assertEqual(node["A命中"], 100)
        self.assertEqual(node["B命中"], 80)
        self.assertEqual(node["四格"], {"双方中": 80, "仅A中": 20, "仅B中": 0, "双方不中": 200})
        self.assertLess(node["McNemar_p"], config.NODE_ALPHA_CORRECTED)
        self.assertIn("显著低于", node["结论"])
        self.assertIsNone(node["方向"])

    def test_reached_not_significant(self):
        node = compute_node_metrics(_records(300, 100, 102), config.PLAY_TTG)
        self.assertTrue(node["已到达"])
        self.assertGreaterEqual(node["McNemar_p"], config.NODE_ALPHA_CORRECTED)
        self.assertIn("未达显著", node["结论"])

    def test_unsettled_records_excluded(self):
        recs = _records(300, 100, 80)
        recs.append({
            "对齐键": {"日期": "2026-01-01", "编号": "999"},
            "A盲判": {"状态": "ok", "候选": ["2球"], "有序": True},
            "B复核": {"状态": "ok", "候选": ["2球"], "有序": True},
            "实际赛果": {},
        })
        node = compute_node_metrics(recs, config.PLAY_TTG)
        self.assertEqual(node["配对数"], 300)


class TestNodeRendering(unittest.TestCase):
    def test_not_reached_renders_nothing(self):
        node = compute_node_metrics(_records(100, 40, 35), config.PLAY_TTG)
        self.assertEqual(_node_section_lines({"node_metrics": node}), [])

    def test_reached_renders_section(self):
        node = compute_node_metrics(_records(300, 100, 80), config.PLAY_TTG)
        text = "\n".join(_node_section_lines({"node_metrics": node}))
        self.assertIn("300 场观察节点", text)
        self.assertIn("McNemar", text)
        self.assertIn("n=300", text)
        self.assertIn("不构成修改生产预测逻辑的依据", text)


if __name__ == "__main__":
    unittest.main()
