"""影子实验统计模块 — 玩法维度（让球/半全场）单元测试。

全部使用内存构造记录，不依赖 output/ 真实文件，不调用任何 API。
覆盖：5 类分类、分母不变式、HAFU 9 组合完备性、分类指标正确性、
AB 配对口径、玩法 canon 函数一致性。

运行：python3 -m unittest shadow_experiment.tests.test_shadow_plays -v
"""
import os
import sys
import unittest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from shadow_experiment import config
from shadow_experiment.evaluate_shadow import (
    _canon_fn_for_play, _play_entry,
    compute_play_group_metrics, compute_play_ab_pair,
)


def make_play_record(play, a_picks, b_picks, actual, a_state="ok", b_state="ok",
                     ordered=True):
    """构造单条已结算的玩法影子记录（内存对象）。"""
    pcfg = config.PLAY_CONFIG[play]
    groups = pcfg["组"]
    d_group = [g for g in groups if g.startswith("D")][0]
    d_picks = (list(config.HAFU_BASELINE_PICKS) if play == config.PLAY_HAFU
               else [a_picks[0]] if a_picks else [])
    rec = {
        "对齐键": {"日期": "2026-10-29", "编号": "周四001",
                   "主队": "甲", "客队": "乙", "开赛": "2026-10-29 20:00"},
        "实验运行ID": "run-1",
        "A盲判": {"状态": a_state, "候选": list(a_picks), "有序": ordered},
        "B复核": {"状态": b_state, "候选": list(b_picks), "有序": ordered},
        "C正式": {"状态": "ok", "候选": list(a_picks), "有序": ordered},
        d_group: {"状态": "ok", "候选": d_picks, "有序": True},
        pcfg["市场键"]: ({"候选": list(a_picks), "赔率": {}}
                        if play == config.PLAY_RQ else list(a_picks)),
        "实际赛果": {pcfg["实际键"]: actual},
    }
    return rec


class TestClassifyOutputGeneral(unittest.TestCase):
    """5 类分类：互斥穷尽，口径与总进球一致。"""

    def setUp(self):
        self.canon = _canon_fn_for_play(config.PLAY_HAFU)

    def test_failed_state(self):
        for st in ("api_error", "skipped", "not_run"):
            cat, _ = config.classify_output_general({"状态": st, "候选": ["HH"]},
                                                    self.canon)
            self.assertEqual(cat, config.CAT_RUN_FAILED)

    def test_illegal_state(self):
        cat, _ = config.classify_output_general({"状态": "illegal", "候选": ["HH"]},
                                                self.canon)
        self.assertEqual(cat, config.CAT_ILLEGAL)

    def test_unknown_state(self):
        cat, _ = config.classify_output_general({"状态": "mystery", "候选": ["HH"]},
                                                self.canon)
        self.assertEqual(cat, config.CAT_UNKNOWN)

    def test_empty_picks(self):
        cat, _ = config.classify_output_general({"状态": "ok", "候选": []},
                                                self.canon)
        self.assertEqual(cat, config.CAT_EMPTY)

    def test_illegal_label_counts_as_empty(self):
        cat, _ = config.classify_output_general({"状态": "ok", "候选": ["XX"]},
                                                self.canon)
        self.assertEqual(cat, config.CAT_EMPTY)

    def test_valid(self):
        cat, _ = config.classify_output_general({"状态": "ok", "候选": ["HH", "DH"]},
                                                self.canon)
        self.assertEqual(cat, config.CAT_VALID)


class TestPlayCanon(unittest.TestCase):
    """玩法 canon 函数：让球剥离让球前缀，半全场仅接受 9 组合。"""

    def test_rq_canon(self):
        canon = _canon_fn_for_play(config.PLAY_RQ)
        self.assertEqual(canon("主胜"), "主胜")
        self.assertEqual(canon("让-1平"), "平")
        self.assertIsNone(canon("7+球"))  # 总进球标签不是让球方向

    def test_hafu_canon_completeness(self):
        canon = _canon_fn_for_play(config.PLAY_HAFU)
        # 9 组合完备性：全部合法，无第 10 种
        self.assertEqual(len(config.HAFU_LABELS), 9)
        for code in config.HAFU_LABELS:
            self.assertIsNotNone(canon(code))
        self.assertIsNone(canon("HDH"))
        self.assertIsNone(canon("2球"))


