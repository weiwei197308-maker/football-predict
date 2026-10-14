"""compare_report.py 与 API Key 安全退出的单元测试。

覆盖任务书要求：
- 体彩让球边界（-1/-2/+1/+2）
- 半全场过滤（冲突项删除可见）
- 总进球修正（补入项可见且不改变原列表）
- 列表格式输入
- 缺失数据（标记无法判定而非默认通过）
- 备选比分冲突（明确提示，不静默忽略）
- 原始预测不被覆盖
- API Key 缺失/格式错误时的安全退出
- 对照报告统计准确
"""
import copy
import csv
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import analyze
from analyze import post_process_prediction, validate_api_key
from compare_report import (
    build_match_comparison,
    build_report_text,
    build_snapshot_comparison,
    canon_ttg,
    format_summary,
    generate_compare_report,
    load_analysis_rows,
    load_snapshot,
    parse_teams,
    rq_direction,
    row_to_prediction,
    split_rec,
    summarize,
)
from lottery_rules import derive_handicap_recommendation


def make_row(**kw):
    row = {
        "编号": "周日001", "联赛": "测试联赛", "对阵": "主队A[测1] vs 客队B[测2]",
        "开赛": "2026-10-11 20:00:00",
        "胜平负推荐": "主胜", "胜平负理由": "理由",
        "让球推荐": "让-1主胜", "让球理由": "理由",
        "总进球推荐": "2球 / 3球", "总进球理由": "理由",
        "半全场推荐": "HH / DH", "半全场理由": "理由",
        "比分推荐": "2:0 / 2:1", "比分理由": "理由",
        "信心指数": "7", "综合分析": "", "历史数据": "",
        "核心剧本": "测试剧本", "净胜球分布": "主胜1", "体彩让球数": "-1",
        "数据缺失说明": "", "一致性检查": "",
    }
    row.update(kw)
    return row


class TestHandicapBoundaries(unittest.TestCase):
    """体彩让球边界：主队让 1/2 球与受让 1/2 球。"""

    def test_handicap_minus_1(self):
        cases = [("2:0", "让-1主胜"), ("1:0", "让-1平"), ("1:1", "让-1客胜"), ("0:1", "让-1客胜")]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(derive_handicap_recommendation(score, "-1"), expected)

    def test_handicap_minus_2(self):
        cases = [("3:0", "让-2主胜"), ("2:0", "让-2平"), ("1:0", "让-2客胜"), ("2:1", "让-2客胜")]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(derive_handicap_recommendation(score, "-2"), expected)

    def test_handicap_plus_1(self):
        cases = [("0:0", "让+1主胜"), ("1:0", "让+1主胜"), ("0:1", "让+1平"), ("0:2", "让+1客胜")]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(derive_handicap_recommendation(score, "+1"), expected)

    def test_handicap_plus_2(self):
        cases = [("0:1", "让+2主胜"), ("1:1", "让+2主胜"), ("0:2", "让+2平"), ("0:3", "让+2客胜")]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(derive_handicap_recommendation(score, "+2"), expected)

    def test_handicap_invalid(self):
        self.assertEqual(derive_handicap_recommendation("2:0", ""), "让球无法判定")
        self.assertEqual(derive_handicap_recommendation("2:0", "abc"), "让球无法判定")
        self.assertEqual(derive_handicap_recommendation("", "-1"), "让球无法判定")


class TestBasicParsers(unittest.TestCase):

    def test_split_rec_string(self):
        self.assertEqual(split_rec("2球 / 3球 / 4球"), ["2球", "3球", "4球"])
        self.assertEqual(split_rec("HH,DH，AH"), ["HH", "DH", "AH"])
        self.assertEqual(split_rec(""), [])

    def test_split_rec_list_input(self):
        self.assertEqual(split_rec(["2球", " 3球 "]), ["2球", "3球"])
        self.assertEqual(split_rec(("HH", "DH")), ["HH", "DH"])
        self.assertEqual(split_rec([]), [])

    def test_canon_ttg(self):
        # 规范化 + 去重：7+ / 7球 / 10 都归并为 7+球，且只保留一个
        self.assertEqual(canon_ttg(["2球", "3", "7+", "7球", "10"]), ["2球", "3球", "7+球"])
        self.assertEqual(canon_ttg(["abc"]), ["abc"])

    def test_parse_teams(self):
        self.assertEqual(parse_teams("乌德勒支[荷甲15] vs 威廉二世[荷甲18]"), ("乌德勒支", "威廉二世"))
        self.assertEqual(parse_teams("主队 vs 客队"), ("主队", "客队"))

    def test_rq_direction(self):
        self.assertEqual(rq_direction("让-1主胜"), "主胜")
        self.assertEqual(rq_direction("让+2平"), "平")
        self.assertEqual(rq_direction("让球无法判定"), None)


