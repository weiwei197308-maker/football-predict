import json
import os
import sys
import types
import unittest
from unittest.mock import patch

# analyze.py imports this project module, which may be unavailable in a portable test bundle.
if "football_history" not in sys.modules:
    stub = types.ModuleType("football_history")
    stub.build_history_for_match = lambda home, away: {"available": False}
    sys.modules["football_history"] = stub

from lottery_rules import (
    normalize_core_result,
    normalize_core_goal_interval,
    goal_diff_to_core_interval,
    validate_core_script,
)
import analyze


def valid_core(**updates):
    result = {
        "home_team": "主队",
        "away_team": "客队",
        "full_time_result": "主胜",
        "net_goal_interval": "主胜1",
        "primary_score": "2:1",
        "alternative_scores": ["1:0", "3:2"],
        "match_script": "主队略占优势，比赛可能以一球优势结束。",
        "net_goal_reasoning": "主队小胜更符合双方攻防差距。",
        "uncertainties": ["临场阵容未知"],
        "confidence": "中",
    }
    result.update(updates)
    return result


class TestCoreEnumNormalization(unittest.TestCase):
    def test_result_aliases(self):
        self.assertEqual(normalize_core_result("平局"), "平")
        self.assertEqual(normalize_core_result("客胜"), "客胜")
        self.assertIsNone(normalize_core_result("不确定"))

    def test_interval_aliases(self):
        self.assertEqual(normalize_core_goal_interval("主胜1球"), "主胜1")
        self.assertEqual(normalize_core_goal_interval("主胜2球及以上"), "主胜2+")
        self.assertEqual(normalize_core_goal_interval("平局"), "平")
        self.assertIsNone(normalize_core_goal_interval("主队大胜"))

    def test_goal_diff_buckets(self):
        expected = {-3: "客胜2+", -2: "客胜2+", -1: "客胜1", 0: "平", 1: "主胜1", 2: "主胜2+", 5: "主胜2+"}
        for diff, interval in expected.items():
            self.assertEqual(goal_diff_to_core_interval(diff), interval)


class TestValidateCoreScript(unittest.TestCase):
    def test_valid_core_script_passes(self):
        result = validate_core_script(valid_core(), "主队", "客队")
        self.assertTrue(result["ok"], result["errors"])
        self.assertTrue(result["chain_ok"])
        self.assertEqual(result["normalized"]["full_time_result"], "主胜")

    def test_non_dict_fails(self):
        self.assertFalse(validate_core_script("not json")["ok"])

    def test_missing_required_field_fails(self):
        core = valid_core()
        del core["net_goal_interval"]
        result = validate_core_script(core)
        self.assertFalse(result["ok"])
        self.assertTrue(any("net_goal_interval" in x for x in result["errors"]))

    def test_invalid_result_enum_fails(self):
        result = validate_core_script(valid_core(full_time_result="可能主胜"))
        self.assertFalse(result["ok"])

    def test_unknown_interval_fails_closed(self):
        result = validate_core_script(valid_core(net_goal_interval="主队小胜"))
        self.assertFalse(result["ok"])
        self.assertTrue(result["unjudgeable"])

    def test_score_result_conflict_fails(self):
        result = validate_core_script(valid_core(full_time_result="客胜"))
        self.assertFalse(result["ok"])
        self.assertTrue(any("核心链冲突" in x for x in result["errors"]))

    def test_score_interval_conflict_fails(self):
        result = validate_core_script(valid_core(primary_score="2:0"))
        self.assertFalse(result["ok"])
        self.assertTrue(any("净胜球区间" in x for x in result["errors"]))

    def test_bad_score_format_fails(self):
        self.assertFalse(validate_core_script(valid_core(primary_score="2比1"))["ok"])

    def test_team_mismatch_fails(self):
        self.assertFalse(validate_core_script(valid_core(home_team="另一支队"), "主队", "客队")["ok"])

    def test_duplicate_alternative_fails(self):
        result = validate_core_script(valid_core(alternative_scores=["1:0", "1:0"]))
        self.assertFalse(result["ok"])
        self.assertTrue(any("重复" in x for x in result["errors"]))

    def test_primary_repeated_as_alternative_fails(self):
        self.assertFalse(validate_core_script(valid_core(alternative_scores=["2:1"]))["ok"])

    def test_alternative_different_interval_is_flagged_not_silently_accepted_as_same_script(self):
        result = validate_core_script(valid_core(alternative_scores=["2:0"]))
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["alternative_scripts"][0]["score"], "2:0")
        self.assertTrue(any("替代剧本" in x for x in result["warnings"]))

    def test_alternative_different_direction_is_flagged(self):
        result = validate_core_script(valid_core(alternative_scores=["0:1"]))
        self.assertTrue(result["ok"], result["errors"])
        self.assertTrue(result["alternative_scripts"])

    def test_missing_confidence_fails(self):
        core = valid_core()
        del core["confidence"]
        self.assertFalse(validate_core_script(core)["ok"])

    def test_does_not_mutate_input(self):
        core = valid_core()
        original = json.loads(json.dumps(core))
        validate_core_script(core)
        self.assertEqual(core, original)


