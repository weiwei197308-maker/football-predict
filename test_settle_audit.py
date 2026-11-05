"""30 天数据审计修复的回归测试。

覆盖任务单要求：
- 半全场中文表达（含 "客胜-客胜" 短式）与合法代码规范化；非法代码不得静默映射
- 实际结果包含在多选推荐中时正确判命中
- 推荐选项和赔率数量不一致时进入异常状态
- 无预测记录不被算作有效预测未中
- 未结算不参与命中率分母
- 各玩法投入、返奖、净盈亏和 ROI 公式（每场 100 总预算均分到实际选项）
- 分组和总体统计守恒
- 历史异常样例回归（24 条半全场误判中的代表、09-30 空预测、10-10 周六003 HP 码）

业务口径依据：summarize_30days.py 文档字符串 —— "每场 100 等额分摊到各预测选项"，
即每场 100 元为该场该玩法总预算，按实际解析出的合法选项数均分。
赔率口径：体彩固定奖金含本金，净盈亏 = 命中选项赔率 × 单选项投注 - 总投入。
"""
import unittest

from lottery_rules import parse_hafu_codes, parse_hafu_picks_strict
from settle_core import (
    STATUS_HIT, STATUS_MISS, STATUS_NO_PREDICTION, STATUS_UNSETTLED,
    STATUS_NO_DATA, STATUS_ODDS_MISMATCH, STATUS_ODDS_MISSING,
    STATUS_INVALID_CODE, STATUS_JUDGEMENT_MISSING,
    classify_play_record, settle_multi_pick, summarize_records,
)
from review import check_spf, check_hafu, check_ttg, check_crs
from update_sim_tracker import CSV_HEADER, build_row as build_sim_row


class TestHafuShortFormChinese(unittest.TestCase):
    """半全场短式中文（无 半/全 前缀）必须能解析；审计 24 条中 5 条属此类。"""

    def test_short_form_away_away(self):
        self.assertEqual(parse_hafu_codes("客胜-客胜"), ["AA"])

    def test_short_form_draw_away(self):
        self.assertEqual(parse_hafu_codes("平-客胜"), ["DA"])

    def test_short_form_draw_draw(self):
        self.assertEqual(parse_hafu_codes("平-平"), ["DD"])

    def test_short_form_away_draw(self):
        self.assertEqual(parse_hafu_codes("客胜-平"), ["AD"])

    def test_full_form_still_works(self):
        self.assertEqual(parse_hafu_codes("半平-全客胜"), ["DA"])

    def test_mixed_list_with_short_forms(self):
        # 09-28 周一004 真实记录
        self.assertEqual(
            parse_hafu_codes("客胜-客胜 / 平-客胜 / 平-平"), ["AA", "DA", "DD"])

    def test_single_chinese_label_not_hafu(self):
        # 单个 "客胜" 是胜平负标签，不是半全场，不得误判为合法码
        self.assertEqual(parse_hafu_codes("客胜"), [])


class TestHafuStrictInvalidCodes(unittest.TestCase):
    """非法编码（如 HP）必须暴露在 invalids 里，不得静默丢弃。"""

    def test_hp_is_invalid_not_mapped(self):
        codes, invalids = parse_hafu_picks_strict("HH / HP / DH")
        self.assertEqual(codes, ["HH", "DH"])
        self.assertEqual(invalids, ["HP"])

    def test_all_valid_no_invalids(self):
        codes, invalids = parse_hafu_picks_strict("半平-全客胜 / 半客胜-全客胜 / 半平-全平")
        self.assertEqual(codes, ["DA", "AA", "DD"])
        self.assertEqual(invalids, [])

    def test_empty_input(self):
        codes, invalids = parse_hafu_picks_strict("")
        self.assertEqual(codes, [])
        self.assertEqual(invalids, [])