class TestBuildMatchComparison(unittest.TestCase):

    def test_hafu_filter_shows_removed(self):
        row = make_row(
            胜平负推荐="客胜", 净胜球分布="客胜1",
            比分推荐="1:2 / 0:1", 让球推荐="让-1客胜",
            总进球推荐="3球 / 2球", 半全场推荐="DA / AA / DD",
        )
        comp = build_match_comparison(row)
        self.assertTrue(comp["半全场"]["被修正"])
        self.assertEqual(comp["半全场"]["删除项"], ["DD"])
        self.assertEqual(comp["半全场"]["修正后"], ["DA", "AA"])
        self.assertEqual(comp["半全场"]["修正前"], ["DA", "AA", "DD"])

    def test_ttg_fix_appended_not_prepend(self):
        row = make_row(
            比分推荐="2:1 / 1:0", 让球推荐="让-1平",
            总进球推荐="2球 / 4球", 净胜球分布="主胜1",
        )
        comp = build_match_comparison(row)
        self.assertTrue(comp["总进球"]["被修正"])
        self.assertEqual(comp["总进球"]["补入项"], ["3球"])
        # 补入项必须在末尾，不得制造优先假象
        self.assertEqual(comp["总进球"]["修正后"][-1], "3球")
        self.assertEqual(comp["总进球"]["修正前"], ["2球", "4球"])

    def test_rq_correction_and_correct_result(self):
        row = make_row(比分推荐="2:1 / 1:0", 让球推荐="让-1主胜", 净胜球分布="主胜1")
        comp = build_match_comparison(row)
        self.assertTrue(comp["让球"]["被修正"])
        self.assertEqual(comp["让球"]["修正前"], "让-1主胜")
        self.assertEqual(comp["让球"]["修正后"], "让-1平")
        self.assertEqual(comp["让球"]["规则计算正确结果"], "让-1平")

    def test_alt_score_conflict_surfaced(self):
        row = make_row(比分推荐="2:0 / 0:1", 净胜球分布="主胜2+")
        comp = build_match_comparison(row)
        self.assertTrue(comp["备选比分冲突"])
        joined = "；".join(comp["备选比分冲突"])
        self.assertIn("0:1", joined)
        self.assertIn("替代剧本", joined)

    def test_missing_handicap_marked_unjudgeable(self):
        # 其余字段保持一致，仅让球数缺失：必须标记无法判定而非默认通过
        row = make_row(
            体彩让球数="", 让球推荐="", 数据缺失说明="让球数缺失",
            比分推荐="1:0 / 2:1", 总进球推荐="1球 / 2球",
        )
        comp = build_match_comparison(row)
        self.assertEqual(comp["一致性检查"]["修正后状态"], "无法判定")
        self.assertEqual(comp["让球"]["修正后"], "让球无法判定")
        self.assertEqual(comp["让球"]["规则计算正确结果"], "让球无法判定")

    def test_missing_score_unjudgeable(self):
        row = make_row(比分推荐="", 让球推荐="让-1主胜")
        comp = build_match_comparison(row)
        chk = comp["一致性检查"]
        self.assertTrue(chk["修正前无法判定"] or chk["修正后状态"] in ("无法判定", "仍有冲突"))

    def test_consistent_row_passes(self):
        row = make_row(
            比分推荐="1:0 / 2:1", 让球推荐="让-1平",
            总进球推荐="1球 / 2球", 半全场推荐="HH / DH",
            净胜球分布="主胜1",
        )
        comp = build_match_comparison(row)
        self.assertFalse(comp["让球"]["被修正"])
        self.assertFalse(comp["半全场"]["被修正"])
        self.assertFalse(comp["总进球"]["被修正"])
        self.assertEqual(comp["一致性检查"]["修正后状态"], "通过")
        self.assertEqual(comp["备选比分冲突"], [])

    def test_original_row_not_modified(self):
        row = make_row(
            胜平负推荐="客胜", 净胜球分布="客胜1",
            比分推荐="1:2 / 1:1", 让球推荐="让-1主胜",
            半全场推荐="DA / AA / DD", 总进球推荐="2球 / 4球",
        )
        snapshot = copy.deepcopy(row)
        build_match_comparison(row)
        self.assertEqual(row, snapshot)

    def test_post_process_does_not_mutate_input(self):
        pred = row_to_prediction(make_row(让球推荐="让-1主胜", 比分推荐="2:1 / 1:0"))
        snapshot = copy.deepcopy(pred)
        post_process_prediction(pred, "-1")
        self.assertEqual(pred, snapshot)

    def test_list_format_csv_fields(self):
        """CSV 字段本身为列表时也能正常构建对照（列表格式输入）。"""
        row = make_row()
        row["总进球推荐"] = ["2球", "3球"]
        row["半全场推荐"] = ["HH", "DH"]
        row["比分推荐"] = ["1:0", "2:1"]
        row["让球推荐"] = "让-1平"
        comp = build_match_comparison(row)
        self.assertEqual(comp["原始主比分"], "1:0")
        self.assertEqual(comp["总进球"]["修正前"], ["2球", "3球"])

    def test_hit_evaluation_with_actual(self):
        row = make_row(
            胜平负推荐="客胜", 净胜球分布="客胜1",
            比分推荐="1:2 / 0:1", 让球推荐="让-1客胜",
            总进球推荐="3球 / 2球", 半全场推荐="DA / AA / DD",
        )
        actual = {
            "编号": "周日001",
            "赛果": {"全场": "1-2", "半场": "0-1", "胜平负": "客胜", "让球": "客胜",
                     "半全场": "AA", "总进球": "3球", "比分": "1:2"},
        }
        comp = build_match_comparison(row, actual)
        self.assertEqual(comp["命中"]["让球"], {"修正前": True, "修正后": True})
        self.assertEqual(comp["命中"]["半全场"], {"修正前": True, "修正后": True})
        self.assertEqual(comp["命中"]["总进球"], {"修正前": True, "修正后": True})
        self.assertEqual(comp["实际赛果"]["全场"], "1-2")