class TestReviewSkipPredicate(unittest.TestCase):
    def test_empty_prediction_is_not_a_formal_prediction(self):
        import importlib.util
        stub = types.ModuleType("get_results")
        stub.fetch_tecai_results = lambda *a, **k: []
        stub.parse_score = lambda *a, **k: (None, None)
        stub.get_result_from_win_flag = lambda *a, **k: ""
        stub.get_result_code_from_win_flag = lambda *a, **k: ""
        with patch.dict(sys.modules, {"get_results": stub}):
            review_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review.py")
            spec = importlib.util.spec_from_file_location("review_under_test", review_path)
            review = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(review)
        self.assertFalse(review.has_formal_prediction({"胜平负推荐": ""}))
        self.assertFalse(review.has_formal_prediction({}))
        self.assertTrue(review.has_formal_prediction({"胜平负推荐": "主胜"}))


class TestStageTwoFlow(unittest.TestCase):
    def test_retry_sends_error_feedback_and_then_passes(self):
        bad = valid_core(primary_score="2:0")
        good = valid_core()
        with patch.object(analyze, "call_deepseek_core_script", side_effect=[json.dumps(bad), json.dumps(good)]) as call:
            core, report, err = analyze.analyze_core_script({"对阵": "主队 vs 客队"}, "test", "主队", "客队", max_retries=1)
        self.assertIsNone(err)
        self.assertIsNotNone(core)
        self.assertEqual(call.call_count, 2)
        self.assertTrue(call.call_args_list[1].kwargs.get("feedback"))
        self.assertIn("通过", report)

    def test_failed_core_blocks_stage_two(self):
        details = {
            "ok": False, "normalized": None, "validation": {"ok": False, "errors": ["核心链冲突"]},
            "validation_attempts": [{"ok": False, "errors": ["核心链冲突"]}],
            "raw_attempts": ['{"bad": true}'], "llm_calls": 1, "parse_errors": [""],
            "report": "校验未通过", "error": "核心剧本校验未通过",
        }
        with patch.object(analyze, "analyze_core_script", return_value=details), \
             patch.object(analyze, "call_deepseek") as stage_two:
            parsed, report, err = analyze.analyze_match_two_stage({}, "test", "-1", "主队", "客队")
        self.assertIsNone(parsed)
        self.assertIsNotNone(err)
        stage_two.assert_not_called()

    def test_core_script_preserves_raw_outputs_validation_and_retry_count(self):
        bad = valid_core(primary_score="2:0")
        good = valid_core()
        raw_bad, raw_good = json.dumps(bad), json.dumps(good)
        with patch.object(analyze, "call_deepseek_core_script", side_effect=[raw_bad, raw_good]):
            details = analyze.analyze_core_script(
                {"对阵": "主队 vs 客队"}, "test", "主队", "客队", max_retries=1, return_details=True
            )
        self.assertTrue(details["ok"])
        self.assertEqual(details["llm_calls"], 2)
        self.assertEqual(details["raw_attempts"], [raw_bad, raw_good])
        self.assertEqual(len(details["validation_attempts"]), 2)
        self.assertIsNotNone(details["normalized"])

    def test_two_invalid_core_attempts_block_play_llm(self):
        invalid = valid_core(primary_score="2:0")
        with patch.object(analyze, "call_deepseek_core_script", side_effect=[json.dumps(invalid), json.dumps(invalid)]), \
             patch.object(analyze, "call_deepseek") as play_call:
            parsed, report, err = analyze.analyze_match_two_stage(
                {"对阵": "主队 vs 客队"}, "test", "-1", "主队", "客队", max_retries=1
            )
        self.assertIsNone(parsed)
        self.assertIn("核心剧本校验未通过", err)
        self.assertIn("raw_attempts", report)
        self.assertIn("validation_attempts", report)
        self.assertIn("llm_calls", report)
        play_call.assert_not_called()

    def test_failed_result_row_has_all_csv_fields_and_no_predictions(self):
        row = analyze.build_failed_result_row(
            "001", "测试联赛", {"对阵": "主队 vs 客队", "开赛": "20:00"},
            {"available": False}, "-1", "核心剧本校验未通过", "两次校验失败"
        )
        self.assertEqual(set(row), set(analyze.CSV_FIELDNAMES))
        for field in ("胜平负推荐", "让球推荐", "总进球推荐", "半全场推荐", "比分推荐"):
            self.assertEqual(row[field], "")
        self.assertIn("核心剧本校验未通过", row["综合分析"])

    def test_valid_two_stage_flow_uses_locked_core(self):
        core = {
            "home_team": "主队", "away_team": "客队", "full_time_result": "主胜",
            "net_goal_interval": "主胜1", "primary_score": "2:1",
            "alternative_scores": ["1:0"], "match_script": "锁定剧本",
            "net_goal_reasoning": "一球小胜", "uncertainties": [], "confidence": "中",
        }
        play = {
            "核心剧本": "模型自己写的剧本", "胜平负推荐": "客胜", "胜平负理由": "理由",
            "净胜球分布": "客胜2+", "让球推荐": "让-1客胜", "让球理由": "旧让球理由",
            "总进球推荐": ["1球", "2球", "3球"], "总进球理由": "旧总进球理由",
            "半全场推荐": ["AA", "DA"], "半全场理由": "半全场理由",
            "比分推荐": ["0:2"], "比分理由": "旧比分理由", "信心指数": 5,
            "数据缺失说明": "", "综合分析": "分析",
        }
        details = {
            "ok": True, "normalized": core, "validation": {"ok": True, "errors": []},
            "validation_attempts": [{"ok": True, "errors": []}], "raw_attempts": [json.dumps(core)],
            "llm_calls": 1, "parse_errors": [""], "report": "核心剧本校验通过",
        }
        with patch.object(analyze, "analyze_core_script", return_value=details), \
             patch.object(analyze, "call_deepseek", return_value=json.dumps(play)) as stage_two:
            parsed, report, err = analyze.analyze_match_two_stage(
                {"主队": "主队", "客队": "客队"}, "test", "-1", "主队", "客队"
            )
        self.assertIsNone(err)
        self.assertEqual(parsed["胜平负推荐"], "主胜")
        self.assertEqual(parsed["净胜球分布"], "主胜1")
        self.assertEqual(parsed["比分推荐"], ["2:1", "1:0"])
        self.assertIn("锁定剧本", parsed["核心剧本"])
        self.assertIn("核心剧本校验通过", report)
        stage_two.assert_called_once()
        self.assertIn('"primary_score": "2:1"', stage_two.call_args.kwargs["extra_user_instruction"])

    def test_locked_core_overrides_play_layer_core_fields(self):
        core = {
            "home_team": "主队", "away_team": "客队", "full_time_result": "主胜",
            "net_goal_interval": "主胜1", "primary_score": "2:1",
            "alternative_scores": ["1:0", "3:0"], "match_script": "锁定剧本",
            "net_goal_reasoning": "一球小胜", "uncertainties": [], "confidence": "中",
        }
        parsed = {"胜平负推荐": "客胜", "净胜球分布": "客胜2+", "比分推荐": ["0:2"], "比分理由": "旧理由"}
        actual = analyze.apply_locked_core_script(parsed, core)
        self.assertEqual(actual["胜平负推荐"], "主胜")
        self.assertEqual(actual["净胜球分布"], "主胜1")
        self.assertEqual(actual["比分推荐"], ["2:1", "1:0"])
        self.assertEqual(actual["核心剧本"], "锁定剧本")


if __name__ == "__main__":
    unittest.main(verbosity=2)