class TestSettleMultiPick(unittest.TestCase):
    """结算公式：每场总预算 100，均分到实际选项数；赔率含本金。"""

    def test_three_options_hit_middle(self):
        # 命中第 2 项：100/3 × 4.60 - 100
        st, pl = settle_multi_pick(100, ["AA", "DA", "DD"], ["2.70", "4.60", "7.15"], 1)
        self.assertEqual(st, STATUS_HIT)
        self.assertAlmostEqual(pl, 53.33, places=2)

    def test_two_options_hit_first(self):
        # 2 个选项时每项 50，不是 100/3
        st, pl = settle_multi_pick(100, ["AA", "DA"], ["2.85", "4.15"], 0)
        self.assertEqual(st, STATUS_HIT)
        self.assertAlmostEqual(pl, 42.50, places=2)

    def test_one_option_hit(self):
        st, pl = settle_multi_pick(100, ["AA"], ["3.75"], 0)
        self.assertEqual(st, STATUS_HIT)
        self.assertAlmostEqual(pl, 275.0, places=2)

    def test_four_options_hit_last(self):
        st, pl = settle_multi_pick(100, ["1球", "2球", "3球", "4球"], ["5.0", "4.0", "3.5", "6.0"], 3)
        self.assertEqual(st, STATUS_HIT)
        self.assertAlmostEqual(pl, 50.0, places=2)  # 25×6.0-100

    def test_all_missed(self):
        st, pl = settle_multi_pick(100, ["AA", "DA"], ["2.85", "4.15"], -1)
        self.assertEqual(st, STATUS_MISS)
        self.assertAlmostEqual(pl, -100.0, places=2)

    def test_hit_option_odds_missing(self):
        st, pl = settle_multi_pick(100, ["AA", "DA"], ["2.85", ""], 1)
        self.assertEqual(st, STATUS_ODDS_MISSING)
        self.assertIsNone(pl)

    def test_odds_count_mismatch(self):
        # 3 个选项 2 个赔率：不得按位置错配
        st, pl = settle_multi_pick(100, ["HH", "HP", "DH"], ["3.35", "4.70"], -1)
        self.assertEqual(st, STATUS_ODDS_MISMATCH)
        self.assertIsNone(pl)

    def test_no_prediction(self):
        st, pl = settle_multi_pick(100, [], [], -1)
        self.assertEqual(st, STATUS_NO_PREDICTION)
        self.assertIsNone(pl)


class TestClassifyPlayRecord(unittest.TestCase):
    """记录状态分类：区分 无预测/未结算/数据缺失/有效未中。"""

    def test_empty_pred_with_result_is_no_prediction(self):
        # 09-30 周三001：有赛果但无预测 → 无预测输出，不是预测未中
        self.assertEqual(classify_play_record("", has_result=True), STATUS_NO_PREDICTION)

    def test_pred_without_result_is_unsettled(self):
        # 10-26 周一001：有预测无赛果 → 未结算
        self.assertEqual(classify_play_record("主胜", has_result=False), STATUS_UNSETTLED)

    def test_no_pred_no_result_is_no_data(self):
        # 10-14 周三008：都无 → 数据/流程缺失
        self.assertEqual(classify_play_record("", has_result=False), STATUS_NO_DATA)

    def test_normal_record(self):
        self.assertEqual(classify_play_record("主胜", has_result=True), STATUS_MISS)


class TestReviewTriState(unittest.TestCase):
    """review.py 判定函数：空预测必须返回 None（非 False），防止空预测被记为未中。"""

    def test_check_spf_empty_pred(self):
        hit, _ = check_spf("", "主胜")
        self.assertIsNone(hit)

    def test_check_hafu_empty_pred(self):
        hit, _ = check_hafu("", "D", "D")
        self.assertIsNone(hit)

    def test_check_ttg_empty_pred(self):
        hit, _ = check_ttg("", 3)
        self.assertIsNone(hit)

    def test_check_crs_empty_pred(self):
        hit, _ = check_crs("", 2, 1)
        self.assertIsNone(hit)

    def test_check_spf_real_miss_still_false(self):
        hit, _ = check_spf("主胜", "客胜")
        self.assertIs(hit, False)

    def test_check_hafu_chinese_hit(self):
        # 09-28 周一003 真实样例：中文描述 + 实际 AA ∈ {DA,AA,DD} → 命中
        hit, _ = check_hafu("半平-全客胜 / 半客胜-全客胜 / 半平-全平", "A", "A")
        self.assertIs(hit, True)

    def test_check_hafu_short_form_hit(self):
        # 09-28 周一004 真实样例：短式中文
        hit, _ = check_hafu("客胜-客胜 / 平-客胜 / 平-平", "A", "A")
        self.assertIs(hit, True)


class TestHistoricalRegression(unittest.TestCase):
    """历史异常样例逐条回归（数据来自异常清单）。"""

    CASES_HIT = [
        ("半平-全客胜 / 半客胜-全客胜 / 半平-全平", "AA"),
        ("客胜-客胜 / 平-客胜 / 平-平", "AA"),
        ("半客胜-全客胜 / 半平-全客胜 / 半客胜-全平", "AA"),
        ("客胜-客胜 / 平-客胜 / 客胜-平", "DA"),
        ("半平-全主胜 / 半平-全平 / 半客胜-全客胜", "DD"),
        ("半平-全平 / 半平-全主胜 / 半主胜-全主胜", "HH"),
        ("半客胜-全客胜 / 半主胜-全主胜 / 半平-全平", "HH"),
    ]

    def test_24_misjudged_samples_now_hit(self):
        for pred, actual in self.CASES_HIT:
            hit, _ = check_hafu(pred, actual[0], actual[1])
            self.assertIs(hit, True, f"{pred} 应命中 {actual}")


