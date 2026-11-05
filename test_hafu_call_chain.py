"""规则 A/B 调用链 Mock 测试（不访问网络、不写文件）。

规则 A：中文半全场描述 → HDA 代码。
规则 B：全场客胜时剔除不兼容的 DD。

测试 mock 掉核心剧本层与 LLM 返回值，但真实执行：
    analyze.analyze_match_two_stage → post_process_prediction → finalize_hafu_picks
用于证明两条规则确实在正式生产调用链内生效，而不只是辅助函数正确。

运行：python3 -m unittest test_hafu_call_chain -v
"""
import json
import os
import unittest
from datetime import date
from unittest import mock

# 影子实验（总进球/让球/半全场）默认开启，会在快照落盘后追加 json.dump 调用，
# 干扰落盘链断言；本文件只验证正式链路，统一显式关闭。
_SHADOW_OFF = {"DISABLE_TTG_SHADOW": "1", "DISABLE_RQ_SHADOW": "1",
               "DISABLE_HAFU_SHADOW": "1"}

import analyze
import lottery_rules
import update_half_full_tracker as tracker


def _core(full_time="客胜", interval="客胜1", primary="0:1", alts=("1:2",), probs=None):
    return {
        "home_team": "A队",
        "away_team": "B队",
        "full_time_result": full_time,
        "net_goal_interval": interval,
        "primary_score": primary,
        "alternative_scores": list(alts),
        "match_script": "客队小胜",
        "net_goal_reasoning": "客队实力占优",
        "uncertainties": [],
        "confidence": "中",
        "net_goal_probabilities": probs or {
            "主胜2+": 0.05, "主胜1": 0.05, "平": 0.20,
            "客胜1": 0.50, "客胜2+": 0.20,
        },
    }


def _play_json(hafu, reason="模型原始半全场理由"):
    """玩法层 LLM 返回的 JSON 文本；胜平负会被锁定核心剧本覆盖。"""
    return json.dumps({
        "核心剧本": "客队小胜",
        "胜平负推荐": "主胜",
        "胜平负理由": "模型原判",
        "净胜球分布": "主胜1",
        "让球推荐": "让-1平",
        "让球理由": "模型原让球理由",
        "总进球推荐": ["2球", "3球"],
        "总进球理由": "节奏",
        "半全场推荐": hafu,
        "半全场理由": reason,
        "比分推荐": ["2:1", "1:0"],
        "比分理由": "模型原比分理由",
        "信心指数": 7,
        "数据缺失说明": "",
        "综合分析": "综合判断",
    }, ensure_ascii=False)


def _pred(**updates):
    """直接调用 post_process_prediction 时的最小合法预测。"""
    pred = {
        "胜平负推荐": "客胜",
        "让球推荐": "让+1客胜",
        "总进球推荐": ["2球", "3球"],
        "半全场推荐": ["DA", "AA"],
        "半全场理由": "模型理由",
        "比分推荐": ["0:1", "1:2"],
        "净胜球分布": "客胜1",
        "信心指数": 7,
    }
    pred.update(updates)
    return pred


