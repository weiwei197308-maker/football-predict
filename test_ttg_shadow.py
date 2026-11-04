#!/usr/bin/env python3
"""总进球影子实验单元测试。全部使用 mock 与临时目录，不调用真实预测 API。

运行：python3 -m unittest test_ttg_shadow -v
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import analyze
import ttg_shadow
from ttg_shadow import (
    build_blind_input, build_market_info, market_top3,
    validate_ttg_blind, validate_ttg_review, validate_ttg_independent,
    enable_ttg_c2,
    collect_shadow_record, write_shadow_file, shadow_file_path,
    group_metrics, evaluate_records, FIXED_BASELINE_PICKS,
)

ROW = {
    "编号": "周四001", "联赛": "测试联赛A",
    "主队": "阿尔法队", "主队排名": "[3]",
    "客队": "贝塔队", "客队排名": "[5]",
    "开赛日期": "2026-10-29", "开赛时间": "20:00",
    "让球数": "-1",
    "胜平负-主胜": "1.85",
    "总进球-0球": "12.00", "总进球-1球": "4.60", "总进球-2球": "3.40",
    "总进球-3球": "3.20", "总进球-4球": "4.80", "总进球-5球": "8.50",
    "总进球-6球": "15.00", "总进球-7+球": "22.00",
    "比分-1:0": "7.00", "比分-2:1": "8.50",
}
HISTORY = {"available": True, "home_recent_text": "主队近5场2胜",
           "away_recent_text": "客队近5场1胜", "h2h_text": "交锋主队占优"}
API_KEY = "sk-" + "a" * 16
DATE = "2026-10-29"


def make_resp(text):
    r = Mock()
    r.raise_for_status = lambda: None
    r.json = lambda: {"choices": [{"message": {"content": text}}]}
    return r


def make_match_data():
    return analyze.build_match_prompt_data(ROW, HISTORY)


def make_record(num="周四001", blind_picks=None, ttg_c=None, c2_independent=None):
    """构造合法影子记录（可选覆盖 A 组候选、C 组正式推荐与 C2 独立判断）。"""
    rec = {
        "对齐键": {"日期": DATE, "编号": num, "主队": "阿尔法队",
                   "客队": "贝塔队", "开赛": "2026-10-29 20:00"},
        "实验运行ID": "test-run",
        "写入时间": "2026-10-29T21:00:00",
        "A盲判": {"状态": "ok", "候选": blind_picks or ["2球", "3球"],
                  "理由": "盲判理由", "不确定说明": "", "有序": True,
                  "错误": "", "原始文本前缀": ""},
        "B复核": {"状态": "ok", "候选": ["2球", "3球", "4球"],
                  "模型声明变更": True, "校验后变更": True,
                  "变更理由": "市场3球最低", "理由缺失": False,
                  "错误": "", "原始文本前缀": ""},
        "C正式": {"状态": "ok", "候选": ttg_c or ["3球", "2球", "4球"], "有序": True},
        "C2独立": c2_independent or {"状态": "disabled", "候选": [], "理由": "", "有序": True,
                                       "错误": "", "原始文本前缀": ""},
        "D固定": {"状态": "ok", "候选": list(FIXED_BASELINE_PICKS), "有序": True},
        "市场前三": ["3球", "2球", "1球"],
        "实际赛果": None,
        "影子错误": "",
    }
    return rec


class TestBlindInputWhitelist(unittest.TestCase):
    def test_whitelist_fields_only(self):
        data = build_blind_input(ROW, HISTORY)
        allowed = {"编号", "联赛", "主队", "客队", "主队排名", "客队排名",
                   "开赛日期", "开赛时间", "对阵", "开赛", "历史数据"}
        self.assertEqual(set(data.keys()), allowed)
        blob = json.dumps(data, ensure_ascii=False)
        for banned in ("总进球赔率", "比分赔率", "胜平负赔率", "让球赔率",
                       "半全场赔率", "赔率", "3.20", "1.85", "8.50",
                       "primary_score", "net_goal"):
            self.assertNotIn(banned, blob, f"盲判输入泄漏了 {banned}")

    def test_no_history(self):
        data = build_blind_input(ROW, None)
        self.assertNotIn("历史数据", data)

    def test_request_body_isolation(self):
        """mock 捕获最终 HTTP 请求体，验证序列化后无赔率/锁定剧本泄漏。"""
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None, **kw):
            captured["payload"] = json  # 注意：此 json 为请求体参数名，遮蔽模块
            return make_resp(globals()["json"].dumps({
                "ttg_blind_picks": ["2球", "3球"],
                "ttg_blind_reasoning": "节奏偏慢", "ttg_blind_uncertainty": "",
                "ordered": True}, ensure_ascii=False))

        with patch("ttg_shadow.requests.post", side_effect=fake_post):
            ttg_shadow.call_deepseek_ttg_blind(build_blind_input(ROW, HISTORY), API_KEY)
        blob = json.dumps(captured["payload"], ensure_ascii=False)
        for banned in ("总进球赔率", "比分赔率", "胜平负赔率", "让球赔率",
                       "半全场赔率", "3.20", "1.85", "primary_score",
                       "match_script", "net_goal_interval"):
            self.assertNotIn(banned, blob, f"实际请求体泄漏了 {banned}")


class TestValidateBlind(unittest.TestCase):
    def test_ok(self):
        r = validate_ttg_blind(json.dumps({
            "ttg_blind_picks": ["3球", "2球"],
            "ttg_blind_reasoning": "理由", "ttg_blind_uncertainty": "不确定",
            "ordered": True}, ensure_ascii=False))
        self.assertEqual(r["状态"], "ok")
        self.assertEqual(r["候选"], ["3球", "2球"])
        self.assertTrue(r["有序"])

    def test_empty_allowed(self):
        r = validate_ttg_blind(json.dumps({
            "ttg_blind_picks": [], "ttg_blind_reasoning": "",
            "ttg_blind_uncertainty": "信息不足，弃选", "ordered": True}))
        self.assertEqual(r["状态"], "empty")
        self.assertEqual(r["候选"], [])

    def test_illegal_label(self):
        r = validate_ttg_blind(json.dumps({"ttg_blind_picks": ["2球", "很多球"]}))
        self.assertEqual(r["状态"], "illegal")

    def test_dedup_keeps_order(self):
        r = validate_ttg_blind(json.dumps({"ttg_blind_picks": ["2球", "2球", "3球"]}))
        self.assertEqual(r["状态"], "ok")
        self.assertEqual(r["候选"], ["2球", "3球"])
        self.assertIn("去重", r["不确定说明"])

    def test_over_max_items(self):
        r = validate_ttg_blind(json.dumps(
            {"ttg_blind_picks": ["0球", "1球", "2球", "3球", "4球"]}))
        self.assertEqual(r["状态"], "illegal")

    def test_api_error(self):
        r = validate_ttg_blind("__API_ERROR__: boom")
        self.assertEqual(r["状态"], "api_error")

    def test_garbage(self):
        r = validate_ttg_blind("这不是 JSON")
        self.assertEqual(r["状态"], "illegal")

    def test_unordered_flag(self):
        r = validate_ttg_blind(json.dumps(
            {"ttg_blind_picks": ["2球", "3球"], "ordered": False}))
        self.assertFalse(r["有序"])


class TestValidateReview(unittest.TestCase):
    def test_no_change(self):
        r = validate_ttg_review(json.dumps({
            "ttg_review_picks": ["2球", "3球"], "changed": False,
            "change_reason": ""}), ["2球", "3球"])
        self.assertEqual(r["状态"], "ok")
        self.assertFalse(r["校验后变更"])

    def test_change_with_reason(self):
        r = validate_ttg_review(json.dumps({
            "ttg_review_picks": ["2球", "3球", "4球"], "changed": True,
            "change_reason": "市场 4球 赔率明显压低"}), ["2球", "3球"])
        self.assertTrue(r["校验后变更"])
        self.assertFalse(r["理由缺失"])

    def test_change_without_reason_flagged(self):
        r = validate_ttg_review(json.dumps({
            "ttg_review_picks": ["4球", "5球"], "changed": True,
            "change_reason": ""}), ["2球", "3球"])
        self.assertTrue(r["校验后变更"])
        self.assertTrue(r["理由缺失"])

    def test_claimed_no_change_but_differs(self):
        r = validate_ttg_review(json.dumps({
            "ttg_review_picks": ["1球", "2球"], "changed": False,
            "change_reason": ""}), ["2球", "3球"])
        self.assertTrue(r["校验后变更"])
        self.assertIn("[校验]", r["变更理由"])

    def test_blind_picks_not_mutated(self):
        blind = ["2球", "3球"]
        validate_ttg_review(json.dumps({
            "ttg_review_picks": ["4球"], "changed": True,
            "change_reason": "x"}), blind)
        self.assertEqual(blind, ["2球", "3球"])


class TestValidateIndependent(unittest.TestCase):
    """C2 独立判断组输出校验。"""

    def test_ok_single_pick(self):
        """候选数弹性：1 个合法候选即 ok。"""
        r = validate_ttg_independent(json.dumps({
            "ttg_independent_picks": ["2球"],
            "ttg_independent_reasoning": "首选比分进球数 2，无充分理由偏离",
            "ordered": True}))
        self.assertEqual(r["状态"], "ok")
        self.assertEqual(r["候选"], ["2球"])

    def test_ok_two_picks(self):
        r = validate_ttg_independent(json.dumps({
            "ttg_independent_picks": ["3球", "2球"],
            "ttg_independent_reasoning": "攻防均衡，3/2 球均可能",
            "ordered": True}))
        self.assertEqual(r["状态"], "ok")
        self.assertEqual(len(r["候选"]), 2)

    def test_ok_three_picks(self):
        r = validate_ttg_independent(json.dumps({
            "ttg_independent_picks": ["2球", "3球", "1球"],
            "ttg_independent_reasoning": "节奏慢，1-3 球区间均合理",
            "ordered": True}))
        self.assertEqual(r["状态"], "ok")

    def test_duplicate_auto_dedup(self):
        """重复候选被自动去重（_validate_picks 设计行为，不是报错）。"""
        r = validate_ttg_independent(json.dumps({
            "ttg_independent_picks": ["2球", "2球", "3球"],
            "ttg_independent_reasoning": "节奏慢，2-3 球均合理"}))
        self.assertEqual(r["状态"], "ok")
        self.assertEqual(r["候选"], ["2球", "3球"])  # 去重后

    def test_illegal_out_of_range(self):
        r = validate_ttg_independent(json.dumps({
            "ttg_independent_picks": ["很多球"]}))
        self.assertNotEqual(r["状态"], "ok")

    def test_illegal_too_many(self):
        r = validate_ttg_independent(json.dumps({
            "ttg_independent_picks": ["1球", "2球", "3球", "4球"]}))
        self.assertNotEqual(r["状态"], "ok")

    def test_api_error(self):
        r = validate_ttg_independent("__API_ERROR__: timeout")
        self.assertEqual(r["状态"], "api_error")

    def test_parse_error(self):
        r = validate_ttg_independent("这不是 JSON")
        self.assertNotEqual(r["状态"], "ok")


class TestEnableC2(unittest.TestCase):
    """C2 组开关逻辑：默认开，DISABLE_TTG_C2=1 时关。"""

    def test_default_enabled(self):
        import os as _os
        old = _os.environ.pop("DISABLE_TTG_C2", None)
        try:
            self.assertTrue(enable_ttg_c2())
        finally:
            if old is not None:
                _os.environ["DISABLE_TTG_C2"] = old

    def test_disabled_when_set(self):
        import os as _os
        old = _os.environ.get("DISABLE_TTG_C2")
        _os.environ["DISABLE_TTG_C2"] = "1"
        try:
            self.assertFalse(enable_ttg_c2())
        finally:
            if old is None:
                _os.environ.pop("DISABLE_TTG_C2", None)
            else:
                _os.environ["DISABLE_TTG_C2"] = old


class TestMarketTop3(unittest.TestCase):
    def test_order_and_tie(self):
        md = make_match_data()
        self.assertEqual(market_top3(md), ["3球", "2球", "1球"])
        # 并列：2球与1球同为 4.00，按规范顺序 1球(1) < 2球(2)，1球靠前
        md2 = {"总进球赔率": {"0球": "9.0", "1球": "4.00", "2球": "4.00",
                            "3球": "3.0", "4球": "8.0", "5球": "9.0",
                            "6球": "15.0", "7+球": "20.0"}}
        self.assertEqual(market_top3(md2), ["3球", "1球", "2球"])

    def test_missing_odds(self):
        self.assertEqual(market_top3({}), [])


class TestShadowSwitch(unittest.TestCase):
    def test_default_on(self):
        """默认开启，除非显式 DISABLE_TTG_SHADOW=1。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISABLE_TTG_SHADOW", None)
            self.assertTrue(ttg_shadow.shadow_enabled())

    def test_off_via_disable(self):
        with patch.dict(os.environ, {"DISABLE_TTG_SHADOW": "1"}):
            self.assertFalse(ttg_shadow.shadow_enabled())


