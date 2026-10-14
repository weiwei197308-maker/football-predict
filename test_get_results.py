# -*- coding: utf-8 -*-
"""get_results.fetch_tecai_results 的日期边界与编号白名单测试。

背景：体彩场次按销售日编号（如“周一001”），深夜场次跨零点开球，官方 API 的
matchDate 记为次日。旧逻辑用 matchDate == date_str 严格过滤，把跨零点场次全部
误杀，导致复盘为空。本测试锁定修复后的双重边界行为。
"""
import unittest
from unittest import mock

import get_results


def _fake_match(num, match_date, score="1:0", status="2"):
    return {
        "matchNumStr": num,
        "matchDate": match_date,
        "leagueName": "测试联赛",
        "allHomeTeam": "主队",
        "allAwayTeam": "客队",
        "goalLine": "-1",
        "sectionsNo1": "0:0",
        "sectionsNo999": score,
        "winFlag": "H",
        "matchResultStatus": status,
        "matchId": num,
    }


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _patch_api(matches):
    payload = {"success": True, "value": {"matchResult": matches}}
    return mock.patch("get_results.requests.get", return_value=_FakeResp(payload))


class TestFetchTecaiResults(unittest.TestCase):
    def test_cross_midnight_match_kept_with_whitelist(self):
        """销售日 2026-10-12 的周一001 在 API 中 matchDate=2026-10-13，必须保留。"""
        matches = [
            _fake_match("周一001", "2026-10-13"),   # 跨零点凌晨场次
            _fake_match("周日028", "2026-10-12"),   # 窗口内的前一销售日晚场
            _fake_match("周二001", "2026-10-13"),   # 相邻销售日场次
        ]
        with _patch_api(matches):
            got = get_results.fetch_tecai_results("2026-10-12", allowed_nums=["周一001"])
        nums = [r["match_num"] for r in got]
        self.assertEqual(nums, ["周一001"])

    def test_same_day_match_kept_without_whitelist(self):
        """无白名单时保留窗口两天内的场次，但拒绝窗口外日期。"""
        matches = [
            _fake_match("周日004", "2026-10-11"),
            _fake_match("周日020", "2026-10-12"),   # 跨零点，md=次日，保留
            _fake_match("周三001", "2026-10-15"),   # 窗口外，拒绝
        ]
        with _patch_api(matches):
            got = get_results.fetch_tecai_results("2026-10-11")
        nums = {r["match_num"] for r in got}
        self.assertEqual(nums, {"周日004", "周日020"})

    def test_whitelist_excludes_other_sales_day_even_same_matchdate(self):
        """白名单是精确按编号圈定，不受同 matchDate 的相邻销售日场次污染。"""
        matches = [
            _fake_match("周一001", "2026-10-13"),
            _fake_match("周二001", "2026-10-13"),  # 同为 10-13 但属次日销售
            _fake_match("周二002", "2026-10-13"),
        ]
        with _patch_api(matches):
            got = get_results.fetch_tecai_results(
                "2026-10-12", allowed_nums={"周一001", "周二999"})
        self.assertEqual([r["match_num"] for r in got], ["周一001"])

    def test_duplicate_num_deduped(self):
        matches = [
            _fake_match("周一001", "2026-10-13"),
            _fake_match("周一001", "2026-10-13"),
        ]
        with _patch_api(matches):
            got = get_results.fetch_tecai_results("2026-10-12", allowed_nums=["周一001"])
        self.assertEqual(len(got), 1)

    def test_empty_num_skipped(self):
        matches = [_fake_match("", "2026-10-13"), _fake_match("周一001", "2026-10-13")]
        with _patch_api(matches):
            got = get_results.fetch_tecai_results("2026-10-12", allowed_nums=["周一001"])
        self.assertEqual([r["match_num"] for r in got], ["周一001"])

    def test_api_failure_returns_empty_list(self):
        with mock.patch("get_results.requests.get", side_effect=Exception("network down")):
            self.assertEqual(get_results.fetch_tecai_results("2026-10-12"), [])
        with mock.patch("get_results.requests.get",
                        return_value=_FakeResp({"success": False, "errorMessage": "x"})):
            self.assertEqual(get_results.fetch_tecai_results("2026-10-12"), [])


if __name__ == "__main__":
    unittest.main()