class TestRuleAChain(unittest.TestCase):
    """规则 A：中文描述在生产链路中被转码。"""

    def _run_pipeline(self, hafu):
        details = {"ok": True, "normalized": _core(), "report": "", "error": None}
        with mock.patch.object(analyze, "analyze_core_script", return_value=details), \
             mock.patch.object(analyze, "format_core_audit_details", return_value=""), \
             mock.patch.object(analyze, "call_deepseek", return_value=_play_json(hafu)):
            return analyze.analyze_match_two_stage({}, "sk-test", "-1", "A队", "B队")

    def test_chinese_transcoded_to_hda(self):
        parsed, report, err = self._run_pipeline(["平/客胜", "平/平", "客胜/客胜"])
        self.assertIsNone(err)
        # 锁定核心剧本为客胜：中文转码 + 剔除 DD 同时发生
        self.assertEqual(parsed["半全场推荐"], ["DA", "AA"])
        self.assertEqual(parsed["半全场来源"], "程序修正")
        self.assertIn("中文半全场描述已规范化转码为 HDA 代码", parsed["半全场理由"])
        self.assertIn("移除与全场 客胜 不兼容的半全场 ['DD']", parsed["半全场理由"])
        self.assertIn("原理由：模型原始半全场理由", parsed["半全场理由"])

    def test_transcode_note_recorded_once(self):
        parsed, report, _ = self._run_pipeline(["半平-全客胜"])
        self.assertEqual(parsed["半全场推荐"], ["DA"])
        self.assertEqual(
            parsed["半全场理由"].count("中文半全场描述已规范化转码为 HDA 代码"), 1)
        self.assertIn("- 半全场推荐含中文描述，已中文转码为 HDA 代码", report)

    def test_short_chinese_form_transcoded(self):
        parsed, _, err = self._run_pipeline(["平-客胜", "客胜-客胜"])
        self.assertIsNone(err)
        self.assertEqual(parsed["半全场推荐"], ["DA", "AA"])
        self.assertEqual(parsed["半全场来源"], "程序修正")

    def test_fragment_chinese_merged(self):
        """碎片拼回中文对：平+客胜→DA、平+平→DD、客胜+客胜→AA。"""
        parsed, _, err = self._run_pipeline(["平", "客胜", "平", "平", "客胜", "客胜"])
        self.assertIsNone(err)
        self.assertEqual(parsed["半全场推荐"], ["DA", "AA"])
        self.assertEqual(parsed["半全场来源"], "程序修正")
        self.assertIn("移除与全场 客胜 不兼容的半全场 ['DD']", parsed["半全场理由"])
        # 原始快照保留碎片，证明合并只作用于修正副本
        self.assertEqual(
            parsed["_原始快照"]["原始玩法输出"]["半全场推荐"],
            ["平", "客胜", "平", "平", "客胜", "客胜"])

    def test_fragment_chinese_reports_transcode(self):
        """碎片合并转码后，理由应声明中文描述已规范化为 HDA 代码。"""
        parsed, _, _ = self._run_pipeline(["平", "客胜", "平", "平", "客胜", "客胜"])
        self.assertIn("中文半全场描述已规范化转码为 HDA 代码", parsed["半全场理由"])


class TestRuleBChain(unittest.TestCase):
    """规则 B：客胜剧本下剔除 DD，并保持候选顺序。"""

    def _run_pipeline(self, hafu):
        details = {"ok": True, "normalized": _core(), "report": "", "error": None}
        with mock.patch.object(analyze, "analyze_core_script", return_value=details), \
             mock.patch.object(analyze, "format_core_audit_details", return_value=""), \
             mock.patch.object(analyze, "call_deepseek", return_value=_play_json(hafu)):
            return analyze.analyze_match_two_stage({}, "sk-test", "-1", "A队", "B队")

    def test_dd_removed(self):
        parsed, report, _ = self._run_pipeline(["DA", "DD", "AA"])
        self.assertEqual(parsed["半全场推荐"], ["DA", "AA"])
        self.assertEqual(parsed["半全场来源"], "程序修正")
        self.assertIn("[\'DD\']", parsed["半全场理由"])
        self.assertIn("- 半全场移除不兼容项 ['DD']", report)
        # 剔除后复查不应再有半全场硬性冲突
        self.assertNotIn("半全场", "\n".join(
            x for x in report.splitlines() if x.startswith("硬性错误")))

    def test_candidate_order_preserved(self):
        parsed, _, _ = self._run_pipeline(["AA", "DA", "DD"])
        self.assertEqual(parsed["半全场推荐"], ["AA", "DA"])

    def test_all_incompatible_uses_fallback(self):
        parsed, report, _ = self._run_pipeline(["HH", "DH", "AH"])
        self.assertEqual(parsed["半全场推荐"], ["HA", "DA", "AA"])
        self.assertEqual(parsed["半全场来源"], "程序兜底")
        self.assertIn("全部不兼容", parsed["半全场理由"])
        self.assertIn("已按规则兜底生成 HA / DA / AA", parsed["半全场理由"])
        self.assertIn("（程序兜底生成，待模型确认）", parsed["半全场理由"])
        self.assertIn("半全场全部不兼容", report)

    def test_fallback_reason_mentions_invalid_codes(self):
        parsed, _, _ = self._run_pipeline(["HH", "HP", "DH"])
        self.assertEqual(parsed["半全场推荐"], ["HA", "DA", "AA"])
        self.assertEqual(parsed["半全场来源"], "程序兜底")
        self.assertIn("已过滤非法半全场编码 ['HP']", parsed["半全场理由"])

    def test_no_rewrite_when_all_compatible(self):
        parsed, _, _ = self._run_pipeline(["DA", "AA"])
        self.assertEqual(parsed["半全场推荐"], ["DA", "AA"])
        self.assertEqual(parsed["半全场来源"], "模型原始")
        # 未发生修正：保留模型原句，不虚构程序修正理由
        self.assertEqual(parsed["半全场理由"], "模型原始半全场理由")


