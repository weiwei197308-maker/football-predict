"""半全场来源标记（CSV「半全场来源」）与 review 非法清空状态专项测试。

覆盖：
1. 合法预测原样保留 → 模型原始；
2. 空预测 + 已知胜平负 → 程序兜底，且兜底候选按有效预测进入 review 分母；
3. HP / XX 全部非法 → 非法输出已清空，review 不计命中/未中，保留非法状态；
4. HH / HP 混合 → 保留 HH，来源为程序修正；
5. 空预测 + 未知胜平负 → 模型未输出；
6. 旧 CSV 缺少「半全场来源」列时 review 仍可读取；
7. 新增列后 CSV 每行字段数与表头一致；
8. 每日记录保留非法清空状态；
9. 来源标记不覆盖原始预测快照中的模型原始输出。

运行：python3 -m unittest test_hafu_source_marker -v
"""
import contextlib
import copy
import csv
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import analyze
import review


def _pred(**updates):
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


class TestHafuSourceClassification(unittest.TestCase):
    """一～五：依据"修正前原始输出 vs 最终候选"判定来源。"""

    def test_legal_prediction_kept_is_model_original(self):
        result, _ = analyze.post_process_prediction(_pred(), "-1")
        self.assertEqual(result["半全场推荐"], ["HH", "DH", "AH"])
        self.assertEqual(result["半全场来源"], "模型原始")

    def test_empty_prediction_with_spf_uses_fallback(self):
        result, _ = analyze.post_process_prediction(_pred(半全场推荐=[]), "-1")
        self.assertEqual(result["半全场推荐"], ["HH", "DH", "AH"])
        self.assertEqual(result["半全场来源"], "程序兜底")
        self.assertIn("按全场 主胜 规则兜底生成", result.get("半全场理由", ""))
        self.assertIn("程序兜底生成", result.get("半全场理由", ""))

    def test_all_invalid_output_is_cleared(self):
        result, _ = analyze.post_process_prediction(
            _pred(半全场推荐=["HP", "XX"]), "-1")
        self.assertEqual(result["半全场推荐"], [])
        self.assertEqual(result["半全场来源"], "非法输出已清空")
        self.assertIn("不含任何合法编码", result.get("半全场理由", ""))
        self.assertIn("已清空", result.get("半全场理由", ""))
        # 不得误写成模型未输出或兜底
        self.assertNotIn("兜底", result.get("半全场理由", ""))
        self.assertNotIn("缺失", result.get("半全场理由", ""))

    def test_mixed_invalid_output_is_corrected(self):
        result, _ = analyze.post_process_prediction(
            _pred(半全场推荐=["HH", "HP"]), "-1")
        self.assertEqual(result["半全场推荐"], ["HH"])
        self.assertEqual(result["半全场来源"], "程序修正")
        # 理由必须明确提及非法编码 HP 的清理，不能只说"移除不兼容 []"
        self.assertIn("过滤非法半全场编码", result.get("半全场理由", ""))
        self.assertIn("HP", result.get("半全场理由", ""))
        self.assertNotIn("移除与全场", result.get("半全场理由", ""))

    def test_empty_prediction_without_spf_is_model_not_output(self):
        result, _ = analyze.post_process_prediction(
            _pred(半全场推荐=[], 胜平负推荐=""), "-1")
        self.assertEqual(result["半全场推荐"], [])
        self.assertEqual(result["半全场来源"], "模型未输出")

    def test_all_incompatible_candidates_marked_as_fallback(self):
        """合法但与全场不兼容 → 候选全部由程序生成，标记为程序兜底。"""
        result, _ = analyze.post_process_prediction(
            _pred(半全场推荐=["HH", "DH", "AH"], 胜平负推荐="客胜"), "-1")
        self.assertEqual(result["半全场推荐"], ["HA", "DA", "AA"])
        self.assertEqual(result["半全场来源"], "程序兜底")

    def test_chinese_transcoding_is_program_correction(self):
        """中文描述经程序转码为 HDA 代码，属于程序修正（表示格式变更）。"""
        result, _ = analyze.post_process_prediction(
            _pred(半全场推荐=["半平-全客胜"], 胜平负推荐="客胜"), "-1")
        self.assertEqual(result["半全场推荐"], ["DA"])
        self.assertEqual(result["半全场来源"], "程序修正")
        self.assertIn("中文半全场描述已规范化转码", result.get("半全场理由", ""))

    def test_invalid_and_incompatible_corrections_both_recorded(self):
        """非法编码清理 + 不兼容候选移除同时发生时，理由同时包含两项操作。"""
        # HH 兼容主胜、HD 不兼容主胜、HP 非法 → 最终保留 HH
        result, _ = analyze.post_process_prediction(
            _pred(半全场推荐=["HH", "HD", "HP"]), "-1")
        self.assertEqual(result["半全场推荐"], ["HH"])
        self.assertEqual(result["半全场来源"], "程序修正")
        reason = result.get("半全场理由", "")
        self.assertIn("移除与全场 主胜 不兼容", reason)
        self.assertIn("['HD']", reason)
        self.assertIn("过滤非法半全场编码", reason)
        self.assertIn("['HP']", reason)

    # -------- 场景 C/D/E：elif codes 分支（胜平负缺失降级）理由补齐 --------

    def test_elif_codes_legal_no_reason(self):
        """胜平负缺失 + 纯合法编码 → 来源=模型原始，理由可以为空。"""
        result, _ = analyze.post_process_prediction(
            _pred(半全场推荐=["HH", "DH"], 胜平负推荐=""), "-1")
        self.assertEqual(result["半全场来源"], "模型原始")
        # 模型原始 → 没有修正操作，理由为空是合理的
        self.assertEqual(result["半全场推荐"], ["HH", "DH"])

    def test_elif_codes_invalid_filter_reason_present(self):
        """胜平负缺失 + 合法/非法混合 → 来源=程序修正，理由必须说明过滤。"""
        result, _ = analyze.post_process_prediction(
            _pred(半全场推荐=["HH", "HP"], 胜平负推荐=""), "-1")
        self.assertEqual(result["半全场推荐"], ["HH"])
        self.assertEqual(result["半全场来源"], "程序修正")
        reason = result.get("半全场理由", "")
        self.assertIn("过滤非法半全场编码", reason)
        self.assertIn("['HP']", reason)
        # 必须明确承认兼容性未核对，不能假称已完成校验
        self.assertIn("胜平负推荐缺失", reason)
        self.assertIn("无法核对兼容性", reason)

    def test_elif_codes_chinese_transcoding_reason_present(self):
        """胜平负缺失 + 中文描述 → 来源=程序修正，理由必须说明转码。"""
        result, _ = analyze.post_process_prediction(
            _pred(半全场推荐=["半平-全客胜"], 胜平负推荐=""), "-1")
        self.assertEqual(result["半全场推荐"], ["DA"])
        self.assertEqual(result["半全场来源"], "程序修正")
        reason = result.get("半全场理由", "")
        self.assertIn("中文半全场描述已规范化转码", reason)
        self.assertIn("无法核对兼容性", reason)

    def test_elif_codes_chinese_and_invalid_together(self):
        """胜平负缺失 + 中文+非法同时 → 理由同时包含转码与过滤。"""
        result, _ = analyze.post_process_prediction(
            _pred(半全场推荐=["半平-全客胜", "HP"], 胜平负推荐=""), "-1")
        self.assertEqual(result["半全场推荐"], ["DA"])
        self.assertEqual(result["半全场来源"], "程序修正")
        reason = result.get("半全场理由", "")
        self.assertIn("中文半全场描述已规范化转码", reason)
        self.assertIn("过滤非法半全场编码", reason)
        self.assertIn("['HP']", reason)