class TestSummarizeConservation(unittest.TestCase):
    """分组统计与总体守恒；无预测/未结算不进入命中率分母。"""

    def test_group_sum_equals_total(self):
        records = [
            {"联赛": "A", "状态": STATUS_HIT, "盈亏": 50.0},
            {"联赛": "A", "状态": STATUS_MISS, "盈亏": -100.0},
            {"联赛": "B", "状态": STATUS_NO_PREDICTION, "盈亏": None},
            {"联赛": "B", "状态": STATUS_UNSETTLED, "盈亏": None},
        ]
        agg = summarize_records(records)
        by_league = agg["by_status"]
        total_valid = by_league[STATUS_HIT] + by_league[STATUS_MISS]
        self.assertEqual(total_valid, 2)
        self.assertEqual(by_league[STATUS_NO_PREDICTION], 1)
        self.assertEqual(by_league[STATUS_UNSETTLED], 1)
        # 命中率为 1/2，不受无预测/未结算稀释
        self.assertAlmostEqual(agg["hit_rate"], 0.5)
        self.assertAlmostEqual(agg["total_pl"], -50.0)


class TestSimTrackerStatusAndFormat(unittest.TestCase):
    """让球模拟盘：状态常量化、判定缺失独立记账、盈亏格式与多选表一致。"""

    NUM = "周日001"

    def _analysis(self, handicap="让+1主胜", spf="主胜"):
        return {
            "编号": self.NUM, "联赛": "英超", "对阵": "A vs B [1]",
            "信心指数": "7", "胜平负推荐": spf, "让球推荐": handicap,
        }

    def _football(self, handicap_odds="2.00"):
        return [{
            "编号": self.NUM, "让球数": "+1",
            "让球-主胜": handicap_odds, "让球-平": "3.30", "让球-客胜": "4.10",
            "胜平负-主胜": "1.80", "胜平负-平": "3.40", "胜平负-客胜": "4.20",
        }]

    def _review(self, hit_rq=True, hit_spf=True, score="2:0"):
        return {self.NUM: {
            "全场": score, "胜平负": "主胜", "hit_rq": hit_rq, "hit_spf": hit_spf,
        }}

    def _build(self, handicap="让+1主胜", handicap_odds="2.00", **review_kw):
        row = build_sim_row(
            self._analysis(handicap), self._football(handicap_odds),
            self._review(**review_kw), "10-27",
        )
        return dict(zip(CSV_HEADER, row))

    def test_hit_pl_uses_two_decimals(self):
        r = self._build()
        self.assertEqual(r["记录状态"], STATUS_HIT)
        self.assertEqual(r["模拟投注额"], 100)
        self.assertEqual(r["模拟盈亏"], "+100.00")

    def test_miss_pl_uses_two_decimals(self):
        r = self._build(hit_rq=False)
        self.assertEqual(r["记录状态"], STATUS_MISS)
        self.assertEqual(r["模拟盈亏"], "-100.00")

    def test_missing_odds_uses_constant(self):
        r = self._build(handicap_odds="")
        self.assertEqual(r["记录状态"], STATUS_ODDS_MISSING)
        self.assertEqual(r["记录状态"], "赔率缺失")
        self.assertEqual(r["模拟盈亏"], "")
        self.assertEqual(r["模拟投注额"], 100)

    def test_non_numeric_odds_uses_constant(self):
        r = self._build(handicap_odds="abc")
        self.assertEqual(r["记录状态"], STATUS_ODDS_MISSING)
        self.assertEqual(r["模拟盈亏"], "")

    def test_judgement_missing_is_not_no_prediction(self):
        r = self._build(hit_rq=None)
        self.assertEqual(r["记录状态"], STATUS_JUDGEMENT_MISSING)
        self.assertNotEqual(r["记录状态"], STATUS_NO_PREDICTION)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertEqual(r["让球命中"], "")
        self.assertIn("JSON命中判定缺失", r["备注"])

    def test_no_prediction_status_unchanged(self):
        r = self._build(handicap="")
        self.assertEqual(r["记录状态"], STATUS_NO_PREDICTION)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")

    def test_unsettled_status_unchanged(self):
        r = self._build(score="")
        self.assertEqual(r["记录状态"], STATUS_UNSETTLED)
        self.assertEqual(r["模拟投注额"], 100)


if __name__ == "__main__":
    unittest.main()