class TestRuleBHomeWin(unittest.TestCase):
    """规则 B 不依赖客胜特例：主胜剧本同样剔除全场客胜候选。"""

    def _run_pipeline(self, hafu):
        core = _core(
            full_time="主胜", interval="主胜2+", primary="2:0", alts=("3:0",),
            probs={"主胜2+": 0.50, "主胜1": 0.20, "平": 0.15,
                   "客胜1": 0.10, "客胜2+": 0.05},
        )
        details = {"ok": True, "normalized": core, "report": "", "error": None}
        with mock.patch.object(analyze, "analyze_core_script", return_value=details), \
             mock.patch.object(analyze, "format_core_audit_details", return_value=""), \
             mock.patch.object(analyze, "call_deepseek", return_value=_play_json(hafu)):
            return analyze.analyze_match_two_stage({}, "sk-test", "-1", "A队", "B队")

    def test_away_full_codes_removed(self):
        parsed, report, _ = self._run_pipeline(["HH", "HA", "DH"])
        self.assertEqual(parsed["半全场推荐"], ["HH", "DH"])
        self.assertEqual(parsed["半全场来源"], "程序修正")
        self.assertIn("移除与全场 主胜 不兼容的半全场 ['HA']", parsed["半全场理由"])
        self.assertIn("- 半全场移除不兼容项 ['HA']", report)

    def test_all_compatible_untouched(self):
        parsed, _, _ = self._run_pipeline(["HH", "DH", "AH"])
        self.assertEqual(parsed["半全场推荐"], ["HH", "DH", "AH"])
        self.assertEqual(parsed["半全场来源"], "模型原始")
        self.assertEqual(parsed["半全场理由"], "模型原始半全场理由")


class TestRuleBDraw(unittest.TestCase):
    """规则 B 在平局剧本下同样剔除全场主胜/客胜候选。"""

    def _run_pipeline(self, hafu):
        core = _core(
            full_time="平", interval="平", primary="1:1", alts=("0:0",),
            probs={"主胜2+": 0.05, "主胜1": 0.10, "平": 0.65,
                   "客胜1": 0.15, "客胜2+": 0.05},
        )
        details = {"ok": True, "normalized": core, "report": "", "error": None}
        with mock.patch.object(analyze, "analyze_core_script", return_value=details), \
             mock.patch.object(analyze, "format_core_audit_details", return_value=""), \
             mock.patch.object(analyze, "call_deepseek", return_value=_play_json(hafu)):
            return analyze.analyze_match_two_stage({}, "sk-test", "-1", "A队", "B队")

    def test_non_draw_codes_removed(self):
        parsed, report, _ = self._run_pipeline(["HD", "HH", "DD"])
        self.assertEqual(parsed["半全场推荐"], ["HD", "DD"])
        self.assertEqual(parsed["半全场来源"], "程序修正")
        self.assertIn("移除与全场 平 不兼容的半全场 ['HH']", parsed["半全场理由"])
        self.assertIn("- 半全场移除不兼容项 ['HH']", report)

    def test_all_compatible_untouched(self):
        parsed, _, _ = self._run_pipeline(["HD", "DD", "AD"])
        self.assertEqual(parsed["半全场推荐"], ["HD", "DD", "AD"])
        self.assertEqual(parsed["半全场来源"], "模型原始")
        self.assertEqual(parsed["半全场理由"], "模型原始半全场理由")


class TestEmptySpf(unittest.TestCase):
    """胜平负为空：保留半全场候选，但不得宣称已完成兼容性判断。"""

    def test_empty_spf_hda_not_rewritten(self):
        result, report = analyze.post_process_prediction(
            _pred(半全场推荐=["DA", "DD"], 胜平负推荐="",
                  半全场理由="模型原始半全场理由"), "-1")
        self.assertEqual(result["半全场推荐"], ["DA", "DD"])
        self.assertEqual(result["半全场来源"], "模型原始")
        self.assertEqual(result["半全场理由"], "模型原始半全场理由")
        self.assertIn("胜平负推荐缺失（''），半全场兼容性无法判定", report)

    def test_empty_spf_chinese_transcoded_without_compat_claim(self):
        result, report = analyze.post_process_prediction(
            _pred(半全场推荐=["平/客胜", "平/平"], 胜平负推荐=""), "-1")
        self.assertEqual(result["半全场推荐"], ["DA", "DD"])
        self.assertEqual(result["半全场来源"], "程序修正")
        self.assertIn("中文半全场描述已规范化转码为 HDA 代码", result["半全场理由"])
        self.assertIn("胜平负推荐缺失（''），无法核对兼容性", result["半全场理由"])
        self.assertNotIn("不兼容", result["半全场理由"])
        self.assertIn("待确认：胜平负推荐缺失（''），半全场兼容性无法判定", report)