class TestChineseTranscodingNoDuplicate(unittest.TestCase):
    """阶段十六修复：中文转码在自由文本报告中只出现一次。"""

    def _count_zhuanma_in_report(self, tokens, spf="主胜"):
        _, report = analyze.post_process_prediction(
            _pred(半全场推荐=tokens, 胜平负推荐=spf), "-1")
        return report.count("中文转码")

    def test_l862_branch_chinese_once(self):
        """L862 分支（兼容+had_chinese）：中文转码只出现一次。"""
        self.assertEqual(self._count_zhuanma_in_report(["半平-全客胜"], "客胜"), 1)

    def test_l853_fallback_branch_chinese_once(self):
        """L853 分支（全部不兼容→兜底+had_chinese）：中文转码只出现一次。"""
        self.assertEqual(self._count_zhuanma_in_report(["半平-全客胜"], "主胜"), 1)

    def test_elif_codes_branch_chinese_once(self):
        """elif codes 分支（spf 缺失+had_chinese）：中文转码只出现一次。"""
        self.assertEqual(self._count_zhuanma_in_report(["半平-全客胜"], ""), 1)

    def test_chinese_plus_invalid_once(self):
        """中文+非法编码混合：中文转码只出现一次（非法过滤另外记录）。"""
        _, report = analyze.post_process_prediction(
            _pred(半全场推荐=["半平-全客胜", "HP"], 胜平负推荐="主胜"), "-1")
        self.assertEqual(report.count("中文转码"), 1)
        self.assertIn("过滤非法编码 ['HP']", report)

    def test_no_chinese_input_no_record(self):
        """原始无中文描述 → 报告不应出现'中文转码'。"""
        self.assertEqual(self._count_zhuanma_in_report(["HH", "HP"], "主胜"), 0)

    def test_unparseable_chinese_no_record(self):
        """无法解析的中文（全部非法清空）→ 不应出现'中文转码'记录。"""
        self.assertEqual(self._count_zhuanma_in_report(["随机中文"], "主胜"), 0)


