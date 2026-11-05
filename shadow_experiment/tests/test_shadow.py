"""影子实验统计模块 — 核心单元测试。

不造 fixtures，直接用真实的 2026-10-29 影子文件。
覆盖：区间距离、方向判断、AB 分类、003 精确验证、开放区间、分母穷尽、可重复性。
"""
import hashlib
import json
import os
import sys
import unittest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from shadow_experiment import config
from shadow_experiment.evaluate_shadow import (
    run_evaluate, _entries_for_runkey, compute_ab_pair_analysis,
)
from shadow_experiment.validate_shadow import load_all_shadow_files

SHADOW_20261029 = os.path.join(_PROJECT_ROOT, "output", "影子实验_总进球_2026-10-29.json")


class TestIntervalDistance(unittest.TestCase):
    """第三轮 §1.2 区间距离定义 — 4 个用户要求的开放区间示例。"""

    def test_pred_5_actual_7plus(self):
        """预测5球、实际7+球：最短距离为2，精确误差未知。"""
        pred = config.label_to_interval("5球")
        actual = config.label_to_interval("7+球")
        self.assertEqual(config.interval_distance(pred, actual), 2.0)
        self.assertTrue(actual[2])  # is_open

    def test_pred_7plus_actual_6(self):
        """预测7+球、实际6球：区间距离为1。"""
        pred = config.label_to_interval("7+球")
        actual = config.label_to_interval("6球")
        self.assertEqual(config.interval_distance(pred, actual), 1.0)

    def test_pred_7plus_actual_8(self):
        """预测7+球、实际8球：区间距离为0（相交），精确误差空。"""
        pred = config.label_to_interval("7+球")
        actual = config.label_to_interval("8球")
        self.assertEqual(config.interval_distance(pred, actual), 0.0)

    def test_pred_7plus_actual_7plus(self):
        """预测7+球、实际7+球：区间距离为0。"""
        pred = config.label_to_interval("7+球")
        actual = config.label_to_interval("7+球")
        self.assertEqual(config.interval_distance(pred, actual), 0.0)


class TestDirection(unittest.TestCase):
    """第三轮 §4.3 方向判断 — 003 和 007 案例。"""

    def test_003_ideal_plus1(self):
        """003: A首选 2球 vs 实际 6球 → ideal = +1（实际整体在 A 上方）。"""
        a_int = config.label_to_interval("2球")
        actual_int = config.label_to_interval("6球")
        self.assertEqual(config.compute_ideal_direction(a_int, actual_int), +1)

    def test_003_b_move_plus1(self):
        """003: B首选 3球 vs A首选 2球 → B_move = +1。"""
        self.assertEqual(config.compute_b_move_direction(2.0, 3.0), +1)

    def test_003_direction_correct(self):
        """003: ideal=+1, B_move=+1 → 方向正确移动。"""
        a_int = config.label_to_interval("2球")
        actual_int = config.label_to_interval("6球")
        b_num = 3.0
        ideal = config.compute_ideal_direction(a_int, actual_int)
        b_move = config.compute_b_move_direction(2.0, b_num)
        self.assertEqual(ideal, +1)
        self.assertEqual(b_move, +1)
        self.assertNotEqual(ideal, 0)
        self.assertNotEqual(b_move, 0)
        self.assertEqual(ideal, b_move)

    def test_007_already_covered(self):
        """007: A首选 2球 已覆盖 实际 2球 → ideal = 0（方向不可判定）。"""
        a_int = config.label_to_interval("2球")
        actual_int = config.label_to_interval("2球")
        self.assertEqual(config.compute_ideal_direction(a_int, actual_int), 0)

    def test_a_already_covered_open_interval(self):
        """A首选 7+球、实际 8球 → ideal = 0（A 首选已覆盖实际）。"""
        a_int = config.label_to_interval("7+球")
        actual_int = config.label_to_interval("8球")
        self.assertEqual(config.compute_ideal_direction(a_int, actual_int), 0)


