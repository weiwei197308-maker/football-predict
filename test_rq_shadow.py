#!/usr/bin/env python3
"""让球影子实验单元测试。全部使用 mock 与临时目录，不调用真实预测 API。

运行：python3 -m unittest test_rq_shadow -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import analyze
import rq_shadow
from rq_shadow import (
    build_blind_input, build_market_info, rq_market_rank, canon_rq_direction,
    validate_rq_blind, validate_rq_review,
    collect_shadow_record, write_shadow_file, shadow_file_path,
    group_metrics, evaluate_records,
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
    "半全场-半主胜-全主胜": "5.60",
    "比分-2:0": "7.00", "比分-2:1": "8.50",
}
HISTORY = {"available": True, "home_recent_text": "主队近5场2胜",
           "away_recent_text": "客队近5场1胜", "h2h_text": "交锋主队占优"}
API_KEY = "sk-" + "a" * 16
DATE = "2026-10-29"

PROBS_TEXT = json.dumps({
    "net_goal_probabilities": {"主胜2+": 0.30, "主胜1": 0.25, "平": 0.20,
                               "客胜1": 0.15, "客胜2+": 0.10},
    "rq_blind_reasoning": "主队攻强守稳", "rq_blind_uncertainty": "",
    "sufficient": True}, ensure_ascii=False)


def make_resp(text):
    r = Mock()
    r.raise_for_status = lambda: None
    r.json = lambda: {"choices": [{"message": {"content": text}}]}
    return r


def make_match_data():
    return analyze.build_match_prompt_data(ROW, HISTORY)


def make_parsed():
    return {
        "让球推荐": "让-1主胜", "让球次选": "让-1平",
        "让球推导版本": "v2_净胜球分布",
        "概率来源": "模型核心剧本 net_goal_probabilities（经程序五项校验与归一化）",
        "净胜球概率分布": {"主胜2+": 0.4, "主胜1": 0.3, "平": 0.15,
                           "客胜1": 0.1, "客胜2+": 0.05},
        "让球结果概率": {"exact": True,
                          "probabilities": {"主胜": 0.55, "平": 0.30, "客胜": 0.15}},
        "_原始快照": {"锁定核心剧本": {"primary_score": "2:0"}},
    }


def make_record(num="周四001", a_picks=None):
    rec = {
        "对齐键": {"日期": DATE, "编号": num, "主队": "阿尔法队",
                   "客队": "贝塔队", "开赛": "2026-10-29 20:00"},
        "实验运行ID": "test-run", "写入时间": "2026-10-29T21:00:00",
        "体彩让球数": "-1",
        "A盲判": {"状态": "ok", "净胜球概率": None, "概率校验": {}, "让球映射": {},
                  "主推": (a_picks or ["主胜", "平"])[0],
                  "次选": (a_picks or ["主胜", "平"])[1] if len(a_picks or ["主胜", "平"]) > 1 else "",
                  "建议双选": False, "候选": a_picks or ["主胜", "平"],
                  "理由": "r", "不确定说明": "", "有序": True,
                  "错误": "", "原始文本前缀": ""},
        "B复核": {"状态": "ok", "净胜球概率": None, "概率校验": {}, "让球映射": {},
                  "主推": "主胜", "次选": "平", "建议双选": False,
                  "候选": ["主胜", "平"], "理由": "", "不确定说明": "",
                  "有序": True, "错误": "", "原始文本前缀": "",
                  "模型声明变更": False, "校验后变更": False,
                  "变更理由": "", "理由缺失": False},
        "C正式": {"状态": "ok", "候选": ["主胜", "平"], "有序": True},
        "D规则": {"状态": "ok", "候选": ["主胜"], "有序": True},
        "市场倾向": {"候选": ["主胜", "客胜", "平"], "赔率": {}},
        "实际赛果": None, "影子错误": "",
    }
    return rec


class TestBlindInputWhitelist(unittest.TestCase):
    def test_whitelist_fields_only(self):
        data = build_blind_input(ROW, HISTORY)
        allowed = {"编号", "联赛", "主队", "客队", "主队排名", "客队排名",
                   "开赛日期", "开赛时间", "让球数", "体彩让球数",
                   "对阵", "开赛", "历史数据"}
        self.assertEqual(set(data.keys()), allowed)
        self.assertEqual(data["体彩让球数"], "-1")
        blob = json.dumps(data, ensure_ascii=False)
        for banned in ("赔率", "2.10", "3.30", "1.85", "7.00",
                       "primary_score", "net_goal"):
            self.assertNotIn(banned, blob, f"盲判输入泄漏了 {banned}")

    def test_request_body_isolation(self):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None, **kw):
            captured["payload"] = json
            return make_resp(globals()["json"].dumps({
                "net_goal_probabilities": {"主胜2+": 0.3, "主胜1": 0.25, "平": 0.2,
                                           "客胜1": 0.15, "客胜2+": 0.1},
                "rq_blind_reasoning": "r", "rq_blind_uncertainty": "",
                "sufficient": True}, ensure_ascii=False))

        with patch("rq_shadow.requests.post", side_effect=fake_post):
            rq_shadow.call_deepseek_rq_blind(build_blind_input(ROW, HISTORY), API_KEY)
        # 只检查 user 消息体中的数据部分（指令前缀自身含「无赔率」说明措辞）
        user_blob = captured["payload"]["messages"][1]["content"]
        data_blob = user_blob.split("\n\n", 1)[-1]
        for banned in ("赔率", "2.10", "3.30", "1.85", "7.00", "primary_score",
                       "match_script", "net_goal_interval"):
            self.assertNotIn(banned, data_blob, f"实际请求体泄漏了 {banned}")
        self.assertIn("让球", user_blob)  # 让球数作为比赛条件必须出现


class TestValidateBlind(unittest.TestCase):
    def test_ok_mapping(self):
        r = validate_rq_blind(PROBS_TEXT, "-1")
        self.assertEqual(r["状态"], "ok")
        # hc=-1：主胜2+→主胜(0.30)，主胜1→平(0.25)，平/客胜1/客胜2+→客胜(0.45)
        self.assertEqual(r["主推"], "客胜")
        self.assertEqual(r["次选"], "主胜")
        self.assertEqual(r["候选"], ["客胜", "主胜"])
        self.assertAlmostEqual(sum(r["净胜球概率"].values()), 1.0)
        self.assertTrue(r["让球映射"]["exact"])

    def test_empty_when_insufficient(self):
        txt = json.dumps({"net_goal_probabilities": None, "sufficient": False,
                          "rq_blind_reasoning": "", "rq_blind_uncertainty": "信息不足"})
        r = validate_rq_blind(txt, "-1")
        self.assertEqual(r["状态"], "empty")
        self.assertEqual(r["候选"], [])

    def test_illegal_bad_sum(self):
        txt = json.dumps({"net_goal_probabilities": {
            "主胜2+": 0.30, "主胜1": 0.25, "平": 0.20, "客胜1": 0.10, "客胜2+": 0.05},
            "rq_blind_reasoning": "r"})
        r = validate_rq_blind(txt, "-1")
        self.assertEqual(r["状态"], "illegal")
        self.assertIn("合计", r["错误"])

    def test_illegal_missing_bucket(self):
        txt = json.dumps({"net_goal_probabilities": {
            "主胜2+": 0.5, "主胜1": 0.5},
            "rq_blind_reasoning": "r"})
        r = validate_rq_blind(txt, "-1")
        self.assertEqual(r["状态"], "illegal")

    def test_api_error(self):
        r = validate_rq_blind("__API_ERROR__: timeout", "-1")
        self.assertEqual(r["状态"], "api_error")

    def test_unparseable_handicap(self):
        r = validate_rq_blind(PROBS_TEXT, "让球X")
        self.assertEqual(r["状态"], "illegal")
        self.assertIn("让球数", r["错误"])


class TestValidateReview(unittest.TestCase):
    def test_no_change(self):
        r = validate_rq_review(json.dumps({
            "net_goal_probabilities": {"主胜2+": 0.30, "主胜1": 0.25, "平": 0.20,
                                       "客胜1": 0.15, "客胜2+": 0.10},
            "changed": False, "change_reason": ""}, ensure_ascii=False),
            ["客胜", "主胜"], "-1")
        self.assertEqual(r["状态"], "ok")
        self.assertFalse(r["校验后变更"])

    def test_changed_requires_reason(self):
        txt = json.dumps({
            "net_goal_probabilities": {"主胜2+": 0.50, "主胜1": 0.20, "平": 0.15,
                                       "客胜1": 0.10, "客胜2+": 0.05},
            "changed": True, "change_reason": ""}, ensure_ascii=False)
        r = validate_rq_review(txt, ["客胜", "主胜"], "-1")
        self.assertEqual(r["状态"], "ok")
        self.assertTrue(r["校验后变更"])
        self.assertTrue(r["理由缺失"])

    def test_undeclared_change_detected(self):
        txt = json.dumps({
            "net_goal_probabilities": {"主胜2+": 0.50, "主胜1": 0.20, "平": 0.15,
                                       "客胜1": 0.10, "客胜2+": 0.05},
            "changed": False, "change_reason": ""}, ensure_ascii=False)
        r = validate_rq_review(txt, ["客胜", "主胜"], "-1")
        self.assertTrue(r["校验后变更"])
        self.assertIn("[校验]", r["变更理由"])


class TestMarketRank(unittest.TestCase):
    def test_tie_break_order(self):
        md = {"让球赔率": {"让球数": "-1", "主胜": "2.00", "平": "3.40", "客胜": "3.40"}}
        ranked = rq_market_rank(md)
        self.assertEqual([d for d, _ in ranked], ["主胜", "平", "客胜"])  # 并列按 主胜→平→客胜

    def test_missing_odds(self):
        self.assertEqual(rq_market_rank({}), [])
        self.assertEqual(rq_market_rank({"让球赔率": {"主胜": "abc"}}), [])


class TestCanon(unittest.TestCase):
    def test_direction_forms(self):
        self.assertEqual(canon_rq_direction("主胜"), "主胜")
        self.assertEqual(canon_rq_direction("让-1平"), "平")
        self.assertEqual(canon_rq_direction("让+2客胜"), "客胜")
        self.assertIsNone(canon_rq_direction("让球无法判定"))
        self.assertIsNone(canon_rq_direction(""))


class TestCollectRecord(unittest.TestCase):
    def test_collect_all_groups(self):
        review_text = json.dumps({
            "net_goal_probabilities": {"主胜2+": 0.30, "主胜1": 0.25, "平": 0.20,
                                       "客胜1": 0.15, "客胜2+": 0.10},
            "changed": False, "change_reason": ""}, ensure_ascii=False)
        with patch("rq_shadow.requests.post",
                   side_effect=[make_resp(PROBS_TEXT), make_resp(review_text)]):
            rec = collect_shadow_record(ROW, HISTORY, make_match_data(),
                                        make_parsed(), API_KEY, DATE)
        self.assertEqual(rec["A盲判"]["状态"], "ok")
        self.assertEqual(rec["A盲判"]["候选"], ["客胜", "主胜"])
        self.assertEqual(rec["B复核"]["状态"], "ok")
        # C 组原样记录
        self.assertEqual(rec["C正式"]["让球推荐"], "让-1主胜")
        self.assertEqual(rec["C正式"]["让球次选"], "让-1平")
        self.assertEqual(rec["C正式"]["候选"], ["主胜", "平"])
        self.assertEqual(rec["C正式"]["让球推导版本"], "v2_净胜球分布")
        self.assertIsNotNone(rec["C正式"]["净胜球概率分布"])
        # D 组 v1：首选比分 2:0 + 让-1 → 让-1主胜 → 主胜
        self.assertEqual(rec["D规则"]["状态"], "ok")
        self.assertEqual(rec["D规则"]["候选"], ["主胜"])
        self.assertEqual(rec["D规则"]["让球推荐"], "让-1主胜")
        # 市场组
        self.assertEqual(rec["市场倾向"]["候选"][0], "主胜")  # 让球-主胜 2.10 最低
        self.assertIsNone(rec["实际赛果"])

    def test_blind_api_error_skips_review(self):
        with patch("rq_shadow.requests.post",
                   side_effect=rq_shadow.requests.RequestException("boom")):
            rec = collect_shadow_record(ROW, HISTORY, make_match_data(),
                                        make_parsed(), API_KEY, DATE)
        self.assertEqual(rec["A盲判"]["状态"], "api_error")
        self.assertEqual(rec["B复核"]["状态"], "skipped")
        # C/D/市场 不受 API 失败影响
        self.assertEqual(rec["C正式"]["状态"], "ok")
        self.assertEqual(rec["D规则"]["状态"], "ok")


class TestWriteShadowFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_write_and_idempotent_merge(self):
        stats1 = write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        self.assertEqual(stats1["状态"], "写入成功")
        self.assertEqual(stats1["新增数"], 1)
        # 同内容重跑：重复跳过
        stats2 = write_shadow_file([make_record()], DATE, output_dir=self.tmp)
        self.assertEqual(stats2["重复跳过数"], 1)
        self.assertEqual(stats2["新增数"], 0)
        with open(shadow_file_path(DATE, self.tmp), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["场次"]), 1)

    def test_conflict_keeps_first(self):
        rec1 = make_record()
        rec2 = make_record()
        rec2["A盲判"]["候选"] = ["平", "主胜"]
        write_shadow_file([rec1], DATE, output_dir=self.tmp)
        stats = write_shadow_file([rec2], DATE, output_dir=self.tmp)
        self.assertEqual(stats["冲突数"], 1)
        with open(shadow_file_path(DATE, self.tmp), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["场次"][0]["A盲判"]["候选"], ["主胜", "平"])
        self.assertEqual(len(data["冲突记录"]), 1)

    def test_invalid_record_rejected(self):
        bad = {"对齐键": {"日期": DATE}}
        stats = write_shadow_file([bad], DATE, output_dir=self.tmp)
        self.assertEqual(stats["非法剔除数"], 1)
        self.assertEqual(stats["新增数"], 0)


class TestEvaluate(unittest.TestCase):
    def test_metrics(self):
        rec = make_record()
        rec["实际赛果"] = {"全场": "2-0", "让球": "主胜"}
        out = evaluate_records([rec])
        self.assertEqual(out["样本量"], 1)
        self.assertEqual(out["A盲判"]["首选命中率"], 100.0)
        self.assertEqual(out["A盲判"]["主推次选覆盖率"], 100.0)
        self.assertEqual(out["D规则"]["首选命中率"], 100.0)

    def test_unsettled_excluded(self):
        out = evaluate_records([make_record()])
        self.assertEqual(out["样本量"], 0)


class TestSettleIntegration(unittest.TestCase):
    """settle_shadow.py 三玩法一次结算：让球/半全场回填 + 总进球行为不变。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "output"), exist_ok=True)

    def _write(self, path, obj):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def test_settle_three_plays(self):
        import ttg_shadow
        import hafu_shadow
        out_dir = os.path.join(self.tmp, "output")

        rq_rec = make_record()
        self._write(rq_shadow.shadow_file_path(DATE, out_dir),
                    {"实验日期": DATE, "版本": 1, "场次": [rq_rec], "冲突记录": []})
        hafu_rec = {
            "对齐键": dict(rq_rec["对齐键"]), "实验运行ID": "test-run",
            "写入时间": "2026-10-29T21:00:00",
            "A盲判": {"状态": "ok", "候选": ["HH", "DH"], "理由": "r",
                      "不确定说明": "", "有序": True, "错误": "", "原始文本前缀": ""},
            "B复核": {"状态": "ok", "候选": ["HH", "DH"], "模型声明变更": False,
                      "校验后变更": False, "变更理由": "", "理由缺失": False,
                      "错误": "", "原始文本前缀": ""},
            "C正式": {"状态": "ok", "候选": ["HH"], "有序": True},
            "D固定": {"状态": "ok", "候选": ["HH", "DD", "AA"], "有序": True},
            "市场前三": ["DH", "AA", "HH"],
            "实际赛果": None, "影子错误": "",
        }
        self._write(hafu_shadow.shadow_file_path(DATE, out_dir),
                    {"实验日期": DATE, "版本": 1, "场次": [hafu_rec], "冲突记录": []})
        self._write(os.path.join(out_dir, f"每日记录_{DATE}.json"),
                    {"复盘日期": DATE, "场次": [
                        {"编号": "周四001", "赛果": {
                            "半场": "1-0", "全场": "2-0", "胜平负": "主胜",
                            "让球": "主胜", "总进球": "2球", "半全场": "HH",
                            "比分": "2:0"}}]})

        env = dict(os.environ)
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "settle_shadow.py")
        env["PYTHONPATH"] = os.path.dirname(script)
        cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            r1 = subprocess.run([sys.executable, script, DATE],
                                capture_output=True, text=True, env=env)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertIn("[让球]", r1.stdout)
            self.assertIn("[半全场]", r1.stdout)

            with open(rq_shadow.shadow_file_path(DATE, out_dir), encoding="utf-8") as f:
                rq_after = json.load(f)
            self.assertEqual(rq_after["场次"][0]["实际赛果"]["让球"], "主胜")
            with open(hafu_shadow.shadow_file_path(DATE, out_dir), encoding="utf-8") as f:
                hafu_after = json.load(f)
            self.assertEqual(hafu_after["场次"][0]["实际赛果"]["半全场"], "HH")

            # 幂等：二次运行新增 0
            r2 = subprocess.run([sys.executable, script, DATE],
                                capture_output=True, text=True, env=env)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("新增赛果 0", r2.stdout)
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