class TestSummaryStats(unittest.TestCase):
    """对照报告统计准确：逐字段核对计数。"""

    def _rows(self):
        # A：半全场冲突 + 备选比分冲突 + 硬性错误（半全场不兼容），有真实赛果
        row_a = make_row(
            编号="周日001", 胜平负推荐="客胜", 净胜球分布="客胜1",
            比分推荐="1:2 / 1:1", 让球推荐="让-1客胜",
            总进球推荐="3球 / 4球", 半全场推荐="DA / AA / DD",
        )
        # B：让球错误 + 总进球缺主比分进球数 + 硬性错误（让球不一致）
        row_b = make_row(
            编号="周日002", 比分推荐="2:1 / 1:0", 让球推荐="让-1主胜",
            总进球推荐="2球 / 4球", 半全场推荐="HH / DH", 净胜球分布="主胜1",
        )
        # C：完全一致
        row_c = make_row(
            编号="周日003", 比分推荐="1:0 / 2:1", 让球推荐="让-1平",
            总进球推荐="1球 / 2球", 半全场推荐="HH / DH", 净胜球分布="主胜1",
        )
        # D：让球数缺失 → 无法判定 + 数据缺失
        row_d = make_row(
            编号="周日004", 体彩让球数="", 让球推荐="",
            比分推荐="1:0 / 2:1", 总进球推荐="1球 / 2球",
            半全场推荐="HH / DH", 数据缺失说明="让球数缺失",
        )
        return [row_a, row_b, row_c, row_d]

    def _actual_a(self):
        return {"编号": "周日001", "赛果": {
            "全场": "1-2", "半场": "0-1", "胜平负": "客胜", "让球": "客胜",
            "半全场": "AA", "总进球": "3球", "比分": "1:2"}}

    def test_stats_counts(self):
        rows = self._rows()
        comps = [
            build_match_comparison(rows[0], self._actual_a()),
            build_match_comparison(rows[1]),
            build_match_comparison(rows[2]),
            build_match_comparison(rows[3]),
        ]
        stats = summarize(comps)
        self.assertEqual(stats["总场数"], 4)
        self.assertEqual(stats["让球被修正"], 2)    # B 方向修正 + D 让球数缺失作废
        self.assertEqual(stats["半全场被修正"], 1)  # A 删除 DD
        self.assertEqual(stats["总进球被修正"], 1)  # B 补入 3球
        self.assertEqual(stats["备选比分冲突"], 1)  # A 的 1:1 替代剧本
        self.assertEqual(stats["数据缺失"], 1)      # D
        self.assertEqual(stats["修正前硬性错误"], 2)  # A 半全场不兼容 + B 让球不一致
        self.assertEqual(stats["规则检查失败"], 1)    # D 无法判定
        self.assertEqual(stats["有赛果场数"], 1)
        self.assertEqual(stats["让球修正前命中"], 1)
        self.assertEqual(stats["让球修正前样本"], 1)
        self.assertEqual(stats["让球修正后命中"], 1)
        self.assertEqual(stats["半全场修正前命中"], 1)
        self.assertEqual(stats["半全场修正后命中"], 1)
        self.assertEqual(stats["总进球修正前命中"], 1)
        self.assertEqual(stats["总进球修正后命中"], 1)

    def test_summary_text_with_results_has_disclaimer(self):
        rows = self._rows()
        comps = [build_match_comparison(rows[0], self._actual_a())]
        text = format_summary(summarize(comps))
        self.assertIn("修正率不等于预测准确率", text)
        self.assertIn("修正前后实际命中率", text)

    def test_summary_text_without_results_no_hit_claim(self):
        comps = [build_match_comparison(r) for r in self._rows()]
        stats = summarize(comps)
        self.assertEqual(stats["有赛果场数"], 0)
        text = format_summary(stats)
        self.assertIn("未获取真实赛果，不统计命中率", text)
        self.assertNotIn("修正前后实际命中率（仅统计", text)


class TestApiKeySafety(unittest.TestCase):
    """API Key 缺失/格式错误时的安全退出；绝不回显完整密钥。"""

    def test_missing_key_rejected(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                ok, msg = validate_api_key(bad)
                self.assertFalse(ok)
                self.assertIn("DEEPSEEK_API_KEY", msg)

    def test_chinese_placeholder_rejected(self):
        ok, msg = validate_api_key("你的真实密钥")
        self.assertFalse(ok)
        self.assertIn("格式非法", msg)

    def test_short_key_rejected(self):
        ok, _ = validate_api_key("sk-abc")
        self.assertFalse(ok)

    def test_valid_format_accepted(self):
        ok, msg = validate_api_key("sk-" + "a1" * 16)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_full_key_never_echoed(self):
        key = "sk-中文占位符abcdef123456"
        ok, msg = validate_api_key(key)
        self.assertFalse(ok)
        self.assertNotIn(key, msg)

    def test_main_exits_before_api_when_key_missing(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            with self.assertRaises(SystemExit) as ctx:
                analyze.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_call_deepseek_bad_key_raises_before_network(self):
        """UnicodeEncodeError 根因修复：非 ASCII 密钥必须在 HTTP 层之前被拦截。"""
        for bad in ("你的真实密钥", "sk-中文占位符abcdef123456", "", None):
            with self.subTest(bad=bad):
                with mock.patch("analyze.requests.post") as post:
                    with self.assertRaises(ValueError) as ctx:
                        analyze.call_deepseek({"编号": "测试001"}, bad)
                    self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))
                    post.assert_not_called()
                    with self.assertRaises(ValueError):
                        analyze.call_deepseek_core_script({"编号": "测试001"}, bad)
                post.assert_not_called()

    def test_call_deepseek_bad_key_error_never_echoes_full_key(self):
        bad = "sk-很长的中文占位密钥不应该被完整回显abcdef123456"
        with mock.patch("analyze.requests.post"):
            with self.assertRaises(ValueError) as ctx:
                analyze.call_deepseek({"编号": "测试001"}, bad)
        self.assertNotIn(bad, str(ctx.exception))

    def test_call_deepseek_valid_key_reaches_network_layer(self):
        """合法格式密钥应通过防线进入网络层（用 RuntimeError 证明到达且不真实联网）。"""
        good = "sk-" + "a1" * 16
        with mock.patch("analyze.requests.post", side_effect=RuntimeError("reached-network")) as post:
            with self.assertRaises(RuntimeError):
                analyze.call_deepseek_core_script({"编号": "测试001"}, good)
            self.assertEqual(post.call_count, 1)


def make_snapshot_entry(mid="周日001", original=None, locked=None, fixed=None, **kw):
    """构造快照场次记录；默认原始让球'让-1主胜'（与 CSV 默认行形成规则后处理差异）。"""
    entry = {
        "编号": mid,
        "对阵": "主队A[测1] vs 客队B[测2]",
        "体彩让球数": "-1",
        "原始玩法输出": original if original is not None else {
            "核心剧本": "测试剧本", "胜平负推荐": "主胜", "胜平负理由": "r",
            "净胜球分布": "主胜1", "让球推荐": "让-1主胜", "让球理由": "r",
            "总进球推荐": ["2球", "3球"], "总进球理由": "r",
            "半全场推荐": ["HH", "DH"], "半全场理由": "r",
            "比分推荐": ["1:0", "2:1"], "比分理由": "r",
            "信心指数": "7", "数据缺失说明": "", "综合分析": "",
        },
        "锁定核心剧本": locked if locked is not None else {
            "home_team": "主队A", "away_team": "客队B",
            "full_time_result": "主胜", "net_goal_interval": "主胜1",
            "primary_score": "1:0", "alternative_scores": ["2:1"],
            "match_script": "测试剧本",
        },
        "修正后预测": fixed if fixed is not None else {
            "胜平负推荐": "主胜", "让球推荐": "让-1平",
            "总进球推荐": ["2球", "3球"], "半全场推荐": ["HH", "DH"],
            "比分推荐": ["1:0", "2:1"],
        },
    }
    entry.update(kw)
    return entry


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _write_snapshot(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"复盘日期": "测试", "场次": entries}, f, ensure_ascii=False)