class TestABClassification(unittest.TestCase):
    """第三轮 §5 AB-0/1/2/3 四分类互斥穷尽。"""

    def test_ab0_no_change(self):
        cls_id, cls_name = config.classify_ab_change(["2球", "3球"], ["2球", "3球"])
        self.assertEqual(cls_id, "AB-0")
        self.assertEqual(cls_name, "无变更")

    def test_ab1_reorder_only(self):
        cls_id, _ = config.classify_ab_change(["1球", "2球", "3球"], ["3球", "2球", "1球"])
        self.assertEqual(cls_id, "AB-1")

    def test_ab2_set_change_preserved_top(self):
        cls_id, _ = config.classify_ab_change(["2球", "3球", "1球"], ["2球", "3球", "4球"])
        self.assertEqual(cls_id, "AB-2")

    def test_ab3_substantive(self):
        cls_id, _ = config.classify_ab_change(["2球", "1球", "3球"], ["3球", "2球", "4球"])
        self.assertEqual(cls_id, "AB-3")

    def test_all_four_exhaustive(self):
        """4 类覆盖全部 2×2 组合。"""
        seen = set()
        for a, b in [
            (["2"], ["2"]),           # AB-0
            (["1", "2"], ["2", "1"]), # AB-1
            (["2", "3"], ["2", "4"]), # AB-2
            (["1", "2"], ["3", "4"]), # AB-3
        ]:
            cid, _ = config.classify_ab_change(a, b)
            seen.add(cid)
        self.assertEqual(seen, {"AB-0", "AB-1", "AB-2", "AB-3"})


class Test003ExactCase(unittest.TestCase):
    """必须通过：003 案例 E1-E10 完整验证。"""

    @classmethod
    def setUpClass(cls):
        cls.loaded = load_all_shadow_files(output_dir="output")
        cls.record_003 = None
        for rec in cls.loaded["records"]:
            align = rec.get("对齐键") or {}
            if align.get("编号") == "周四003":
                cls.record_003 = rec
                break
        assert cls.record_003 is not None, "003 案例未找到"

    def test_e1_a_exact_error_eq_4(self):
        """E1: A首选精确误差 = |2−6| = 4。"""
        entries = _entries_for_runkey(self.record_003)
        ae = entries["A盲判"]
        self.assertEqual(ae["首选_num"], 2.0)
        self.assertEqual(ae["实际_num"], 6.0)
        self.assertFalse(ae["实际_is_open"])
        # 精确误差
        err = abs(ae["首选_num"] - ae["实际_num"])
        self.assertEqual(err, 4.0)

    def test_e2_b_exact_error_eq_3(self):
        """E2: B首选精确误差 = |3−6| = 3。"""
        entries = _entries_for_runkey(self.record_003)
        be = entries["B复核"]
        self.assertEqual(be["首选_num"], 3.0)
        err = abs(be["首选_num"] - be["实际_num"])
        self.assertEqual(err, 3.0)

    def test_e3_exact_error_improve_eq_plus1(self):
        """E3: 首选精确误差改善 = 4 − 3 = +1。"""
        entries = _entries_for_runkey(self.record_003)
        ae, be = entries["A盲判"], entries["B复核"]
        improve = abs(ae["首选_num"] - ae["实际_num"]) - abs(be["首选_num"] - be["实际_num"])
        self.assertEqual(improve, 1.0)

    def test_e4_a_min_set_dist_eq_3(self):
        """E4: A集合最短距离 = min(4,5,3) = 3。"""
        entries = _entries_for_runkey(self.record_003)
        ae = entries["A盲判"]
        actual = ae["实际_interval"]
        ds = [config.interval_distance(ci, actual) for ci in ae["候选_intervals"]]
        self.assertEqual(min(ds), 3.0)

    def test_e5_b_min_set_dist_eq_2(self):
        """E5: B集合最短距离 = min(3,4,2) = 2。"""
        entries = _entries_for_runkey(self.record_003)
        be = entries["B复核"]
        actual = be["实际_interval"]
        ds = [config.interval_distance(ci, actual) for ci in be["候选_intervals"]]
        self.assertEqual(min(ds), 2.0)

    def test_e6_set_dist_improve_eq_plus1(self):
        """E6: 集合距离改善 = 3 − 2 = +1。"""
        entries = _entries_for_runkey(self.record_003)
        ae, be = entries["A盲判"], entries["B复核"]
        actual = ae["实际_interval"]
        a_min = min(config.interval_distance(ci, actual) for ci in ae["候选_intervals"])
        b_min = min(config.interval_distance(ci, actual) for ci in be["候选_intervals"])
        self.assertEqual(a_min - b_min, 1.0)

    def test_e7_a_not_cover_actual(self):
        """E7: A候选 {2,1,3} 不覆盖实际 6。"""
        entries = _entries_for_runkey(self.record_003)
        ae = entries["A盲判"]
        actual = ae["实际_interval"]
        covers = any(
            config.interval_distance(ci, actual) == 0 for ci in ae["候选_intervals"])
        self.assertFalse(covers)

    def test_e8_b_not_cover_actual_despite_improve(self):
        """E8: B候选 {3,2,4} 也不覆盖实际 6（首选误差改善但集合仍未覆盖）。"""
        entries = _entries_for_runkey(self.record_003)
        be = entries["B复核"]
        actual = be["实际_interval"]
        covers = any(
            config.interval_distance(ci, actual) == 0 for ci in be["候选_intervals"])
        self.assertFalse(covers)

    def test_e9_direction_correct_move(self):
        """E9: ideal=+1, B_move=+1 → 方向正确移动。"""
        entries = _entries_for_runkey(self.record_003)
        ae, be = entries["A盲判"], entries["B复核"]
        ideal = config.compute_ideal_direction(ae["首选_interval"], ae["实际_interval"])
        b_move = config.compute_b_move_direction(ae["首选_num"], be["首选_num"])
        self.assertEqual(ideal, +1)
        self.assertEqual(b_move, +1)
        self.assertEqual(ideal, b_move)

    def test_e10_ab3_classification(self):
        """E10: 分类 AB-3 实质性变更。"""
        entries = _entries_for_runkey(self.record_003)
        cls_id, _ = config.classify_ab_change(entries["A盲判"]["候选"], entries["B复核"]["候选"])
        self.assertEqual(cls_id, "AB-3")