class TestCsvAndSnapshot(unittest.TestCase):
    """七、九：CSV 字段对齐；来源标记不覆盖原始快照。"""

    def test_csv_rows_match_header_and_new_column_present(self):
        failed = analyze.build_failed_result_row(
            "测试001", "测试联赛", {"对阵": "A vs B", "开赛": "2026-10-27 20:00"},
            None, "-1", "核心剧本校验未通过", "报告",
        )
        success = {field: "" for field in analyze.CSV_FIELDNAMES}
        success.update({"编号": "测试002", "半全场来源": "程序修正"})

        with tempfile.TemporaryDirectory() as tmp:
            path = analyze.save_analysis_csv([failed, success], "2026-10-27", output_dir=tmp)
            self.assertIsNotNone(path)
            with open(path, encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))

        self.assertEqual(rows[0], analyze.CSV_FIELDNAMES)
        self.assertIn("半全场来源", rows[0])
        for row in rows[1:]:
            self.assertEqual(len(row), len(rows[0]))
        self.assertEqual(rows[2][rows[0].index("半全场来源")], "程序修正")

    def test_source_marker_does_not_touch_raw_snapshot(self):
        raw = _pred(半全场推荐=["HH", "HP"])
        raw_snapshot = copy.deepcopy(raw)
        result, _ = analyze.post_process_prediction(raw, "-1")
        # 入口深拷贝：原始玩法输出保持模型原样（HP 仍在）
        self.assertEqual(raw, raw_snapshot)
        self.assertEqual(raw["半全场推荐"], ["HH", "HP"])
        self.assertNotIn("半全场来源", raw)
        # 来源落在修正后的结果副本上
        self.assertEqual(result["半全场来源"], "程序修正")


class TestLegacyCsvCompatibility(unittest.TestCase):
    """六：旧 CSV 无「半全场来源」列时仍可读取，状态退回按命中结果判断。"""

    def test_legacy_csv_without_source_column(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                os.makedirs("output", exist_ok=True)
                path = os.path.join("output", "竞彩分析_2026-10-27.csv")
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(["编号", "联赛", "对阵", "胜平负推荐", "半全场推荐"])
                    writer.writerow(["测试001", "测试联赛", "A vs B", "主胜", "HH / DH"])
                tecai, rows = review.load_tecai_data("2026-10-27")
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rows[0]["编号"], "测试001")
        self.assertNotIn("半全场来源", rows[0])
        # 缺列时 review 用空串回退，不崩溃，也不误判为非法清空
        self.assertEqual(review.resolve_hafu_status(rows[0].get("半全场来源", ""), None),
                         review.HAFU_STATUS_NO_PREDICTION)
        self.assertEqual(review.resolve_hafu_status("", True), review.HAFU_STATUS_VALID)