class TestChainInvocation(unittest.TestCase):
    """调用链实证：规则函数确实被生产函数调用，且入参来自锁定剧本。"""

    def test_pipeline_calls_finalize_once_with_locked_spf(self):
        details = {"ok": True, "normalized": _core(), "report": "", "error": None}
        with mock.patch.object(analyze, "analyze_core_script", return_value=details), \
             mock.patch.object(analyze, "format_core_audit_details", return_value=""), \
             mock.patch.object(analyze, "call_deepseek", return_value=_play_json(["DA", "DD", "AA"])), \
             mock.patch.object(analyze, "finalize_hafu_picks",
                               wraps=lottery_rules.finalize_hafu_picks) as spy:
            parsed, _, err = analyze.analyze_match_two_stage({}, "sk-test", "-1", "A队", "B队")
        self.assertIsNone(err)
        self.assertEqual(spy.call_count, 1)
        args = spy.call_args.args
        self.assertEqual(args[0], ["DA", "DD", "AA"])
        # 入参胜平负来自 apply_locked_core_script 覆盖后的客胜，而非模型原判主胜
        self.assertEqual(args[1], "客胜")
        self.assertEqual(parsed["半全场推荐"], ["DA", "AA"])

    def test_pipeline_invokes_parser_and_compatibility(self):
        """函数级证据：解析与兼容过滤在生产链路内被真实调用。"""
        details = {"ok": True, "normalized": _core(), "report": "", "error": None}
        with mock.patch.object(analyze, "analyze_core_script", return_value=details), \
             mock.patch.object(analyze, "format_core_audit_details", return_value=""), \
             mock.patch.object(analyze, "call_deepseek",
                               return_value=_play_json(["平/客胜", "平/平", "客胜/客胜"])), \
             mock.patch.object(lottery_rules, "parse_hafu_code_from_text",
                               wraps=lottery_rules.parse_hafu_code_from_text) as parse_spy, \
             mock.patch.object(lottery_rules, "is_hafu_compatible",
                               wraps=lottery_rules.is_hafu_compatible) as compat_spy:
            parsed, _, err = analyze.analyze_match_two_stage({}, "sk-test", "-1", "A队", "B队")
        self.assertIsNone(err)
        self.assertEqual(parsed["半全场推荐"], ["DA", "AA"])
        self.assertGreaterEqual(parse_spy.call_count, 3)
        self.assertIn(mock.call("平/客胜"), parse_spy.call_args_list)
        self.assertGreaterEqual(compat_spy.call_count, 3)
        self.assertIn(mock.call("DD", "客胜"), compat_spy.call_args_list)

    def test_raw_snapshot_keeps_model_output(self):
        details = {"ok": True, "normalized": _core(), "report": "", "error": None}
        with mock.patch.object(analyze, "analyze_core_script", return_value=details), \
             mock.patch.object(analyze, "format_core_audit_details", return_value=""), \
             mock.patch.object(analyze, "call_deepseek", return_value=_play_json(["DA", "DD", "AA"])):
            parsed, _, _ = analyze.analyze_match_two_stage({}, "sk-test", "-1", "A队", "B队")
        self.assertEqual(
            parsed["_原始快照"]["原始玩法输出"]["半全场推荐"], ["DA", "DD", "AA"])
        self.assertEqual(parsed["_原始快照"]["原始玩法输出"]["半全场理由"], "模型原始半全场理由")

    def test_tracker_snapshot_recompute_uses_same_rule(self):
        row = {"胜平负推荐": "客胜"}
        snapshot = {"原始玩法输出": {"半全场推荐": ["DA", "DD", "AA"]}}
        with mock.patch.object(tracker, "finalize_hafu_picks",
                               wraps=lottery_rules.finalize_hafu_picks) as spy:
            out = tracker.recompute_hafu_from_snapshot(row, snapshot)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(out["推荐"], ["DA", "AA"])
        self.assertEqual(out["来源"], "程序修正")
        self.assertIn("['DD']", out["理由"])

    def test_tracker_snapshot_missing_returns_none(self):
        self.assertIsNone(tracker.recompute_hafu_from_snapshot(
            {"胜平负推荐": "客胜"}, {"原始玩法输出": {}}))
        self.assertIsNone(tracker.recompute_hafu_from_snapshot(
            {"胜平负推荐": "客胜"}, None))