class TestDenominatorInvariant(unittest.TestCase):
    """第三轮 §3.2 分母不变式：N_settled = N_valid + N_empty + N_invalid + N_failed + N_unknown。"""

    def test_real_20261029_invariant(self):
        """真实数据：每组不变式成立。"""
        result = run_evaluate(output_dir="output")
        for g, m in result["group_metrics"].items():
            invariant = m["已结算场次数 N_settled"]
            total_sum = (m["有效预测场次 N_valid"] + m["空输出场次 N_empty"]
                        + m["非法输出场次 N_invalid"] + m["运行失败场次 N_failed"]
                        + m.get("未知状态场次 N_unknown", 0))
            self.assertEqual(invariant, total_sum, f"{g} 不变式失败: {invariant} != {total_sum}")


class TestReplayability(unittest.TestCase):
    """S4 可重复性：同一份输入跑两次，核心输出 MD5 完全一致。"""

    def test_two_runs_produce_identical_ab_details(self):
        run1 = run_evaluate(output_dir="output")
        run2 = run_evaluate(output_dir="output")

        d1 = json.dumps(run1["ab_pair"]["per_runkey_details"], sort_keys=True, ensure_ascii=False)
        d2 = json.dumps(run2["ab_pair"]["per_runkey_details"], sort_keys=True, ensure_ascii=False)
        self.assertEqual(hashlib.sha256(d1.encode()).hexdigest(),
                         hashlib.sha256(d2.encode()).hexdigest())

    def test_two_runs_produce_identical_group_metrics(self):
        run1 = run_evaluate(output_dir="output")
        run2 = run_evaluate(output_dir="output")

        m1 = json.dumps(run1["group_metrics"], sort_keys=True, ensure_ascii=False)
        m2 = json.dumps(run2["group_metrics"], sort_keys=True, ensure_ascii=False)
        self.assertEqual(hashlib.sha256(m1.encode()).hexdigest(),
                         hashlib.sha256(m2.encode()).hexdigest())


class TestDSMarketTable(unittest.TestCase):
    """第三轮 §6 D 组 × 市场四格表。"""

    def test_real_20261029_numbers(self):
        """真实数据：a=2, b=0, c=0, d=1, N=3（仅 2026-10-29 文件，后续日期文件不影响）。"""
        from shadow_experiment.evaluate_shadow import compute_d_market_table
        loaded = load_all_shadow_files(output_dir="output")
        records = [r for r in loaded["records"]
                   if (r.get("对齐键") or {}).get("日期") == "2026-10-29"]
        dm = compute_d_market_table(records)
        self.assertEqual(dm["a_D和市场都覆盖"], 2)
        self.assertEqual(dm["b_D覆盖市场未覆盖"], 0)
        self.assertEqual(dm["c_D未覆盖市场覆盖"], 0)
        self.assertEqual(dm["d_D和市场都未覆盖"], 1)
        self.assertEqual(dm["N"], 3)
        self.assertEqual(dm["D相对市场覆盖率差"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