def _review_fixture():
    """两场：非法清空 + 程序兜底，其余四玩法全部命中，确保只有半全场差异。"""
    tecai = {
        "测试001": {"league": "测试联赛", "goal_line": "-1"},
        "测试002": {"league": "测试联赛", "goal_line": "-1"},
    }
    analysis_rows = []
    api_results = []
    for num, hafu_pred, source in (("测试001", "", "非法输出已清空"),
                                   ("测试002", "HH / DH", "程序兜底")):
        analysis_rows.append({
            "编号": num,
            "联赛": "测试联赛",
            "对阵": "A队 vs B队",
            "信心指数": "5",
            "胜平负推荐": "主胜",
            "让球推荐": "让-1平",
            "总进球推荐": "1球",
            "比分推荐": "1:0",
            "半全场推荐": hafu_pred,
            "半全场来源": source,
            "综合分析": "测试样本",
        })
        api_results.append({
            "match_num": num,
            "home": "A队",
            "away": "B队",
            "goal_line": "-1",
            "full_score_raw": "1:0",
            "half_score_raw": "1:0",
            "win_flag": "H",
            "league": "测试联赛",
        })
    return tecai, analysis_rows, api_results


class TestReviewInvalidStatus(unittest.TestCase):
    """二、三、八：review 统计与每日记录必须区分非法清空与无预测。"""

    def test_review_status_and_daily_record(self):
        tecai, analysis_rows, api_results = _review_fixture()
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                # 复盘的输入有效性保护要求分析 CSV 真实存在（load_tecai_data 已 mock）
                os.makedirs("output", exist_ok=True)
                with open(os.path.join("output", "竞彩分析_2026-10-27.csv"),
                          "w", encoding="utf-8-sig") as f:
                    f.write("编号,联赛,对阵,胜平负推荐\n")
                with mock.patch.object(review, "load_tecai_data",
                                       return_value=(tecai, analysis_rows)), \
                     mock.patch.object(review, "fetch_tecai_results",
                                       return_value=api_results), \
                     mock.patch.object(review, "call_deepseek_for_attribution",
                                       return_value=None):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        review.run_review("2026-10-27")
                with open(os.path.join(tmp, "output", "每日记录_2026-10-27.json"),
                          encoding="utf-8") as f:
                    record = json.load(f)
                with open(os.path.join(tmp, "output", "竞彩复盘_2026-10-27.txt"),
                          encoding="utf-8") as f:
                    report_text = f.read()
            finally:
                os.chdir(old_cwd)

        matches = {m["编号"]: m for m in record["场次"]}
        # 非法清空：不计命中/未中，但状态不被降级为普通无预测
        self.assertIsNone(matches["测试001"]["命中"]["半全场"])
        self.assertEqual(matches["测试001"]["半全场来源"], "非法输出已清空")
        self.assertEqual(matches["测试001"]["半全场状态"], "非法输出已清空")
        # 程序兜底：按最终有效预测正常判定
        self.assertTrue(matches["测试002"]["命中"]["半全场"])
        self.assertEqual(matches["测试002"]["半全场来源"], "程序兜底")
        self.assertEqual(matches["测试002"]["半全场状态"], "有效预测")

        stats = record["命中统计"]
        self.assertEqual(stats["hafu_valid"], 1)
        self.assertEqual(stats["hafu_hit"], 1)
        self.assertEqual(stats["hafu_invalid"], 1)
        self.assertEqual(stats["hafu_no_prediction"], 0)

        self.assertIn("非法输出已清空（不计命中）", report_text)
        self.assertIn("非法输出已清空 1", report_text)


if __name__ == "__main__":
    unittest.main()