class TestPersistenceChain(unittest.TestCase):
    """落盘链：main 写 CSV 与快照时使用同一份半全场理由。"""

    def test_main_writes_same_hafu_reason_to_csv_and_snapshot(self):
        parsed = {
            "胜平负推荐": "客胜",
            "半全场推荐": ["DA", "AA"],
            "半全场理由": "统一半全场理由",
            "半全场来源": "程序修正",
            "_原始快照": {
                "原始玩法输出": {"半全场推荐": ["DA", "DD", "AA"]},
                "锁定核心剧本": {},
            },
        }
        csv_rows = []
        snapshot_dump = mock.Mock()
        today_str = date.today().strftime("%Y-%m-%d")
        with mock.patch.object(analyze, "validate_api_key", return_value=(True, "")), \
             mock.patch.object(analyze, "find_latest_csv",
                               return_value=f"output/竞彩足球_{today_str}.csv"), \
             mock.patch.object(analyze, "load_matches_from_csv", return_value=[{
                 "编号": "周四014", "主队": "A队", "客队": "B队",
                 "联赛": "测试联赛", "让球数": "-1"}]), \
             mock.patch.object(analyze, "build_history_for_match",
                               return_value={"available": False}), \
             mock.patch.object(analyze, "analyze_match_two_stage",
                               return_value=(parsed, "复查报告", None)), \
             mock.patch.object(analyze, "save_analysis_csv",
                               side_effect=lambda rows, d: csv_rows.extend(rows)), \
             mock.patch.object(analyze, "print", create=True), \
             mock.patch.object(analyze.os, "makedirs"), \
             mock.patch.object(analyze, "open", mock.mock_open(), create=True), \
             mock.patch.object(analyze.json, "dump", snapshot_dump), \
             mock.patch.dict(os.environ, _SHADOW_OFF):
            analyze.main()

        self.assertEqual(len(csv_rows), 1)
        row = csv_rows[0]
        self.assertEqual(row["半全场理由"], "统一半全场理由")
        self.assertEqual(row["半全场推荐"], "DA / AA")
        self.assertEqual(row["半全场来源"], "程序修正")

        snap = snapshot_dump.call_args.args[0]
        self.assertEqual(snap["复盘日期"], today_str)
        entry = snap["场次"][0]
        self.assertEqual(entry["修正后预测"]["半全场理由"], row["半全场理由"])
        self.assertEqual(entry["修正后预测"]["半全场来源"], row["半全场来源"])
        self.assertEqual(entry["修正后预测"]["一致性检查"], "复查报告")
        # 原始模型输出未被落盘链改写
        self.assertEqual(entry["原始玩法输出"]["半全场推荐"], ["DA", "DD", "AA"])


class TestSaleDateGuard(unittest.TestCase):
    """销售日保护：赛程抓取失败时不得回退前一日 CSV（10-27 覆写事故回归）。"""

    def test_refuses_non_today_csv_and_writes_nothing(self):
        with mock.patch.object(analyze, "validate_api_key", return_value=(True, "")), \
             mock.patch.object(analyze, "find_latest_csv",
                               return_value="output/竞彩足球_2000-01-01.csv"), \
             mock.patch.object(analyze, "load_matches_from_csv") as load, \
             mock.patch.object(analyze, "save_analysis_csv") as save, \
             mock.patch.object(analyze, "print", create=True):
            with self.assertRaises(SystemExit) as ctx:
                analyze.main()
        self.assertEqual(ctx.exception.code, 1)
        load.assert_not_called()
        save.assert_not_called()


class TestUnrecognizedSpf(unittest.TestCase):
    """胜平负不可识别：降级为无法判定，不得报硬性冲突。"""

    def test_unrecognized_spf_should_not_report_hard_conflict(self):
        result, report = analyze.post_process_prediction(
            _pred(半全场推荐=["平/客胜", "平/平", "客胜/客胜"], 胜平负推荐="未知"), "-1")
        self.assertEqual(result["半全场来源"], "程序修正")
        self.assertIn("胜平负推荐无法识别（'未知'），无法核对兼容性", result["半全场理由"])
        self.assertNotIn("与全场 未知 不一致", report)
        self.assertIn("胜平负推荐无法识别（'未知'），半全场兼容性无法判定", report)
        self.assertFalse(any("硬性错误" in x and "半全场" in x
                             for x in report.splitlines()))


if __name__ == "__main__":
    unittest.main()