class TestPlayGroupMetrics(unittest.TestCase):
    """分类指标：分母不变式 + 命中率/覆盖率手工核对。"""

    def test_invariant_and_counts(self):
        canon = _canon_fn_for_play(config.PLAY_HAFU)
        labels = config.HAFU_LABELS
        # 4 条记录：2 有效（1 首选命中）、1 空、1 失败
        recs = [
            make_play_record(config.PLAY_HAFU, ["HH", "DH"], ["HH"], "HH"),
            make_play_record(config.PLAY_HAFU, ["DH", "DD"], ["DH"], "AA"),
            make_play_record(config.PLAY_HAFU, [], [], "DD"),
            make_play_record(config.PLAY_HAFU, ["HH"], ["HH"], "HH",
                             a_state="api_error", b_state="skipped"),
        ]
        entries = []
        for r in recs:
            e = _play_entry(r, config.PLAY_HAFU, "A盲判", canon)
            e["实际"] = r["实际赛果"]["半全场"]
            entries.append(e)
        m = compute_play_group_metrics(entries, "A盲判", labels)
        self.assertEqual(m["已结算场次数 N_settled"], 4)
        self.assertEqual(m["有效预测场次 N_valid"], 2)
        self.assertEqual(m["空输出场次 N_empty"], 1)
        self.assertEqual(m["运行失败场次 N_failed"], 1)
        self.assertTrue(m["分母不变式成立"])
        # 首选：rec1 HH 命中，rec2 首选 DH 未中 → 1/2
        self.assertEqual(m["首选命中率"], 1)
        self.assertEqual(m["首选命中率_分母"], 2)
        self.assertEqual(m["首选命中率_率"], 50.0)
        # 原始覆盖：rec1 含 HH 中，rec2 不含 AA → 1/2
        self.assertEqual(m["原始候选集合覆盖率_率"], 50.0)
        # 分布键完备（9 组合）
        self.assertEqual(set(m["首选标签分布"].keys()), set(labels))
        self.assertEqual(set(m["候选标签分布"].keys()), set(labels))

    def test_rq_direction_labels(self):
        canon = _canon_fn_for_play(config.PLAY_RQ)
        recs = [make_play_record(config.PLAY_RQ, ["主胜", "平"], ["主胜"], "主胜")]
        e = _play_entry(recs[0], config.PLAY_RQ, "A盲判", canon)
        e["实际"] = "主胜"
        m = compute_play_group_metrics([e], "A盲判", config.RQ_LABELS)
        self.assertEqual(set(m["首选标签分布"].keys()), {"主胜", "平", "客胜"})
        self.assertEqual(m["首选命中率_率"], 100.0)


class TestPlayABPair(unittest.TestCase):
    """AB 配对口径：首选命中四格 + 改善率 + 覆盖变化。"""

    def test_four_cells_and_improvement(self):
        # 3 对：A错B对 / A对B错 / A对B对
        recs = [
            make_play_record(config.PLAY_HAFU, ["DH"], ["AA"], "AA"),   # A错B对
            make_play_record(config.PLAY_HAFU, ["HH"], ["DD"], "HH"),   # A对B错
            make_play_record(config.PLAY_HAFU, ["HH"], ["HH"], "HH"),   # A对B对
        ]
        out = compute_play_ab_pair(recs, config.PLAY_HAFU)
        self.assertEqual(out["配对数"], 3)
        cells = out["首选命中四格"]
        self.assertEqual(cells["A错B对"], 1)
        self.assertEqual(cells["A对B错"], 1)
        self.assertEqual(cells["A对B对"], 1)
        self.assertEqual(cells["A错B错"], 0)
        # 改善率 = (1-1)/3 = 0
        self.assertEqual(out["复核首选改善率"], 0.0)

    def test_cover_changes(self):
        recs = [
            make_play_record(config.PLAY_RQ, ["主胜"], ["客胜", "主胜"], "客胜"),  # 补全
            make_play_record(config.PLAY_RQ, ["客胜", "主胜"], ["主胜"], "客胜"),  # 丧失
            make_play_record(config.PLAY_RQ, ["主胜"], ["主胜"], "主胜"),          # 不变
        ]
        out = compute_play_ab_pair(recs, config.PLAY_RQ)
        self.assertEqual(out["覆盖补全(A未→B已)"], 1)
        self.assertEqual(out["覆盖丧失(A已→B未)"], 1)
        self.assertEqual(out["覆盖不变"], 1)

    def test_unsettled_excluded(self):
        rec = make_play_record(config.PLAY_HAFU, ["HH"], ["HH"], "HH")
        rec["实际赛果"] = None
        out = compute_play_ab_pair([rec], config.PLAY_HAFU)
        self.assertEqual(out["配对数"], 0)
        self.assertIsNone(out["复核首选改善率"])

    def test_ab_classification_present(self):
        recs = [make_play_record(config.PLAY_HAFU, ["HH"], ["HH", "DD"], "HH")]
        out = compute_play_ab_pair(recs, config.PLAY_HAFU)
        self.assertEqual(len(out["classifications"]), 1)
        self.assertEqual(len(out["per_runkey_details"]), 1)
        self.assertIn("AB分类", out["per_runkey_details"][0])


if __name__ == "__main__":
    unittest.main()
