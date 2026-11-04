#!/usr/bin/env python3
"""半全场影子实验单元测试。全部使用 mock 与临时目录，不调用真实预测 API。

运行：python3 -m unittest test_hafu_shadow -v
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import analyze
import hafu_shadow
from hafu_shadow import (
    HAFU_LABELS, FIXED_BASELINE_PICKS,
    build_blind_input, build_market_info, market_top3, canon_hafu_code,
    validate_hafu_blind, validate_hafu_review,
    collect_shadow_record, write_shadow_file, shadow_file_path,
    evaluate_records,
)

ROW = {
    "编号": "周四001", "联赛": "测试联赛A",
    "主队": "阿尔法队", "主队排名": "[3]",
    "客队": "贝塔队", "客队排名": "[5]",
    "开赛日期": "2026-10-29", "开赛时间": "20:00",
    "让球数": "-1",
    "胜平负-主胜": "1.85",
    "让球-主胜": "2.10", "让球-平": "3.30", "让球-客胜": "3.00",
    "总进球-2球": "3.40", "总进球-3球": "3.20",
    "半全场-半主胜-全主胜": "5.60", "半全场-半平-全主胜": "4.80",
    "半全场-半客胜-全客胜": "4.80", "半全场-半平-全平": "6.50",
    "比分-2:0": "7.00", "比分-2:1": "8.50",
}
HISTORY = {"available": True, "home_recent_text": "主队近5场2胜",
           "away_recent_text": "客队近5场1胜", "h2h_text": "交锋主队占优"}
API_KEY = "sk-" + "a" * 16
DATE = "2026-10-29"

BLIND_TEXT = json.dumps({
    "hafu_blind_picks": ["HH", "DH"],
    "hafu_blind_reasoning": "主队开局强势",
    "hafu_blind_uncertainty": "", "ordered": True}, ensure_ascii=False)
REVIEW_TEXT = json.dumps({
    "hafu_review_picks": ["HH", "DH", "DD"],
    "changed": True, "change_reason": "市场 DD 低位补入"}, ensure_ascii=False)


def make_resp(text):
    r = Mock()
    r.raise_for_status = lambda: None
    r.json = lambda: {"choices": [{"message": {"content": text}}]}
    return r


def make_match_data():
    return analyze.build_match_prompt_data(ROW, HISTORY)


def make_parsed():
    return {
        "半全场推荐": ["HH", "DH"],
        "半全场理由": "[程序校验修正] 中文半全场描述已规范化转码为 HDA 代码。原理由：主队上半场压制",
        "半全场来源": "程序修正",
        "_原始快照": {
            "锁定核心剧本": {"primary_score": "2:0"},
            "原始玩法输出": {"半全场推荐": "hh/主胜-主胜/DH",
                            "半全场理由": "主队上半场压制"},
        },
    }


def make_record(num="周四001", a_picks=None):
    return {
        "对齐键": {"日期": DATE, "编号": num, "主队": "阿尔法队",
                   "客队": "贝塔队", "开赛": "2026-10-29 20:00"},
        "实验运行ID": "test-run", "写入时间": "2026-10-29T21:00:00",
        "A盲判": {"状态": "ok", "候选": a_picks or ["HH", "DH"], "理由": "r",
                  "不确定说明": "", "有序": True, "错误": "", "原始文本前缀": ""},
        "B复核": {"状态": "ok", "候选": ["HH", "DH", "DD"], "模型声明变更": True,
                  "校验后变更": True, "变更理由": "市场 DD 低位补入",
                  "理由缺失": False, "错误": "", "原始文本前缀": ""},
        "C正式": {"状态": "ok", "候选": ["HH", "DH"], "有序": True},
        "D固定": {"状态": "ok", "候选": list(FIXED_BASELINE_PICKS), "有序": True},
        "市场前三": ["DH", "AA", "HH"],
        "实际赛果": None, "影子错误": "",
    }


class TestBlindInputWhitelist(unittest.TestCase):
    def test_whitelist_fields_only(self):
        data = build_blind_input(ROW, HISTORY)
        allowed = {"编号", "联赛", "主队", "客队", "主队排名", "客队排名",
                   "开赛日期", "开赛时间", "对阵", "开赛", "历史数据"}
        self.assertEqual(set(data.keys()), allowed)
        blob = json.dumps(data, ensure_ascii=False)
        for banned in ("赔率", "5.60", "4.80", "1.85", "7.00",
                       "primary_score", "半全场推荐", "让球"):
            self.assertNotIn(banned, blob, f"盲判输入泄漏了 {banned}")

    def test_request_body_isolation(self):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None, **kw):
            captured["payload"] = json
            return make_resp(BLIND_TEXT)

        with patch("hafu_shadow.requests.post", side_effect=fake_post):
            hafu_shadow.call_deepseek_hafu_blind(build_blind_input(ROW, HISTORY), API_KEY)
        # 只检查 user 消息体中的数据部分（指令前缀自身含「无赔率」说明措辞）
        user_blob = captured["payload"]["messages"][1]["content"]
        data_blob = user_blob.split("\n\n", 1)[-1]
        for banned in ("赔率", "5.60", "4.80", "1.85", "7.00", "primary_score",
                       "match_script", "半全场推荐"):
            self.assertNotIn(banned, data_blob, f"实际请求体泄漏了 {banned}")


class TestCanon(unittest.TestCase):
    def test_nine_codes_complete(self):
        # 9 组合完备性：全部合法代码一一通过
        self.assertEqual(len(HAFU_LABELS), 9)
        for code in ("HH", "HD", "HA", "DH", "DD", "DA", "AH", "AD", "AA"):
            self.assertEqual(canon_hafu_code(code), code)
            self.assertEqual(canon_hafu_code(code.lower()), code)

    def test_invalid_rejected(self):
        for bad in ("HX", "主胜", "HHH", "H", "", None, "主胜-主胜"):
            self.assertIsNone(canon_hafu_code(bad))


class TestValidateBlind(unittest.TestCase):
    def test_ok(self):
        r = validate_hafu_blind(BLIND_TEXT)
        self.assertEqual(r["状态"], "ok")
        self.assertEqual(r["候选"], ["HH", "DH"])
        self.assertTrue(r["有序"])

    def test_empty_picks(self):
        txt = json.dumps({"hafu_blind_picks": [], "hafu_blind_reasoning": "",
                          "hafu_blind_uncertainty": "信息不足"})
        r = validate_hafu_blind(txt)
        self.assertEqual(r["状态"], "empty")
        self.assertEqual(r["候选"], [])

    def test_illegal_token(self):
        txt = json.dumps({"hafu_blind_picks": ["HH", "XX"],
                          "hafu_blind_reasoning": "r"})
        r = validate_hafu_blind(txt)
        self.assertEqual(r["状态"], "illegal")
        self.assertIn("非法", r["错误"])

    def test_illegal_too_many(self):
        txt = json.dumps({"hafu_blind_picks": ["HH", "DH", "DD", "AA", "HA"],
                          "hafu_blind_reasoning": "r"})
        r = validate_hafu_blind(txt)
        self.assertEqual(r["状态"], "illegal")
        self.assertIn("超过", r["错误"])

    def test_dedup_recorded(self):
        txt = json.dumps({"hafu_blind_picks": ["HH", "HH", "DH"],
                          "hafu_blind_reasoning": "r"})
        r = validate_hafu_blind(txt)
        self.assertEqual(r["状态"], "ok")
        self.assertEqual(r["候选"], ["HH", "DH"])
        self.assertIn("去重", r["不确定说明"])

    def test_api_error(self):
        r = validate_hafu_blind("__API_ERROR__: timeout")
        self.assertEqual(r["状态"], "api_error")

    def test_parse_error(self):
        r = validate_hafu_blind("这不是 JSON")
        self.assertEqual(r["状态"], "illegal")
        self.assertTrue(r["错误"])


class TestValidateReview(unittest.TestCase):
    def test_no_change(self):
        txt = json.dumps({"hafu_review_picks": ["HH", "DH"],
                          "changed": False, "change_reason": ""})
        r = validate_hafu_review(txt, ["HH", "DH"])
        self.assertEqual(r["状态"], "ok")
        self.assertFalse(r["校验后变更"])

    def test_changed_requires_reason(self):
        txt = json.dumps({"hafu_review_picks": ["HH", "DD"],
                          "changed": True, "change_reason": ""})
        r = validate_hafu_review(txt, ["HH", "DH"])
        self.assertEqual(r["状态"], "ok")
        self.assertTrue(r["校验后变更"])
        self.assertTrue(r["理由缺失"])

    def test_undeclared_change_detected(self):
        txt = json.dumps({"hafu_review_picks": ["HH", "DD"],
                          "changed": False, "change_reason": ""})
        r = validate_hafu_review(txt, ["HH", "DH"])
        self.assertTrue(r["校验后变更"])
        self.assertIn("[校验]", r["变更理由"])

    def test_empty_picks_rejected(self):
        # B 组不允许弃选后覆盖盲判
        txt = json.dumps({"hafu_review_picks": [],
                          "changed": True, "change_reason": "全部放弃"})
        r = validate_hafu_review(txt, ["HH", "DH"])
        self.assertEqual(r["状态"], "illegal")


class TestMarketTop3(unittest.TestCase):
    def test_ascending_order(self):
        md = make_match_data()
        top3 = market_top3(md)
        # 赔率：DH=4.80，AA=4.80，HH=5.60，DD=6.50；并列按 HH→AA 顺序裁决
        self.assertEqual(top3, ["DH", "AA", "HH"])

    def test_tie_break_canonical_order(self):
        md = {"半全场赔率": {"半客胜-全客胜": "4.80", "半平-全主胜": "4.80"}}
        # AA 规范序在 DH 之后？规范序为 HH,HD,HA,DH,DD,DA,AH,AD,AA → DH 靠前
        self.assertEqual(market_top3(md), ["DH", "AA"])

    def test_missing_odds(self):
        self.assertEqual(market_top3({}), [])
        self.assertEqual(market_top3({"半全场赔率": {"半主胜-全主胜": "abc"}}), [])


class TestBuildCOfficial(unittest.TestCase):
    def test_three_layer_record(self):
        rec = hafu_shadow._build_c_official(make_parsed())
        self.assertEqual(rec["状态"], "ok")
        # 原始层：快照原始输出原文保留；可规范化的代码被转码，中文描述不转
        self.assertEqual(rec["原始候选"], ["HH", "DH"])
        self.assertEqual(rec["原始推荐原文"], "hh/主胜-主胜/DH")
        self.assertEqual(rec["原始理由"], "主队上半场压制")
        self.assertEqual(rec["半全场来源"], "程序修正")
        # 最终层
        self.assertEqual(rec["候选"], ["HH", "DH"])
        self.assertIn("程序校验修正", rec["最终理由"])

    def test_empty_official(self):
        rec = hafu_shadow._build_c_official({"半全场推荐": [], "_原始快照": {}})
        self.assertEqual(rec["状态"], "empty")
        self.assertEqual(rec["候选"], [])


class TestCollectRecord(unittest.TestCase):
    def test_collect_all_groups(self):
        with patch("hafu_shadow.requests.post",
                   side_effect=[make_resp(BLIND_TEXT), make_resp(REVIEW_TEXT)]):
            rec = collect_shadow_record(ROW, HISTORY, make_match_data(),
                                        make_parsed(), API_KEY, DATE)
        self.assertEqual(rec["A盲判"]["状态"], "ok")
        self.assertEqual(rec["A盲判"]["候选"], ["HH", "DH"])
        self.assertEqual(rec["B复核"]["状态"], "ok")
        self.assertEqual(rec["B复核"]["候选"], ["HH", "DH", "DD"])
        self.assertTrue(rec["B复核"]["校验后变更"])
        # C 组三层记录
        self.assertEqual(rec["C正式"]["状态"], "ok")
        self.assertEqual(rec["C正式"]["原始推荐原文"], "hh/主胜-主胜/DH")
        self.assertEqual(rec["C正式"]["候选"], ["HH", "DH"])
        # D 组固定基线
        self.assertEqual(rec["D固定"]["候选"], ["HH", "DD", "AA"])
        # 市场组
        self.assertEqual(rec["市场前三"], ["DH", "AA", "HH"])
        self.assertIsNone(rec["实际赛果"])

    def test_blind_api_error_skips_review(self):
        with patch("hafu_shadow.requests.post",
                   side_effect=hafu_shadow.requests.RequestException("boom")):
            rec = collect_shadow_record(ROW, HISTORY, make_match_data(),
                                        make_parsed(), API_KEY, DATE)
        self.assertEqual(rec["A盲判"]["状态"], "api_error")
        self.assertEqual(rec["B复核"]["状态"], "skipped")
        # C/D/市场 不受 API 失败影响
        self.assertEqual(rec["C正式"]["状态"], "ok")
        self.assertEqual(rec["D固定"]["状态"], "ok")


class TestWriteShadowFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_write_and_idempotent_merge(self):
        stats1 = write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        self.assertEqual(stats1["状态"], "写入成功")
        self.assertEqual(stats1["新增数"], 1)
        stats2 = write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        self.assertEqual(stats2["重复跳过数"], 1)
        self.assertEqual(stats2["新增数"], 0)
        with open(shadow_file_path(DATE, self.tmp), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["场次"]), 1)

    def test_conflict_keeps_first(self):
        rec1 = make_record()
        rec2 = make_record()
        rec2["A盲判"]["候选"] = ["DD", "HH"]
        write_shadow_file([rec1], DATE, output_dir=self.tmp)
        stats = write_shadow_file([rec2], DATE, output_dir=self.tmp)
        self.assertEqual(stats["冲突数"], 1)
        with open(shadow_file_path(DATE, self.tmp), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["场次"][0]["A盲判"]["候选"], ["HH", "DH"])
        self.assertEqual(len(data["冲突记录"]), 1)

    def test_invalid_record_rejected(self):
        stats = write_shadow_file([{"对齐键": {"日期": DATE}}], DATE,
                                  output_dir=self.tmp)
        self.assertEqual(stats["非法剔除数"], 1)
        self.assertEqual(stats["新增数"], 0)


class TestEvaluate(unittest.TestCase):
    def test_metrics(self):
        rec = make_record()
        rec["实际赛果"] = {"全场": "2-0", "半全场": "HH"}
        out = evaluate_records([rec])
        self.assertEqual(out["样本量"], 1)
        self.assertEqual(out["A盲判"]["首选命中率"], 100.0)
        self.assertEqual(out["A盲判"]["等宽3覆盖率"], 100.0)
        self.assertEqual(out["D固定"]["首选命中率"], 100.0)
        # 九组合候选分布键完备
        self.assertEqual(set(out["A盲判"]["九组合候选分布"].keys()),
                         set(HAFU_LABELS))

    def test_unsettled_excluded(self):
        out = evaluate_records([make_record()])
        self.assertEqual(out["样本量"], 0)


if __name__ == "__main__":
    unittest.main()
