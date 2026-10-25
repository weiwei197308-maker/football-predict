"""build_attribution_table.py 自动化测试。

覆盖：错题识别（v is False 严格口径）、归因来源判定与降级、
【失败根因】提取与摘要压缩、关键词根因分类、玩法枚举与列顺序。
运行：python3 -m unittest test_build_attribution_table -v
"""
import json
import os
import tempfile
import unittest

from build_attribution_table import (
    CSV_HEADER,
    PLAYBOOKS,
    SUMMARY_MAX,
    build_rows,
    classify,
    extract_root_cause_section,
    truncate,
)


def _record(num, hits, old_attr="旧归因文本", pred=None, actual=None):
    return {
        "编号": num,
        "联赛": "测试联赛",
        "对阵": "A队 vs B队",
        "信心指数": 5,
        "预判": pred or {"胜平负": "主胜", "让球": "让-1主胜", "总进球": "2球", "半全场": "HH", "比分": "2:0"},
        "赛果": actual or {"胜平负": "客胜", "让球": "客胜", "总进球": "1球", "半全场": "AA", "比分": "0:1"},
        "命中": hits,
        "错误归因": old_attr,
    }


def _write_record(tmpdir, date_str, matches):
    data = {"复盘日期": date_str, "命中统计": {}, "场次": matches}
    with open(os.path.join(tmpdir, f"每日记录_{date_str}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _write_deep(tmpdir, date_str, entries):
    data = {"复盘日期": date_str, "场次": entries}
    with open(os.path.join(tmpdir, f"深度归因_{date_str}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


DEEP_TEXT = (
    "【事实依据】材料显示主队全场占优却输球。\n"
    "【失败根因】\n1. 低估客队反击效率，主要影响让球与比分。\n2. 忽视主队红牌减员的连锁反应。\n"
    "【与旧归因的差异】相比旧归因的“高估主队”，本次发现了红牌因素。"
)


class TestWrongDetection(unittest.TestCase):
    """错题识别：命中 v is False 严格口径"""

    def test_hit_true_no_row(self):
        with tempfile.TemporaryDirectory() as d:
            _write_record(d, "2026-01-01", [_record("测试001", {p: True for p in PLAYBOOKS})])
            self.assertEqual(build_rows(d), [])

    def test_hit_false_produces_row(self):
        with tempfile.TemporaryDirectory() as d:
            hits = {p: True for p in PLAYBOOKS}
            hits["让球"] = False
            _write_record(d, "2026-01-01", [_record("测试001", hits)])
            rows = build_rows(d)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][4], "让球")

    def test_three_false_produces_three_rows(self):
        with tempfile.TemporaryDirectory() as d:
            hits = {p: True for p in PLAYBOOKS}
            for p in ("让球", "总进球", "比分"):
                hits[p] = False
            _write_record(d, "2026-01-01", [_record("测试001", hits)])
            rows = build_rows(d)
            self.assertEqual(len(rows), 3)
            self.assertEqual([r[4] for r in rows], ["让球", "总进球", "比分"])  # 按玩法枚举顺序

    def test_missing_hit_key_no_row(self):
        """命中 key 缺失不产行（与 tracker 的 bool(get) 口径不同）"""
        with tempfile.TemporaryDirectory() as d:
            hits = {p: True for p in PLAYBOOKS}
            del hits["让球"]  # key 缺失
            _write_record(d, "2026-01-01", [_record("测试001", hits)])
            self.assertEqual(build_rows(d), [])

    def test_none_hit_no_row(self):
        with tempfile.TemporaryDirectory() as d:
            hits = {p: True for p in PLAYBOOKS}
            hits["比分"] = None
            _write_record(d, "2026-01-01", [_record("测试001", hits)])
            self.assertEqual(build_rows(d), [])


class TestSourceSelection(unittest.TestCase):
    """归因来源判定与安全降级"""

    def _setup(self, tmpdir, deep_entries=None):
        hits = {p: True for p in PLAYBOOKS}
        hits["让球"] = False
        _write_record(tmpdir, "2026-01-01", [_record("测试001", hits, old_attr="AI高估了主队")])
        if deep_entries is not None:
            _write_deep(tmpdir, "2026-01-01", deep_entries)

    def test_deep_source_when_valid(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup(d, [{"编号": "测试001", "深度归因": DEEP_TEXT}])
            rows = build_rows(d)
            self.assertEqual(rows[0][7], "深度")

    def test_old_source_when_no_deep_file(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup(d)
            rows = build_rows(d)
            self.assertEqual(rows[0][7], "旧")

    def test_old_source_when_deep_failed_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup(d, [{"编号": "测试001", "深度归因": "(深度归因调用失败：timeout)"}])
            rows = build_rows(d)
            self.assertEqual(rows[0][7], "旧")
            self.assertEqual(rows[0][9][:6], "AI高估了主")

    def test_old_source_when_deep_missing_section(self):
        """深度归因缺【失败根因】段 → 降级为旧来源"""
        with tempfile.TemporaryDirectory() as d:
            self._setup(d, [{"编号": "测试001", "深度归因": "【事实依据】只有事实没有根因段。"}])
            rows = build_rows(d)
            self.assertEqual(rows[0][7], "旧")

    def test_deep_only_covers_same_match(self):
        """深度归因按编号匹配，不串场"""
        with tempfile.TemporaryDirectory() as d:
            self._setup(d, [{"编号": "别的场次", "深度归因": DEEP_TEXT}])
            rows = build_rows(d)
            self.assertEqual(rows[0][7], "旧")


class TestSummary(unittest.TestCase):
    """摘要提取、截断、换行折叠"""

    def test_extract_root_cause_section(self):
        section = extract_root_cause_section(DEEP_TEXT)
        self.assertIn("低估客队反击效率", section)
        self.assertNotIn("事实依据", section)
        self.assertNotIn("与旧归因的差异", section)

    def test_extract_missing_section_returns_empty(self):
        self.assertEqual(extract_root_cause_section("没有小节标题的文本"), "")
        self.assertEqual(extract_root_cause_section(None), "")

    def test_summary_truncates_over_60(self):
        long_text = "很" * 100
        out = truncate(long_text)
        self.assertEqual(len(out), SUMMARY_MAX + 1)  # 60 字 + 省略号
        self.assertTrue(out.endswith("…"))

    def test_summary_not_truncate_at_60(self):
        text = "刚" * 60
        self.assertEqual(truncate(text), text)

    def test_summary_folds_newlines(self):
        self.assertEqual(truncate("第一行\n\n  第二行\t第三行"), "第一行 第二行 第三行")

    def test_deep_summary_from_section_only(self):
        """深度来源摘要必须来自【失败根因】段，不含其他小节"""
        with tempfile.TemporaryDirectory() as d:
            hits = {p: True for p in PLAYBOOKS}
            hits["比分"] = False
            _write_record(d, "2026-01-01", [_record("测试001", hits)])
            _write_deep(d, "2026-01-01", [{"编号": "测试001", "深度归因": DEEP_TEXT}])
            rows = build_rows(d)
            summary = rows[0][9]
            self.assertIn("低估客队反击效率", summary)
            self.assertNotIn("材料显示", summary)  # 事实依据段不进摘要
            self.assertNotIn("高估", summary)      # 差异段的"高估主队"不进摘要


class TestClassify(unittest.TestCase):
    """根因分类关键词映射"""

    def test_old_gaogu(self):
        self.assertEqual(classify("AI高估了主队进攻效率"), "模型高估")

    def test_old_digu(self):
        self.assertEqual(classify("低估了客队防守韧性"), "模型低估")

    def test_multi_category_enum_order(self):
        """多类命中按枚举顺序输出，不按文中出现顺序"""
        # 文本中"剧本"在前、"高估"在后，输出仍按枚举顺序 模型高估|剧本判断错
        self.assertEqual(classify("剧本判断过于乐观，同时高估主队"), "模型高估|剧本判断错")
        self.assertEqual(classify("门将失误叠加红牌减员"), "红牌|门将失误")

    def test_unmapped_is_unclassified(self):
        self.assertEqual(classify("完全无关的普通描述"), "未分类")
        self.assertEqual(classify(""), "未分类")

    def test_referee_narrow_no_bare_cancaipan(self):
        """裸词'裁判'不归入裁判误判（宁漏勿错）"""
        self.assertEqual(classify("裁判判罚尺度较松"), "未分类")
        self.assertEqual(classify("裁判误判改变走势"), "裁判误判")

    def test_shuangque_maps_to_injury_only(self):
        """回归：'主力门将及后卫双缺'→ 伤病，且不得误配其他类（裸'门将'不配）"""
        text = "1. 防线重组致防守崩盘：柏太阳神主力门将及后卫双缺，防线实力大损，直接导致主场失3球惨败。主要影响：胜平负、让球、总进球。"
        self.assertEqual(classify(text), "伤病")

    def test_classification_scope_deep(self):
        """深度来源分类只匹配【失败根因】段：差异段的'高估'是元描述，不得污染分类"""
        with tempfile.TemporaryDirectory() as d:
            hits = {p: True for p in PLAYBOOKS}
            hits["比分"] = False
            _write_record(d, "2026-01-01", [_record("测试001", hits)])
            _write_deep(d, "2026-01-01", [{"编号": "测试001", "深度归因": DEEP_TEXT}])
            rows = build_rows(d)
            cause = rows[0][8]
            # 【失败根因】段含"低估"+"红牌" → 应归入 红牌|模型低估；
            # 差异段的"高估"不得进入分类
            self.assertEqual(cause, "红牌|模型低估")
            self.assertNotIn("模型高估", cause)


class TestStructure(unittest.TestCase):
    def test_playbook_enum_fixed(self):
        self.assertEqual(PLAYBOOKS, ["胜平负", "让球", "总进球", "半全场", "比分"])

    def test_csv_header_fixed(self):
        self.assertEqual(
            CSV_HEADER,
            ["日期", "编号", "联赛", "对阵", "玩法", "AI预测", "实际赛果", "归因来源", "根因分类", "归因摘要"],
        )

    def test_row_fields_from_record(self):
        """AI预测/实际赛果取每日记录 预判/赛果 对应玩法字段"""
        with tempfile.TemporaryDirectory() as d:
            hits = {p: True for p in PLAYBOOKS}
            hits["比分"] = False
            _write_record(d, "2026-01-01", [_record("测试001", hits)])
            rows = build_rows(d)
            r = rows[0]
            self.assertEqual(r[0], "2026-01-01")
            self.assertEqual(r[1], "测试001")
            self.assertEqual(r[4], "比分")
            self.assertEqual(r[5], "2:0")   # 预判.比分
            self.assertEqual(r[6], "0:1")   # 赛果.比分


if __name__ == "__main__":
    unittest.main()
