"""半全场非法编码（如 HP）跨模块状态一致性回归测试。

统一业务规则：半全场预测只要包含任何非法编码，整条预测即无效 —— 不判中、不判错、
不计入命中率分母、不按有效投注结算、不进普通错误清单、不产归因行。

覆盖范围：
- lottery_rules：严格解析区分合法/非法，宽松解析仍只返回合法编码；
- update_half_full_tracker：混合编码/全部非法/全合法对照组的结算行为；
- review：check_hafu 返回值，以及 run_review 统计调用链中的有效样本分母与错误清单；
- build_attribution_table：judge_playbook 与 build_rows 的归因行为；
- 2026-10-10 周六003 真实历史样本只读回归。

运行：
    python3 -m unittest test_hafu_invalid_code_consistency -v
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import analyze
import review
from build_attribution_table import build_rows, judge_playbook
from lottery_rules import (finalize_hafu_picks, parse_hafu_codes,
                           parse_hafu_picks_strict)
from settle_core import (STATUS_HIT, STATUS_MISS, STATUS_INVALID_CODE,
                         STATUS_NO_DATA, STATUS_NO_PREDICTION, STATUS_UNSETTLED)
from update_half_full_tracker import CSV_HEADER as HF_HEADER
from update_half_full_tracker import build_row as build_hf_row


ROOT = os.path.dirname(os.path.abspath(__file__))
MIXED = "HH / HP / DH"
LEGAL = "HH / DH / DA"

# 赔率：HH=3.20，DH=9.00，DA=7.00；每场总预算 100 元，按合法选项数均分。
FOOTBALL_ROWS = [{
    "编号": "测试001",
    "半全场-半主胜-全主胜": "3.20",
    "半全场-半主胜-全平": "15.0",
    "半全场-半主胜-全客胜": "26.0",
    "半全场-半平-全主胜": "9.00",
    "半全场-半平-全平": "4.50",
    "半全场-半平-全客胜": "7.00",
    "半全场-半客胜-全主胜": "30.0",
    "半全场-半客胜-全平": "15.0",
    "半全场-半客胜-全客胜": "6.00",
}]


def _analysis(hafu_pred, source=None, reason=None):
    row = {
        "编号": "测试001",
        "联赛": "测试联赛",
        "对阵": "A队 vs B队",
        "信心指数": "5",
        "半全场推荐": hafu_pred,
    }
    # source=None 表示旧 CSV 缺少「半全场来源」列；空串表示列存在但值为空
    if source is not None:
        row["半全场来源"] = source
    if reason is not None:
        row["半全场理由"] = reason
    return row


def _tracker(hafu_pred, actual, source=None, reason=None):
    row = build_hf_row(
        _analysis(hafu_pred, source, reason),
        FOOTBALL_ROWS,
        {"测试001": {"半全场": actual}},
        "10-27",
    )
    return dict(zip(HF_HEADER, row))


def _review_fixture(hafu_pred, actual):
    """构造与 actual 匹配的其余四玩法预测，确保只有半全场产生错误。"""
    if actual == "AA":
        half, full, win_flag = "0:1", "0:1", "A"
        preds = {"胜平负推荐": "客胜", "让球推荐": "让-1客胜",
                 "总进球推荐": "1球", "比分推荐": "0:1"}
    elif actual == "HH":
        half, full, win_flag = "1:0", "1:0", "H"
        preds = {"胜平负推荐": "主胜", "让球推荐": "让-1平",
                 "总进球推荐": "1球", "比分推荐": "1:0"}
    elif actual == "DH":
        half, full, win_flag = "0:0", "1:0", "H"
        preds = {"胜平负推荐": "主胜", "让球推荐": "让-1平",
                 "总进球推荐": "1球", "比分推荐": "1:0"}
    else:
        raise ValueError(actual)

    analysis = _analysis(hafu_pred)
    analysis.update(preds)
    analysis["综合分析"] = "测试样本"
    tecai = {"测试001": {"league": "测试联赛", "goal_line": "-1"}}
    api_results = [{
        "match_num": "测试001",
        "home": "A队",
        "away": "B队",
        "goal_line": "-1",
        "full_score_raw": full,
        "half_score_raw": half,
        "win_flag": win_flag,
        "league": "测试联赛",
    }]
    return tecai, [analysis], api_results


def _run_review(hafu_pred, actual):
    """在临时目录运行 review.run_review，读取统计与错误清单调用参数。"""
    tecai, analysis_rows, api_results = _review_fixture(hafu_pred, actual)
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.chdir(tmp)
            # 复盘的输入有效性保护要求分析 CSV 真实存在（load_tecai_data 已 mock）
            os.makedirs("output", exist_ok=True)
            with open(os.path.join("output", "竞彩分析_2026-10-10.csv"),
                      "w", encoding="utf-8-sig") as f:
                f.write("编号,联赛,对阵,胜平负推荐\n")
            with mock.patch.object(review, "load_tecai_data",
                                   return_value=(tecai, analysis_rows)), \
                 mock.patch.object(review, "fetch_tecai_results",
                                   return_value=api_results), \
                 mock.patch.object(review, "call_deepseek_for_attribution",
                                   return_value=None) as attribution_mock:
                with contextlib.redirect_stdout(io.StringIO()):
                    review.run_review("2026-10-10")
            path = os.path.join(tmp, "output", "每日记录_2026-10-10.json")
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
        finally:
            os.chdir(old_cwd)
    return record, attribution_mock


def _record(num, pred, actual, attr="旧归因"):
    return {
        "编号": num,
        "联赛": "测试联赛",
        "对阵": "A队 vs B队",
        "信心指数": 5,
        "预判": {"半全场": pred},
        "赛果": {"半全场": actual},
        "命中": {"半全场": False},
        "错误归因": attr,
    }


class TestHafuParsingContract(unittest.TestCase):
    """A. 解析约定：严格解析区分合法/非法，宽松解析保持只返回合法编码。"""

    def test_mixed_codes_strict(self):
        codes, invalids = parse_hafu_picks_strict(MIXED)
        self.assertEqual(codes, ["HH", "DH"])
        self.assertEqual(invalids, ["HP"])

    def test_mixed_codes_loose_keeps_only_legal(self):
        self.assertEqual(parse_hafu_codes(MIXED), ["HH", "DH"])

    def test_all_invalid_strict(self):
        codes, invalids = parse_hafu_picks_strict("HP")
        self.assertEqual(codes, [])
        self.assertEqual(invalids, ["HP"])
        self.assertEqual(parse_hafu_codes("HP"), [])

    def test_all_legal_no_invalid(self):
        codes, invalids = parse_hafu_picks_strict(LEGAL)
        self.assertEqual(codes, ["HH", "DH", "DA"])
        self.assertEqual(invalids, [])


class TestTrackerInvalidCodeRule(unittest.TestCase):
    """B. tracker：含任何非法编码即整条无效，不结算。"""

    def test_mixed_actual_aa_is_invalid(self):
        """混合编码 + 实际 AA：判含非法编码，投注 0。"""
        r = _tracker(MIXED, "AA")
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertIn("HP", r["备注"])

    def test_mixed_actual_hh_is_invalid(self):
        """混合编码 + 实际 HH（命中合法项）：整条仍无效，不结算、不判命中。"""
        r = _tracker(MIXED, "HH")
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertIn("HP", r["备注"])

    def test_mixed_actual_dh_is_invalid(self):
        """混合编码 + 实际 DH（命中合法项）：整条仍无效，不结算、不判命中。"""
        r = _tracker(MIXED, "DH")
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertIn("HP", r["备注"])

    def test_all_invalid_is_invalid(self):
        """仅 HP：当前判含非法编码，投注 0。"""
        r = _tracker("HP", "AA")
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertEqual(r["备注"], "全部选项非法:HP")

    def test_all_legal_actual_aa_is_miss(self):
        """全部合法 + 实际不在推荐集合：正常未中，-100。"""
        r = _tracker(LEGAL, "AA")
        self.assertEqual(r["是否命中"], "否")
        self.assertEqual(r["记录状态"], STATUS_MISS)
        self.assertEqual(r["模拟投注额"], 100)
        self.assertEqual(r["模拟盈亏"], "-100.00")
        self.assertEqual(r["备注"], "")

    def test_all_legal_actual_dh_is_hit(self):
        """全部合法 + 实际 DH：正常命中，3 项均分。"""
        r = _tracker(LEGAL, "DH")
        self.assertEqual(r["是否命中"], "是")
        self.assertEqual(r["记录状态"], STATUS_HIT)
        self.assertEqual(r["模拟投注额"], 100)
        self.assertEqual(r["模拟盈亏"], "+200.00")
        self.assertEqual(r["备注"], "")


class TestTrackerInvalidClearedSource(unittest.TestCase):
    """B2. 推荐为空时按「半全场来源」分类（阶段十九·第三项修复）。

    来源字段只能证明 analyze.py 曾把输出清空，原始 token 未在推荐字段留存。
    """

    def test_empty_with_cleared_source_is_invalid_code(self):
        """推荐为空 + 非法输出已清空：含非法编码、投注 0，备注说明程序已清空且原文未保留。"""
        r = _tracker("", "HH", source="非法输出已清空")
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertIn("非法输出已清空", r["备注"])
        self.assertIn("已按校验规则清空", r["备注"])
        self.assertIn("未在推荐字段保留", r["备注"])

    def test_empty_with_model_not_output_stays_no_prediction(self):
        """推荐为空 + 模型未输出：维持无预测输出。"""
        r = _tracker("", "HH", source="模型未输出")
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["记录状态"], STATUS_NO_PREDICTION)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["备注"], "")

    def test_empty_with_empty_source_keeps_legacy_behavior(self):
        """推荐为空 + 来源为空串：维持无预测输出。"""
        r = _tracker("", "HH", source="")
        self.assertEqual(r["记录状态"], STATUS_NO_PREDICTION)
        self.assertEqual(r["模拟投注额"], 0)

    def test_empty_without_source_column_keeps_legacy_behavior(self):
        """旧 CSV 缺「半全场来源」列：维持无预测输出。"""
        r = _tracker("", "HH")
        self.assertEqual(r["记录状态"], STATUS_NO_PREDICTION)
        self.assertEqual(r["模拟投注额"], 0)

    def test_mixed_codes_with_corrected_source_unchanged(self):
        """混合合法/非法 + 程序修正：既有整条无效规则不变。"""
        r = _tracker(MIXED, "HH", source="程序修正")
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertIn("HP", r["备注"])

    def test_all_invalid_with_cleared_source_keeps_invalid_note(self):
        """推荐非空且全部非法 + 来源非法输出已清空：不得被来源分支改写备注。"""
        r = _tracker("HP", "AA", source="非法输出已清空")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["备注"], "全部选项非法:HP")

    def test_legal_with_model_original_source_hit_unchanged(self):
        r = _tracker(LEGAL, "DH", source="模型原始")
        self.assertEqual(r["是否命中"], "是")
        self.assertEqual(r["记录状态"], STATUS_HIT)
        self.assertEqual(r["模拟投注额"], 100)
        self.assertEqual(r["模拟盈亏"], "+200.00")
        self.assertEqual(r["备注"], "")

    def test_legal_with_model_original_source_miss_unchanged(self):
        r = _tracker(LEGAL, "AA", source="模型原始")
        self.assertEqual(r["是否命中"], "否")
        self.assertEqual(r["记录状态"], STATUS_MISS)
        self.assertEqual(r["模拟投注额"], 100)
        self.assertEqual(r["模拟盈亏"], "-100.00")
        self.assertEqual(r["备注"], "")


class TestTrackerLegacyReasonFallback(unittest.TestCase):
    """B3. 旧 CSV 缺「半全场来源」列：按程序写入的清空理由回溯分类（不回填历史）。

    仅在推荐为空、来源缺失、理由同时含「[程序校验修正]」与「已清空」时生效；
    理由只能证明曾被程序清空，原始 token 未在推荐字段留存。
    """

    # analyze.py 两个清空分支的真实理由文本（新旧两种措辞）
    OLD_CLEARED = ("[程序校验修正] 半全场推荐无法解析且胜平负缺失，无法判定，"
                   "已清空；需模型重新生成。")
    NEW_CLEARED = ("[程序校验修正] 半全场推荐不含任何合法编码且胜平负缺失（''），"
                   "无法判定，已清空；需模型重新生成。")
    FALLBACK = ("[程序校验修正] 半全场推荐缺失，按全场 主胜 规则兜底生成 "
                "HH / DH / AH（程序兜底生成，待模型确认）。")

    def test_legacy_old_reason_is_invalid_code(self):
        """旧措辞清空理由 → 含非法编码、投注 0，备注标明按理由回溯。"""
        r = _tracker("", "HH", reason=self.OLD_CLEARED)
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertIn("按历史理由回溯", r["备注"])
        self.assertIn("已按校验规则清空", r["备注"])
        self.assertIn("未在推荐字段保留", r["备注"])

    def test_legacy_new_reason_is_invalid_code(self):
        r = _tracker("", "HH", reason=self.NEW_CLEARED)
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)

    def test_legacy_empty_reason_stays_no_prediction(self):
        """空推荐 + 旧 CSV 空理由：维持无预测输出（2026-09-30 类）。"""
        r = _tracker("", "AA", reason="")
        self.assertEqual(r["记录状态"], STATUS_NO_PREDICTION)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["备注"], "")

    def test_legacy_fallback_reason_is_not_cleared(self):
        """程序兜底理由含程序标记但无「已清空」：不得误判为清空。"""
        r = _tracker("", "AA", reason=self.FALLBACK)
        self.assertEqual(r["记录状态"], STATUS_NO_PREDICTION)
        self.assertEqual(r["模拟投注额"], 0)

    def test_legacy_reason_without_program_marker_not_cleared(self):
        """理由含「已清空」但无程序标记：不扩大匹配范围，维持无预测输出。"""
        r = _tracker("", "AA", reason="模型输出异常，已清空。")
        self.assertEqual(r["记录状态"], STATUS_NO_PREDICTION)

    def test_explicit_source_wins_over_reason(self):
        """来源明确为「模型未输出」时，即使理由含清空文本也不回溯。"""
        r = _tracker("", "AA", source="模型未输出", reason=self.OLD_CLEARED)
        self.assertEqual(r["记录状态"], STATUS_NO_PREDICTION)
        self.assertEqual(r["备注"], "")

    def test_legal_prediction_with_cleared_reason_unchanged(self):
        """推荐非空且合法：理由文本不影响正常命中结算。"""
        r = _tracker(LEGAL, "DH", reason=self.OLD_CLEARED)
        self.assertEqual(r["记录状态"], STATUS_HIT)
        self.assertEqual(r["模拟投注额"], 100)
        self.assertEqual(r["模拟盈亏"], "+200.00")

    def test_all_invalid_with_cleared_reason_keeps_invalid_note(self):
        """推荐非空但全部非法：维持「全部选项非法」，不被回溯分支改写。"""
        r = _tracker("HP", "AA", reason=self.OLD_CLEARED)
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["备注"], "全部选项非法:HP")


class TestTrackerNoResultProvenance(unittest.TestCase):
    """B4. 无赛果分支：已清空输出按来源归为含非法编码，其余保持数据缺失。"""

    def test_cleared_source_without_result_is_invalid_code(self):
        """空推荐 + 非法输出已清空 + 无赛果：含非法编码、投注 0。"""
        r = _tracker("", "", source="非法输出已清空")
        self.assertEqual(r["实际半全场"], "")
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertIn("非法输出已清空", r["备注"])
        self.assertIn("已按校验规则清空", r["备注"])
        self.assertIn("未在推荐字段保留", r["备注"])

    def test_model_not_output_without_result_stays_no_data(self):
        """空推荐 + 模型未输出 + 无赛果：维持数据缺失。"""
        r = _tracker("", "", source="模型未输出")
        self.assertEqual(r["记录状态"], STATUS_NO_DATA)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["备注"], "")

    def test_missing_source_column_without_result_stays_no_data(self):
        """旧 CSV 缺来源列 + 空推荐 + 无赛果：维持数据缺失，不按理由回溯。"""
        r = _tracker("", "")
        self.assertEqual(r["记录状态"], STATUS_NO_DATA)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["备注"], "")

    def test_missing_source_column_with_legacy_cleared_reason_without_result_stays_no_data(self):
        """缺来源列但理由含清空标记 + 无赛果：仍为数据缺失，无赛果分支不做理由回溯。"""
        reason = ("[程序校验修正] 半全场推荐无法解析且胜平负缺失，无法判定，"
                  "已清空；需模型重新生成。")
        r = _tracker("", "", reason=reason)
        self.assertEqual(r["记录状态"], STATUS_NO_DATA)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["备注"], "")

    def test_nonempty_all_invalid_recommendation_without_result_stays_no_data(self):
        """推荐非空但全部非法 + 无赛果：维持数据缺失、投注 0，备注为空。"""
        r = _tracker("HP", "", source="模型未输出")
        self.assertEqual(r["实际半全场"], "")
        self.assertEqual(r["是否命中"], "")
        self.assertEqual(r["记录状态"], STATUS_NO_DATA)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")
        self.assertEqual(r["备注"], "")


class TestReviewCheckHafuInvalidCodeRule(unittest.TestCase):
    """C1. check_hafu：含任何非法编码即返回 None（无法判定）。"""

    def test_mixed_actual_aa_returns_none(self):
        self.assertEqual(review.check_hafu(MIXED, "A", "A"), (None, "AA"))

    def test_mixed_actual_hh_returns_none(self):
        self.assertEqual(review.check_hafu(MIXED, "H", "H"), (None, "HH"))

    def test_mixed_actual_dh_returns_none(self):
        self.assertEqual(review.check_hafu(MIXED, "D", "H"), (None, "DH"))

    def test_all_invalid_returns_none(self):
        self.assertEqual(review.check_hafu("HP", "A", "A"), (None, "AA"))

    def test_all_legal_hit_and_miss(self):
        self.assertEqual(review.check_hafu(LEGAL, "D", "H"), (True, "DH"))
        self.assertEqual(review.check_hafu(LEGAL, "A", "A"), (False, "AA"))


class TestReviewStatisticsChainInvalidCodeRule(unittest.TestCase):
    """C2. 通过 run_review 实际统计链，验证非法编码不进分母、不进错误清单。"""

    def _assert_case(self, hafu_pred, actual, expected_hit, expected_valid,
                     expected_attribution_called):
        record, attribution_mock = _run_review(hafu_pred, actual)
        stats = record["命中统计"]
        match = record["场次"][0]

        self.assertEqual(stats["hafu_hit"], expected_hit)
        self.assertEqual(stats["hafu_valid"], expected_valid)
        self.assertIs(match["命中"]["半全场"], bool(expected_hit) if expected_valid else None)

        if expected_attribution_called:
            attribution_mock.assert_called_once()
            errors = attribution_mock.call_args.args[1]
            self.assertIn(("hafu", hafu_pred, actual), errors)
        else:
            attribution_mock.assert_not_called()
        return record, attribution_mock

    def test_mixed_actual_aa_not_in_denominator(self):
        """混合 + AA：整条无效，不计入分母、不进错误清单、不触发归因。"""
        self._assert_case(MIXED, "AA", expected_hit=0, expected_valid=0,
                          expected_attribution_called=False)

    def test_mixed_actual_hh_not_counted(self):
        """混合 + HH（命中合法项）：整条仍无效，不计入分母。"""
        self._assert_case(MIXED, "HH", expected_hit=0, expected_valid=0,
                          expected_attribution_called=False)

    def test_mixed_actual_dh_not_counted(self):
        """混合 + DH（命中合法项）：整条仍无效，不计入分母。"""
        self._assert_case(MIXED, "DH", expected_hit=0, expected_valid=0,
                          expected_attribution_called=False)

    def test_all_invalid_not_in_denominator(self):
        """仅 HP：check_hafu 返回 None，分母 0、无错误清单。"""
        self._assert_case("HP", "AA", expected_hit=0, expected_valid=0,
                          expected_attribution_called=False)

    def test_all_legal_hit(self):
        self._assert_case(LEGAL, "DH", expected_hit=1, expected_valid=1,
                          expected_attribution_called=False)

    def test_all_legal_miss(self):
        self._assert_case(LEGAL, "AA", expected_hit=0, expected_valid=1,
                          expected_attribution_called=True)


class TestAttributionInvalidCodeRule(unittest.TestCase):
    """D. judge_playbook 与 build_rows：含非法编码整条不可归因。"""

    def test_judge_mixed_actual_aa_is_none(self):
        self.assertIsNone(judge_playbook("半全场", {"半全场": MIXED}, {"半全场": "AA"}))

    def test_judge_mixed_actual_hh_is_none(self):
        self.assertIsNone(judge_playbook("半全场", {"半全场": MIXED}, {"半全场": "HH"}))

    def test_judge_mixed_actual_dh_is_none(self):
        self.assertIsNone(judge_playbook("半全场", {"半全场": MIXED}, {"半全场": "DH"}))

    def test_judge_all_invalid_is_none(self):
        self.assertIsNone(judge_playbook("半全场", {"半全场": "HP"}, {"半全场": "AA"}))

    def test_judge_all_legal_hit_and_miss(self):
        self.assertIs(judge_playbook("半全场", {"半全场": LEGAL}, {"半全场": "DH"}), True)
        self.assertIs(judge_playbook("半全场", {"半全场": LEGAL}, {"半全场": "AA"}), False)

    def test_mixed_invalid_never_produces_attribution_row(self):
        with tempfile.TemporaryDirectory() as d:
            data = {
                "复盘日期": "2026-10-10",
                "场次": [
                    _record("测试001", MIXED, "AA"),
                    _record("测试002", MIXED, "HH"),
                    _record("测试003", MIXED, "DH"),
                    _record("测试004", "HP", "AA"),
                    _record("测试005", LEGAL, "DH"),
                    _record("测试006", LEGAL, "AA"),
                ],
            }
            with open(os.path.join(d, "每日记录_2026-10-10.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            rows = build_rows(d)

        self.assertEqual([r[1] for r in rows], ["测试006"])
        self.assertEqual(rows[0][4], "半全场")
        self.assertEqual(rows[0][6], "AA")


class TestHistoricalRegression(unittest.TestCase):
    """E. 真实历史样本只读回归（缺失时跳过，不写回任何文件）。"""

    HISTORY_PATH = os.path.join(ROOT, "output", "每日记录_2026-10-10.json")

    @unittest.skipUnless(os.path.exists(HISTORY_PATH), "历史记录文件不存在")
    def test_real_1010_003_record(self):
        with open(self.HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        match = next(m for m in data["场次"] if m["编号"] == "周六003")
        self.assertEqual(match["预判"]["半全场"], MIXED)
        self.assertEqual(match["赛果"]["半全场"], "AA")
        self.assertIs(match["命中"]["半全场"], False)


class TestUnifiedInvalidRule(unittest.TestCase):
    """统一口径：只要推荐中含非法 token，就记“含非法编码”，
    不判中、不判错、不结算、不进命中率分母、不进错误清单、不产归因行。
    """

    def test_review_mixed_aa_should_not_count_as_miss(self):
        self.assertIsNone(review.check_hafu(MIXED, "A", "A")[0])

    def test_tracker_mixed_hh_should_be_invalid_not_hit(self):
        r = _tracker(MIXED, "HH")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["模拟盈亏"], "")

    def test_attribution_mixed_hh_should_be_none(self):
        self.assertIsNone(judge_playbook("半全场", {"半全场": MIXED}, {"半全场": "HH"}))


class TestTrackerSnapshotRecompute(unittest.TestCase):
    """B5. 旧 CSV 缺来源列且推荐为空：优先按原始预测快照重算（阶段二十）。

    快照可恢复模型原始半全场输出，避免把有效输出误判为「已清空/无预测」；
    快照缺失或结构不符时才退回理由回溯口径。
    """

    SNAP_VALID = {"原始玩法输出": {"半全场推荐": ["客胜-客胜", "平-客胜", "平-平"]}}

    def _tracker_snap(self, snapshot, actual="DA", source=None, reason=None):
        row = build_hf_row(
            _analysis("", source, reason),
            FOOTBALL_ROWS,
            {"测试001": {"半全场": actual}},
            "10-27",
            snapshot,
        )
        return dict(zip(HF_HEADER, row))

    def test_snapshot_recompute_hit(self):
        """快照含中文半全场输出 → 重算为 HDA 并正常命中结算。"""
        r = self._tracker_snap(self.SNAP_VALID, actual="DA")
        self.assertEqual(r["AI半全场预测"], "AA / DA / DD")
        self.assertEqual(r["是否命中"], "是")
        self.assertEqual(r["记录状态"], STATUS_HIT)
        self.assertEqual(r["模拟投注额"], 100)
        self.assertIn("依据快照重算", r["备注"])

    def test_snapshot_recompute_miss(self):
        """快照重算后未命中：正常判否，不因历史清空理由降级。"""
        r = self._tracker_snap(self.SNAP_VALID, actual="HH")
        self.assertEqual(r["是否命中"], "否")
        self.assertEqual(r["记录状态"], STATUS_MISS)
        self.assertEqual(r["模拟投注额"], 100)
        self.assertIn("依据快照重算", r["备注"])

    def test_snapshot_valid_without_result_is_unsettled(self):
        """快照重算出有效候选但暂无赛果：未结算、预算 100，不误判为数据缺失。"""
        r = self._tracker_snap(self.SNAP_VALID, actual="")
        self.assertEqual(r["记录状态"], STATUS_UNSETTLED)
        self.assertEqual(r["模拟投注额"], 100)
        self.assertEqual(r["是否命中"], "")
        self.assertIn("依据快照重算", r["备注"])

    def test_snapshot_raw_all_invalid_is_cleared(self):
        """快照原始输出全部非法 → 仍按含非法编码处理，备注说明依据快照重算。"""
        r = self._tracker_snap({"原始玩法输出": {"半全场推荐": ["HP", "XX"]}},
                               actual="HH")
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["是否命中"], "")
        self.assertIn("依据快照重算", r["备注"])
        self.assertIn("不含任何合法编码", r["备注"])

    def test_snapshot_missing_falls_back_to_reason(self):
        """快照缺失 → 退回理由回溯；历史清空行仍归为含非法编码。"""
        cleared = ("[程序校验修正] 半全场推荐不含任何合法编码，无法判定，"
                   "已清空；需模型重新生成。")
        r = self._tracker_snap({}, actual="HH", reason=cleared)
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertIn("按历史理由回溯", r["备注"])

    def test_snapshot_without_raw_output_falls_back(self):
        """快照条目缺少原始玩法输出 → 退回原口径，不因空推荐误判为无预测。"""
        cleared = ("[程序校验修正] 半全场推荐不含任何合法编码，无法判定，"
                   "已清空；需模型重新生成。")
        r = self._tracker_snap({"编号": "测试001"}, actual="HH", reason=cleared)
        self.assertEqual(r["记录状态"], STATUS_INVALID_CODE)
        self.assertIn("按历史理由回溯", r["备注"])

    def test_existing_source_column_skips_snapshot(self):
        """来源列已存在（模型未输出）时不走快照，维持无预测输出。"""
        r = self._tracker_snap(self.SNAP_VALID, actual="DA", source="模型未输出")
        self.assertEqual(r["AI半全场预测"], "")
        self.assertEqual(r["记录状态"], STATUS_NO_PREDICTION)
        self.assertEqual(r["模拟投注额"], 0)
        self.assertEqual(r["备注"], "")

    def test_nonempty_recommendation_skips_snapshot(self):
        """推荐非空时不走快照，原始推荐优先。"""
        row = build_hf_row(
            _analysis(LEGAL),
            FOOTBALL_ROWS,
            {"测试001": {"半全场": "AA"}},
            "10-27",
            self.SNAP_VALID,
        )
        r = dict(zip(HF_HEADER, row))
        self.assertEqual(r["AI半全场预测"], LEGAL)
        self.assertEqual(r["记录状态"], STATUS_MISS)
        self.assertEqual(r["备注"], "")


class TestFinalizeHafuPicksParity(unittest.TestCase):
    """B6. analyze 与公共纯函数口径一致：推荐/来源/理由/修正记录逐字相同。"""

    def _assert_parity(self, hafu, spf, old_reason=None):
        pred = {
            "胜平负推荐": spf,
            "让球推荐": "让-1主胜",
            "总进球推荐": ["2球"],
            "半全场推荐": hafu,
            "比分推荐": ["2:0"],
            "净胜球分布": "主胜2+",
        }
        if old_reason is not None:
            pred["半全场理由"] = old_reason
        result, report = analyze.post_process_prediction(pred, "-1")
        outcome = finalize_hafu_picks(
            hafu, spf, old_reason=str(pred.get("半全场理由", "") or "").strip())
        self.assertEqual(
            hafu if outcome["推荐"] is None else outcome["推荐"],
            result["半全场推荐"])
        self.assertEqual(outcome["来源"], result["半全场来源"])
        if outcome["理由"] is None:
            # 未改写：原理由字段保持模型文本（缺失时仍缺失）
            self.assertEqual(result.get("半全场理由"), pred.get("半全场理由"))
        else:
            self.assertEqual(outcome["理由"], result.get("半全场理由"))
        for f in outcome["fixes"]:
            self.assertIn(f, report)
        for u in outcome["unjudgeable"]:
            self.assertIn(u, report)

    def test_legal_model_original(self):
        self._assert_parity(["HH", "DH", "AH"], "主胜")

    def test_all_incompatible_fallback(self):
        self._assert_parity(["HH", "DH", "AH"], "客胜")

    def test_mixed_invalid_and_incompatible(self):
        self._assert_parity(["HH", "HD", "HP"], "主胜", old_reason="模型理由")

    def test_all_invalid_with_spf(self):
        self._assert_parity(["HP", "XX"], "主胜")

    def test_all_invalid_without_spf(self):
        self._assert_parity(["HP"], "")

    def test_chinese_without_spf(self):
        self._assert_parity(["半平-全客胜"], "")

    def test_empty_with_spf(self):
        self._assert_parity([], "主胜")

    def test_empty_without_spf(self):
        self._assert_parity([], "")

    def test_empty_string_input_with_spf(self):
        self._assert_parity("", "主胜")


if __name__ == "__main__":
    unittest.main()