def _md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


class TestSnapshotComparison(unittest.TestCase):
    """快照口径单场对照：三视角取值、归因、规范化、命中。"""

    def test_snapshot_preferred_over_csv(self):
        row = make_row(让球推荐="让-1平", 比分推荐="1:0 / 2:1", 净胜球分布="主胜1")
        comp = build_snapshot_comparison(row, make_snapshot_entry())
        self.assertEqual(comp["让球"]["修正前"], "让-1主胜")  # 来自快照而非 CSV
        self.assertEqual(comp["让球"]["修正后"], "让-1平")
        self.assertTrue(comp["让球"]["被修正"])
        self.assertEqual(comp["归因"]["让球"], "玩法规则后处理")

    def test_snapshot_unchanged_not_counted(self):
        row = make_row(让球推荐="让-1平", 比分推荐="1:0 / 2:1")
        entry = make_snapshot_entry(original={
            "胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
            "总进球推荐": ["1球", "2球"], "半全场推荐": ["HH", "DH"],
            "比分推荐": ["1:0", "2:1"], "核心剧本": "测试剧本",
        })
        comp = build_snapshot_comparison(row, entry)
        self.assertFalse(comp["让球"]["被修正"])
        self.assertEqual(comp["归因"]["让球"], "无变化")

    def test_spf_override_attribution(self):
        row = make_row(胜平负推荐="主胜", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "客胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
                      "总进球推荐": ["1球"], "半全场推荐": ["HH"], "比分推荐": ["1:0", "2:1"],
                      "核心剧本": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertEqual(comp["归因"]["胜平负"], "核心剧本锁定覆盖")
        self.assertTrue(comp["玩法变化"]["胜平负"])

    def test_joint_cause_attribution(self):
        # 比分被锁定覆盖（3:1→1:0），让球推导基准随之改变 → 共同作用
        row = make_row(让球推荐="让-1平", 比分推荐="1:0")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "主胜", "净胜球分布": "主胜2+", "让球推荐": "让-1主胜",
                      "总进球推荐": ["4球"], "半全场推荐": ["HH"], "比分推荐": ["3:1"], "核心剧本": "s"},
            locked={"full_time_result": "主胜", "net_goal_interval": "主胜1",
                    "primary_score": "1:0", "alternative_scores": [], "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertEqual(comp["归因"]["比分"], "核心剧本锁定覆盖")
        self.assertEqual(comp["归因"]["让球"], "共同作用")

    def test_unattributable_when_evidence_insufficient(self):
        # 缺锁定核心剧本时胜平负变化无法归因，不得凭差异推断
        row = make_row(胜平负推荐="主胜", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "客胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
                      "总进球推荐": ["1球"], "半全场推荐": ["HH"], "比分推荐": ["1:0", "2:1"],
                      "核心剧本": "s"},
            locked={},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertEqual(comp["归因"]["胜平负"], "无法归因")

    def test_hafu_chinese_equivalence(self):
        row = make_row(半全场推荐="HH / DH", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")
        entry = make_snapshot_entry(original={
            "胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
            "总进球推荐": ["1球", "2球"], "半全场推荐": ["半主胜-全主胜", "半平-全主胜"],
            "比分推荐": ["1:0", "2:1"], "核心剧本": "s",
        })
        comp = build_snapshot_comparison(row, entry)
        self.assertFalse(comp["半全场"]["被修正"])  # 中文与 HDA 等价不算变化

    def test_hafu_chinese_real_filter(self):
        row = make_row(胜平负推荐="客胜", 净胜球分布="客胜1",
                       半全场推荐="DA / AA", 让球推荐="让-1平", 比分推荐="1:2 / 0:1")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "客胜", "净胜球分布": "客胜1", "让球推荐": "让-1平",
                      "总进球推荐": ["3球"], "比分推荐": ["1:2", "0:1"], "核心剧本": "s",
                      "半全场推荐": ["半平-全客胜", "半客胜-全客胜", "半平-全平"]},
            locked={"full_time_result": "客胜", "net_goal_interval": "客胜1",
                    "primary_score": "1:2", "alternative_scores": ["0:1"], "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertTrue(comp["半全场"]["被修正"])
        self.assertEqual(comp["半全场"]["删除项"], ["DD"])
        self.assertEqual(comp["归因"]["半全场"], "玩法规则后处理")

    def test_ttg_label_equivalence(self):
        row = make_row(总进球推荐="3球 / 2球", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")
        entry = make_snapshot_entry(original={
            "胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
            "总进球推荐": ["3", "2球"], "半全场推荐": ["HH", "DH"],
            "比分推荐": ["1:0", "2:1"], "核心剧本": "s",
        })
        comp = build_snapshot_comparison(row, entry)
        self.assertFalse(comp["总进球"]["被修正"])  # "3" 与 "3球" 等价不算变化

    def test_nonstandard_rq_hit_unjudgeable(self):
        # "让-1主负"方向无法识别：修正前命中必须为 None，不得强行判定
        row = make_row(让球推荐="让-1平", 比分推荐="1:0 / 2:1")
        entry = make_snapshot_entry(original={
            "胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1主负",
            "总进球推荐": ["1球", "2球"], "半全场推荐": ["HH", "DH"],
            "比分推荐": ["1:0", "2:1"], "核心剧本": "s",
        })
        actual = {"编号": "周日001", "赛果": {"全场": "1-0", "半场": "1-0", "胜平负": "主胜",
                                            "让球": "平", "半全场": "HH", "总进球": "1球", "比分": "1:0"}}
        comp = build_snapshot_comparison(row, entry, actual)
        self.assertIsNone(comp["命中"]["让球"]["修正前"])
        self.assertTrue(comp["命中"]["让球"]["修正后"])

    def test_snapshot_fixed_vs_csv_mismatch_reported(self):
        row = make_row(让球推荐="让-1平", 比分推荐="1:0 / 2:1")
        entry = make_snapshot_entry(fixed={"胜平负推荐": "主胜", "让球推荐": "让-1主胜",
                                           "总进球推荐": ["2球"], "半全场推荐": ["HH"],
                                           "比分推荐": ["1:0"]})
        comp = build_snapshot_comparison(row, entry)
        self.assertTrue(any("不一致（让球）" in q for q in comp["快照问题"]))

    def test_inputs_not_modified(self):
        row = make_row(让球推荐="让-1平", 比分推荐="1:0 / 2:1")
        entry = make_snapshot_entry()
        row_snap, entry_snap = copy.deepcopy(row), copy.deepcopy(entry)
        build_snapshot_comparison(row, entry)
        self.assertEqual(row, row_snap)
        self.assertEqual(entry, entry_snap)

    def test_score_filter_attribution_locked(self):
        # 锁定值与原始一致，但同剧本过滤使备选减少 → 仍归因核心剧本锁定覆盖
        row = make_row(体彩让球数="+1", 胜平负推荐="客胜", 净胜球分布="客胜1",
                       让球推荐="让+1平", 比分推荐="1:2 / 0:1")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "客胜", "净胜球分布": "客胜1", "让球推荐": "让+1平",
                      "总进球推荐": ["3球"], "半全场推荐": ["DA", "AA"],
                      "比分推荐": ["1:2", "0:1", "1:3"], "核心剧本": "s"},
            locked={"full_time_result": "客胜", "net_goal_interval": "客胜1",
                    "primary_score": "1:2", "alternative_scores": ["0:1", "1:3"], "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertTrue(comp["比分"]["被修正"])
        self.assertEqual(comp["归因"]["比分"], "核心剧本锁定覆盖")

    def test_score_outside_locked_unattributable(self):
        # 最终比分含锁定集合之外的值 → 证据不足，必须无法归因
        row = make_row(比分推荐="1:0 / 5:5", 让球推荐="让-1平")
        entry = make_snapshot_entry(original={
            "胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
            "总进球推荐": ["1球"], "半全场推荐": ["HH"], "比分推荐": ["1:0", "2:1"], "核心剧本": "s"})
        comp = build_snapshot_comparison(row, entry)
        self.assertTrue(comp["比分"]["被修正"])
        self.assertEqual(comp["归因"]["比分"], "无法归因")

    def test_covers_boundary_alt_scores_string_type(self):
        # alternative_scores 为 str（类型异常）→ 不得逐字符迭代，按证据不足处理
        row = make_row(比分推荐="1:0 / 2:1", 让球推荐="让-1平")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
                      "总进球推荐": ["1球"], "半全场推荐": ["HH"],
                      "比分推荐": ["1:0", "2:1", "3:1"], "核心剧本": "s"},
            locked={"full_time_result": "主胜", "net_goal_interval": "主胜1",
                    "primary_score": "1:0", "alternative_scores": "2:1", "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertTrue(comp["比分"]["被修正"])
        self.assertEqual(comp["归因"]["比分"], "无法归因")
        # 类型异常必须显式记录：含场次编号与字段名
        self.assertTrue(any("alternative_scores" in q and "周日001" in q
                            for q in comp["快照问题"]))

    def test_locked_primary_type_anomaly_recorded(self):
        # primary_score 为 list（类型异常）→ 显式记录并按证据不足处理，不得 str() 强转污染
        row = make_row(比分推荐="1:0 / 2:1", 让球推荐="让-1平")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
                      "总进球推荐": ["1球"], "半全场推荐": ["HH"],
                      "比分推荐": ["1:0", "2:1", "3:1"], "核心剧本": "s"},
            locked={"full_time_result": "主胜", "net_goal_interval": "主胜1",
                    "primary_score": ["1:0"], "alternative_scores": ["2:1"], "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertTrue(comp["比分"]["被修正"])
        self.assertEqual(comp["归因"]["比分"], "无法归因")
        self.assertTrue(any("primary_score" in q and "周日001" in q
                            for q in comp["快照问题"]))

    def test_covers_boundary_missing_primary(self):
        # 锁定主比分缺失 → covers False → 比分变化无法归因
        row = make_row(比分推荐="1:0 / 2:1", 让球推荐="让-1平")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
                      "总进球推荐": ["1球"], "半全场推荐": ["HH"],
                      "比分推荐": ["1:0", "2:1", "3:1"], "核心剧本": "s"},
            locked={"full_time_result": "主胜", "net_goal_interval": "主胜1",
                    "primary_score": "", "alternative_scores": ["2:1"], "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertEqual(comp["归因"]["比分"], "无法归因")

    def test_covers_boundary_empty_final_scores(self):
        # 最终比分列表为空、原始非空 → 有变化但证据不足 → 无法归因
        row = make_row(比分推荐="", 让球推荐="让-1平")
        entry = make_snapshot_entry(original={
            "胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
            "总进球推荐": ["1球"], "半全场推荐": ["HH"], "比分推荐": ["1:0", "2:1"], "核心剧本": "s"})
        comp = build_snapshot_comparison(row, entry)
        self.assertTrue(comp["比分"]["被修正"])
        self.assertEqual(comp["归因"]["比分"], "无法归因")

    def test_snapshot_hit_direction_match_mismatch(self):
        # 方向与实际赛果一致判命中、不一致判未命中（与赛果比，非与 CSV 自身比）
        row = make_row(体彩让球数="+1", 让球推荐="让+1平", 半全场推荐="DA / AA",
                       总进球推荐="3球 / 2球", 比分推荐="1:2 / 0:1")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "客胜", "净胜球分布": "客胜1", "让球推荐": "让+1平",
                      "总进球推荐": ["3球", "2球"], "半全场推荐": ["DA", "AA"],
                      "比分推荐": ["1:2", "0:1"], "核心剧本": "s"},
            locked={"full_time_result": "客胜", "net_goal_interval": "客胜1",
                    "primary_score": "1:2", "alternative_scores": ["0:1"], "match_script": "s"},
        )
        hit_actual = {"编号": "周日001", "赛果": {"全场": "1-2", "让球": "平", "半全场": "DA", "总进球": "3球"}}
        comp = build_snapshot_comparison(row, entry, hit_actual)
        self.assertTrue(comp["命中"]["让球"]["修正后"])    # 平 == 平
        self.assertTrue(comp["命中"]["半全场"]["修正后"])  # DA ∈ 列表
        self.assertTrue(comp["命中"]["总进球"]["修正后"])  # 3球 ∈ 列表
        miss_actual = {"编号": "周日001", "赛果": {"全场": "2-0", "让球": "主胜", "半全场": "HH", "总进球": "4球"}}
        comp2 = build_snapshot_comparison(row, entry, miss_actual)
        self.assertFalse(comp2["命中"]["让球"]["修正后"])   # 主胜 != 平
        self.assertFalse(comp2["命中"]["半全场"]["修正后"])  # HH ∉ 列表
        self.assertFalse(comp2["命中"]["总进球"]["修正后"])  # 4球 ∉ 列表

    def test_snapshot_unjudgeable_excluded_from_denominator(self):
        # 无法解析方向的让球推荐不进入命中率分母
        row = make_row(让球推荐="让-1平", 比分推荐="1:0 / 2:1")
        entry = make_snapshot_entry(original={
            "胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1主负",
            "总进球推荐": ["1球", "2球"], "半全场推荐": ["HH", "DH"],
            "比分推荐": ["1:0", "2:1"], "核心剧本": "s"})
        actual = {"编号": "周日001", "赛果": {"全场": "1-0", "让球": "平", "半全场": "HH", "总进球": "1球"}}
        comp = build_snapshot_comparison(row, entry, actual)
        stats = summarize([comp])
        self.assertEqual(stats["让球修正前样本"], 0)   # "主负"不可解析 → 不进分母
        self.assertEqual(stats["让球修正后样本"], 1)
        self.assertEqual(stats["让球修正后命中"], 1)


class TestJointCauseEvidence(unittest.TestCase):
    """共同作用证据链：仅当主推比分或胜平负锁定真实生效时才可归共同作用；
    仅凭备选比分列表变化，让球/总进球/半全场必须无法归因。"""

    def test_score_filter_still_locked_when_lists_identical(self):
        # 原始与锁定比分列表完全相同，最终备选被同方向同区间过滤 → 仍归核心剧本锁定覆盖
        row = make_row(体彩让球数="+1", 胜平负推荐="客胜", 净胜球分布="客胜1",
                       让球推荐="让+1平", 比分推荐="1:2 / 0:1")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "客胜", "净胜球分布": "客胜1", "让球推荐": "让+1平",
                      "总进球推荐": ["3球"], "半全场推荐": ["DA", "AA"],
                      "比分推荐": ["1:2", "0:1", "1:3"], "核心剧本": "s"},
            locked={"full_time_result": "客胜", "net_goal_interval": "客胜1",
                    "primary_score": "1:2", "alternative_scores": ["0:1", "1:3"], "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertEqual(comp["归因"]["比分"], "核心剧本锁定覆盖")
        self.assertEqual(comp["比分"]["锁定机制"], "锁定列表与原始一致，按同方向同区间过滤")

    def test_primary_lock_change_enables_joint(self):
        # 锁定主推比分真实生效（原始主推 1:0 → 锁定主推 2:0）→ 让球/总进球可归共同作用
        row = make_row(胜平负推荐="主胜", 净胜球分布="主胜2+", 比分推荐="2:0",
                       让球推荐="让-1主胜", 总进球推荐="2球", 半全场推荐="HH / DH")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
                      "总进球推荐": ["1球"], "半全场推荐": ["HH", "DH"],
                      "比分推荐": ["1:0", "2:1"], "核心剧本": "s"},
            locked={"full_time_result": "主胜", "net_goal_interval": "主胜2+",
                    "primary_score": "2:0", "alternative_scores": [], "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertEqual(comp["归因"]["比分"], "核心剧本锁定覆盖")
        self.assertEqual(comp["比分"]["锁定机制"], "锁定列表替换原始输出")
        self.assertEqual(comp["归因"]["让球"], "共同作用")
        self.assertEqual(comp["归因"]["总进球"], "共同作用")

    def test_alt_only_change_not_joint_for_rq_ttg(self):
        # 仅备选比分列表变化（主推相同）：让球/总进球不得归共同作用 → 无法归因
        row = make_row(胜平负推荐="主胜", 净胜球分布="主胜1", 比分推荐="1:0 / 2:1",
                       让球推荐="让-1主胜", 总进球推荐="4球", 半全场推荐="HH / DH")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1客胜",
                      "总进球推荐": ["2球"], "半全场推荐": ["HH", "DH"],
                      "比分推荐": ["1:0", "2:1"], "核心剧本": "s"},
            locked={"full_time_result": "主胜", "net_goal_interval": "主胜1",
                    "primary_score": "1:0", "alternative_scores": ["2:1", "3:1"], "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertFalse(comp["比分"]["被修正"])            # 最终比分与原始一致，无变化
        self.assertTrue(comp["让球"]["被修正"])
        self.assertEqual(comp["归因"]["让球"], "无法归因")    # post_only=让-1平≠最终，主推未变
        self.assertTrue(comp["总进球"]["被修正"])
        self.assertEqual(comp["归因"]["总进球"], "无法归因")  # post_only=[2球,1球]≠最终，主推未变

    def test_alt_only_change_not_joint_for_hafu(self):
        # 仅备选比分列表变化：半全场不得归共同作用 → 无法归因
        row = make_row(胜平负推荐="主胜", 净胜球分布="主胜1", 比分推荐="1:0 / 2:1",
                       让球推荐="让-1平", 总进球推荐="1球 / 2球", 半全场推荐="HH / DH")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
                      "总进球推荐": ["1球", "2球"], "半全场推荐": ["HH", "DH", "AH"],
                      "比分推荐": ["1:0", "2:1"], "核心剧本": "s"},
            locked={"full_time_result": "主胜", "net_goal_interval": "主胜1",
                    "primary_score": "1:0", "alternative_scores": ["2:1", "3:1"], "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertTrue(comp["半全场"]["被修正"])             # AH 被删除
        self.assertEqual(comp["归因"]["半全场"], "无法归因")   # 胜平负未锁定改变，主推未变

    def test_hafu_joint_via_spf_lock(self):
        # 胜平负锁定生效（平→主胜）且半全场规则过滤依据随之改变 → 共同作用
        row = make_row(胜平负推荐="主胜", 净胜球分布="主胜1", 比分推荐="1:0",
                       让球推荐="让-1平", 总进球推荐="1球", 半全场推荐="HH / DH")
        entry = make_snapshot_entry(
            original={"胜平负推荐": "平", "净胜球分布": "平", "让球推荐": "让-1客胜",
                      "总进球推荐": ["2球"], "半全场推荐": ["DD"],
                      "比分推荐": ["1:1", "0:0"], "核心剧本": "s"},
            locked={"full_time_result": "主胜", "net_goal_interval": "主胜1",
                    "primary_score": "1:0", "alternative_scores": ["2:1"], "match_script": "s"},
        )
        comp = build_snapshot_comparison(row, entry)
        self.assertEqual(comp["归因"]["胜平负"], "核心剧本锁定覆盖")
        self.assertEqual(comp["归因"]["半全场"], "共同作用")


class TestRealDataCaliber1012(unittest.TestCase):
    """2026-10-12 真实数据统计口径回归：归因调整不得无意改变既有统计（只读）。"""

    DATE = "2026-10-12"
    OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

    def test_caliber_unchanged(self):
        csv_p = os.path.join(self.OUT, f"竞彩分析_{self.DATE}.csv")
        snap_p = os.path.join(self.OUT, f"原始预测快照_{self.DATE}.json")
        if not (os.path.isfile(csv_p) and os.path.isfile(snap_p)):
            self.skipTest("真实数据文件不存在")
        rows = load_analysis_rows(csv_p)
        snap, issues = load_snapshot(self.DATE, self.OUT)
        self.assertEqual(issues, [])
        comps = [build_snapshot_comparison(r, snap[(r.get("编号") or "").strip()])
                 for r in rows if (r.get("编号") or "").strip() in snap]
        # 期望值取自 2026-10-12 真实数据（10 场）的当前口径（数据重生后重新冻结）。
        self.assertEqual(len(comps), 10)
        stats = summarize(comps)
        self.assertEqual(stats["玩法变化"],
                         {"胜平负": 0, "让球": 7, "半全场": 9, "总进球": 0, "比分": 10})
        self.assertEqual(stats["变化原因"],
                         {"核心剧本锁定覆盖": 10, "玩法规则后处理": 10,
                          "净胜球分布规则推导(v2)": 0, "共同作用": 0, "无法归因": 0})
        self.assertEqual(stats["有变化场数"], 10)


class TestSnapshotLoading(unittest.TestCase):
    """快照加载：仅文件不存在才回退；损坏/结构错/缺字段/重复编号显式异常。"""

    def test_no_snapshot_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            idx, issues = load_snapshot("2099-01-01", d)
        self.assertIsNone(idx)
        self.assertEqual(issues, [])

    def test_corrupt_snapshot_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "原始预测快照_2099-01-01.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"场次": [broken')
            with self.assertRaises(ValueError) as ctx:
                load_snapshot("2099-01-01", d)
        self.assertIn("原始预测快照_2099-01-01.json", str(ctx.exception))

    def test_structure_error_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "原始预测快照_2099-01-01.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"x": 1}, f)
            with self.assertRaises(ValueError):
                load_snapshot("2099-01-01", d)

    def test_missing_original_output_excluded_with_issue(self):
        with tempfile.TemporaryDirectory() as d:
            _write_snapshot(os.path.join(d, "原始预测快照_2099-01-01.json"),
                            [{"编号": "周日001", "对阵": "A vs B",
                              "锁定核心剧本": {}, "修正后预测": {}}])
            idx, issues = load_snapshot("2099-01-01", d)
        self.assertEqual(idx, {})
        self.assertTrue(any("缺必要字段" in q for q in issues))

    def test_duplicate_id_reported_first_kept(self):
        with tempfile.TemporaryDirectory() as d:
            e1 = make_snapshot_entry("周日001")
            e2 = make_snapshot_entry("周日001")
            _write_snapshot(os.path.join(d, "原始预测快照_2099-01-01.json"), [e1, e2])
            idx, issues = load_snapshot("2099-01-01", d)
        self.assertEqual(len(idx), 1)
        self.assertTrue(any("编号重复" in q for q in issues))


class TestSnapshotReportFlow(unittest.TestCase):
    """报告编排：快照优先、旧口径标注、排除清单、统计一致、文件保护。"""

    DATE = "2099-01-01"

    def _setup(self, d, rows, entries=None, actuals=None):
        os.makedirs(os.path.join(d, "output"), exist_ok=True)
        out = os.path.join(d, "output")
        _write_csv(os.path.join(out, f"竞彩分析_{self.DATE}.csv"), rows)
        if entries is not None:
            _write_snapshot(os.path.join(out, f"原始预测快照_{self.DATE}.json"), entries)
        if actuals is not None:
            with open(os.path.join(out, f"每日记录_{self.DATE}.json"), "w", encoding="utf-8") as f:
                json.dump({"场次": actuals}, f, ensure_ascii=False)
        return out

    def test_snapshot_mode_header(self):
        rows = [make_row(让球推荐="让-1平", 比分推荐="1:0 / 2:1")]
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=[make_snapshot_entry()])
            text = build_report_text(self.DATE, out)
        self.assertIn("不对已修正 CSV 重跑", text)
        self.assertIn("原始预测=原始预测快照_", text)
        self.assertNotIn("旧口径", text)

    def test_legacy_mode_marked(self):
        rows = [make_row()]
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=None)
            text = build_report_text(self.DATE, out)
        self.assertIn("旧口径：无法恢复原始模型输出", text)

    def test_csv_only_match_excluded_and_listed(self):
        rows = [make_row(编号="周日001", 让球推荐="让-1平", 比分推荐="1:0 / 2:1"),
                make_row(编号="周日002", 综合分析="")]
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=[make_snapshot_entry("周日001")])
            text = build_report_text(self.DATE, out)
        self.assertIn("CSV 中存在但快照缺失", text)
        self.assertIn("周日002", text)
        self.assertIn("原因未知", text)
        self.assertIn("总比赛场数：1", text)

    def test_blocked_row_reason_labeled(self):
        rows = [make_row(编号="周日001", 综合分析="核心剧本校验未通过：比分与净胜球冲突")]
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=[])
            text = build_report_text(self.DATE, out)
        self.assertIn("校验未通过/分析失败记录", text)
        self.assertIn("属预期", text)

    def test_snap_only_match_listed(self):
        rows = [make_row(编号="周日001", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")]
        entries = [make_snapshot_entry("周日001"), make_snapshot_entry("周日099")]
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=entries)
            text = build_report_text(self.DATE, out)
        self.assertIn("快照中存在但 CSV 缺失", text)
        self.assertIn("周日099", text)

    def test_play_and_cause_stats_two_dimensions(self):
        # 场1：让球规则后处理；场2：胜平负锁定覆盖。两组维度独立计数。
        rows = [make_row(编号="周日001", 让球推荐="让-1平", 比分推荐="1:0 / 2:1"),
                make_row(编号="周日002", 胜平负推荐="主胜", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")]
        e2 = make_snapshot_entry("周日002", original={
            "胜平负推荐": "客胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
            "总进球推荐": ["1球"], "半全场推荐": ["HH"], "比分推荐": ["1:0", "2:1"], "核心剧本": "s"})
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=[make_snapshot_entry("周日001"), e2])
            text = build_report_text(self.DATE, out)
        self.assertIn("按玩法字段统计变化场数", text)
        self.assertIn("按变化原因统计场数", text)
        self.assertIn("  让球：1 场", text)
        self.assertIn("  胜平负：1 场", text)
        self.assertIn("  玩法规则后处理：1 场", text)
        self.assertIn("  核心剧本锁定覆盖：1 场", text)
        self.assertIn("存在任一变化的唯一比赛数：2 场", text)

    def test_detail_matches_summary_snapshot_mode(self):
        rows = [make_row(编号="周日001", 让球推荐="让-1平", 比分推荐="1:0 / 2:1"),
                make_row(编号="周日002", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")]
        e2 = make_snapshot_entry("周日002", original={
            "胜平负推荐": "主胜", "净胜球分布": "主胜1", "让球推荐": "让-1平",
            "总进球推荐": ["1球", "2球"], "半全场推荐": ["HH", "DH"],
            "比分推荐": ["1:0", "2:1"], "核心剧本": "s"})
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=[make_snapshot_entry("周日001"), e2])
            text = build_report_text(self.DATE, out)
        rq_blocks = text.count("让球胜平负 【已修正】")
        self.assertIn("  让球：1 场", text)
        self.assertEqual(rq_blocks, 1)

    def test_repeat_generation_identical(self):
        rows = [make_row(编号="周日001", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")]
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=[make_snapshot_entry("周日001")])
            t1 = build_report_text(self.DATE, out)
            t2 = build_report_text(self.DATE, out)
        self.assertEqual(t1, t2)

    def test_inputs_md5_unchanged(self):
        rows = [make_row(编号="周日001", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")]
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=[make_snapshot_entry("周日001")],
                              actuals=[{"编号": "周日001", "赛果": {"全场": "1-0", "让球": "平",
                                                                  "半全场": "HH", "总进球": "1球"}}])
            paths = [os.path.join(out, f"竞彩分析_{self.DATE}.csv"),
                     os.path.join(out, f"原始预测快照_{self.DATE}.json"),
                     os.path.join(out, f"每日记录_{self.DATE}.json")]
            before = [_md5(p) for p in paths]
            generate_compare_report(self.DATE, out)
            after = [_md5(p) for p in paths]
        self.assertEqual(before, after)

    def test_existing_report_not_overwritten(self):
        rows = [make_row(编号="周日001", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")]
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=[make_snapshot_entry("周日001")])
            p1 = generate_compare_report(self.DATE, out)
            with open(p1, encoding="utf-8") as f:
                content = f.read()
            with self.assertRaises(FileExistsError):
                generate_compare_report(self.DATE, out)
            with open(p1, encoding="utf-8") as f:
                self.assertEqual(f.read(), content)

    def test_suffix_generates_new_file_keeps_existing(self):
        rows = [make_row(编号="周日001", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")]
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=[make_snapshot_entry("周日001")])
            p1 = generate_compare_report(self.DATE, out)
            with open(p1, encoding="utf-8") as f:
                content_before = f.read()
            p2 = generate_compare_report(self.DATE, out, suffix="_v2")
            self.assertEqual(os.path.basename(p2), f"修正对照报告_{self.DATE}_v2.txt")
            self.assertTrue(os.path.isfile(p2))
            # 原报告文件必须原样保留
            with open(p1, encoding="utf-8") as f:
                self.assertEqual(f.read(), content_before)
            self.assertNotEqual(p1, p2)

    def test_illegal_suffix_rejected(self):
        rows = [make_row(编号="周日001", 让球推荐="让-1平", 比分推荐="1:0 / 2:1")]
        with tempfile.TemporaryDirectory() as d:
            out = self._setup(d, rows, entries=[make_snapshot_entry("周日001")])
            for bad in ("../x", "a/b", "a\\b"):
                with self.assertRaises(ValueError):
                    generate_compare_report(self.DATE, out, suffix=bad)


if __name__ == "__main__":
    unittest.main()
