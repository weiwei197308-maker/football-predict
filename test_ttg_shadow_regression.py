#!/usr/bin/env python3
"""总进球影子实验回归测试：正式输出可信基准对照 + 影子五场景 + 赛果回填。

基准来源：test_ttg_shadow_goldens.json 由改造前 analyze.py
（工作区版本 md5 19f5d1e4d0d0181733a6bec7c079fa1a）在固定输入 +
固定 mock 响应下生成。本文件用相同固定输入重放改造后代码，逐字段比较。

全部使用 mock 与临时目录，不调用真实预测 API，不触碰真实 output/ 数据。

运行：python3 -m unittest test_ttg_shadow_regression -v
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date as _date
from unittest.mock import Mock, patch

import analyze
import ttg_shadow

FIXTURES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "test_ttg_shadow_fixtures.json")
GOLDENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "test_ttg_shadow_goldens.json")
API_KEY = "sk-" + "a" * 16

with open(FIXTURES_PATH, encoding="utf-8") as f:
    FIXTURES = json.load(f)
with open(GOLDENS_PATH, encoding="utf-8") as f:
    GOLDENS = json.load(f)

ROWS = FIXTURES["rows"]
HISTORY = FIXTURES["history"]
FIXED_DATE = FIXTURES["fixed_date"]


class FakeDate(_date):
    @classmethod
    def today(cls):
        y, m, d = (int(x) for x in FIXED_DATE.split("-"))
        return cls(y, m, d)


def make_resp(text):
    r = Mock()
    r.raise_for_status = lambda: None
    r.json = lambda: {"choices": [{"message": {"content": text}}]}
    return r


def official_dispatcher(url, headers=None, json=None, timeout=None, **kw):
    """仅应答正式流程的两阶段请求；影子请求会触发 AssertionError。"""
    sys_prompt = json["messages"][0]["content"]
    user = json["messages"][1]["content"]
    num = ROWS[0]["编号"] if ROWS[0]["编号"] in user else ROWS[1]["编号"]
    if "本阶段只判断核心比赛剧本" in sys_prompt:
        return make_resp(FIXTURES["core_responses"][num])
    if "总进球数盲判" in sys_prompt or "总进球市场复核" in sys_prompt:
        raise AssertionError("影子关闭时不允许发起影子请求")
    return make_resp(FIXTURES["play_responses"][num])


BLIND_TEXT = json.dumps({
    "ttg_blind_picks": ["2球", "3球"],
    "ttg_blind_reasoning": "双方攻防均衡，节奏中等",
    "ttg_blind_uncertainty": "客队轮换情况不明",
    "ordered": True}, ensure_ascii=False)
REVIEW_TEXT = json.dumps({
    "ttg_review_picks": ["2球", "3球", "4球"],
    "changed": True,
    "change_reason": "市场 4球 赔率处于低位，补入覆盖"}, ensure_ascii=False)


def full_dispatcher(url, headers=None, json=None, timeout=None, **kw):
    sys_prompt = json["messages"][0]["content"]
    if "总进球数盲判" in sys_prompt:
        return make_resp(BLIND_TEXT)
    if "总进球市场复核" in sys_prompt:
        return make_resp(REVIEW_TEXT)
    return official_dispatcher(url, headers=headers, json=json, timeout=timeout)


def run_main_in_tmp(dispatcher, shadow_on=False):
    """在临时目录运行真实 main()，返回 (csv_text, snapshot_text, post_mock, shadow_dir)。"""
    tmp = tempfile.mkdtemp(prefix="ttg_regression_")
    os.makedirs(os.path.join(tmp, "output"))
    import csv as _csv
    cols = []
    for r in ROWS:
        for k in r.keys():
            if k not in cols:
                cols.append(k)
    with open(os.path.join(tmp, "output", f"竞彩足球_{FIXED_DATE}.csv"), "w",
              encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in ROWS:
            w.writerow(r)

    # 本文件只做总进球影子的 golden 回归；让球/半全场影子默认开启，
    # 这里显式关闭以保持「影子关闭→无额外 API 调用」的调用数断言语义。
    env = {"DISABLE_RQ_SHADOW": "1", "DISABLE_HAFU_SHADOW": "1"}
    if not shadow_on:
        env["DISABLE_TTG_SHADOW"] = "1"
    env["DEEPSEEK_API_KEY"] = API_KEY  # 仅供格式校验，mock 拦截不会真实请求
    old_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        with patch("analyze.requests.post", side_effect=dispatcher) as post_mock, \
             patch("analyze.build_history_for_match", return_value=HISTORY), \
             patch("analyze.date", FakeDate), \
             patch.dict(os.environ, env, clear=False):
            if shadow_on:
                os.environ.pop("DISABLE_TTG_SHADOW", None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                analyze.main()
        csv_path = os.path.join(tmp, "output", f"竞彩分析_{FIXED_DATE}.csv")
        snap_path = os.path.join(tmp, "output", f"原始预测快照_{FIXED_DATE}.json")
        csv_text = open(csv_path, encoding="utf-8-sig").read() \
            if os.path.exists(csv_path) else None
        snap_text = open(snap_path, encoding="utf-8").read() \
            if os.path.exists(snap_path) else None
        return csv_text, snap_text, post_mock, tmp, buf.getvalue()
    finally:
        os.chdir(old_cwd)


class TestGoldenFields(unittest.TestCase):
    """函数级对照：build_result_row / build_snapshot_entry 与改造前基准逐字段一致。"""

    def _run(self, row):
        with patch("analyze.requests.post", side_effect=official_dispatcher):
            md = analyze.build_match_prompt_data(row, HISTORY)
            parsed, report, err = analyze.analyze_match_two_stage(
                md, API_KEY, row["让球数"],
                home=row["主队"], away=row["客队"], max_retries=1)
        self.assertIsNone(err)
        return md, parsed, report

    def test_result_rows_match_golden(self):
        for row in ROWS:
            md, parsed, report = self._run(row)
            got = analyze.build_result_row(
                parsed, md, row["编号"], row["联赛"], row["让球数"], report, HISTORY)
            self.assertEqual(got, GOLDENS["result_rows"][row["编号"]],
                             f"{row['编号']} result_row 与基准不一致")

    def test_snapshots_match_golden(self):
        for row in ROWS:
            md, parsed, report = self._run(row)
            got = analyze.build_snapshot_entry(
                parsed, md, row["编号"], row["让球数"], report)
            self.assertEqual(got, GOLDENS["snapshots"][row["编号"]],
                             f"{row['编号']} 快照与基准不一致")


class TestMainEndToEnd(unittest.TestCase):
    """main() 端到端 L2 对照：正式 CSV 与快照字节级一致。"""

    def test_shadow_off_matches_golden_and_no_extra_calls(self):
        csv_text, snap_text, post_mock, tmp, _ = run_main_in_tmp(
            official_dispatcher, shadow_on=False)
        try:
            self.assertEqual(csv_text, GOLDENS["csv_text"])
            self.assertEqual(snap_text, GOLDENS["snapshot_text"])
            # 2 场 × 2 次（核心剧本 + 玩法层），影子关闭不得新增调用
            self.assertEqual(post_mock.call_count, 2 * len(ROWS))
            self.assertFalse(os.path.exists(
                ttg_shadow.shadow_file_path(FIXED_DATE, os.path.join(tmp, "output"))))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_shadow_on_success_official_identical(self):
        csv_text, snap_text, post_mock, tmp, out = run_main_in_tmp(
            full_dispatcher, shadow_on=True)
        try:
            self.assertEqual(csv_text, GOLDENS["csv_text"])
            self.assertEqual(snap_text, GOLDENS["snapshot_text"])
            # 2 场 ×（2 正式 + A/B/C/C2/D 五组影子；C2 默认启用）
            self.assertEqual(post_mock.call_count, 5 * len(ROWS))
            path = ttg_shadow.shadow_file_path(FIXED_DATE, os.path.join(tmp, "output"))
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["场次"]), len(ROWS))
            for rec in data["场次"]:
                self.assertEqual(rec["A盲判"]["候选"], ["2球", "3球"])
                self.assertTrue(rec["B复核"]["校验后变更"])
                self.assertIsNone(rec["实际赛果"])  # 赛前文件绝不含赛果
            self.assertIn("[影子实验] 写入成功", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_shadow_api_error_official_identical(self):
        def dispatcher(url, headers=None, json=None, timeout=None, **kw):
            sys_prompt = json["messages"][0]["content"]
            if "总进球数盲判" in sys_prompt or "总进球市场复核" in sys_prompt:
                raise ttg_shadow.requests.RequestException("shadow network down")
            return official_dispatcher(url, headers=headers, json=json, timeout=timeout)

        csv_text, snap_text, _, tmp, _ = run_main_in_tmp(dispatcher, shadow_on=True)
        try:
            self.assertEqual(csv_text, GOLDENS["csv_text"])
            self.assertEqual(snap_text, GOLDENS["snapshot_text"])
            path = ttg_shadow.shadow_file_path(FIXED_DATE, os.path.join(tmp, "output"))
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for rec in data["场次"]:
                self.assertEqual(rec["A盲判"]["状态"], "api_error")
                self.assertEqual(rec["B复核"]["状态"], "skipped")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_shadow_parse_error_official_identical(self):
        def dispatcher(url, headers=None, json=None, timeout=None, **kw):
            sys_prompt = json["messages"][0]["content"]
            if "总进球数盲判" in sys_prompt:
                return make_resp("这不是 JSON 输出")
            if "总进球市场复核" in sys_prompt:
                return make_resp(REVIEW_TEXT)
            return official_dispatcher(url, headers=headers, json=json, timeout=timeout)

        csv_text, snap_text, _, tmp, _ = run_main_in_tmp(dispatcher, shadow_on=True)
        try:
            self.assertEqual(csv_text, GOLDENS["csv_text"])
            self.assertEqual(snap_text, GOLDENS["snapshot_text"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_shadow_write_error_official_identical(self):
        with patch("ttg_shadow.os.replace", side_effect=OSError("disk error")):
            csv_text, snap_text, _, tmp, out = run_main_in_tmp(
                full_dispatcher, shadow_on=True)
        try:
            self.assertEqual(csv_text, GOLDENS["csv_text"])
            self.assertEqual(snap_text, GOLDENS["snapshot_text"])
            self.assertIn("写入失败", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSettleShadow(unittest.TestCase):
    """赛后回填：只允许写赛果白名单字段，预测字段逐字节不变。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ttg_settle_test_")
        self.old_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prepare(self):
        os.makedirs(os.path.join(self.tmp, "output"))
        rec1 = {
            "对齐键": {"日期": FIXED_DATE, "编号": "周四001", "主队": "阿尔法队",
                       "客队": "贝塔队", "开赛": "2026-10-29 20:00"},
            "实验运行ID": "r1", "写入时间": "2026-10-29T21:00:00",
            "A盲判": {"状态": "ok", "候选": ["2球", "3球"], "理由": "r",
                      "不确定说明": "", "有序": True, "错误": "", "原始文本前缀": ""},
            "B复核": {"状态": "ok", "候选": ["2球", "3球", "4球"],
                      "模型声明变更": True, "校验后变更": True,
                      "变更理由": "x", "理由缺失": False, "错误": "", "原始文本前缀": ""},
            "C正式": {"状态": "ok", "候选": ["3球", "2球", "4球"], "有序": True},
            "D固定": {"状态": "ok", "候选": ["2球", "3球", "4球"], "有序": True},
            "市场前三": ["3球", "2球", "1球"],
            "实际赛果": None, "影子错误": "",
        }
        rec2 = json.loads(json.dumps(rec1, ensure_ascii=False))
        rec2["对齐键"]["编号"] = "周四002"  # 每日记录中不存在 → 未找到赛果
        shadow = {"实验日期": FIXED_DATE, "版本": 1, "场次": [rec1, rec2],
                  "冲突记录": []}
        with open(ttg_shadow.shadow_file_path(
                FIXED_DATE, os.path.join(self.tmp, "output")),
                "w", encoding="utf-8") as f:
            json.dump(shadow, f, ensure_ascii=False)
        daily = {"复盘日期": FIXED_DATE, "场次": [
            {"编号": "周四001", "赛果": {"全场": "2-1", "半场": "1-0"}},
        ]}
        with open(os.path.join(self.tmp, "output", f"每日记录_{FIXED_DATE}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(daily, f, ensure_ascii=False)

    def _run_settle(self):
        os.chdir(self.tmp)
        env = dict(os.environ)
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "settle_shadow.py")
        env["PYTHONPATH"] = os.path.dirname(script)
        return subprocess.run(
            [sys.executable, script, FIXED_DATE],
            capture_output=True, text=True, env=env)

    def test_backfill_only_result_field(self):
        self._prepare()
        before = open(ttg_shadow.shadow_file_path(FIXED_DATE, os.path.join(self.tmp, "output")),
                      encoding="utf-8").read()
        r = self._run_settle()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(ttg_shadow.shadow_file_path(FIXED_DATE, os.path.join(self.tmp, "output")),
                  encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(after["场次"][0]["实际赛果"]["总进球"], "3球")
        self.assertIsNone(after["场次"][1]["实际赛果"])  # 未找到赛果，保持 null
        # 预测字段逐字节不变：去掉白名单字段后必须与原文件完全一致
        orig = json.loads(before)
        for rec in after["场次"]:
            rec.pop("实际赛果", None)
        for rec in orig["场次"]:
            rec.pop("实际赛果", None)
        after.pop("最近回填时间", None)
        self.assertEqual(after, orig)

    def test_second_run_skips_existing(self):
        self._prepare()
        self.assertEqual(self._run_settle().returncode, 0)
        r = self._run_settle()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("已有跳过 1", r.stdout)
        self.assertIn("新增赛果 0", r.stdout)

    def test_missing_daily_record_aborts(self):
        self._prepare()
        os.remove(os.path.join(self.tmp, "output", f"每日记录_{FIXED_DATE}.json"))
        before = open(ttg_shadow.shadow_file_path(FIXED_DATE, os.path.join(self.tmp, "output")),
                      encoding="utf-8").read()
        r = self._run_settle()
        self.assertNotEqual(r.returncode, 0)
        after = open(ttg_shadow.shadow_file_path(FIXED_DATE, os.path.join(self.tmp, "output")),
                     encoding="utf-8").read()
        self.assertEqual(before, after)  # 原文件不变


if __name__ == "__main__":
    unittest.main()