class TestCollectRecord(unittest.TestCase):
    def _dispatcher(self, blind_text, review_text):
        def _d(url, headers=None, json=None, timeout=None, **kw):
            sys_prompt = json["messages"][0]["content"]
            if "总进球数盲判" in sys_prompt:
                return make_resp(blind_text)
            if "总进球市场复核" in sys_prompt:
                return make_resp(review_text)
            raise AssertionError("影子流程不应触发其他请求")
        return _d

    def test_success_abcd(self):
        blind = json.dumps({"ttg_blind_picks": ["2球", "3球"],
                            "ttg_blind_reasoning": "r", "ttg_blind_uncertainty": "",
                            "ordered": True})
        review = json.dumps({"ttg_review_picks": ["2球", "3球", "4球"],
                             "changed": True, "change_reason": "市场倾向"})
        parsed = {"总进球推荐": ["3球", "2球", "4球"],
                  "_原始快照": {"锁定核心剧本": {"primary_score": "2:1"}}}
        with patch("ttg_shadow.requests.post",
                   side_effect=self._dispatcher(blind, review)):
            rec = collect_shadow_record(ROW, HISTORY, make_match_data(),
                                        parsed, API_KEY, DATE)
        self.assertEqual(rec["A盲判"]["状态"], "ok")
        self.assertEqual(rec["A盲判"]["候选"], ["2球", "3球"])
        self.assertEqual(rec["B复核"]["状态"], "ok")
        self.assertTrue(rec["B复核"]["校验后变更"])
        self.assertEqual(rec["C正式"]["候选"], ["3球", "2球", "4球"])
        self.assertEqual(rec["D固定"]["候选"], ["2球", "3球", "4球"])
        self.assertEqual(rec["市场前三"], ["3球", "2球", "1球"])
        self.assertIsNone(rec["实际赛果"])

    def test_api_error_skips_review_and_never_raises(self):
        def _boom(url, headers=None, json=None, timeout=None, **kw):
            raise ttg_shadow.requests.RequestException("network down")
        parsed = {"总进球推荐": ["2球", "3球", "4球"], "_原始快照": {}}
        with patch("ttg_shadow.requests.post", side_effect=_boom):
            rec = collect_shadow_record(ROW, HISTORY, make_match_data(),
                                        parsed, API_KEY, DATE)
        self.assertEqual(rec["A盲判"]["状态"], "api_error")
        self.assertEqual(rec["B复核"]["状态"], "skipped")

    def test_parse_error(self):
        blind = "完全不是 JSON"
        review = json.dumps({"ttg_review_picks": ["2球"], "changed": True,
                             "change_reason": "x"})
        parsed = {"总进球推荐": ["2球"], "_原始快照": {}}
        with patch("ttg_shadow.requests.post",
                   side_effect=self._dispatcher(blind, review)):
            rec = collect_shadow_record(ROW, HISTORY, make_match_data(),
                                        parsed, API_KEY, DATE)
        self.assertEqual(rec["A盲判"]["状态"], "illegal")
        self.assertEqual(rec["B复核"]["状态"], "skipped")

    def test_official_objects_not_mutated(self):
        blind = json.dumps({"ttg_blind_picks": ["1球"], "ttg_blind_reasoning": "",
                            "ttg_blind_uncertainty": "", "ordered": True})
        review = json.dumps({"ttg_review_picks": ["1球"], "changed": False,
                             "change_reason": ""})
        md = make_match_data()
        parsed = {"总进球推荐": ["2球", "3球", "4球"],
                  "_原始快照": {"锁定核心剧本": {"primary_score": "2:1"}}}
        md_before = json.dumps(md, ensure_ascii=False, sort_keys=True)
        parsed_before = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        with patch("ttg_shadow.requests.post",
                   side_effect=self._dispatcher(blind, review)):
            collect_shadow_record(ROW, HISTORY, md, parsed, API_KEY, DATE)
        self.assertEqual(json.dumps(md, ensure_ascii=False, sort_keys=True), md_before)
        self.assertEqual(json.dumps(parsed, ensure_ascii=False, sort_keys=True),
                         parsed_before)


class TestWriteShadowFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ttg_shadow_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_new(self):
        stats = write_shadow_file([make_record(), make_record("周四002")],
                                  DATE, output_dir=self.tmp)
        self.assertEqual(stats["状态"], "写入成功")
        self.assertEqual(stats["新增数"], 2)
        self.assertEqual(stats["合并后总记录数"], 2)
        with open(shadow_file_path(DATE, self.tmp), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["场次"]), 2)

    def test_dedup_same_content(self):
        write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        stats = write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        self.assertEqual(stats["重复跳过数"], 1)
        self.assertEqual(stats["新增数"], 0)
        self.assertEqual(stats["合并后总记录数"], 1)

    def test_conflict_keeps_first(self):
        write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        changed = make_record()
        changed["A盲判"]["候选"] = ["5球"]
        stats = write_shadow_file([changed], DATE, output_dir=self.tmp)
        self.assertEqual(stats["冲突数"], 1)
        self.assertEqual(stats["新增数"], 0)
        with open(shadow_file_path(DATE, self.tmp), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["场次"][0]["A盲判"]["候选"], ["2球", "3球"])  # 首次保留
        self.assertEqual(len(data["冲突记录"]), 1)

    def test_corrupted_file_backup(self):
        path = shadow_file_path(DATE, self.tmp)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{损坏的json")
        stats = write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        self.assertEqual(stats["状态"], "写入成功")
        self.assertTrue(stats["备份路径"])
        self.assertTrue(os.path.exists(stats["备份路径"]))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["场次"]), 1)

    def test_corrupted_backup_failure_aborts(self):
        path = shadow_file_path(DATE, self.tmp)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{损坏的json")
        with patch("ttg_shadow.shutil.copy2", side_effect=OSError("disk full")):
            stats = write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        self.assertIn("备份失败", stats["状态"])
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{损坏的json")  # 原文件未被覆盖

    def test_lock_failure_skips(self):
        with patch("ttg_shadow.fcntl.flock", side_effect=OSError("locked")):
            stats = write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        self.assertIn("锁获取失败", stats["状态"])
        self.assertFalse(os.path.exists(shadow_file_path(DATE, self.tmp)))

    def test_tmp_same_dir_and_cleanup_on_replace_failure(self):
        seen = {}

        def fake_replace(src, dst):
            seen["src"] = src
            raise OSError("replace boom")
        path = shadow_file_path(DATE, self.tmp)
        with patch("ttg_shadow.os.replace", side_effect=fake_replace):
            stats = write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        self.assertIn("写入失败", stats["状态"])
        self.assertEqual(os.path.dirname(seen["src"]), self.tmp)  # 同目录临时文件
        self.assertFalse(os.path.exists(seen["src"]))             # 临时文件已清理
        self.assertFalse(os.path.exists(path))                    # 原文件状态保留（不存在）

    def test_invalid_record_rejected(self):
        bad = {"foo": "bar"}
        stats = write_shadow_file([bad], DATE, output_dir=self.tmp)
        self.assertEqual(stats["非法剔除数"], 1)
        self.assertEqual(stats["合并后总记录数"], 0)

    def test_counts_are_distinct(self):
        stats = write_shadow_file([make_record(), make_record("周四002")],
                                  DATE, output_dir=self.tmp, collected_count=2)
        for k in ("本次输入数", "成功收集数", "非法剔除数", "新增数",
                  "重复跳过数", "冲突数", "合并后总记录数"):
            self.assertIn(k, stats)


class TestMetrics(unittest.TestCase):
    def _entry(self, picks, actual, ordered=True, status="ok", top3=None):
        return {"候选": picks, "有序": ordered, "状态": status,
                "实际": actual, "市场前三": top3 or ["2球", "3球", "4球"]}

    def test_narrow_not_padded(self):
        m = group_metrics([self._entry(["2球", "3球"], "2球")])
        self.assertEqual(m["原始候选集合覆盖率"], 100.0)
        self.assertEqual(m["等宽3覆盖率"], 100.0)  # 窄集合按实际 2 项计
        self.assertEqual(m["窄集合占比"], 100.0)

    def test_wide_truncated_by_original_order(self):
        # 宽集合 4 项，实际命中第 4 项：原始覆盖命中，等宽-3 未命中
        m = group_metrics([self._entry(["1球", "2球", "3球", "4球"], "4球")])
        self.assertEqual(m["原始候选集合覆盖率"], 100.0)
        self.assertEqual(m["等宽3覆盖率"], 0.0)
        self.assertEqual(m["宽集合数"], 1)

    def test_unordered_excluded_from_first_and_eq3(self):
        m = group_metrics([
            self._entry(["2球", "3球", "4球"], "4球", ordered=False),
            self._entry(["2球", "3球", "4球"], "2球", ordered=True),
        ])
        self.assertEqual(m["无序输出数"], 1)
        self.assertEqual(m["原始候选集合覆盖率"], 100.0)   # 无序仍计入原始覆盖
        self.assertEqual(m["首选命中率"], 100.0)            # 仅有序 1 场
        self.assertEqual(m["等宽3覆盖率"], 100.0)

    def test_empty_and_illegal_rates(self):
        m = group_metrics([
            self._entry([], "2球", status="ok"),
            self._entry([], "2球", status="illegal"),
            self._entry(["2球"], "2球"),
        ])
        self.assertEqual(m["已结算场次数"], 3)
        self.assertEqual(m["空输出数"], 1)
        self.assertEqual(m["非法或失败输出数"], 1)
        self.assertEqual(m["可用结果覆盖率"], round(100 / 3, 2))
        self.assertEqual(m["原始候选集合覆盖率"], 100.0)

    def test_first_pick_hit(self):
        m = group_metrics([
            self._entry(["3球", "2球"], "2球"),
            self._entry(["2球", "3球"], "2球"),
        ])
        self.assertEqual(m["首选命中率"], 50.0)
        self.assertEqual(m["原始候选集合覆盖率"], 100.0)

    def test_evaluate_records(self):
        rec = make_record()
        rec["实际赛果"] = {"全场": "2-1", "总进球": "3球"}
        out = evaluate_records([rec])
        self.assertEqual(out["样本量"], 1)
        self.assertEqual(out["C正式"]["原始候选集合覆盖率"], 100.0)
        self.assertEqual(out["A盲判"]["原始候选集合覆盖率"], 100.0)
        self.assertIn("与赔率前三重合率_描述性", out["A盲判"])


if __name__ == "__main__":
    unittest.main()
