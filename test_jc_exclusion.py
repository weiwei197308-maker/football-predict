"""竞彩排除逻辑测试：按联赛+开球时间剔除主环境已开售的同场次。"""

import csv
import os
import tempfile
import unittest

import get_matches_api as gm
import get_matches_flashscore as gmf
import settle_all


def make_fixture(league_name, kickoff_utc, home="Home", away="Away", fid=1):
    return ({"name": league_name, "api_id": 1, "priority": 1},
            {"fixture": {"id": fid, "date": kickoff_utc},
             "teams": {"home": {"name": home}, "away": {"name": away}}})


def write_jc_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=["联赛", "开赛日期", "开赛时间"])
        w.writeheader()
        w.writerows(rows)


class TestParseKickoff(unittest.TestCase):
    def test_seconds_and_minutes(self):
        self.assertEqual(gm._parse_kickoff("2026-11-07", "04:00:00"),
                         gm.datetime(2026, 11, 7, 4, 0))
        self.assertEqual(gm._parse_kickoff("2026-11-07", "04:00"),
                         gm.datetime(2026, 11, 7, 4, 0))

    def test_invalid_returns_none(self):
        self.assertIsNone(gm._parse_kickoff("", ""))
        self.assertIsNone(gm._parse_kickoff("2026-11-07", "abc"))


class TestLoadJcSold(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(gm.load_jc_sold_matches("2026-11-07", td), [])

    def test_reads_league_and_kickoff(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "竞彩足球_2026-11-07.csv")
            write_jc_csv(path, [{"联赛": "英超", "开赛日期": "2026-11-07",
                                 "开赛时间": "04:00:00"}])
            self.assertEqual(gm.load_jc_sold_matches("2026-11-07", td),
                             [("英超", gm.datetime(2026, 11, 7, 4, 0))])

    def test_skips_invalid_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "竞彩足球_2026-11-07.csv")
            write_jc_csv(path, [{"联赛": "", "开赛日期": "2026-11-07",
                                 "开赛时间": "04:00:00"}])
            self.assertEqual(gm.load_jc_sold_matches("2026-11-07", td), [])


class TestExcludeJcMatches(unittest.TestCase):
    def test_one_to_one_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            write_jc_csv(os.path.join(td, "竞彩足球_2026-11-07.csv"),
                         [{"联赛": "英超", "开赛日期": "2026-11-07",
                           "开赛时间": "04:00:00"}])
            picked = [make_fixture("英超", "2026-11-06T20:00:00+00:00")]
            kept, stats = gm.exclude_jc_matches(picked, "2026-11-07", td)
            self.assertEqual(kept, [])
            self.assertEqual(stats["excluded"], 1)
            self.assertEqual(stats["ambiguous"], 0)

    def test_utc_to_cst_date_shift(self):
        """UTC 20:00 是 CST 次日 04:00，需按 CST 对齐而非 UTC 日期。"""
        with tempfile.TemporaryDirectory() as td:
            write_jc_csv(os.path.join(td, "竞彩足球_2026-11-07.csv"),
                         [{"联赛": "英超", "开赛日期": "2026-11-07",
                           "开赛时间": "04:00:00"}])
            picked = [make_fixture("英超", "2026-11-06T20:00:00Z")]
            kept, _ = gm.exclude_jc_matches(picked, "2026-11-07", td)
            self.assertEqual(kept, [])

    def test_different_league_kept(self):
        with tempfile.TemporaryDirectory() as td:
            write_jc_csv(os.path.join(td, "竞彩足球_2026-11-07.csv"),
                         [{"联赛": "英超", "开赛日期": "2026-11-07",
                           "开赛时间": "04:00:00"}])
            picked = [make_fixture("法乙", "2026-11-06T20:00:00+00:00")]
            kept, stats = gm.exclude_jc_matches(picked, "2026-11-07", td)
            self.assertEqual(len(kept), 1)
            self.assertEqual(stats["excluded"], 0)

    def test_time_outside_window_kept(self):
        """04:07 与 04:00 不在同一 5 分钟桶，保留本环境场次。"""
        with tempfile.TemporaryDirectory() as td:
            write_jc_csv(os.path.join(td, "竞彩足球_2026-11-07.csv"),
                         [{"联赛": "英超", "开赛日期": "2026-11-07",
                           "开赛时间": "04:00:00"}])
            picked = [make_fixture("英超", "2026-11-06T20:07:00+00:00")]
            kept, stats = gm.exclude_jc_matches(picked, "2026-11-07", td)
            self.assertEqual(len(kept), 1)
            self.assertEqual(stats["ambiguous"], 0)

    def test_equal_counts_multi_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            write_jc_csv(os.path.join(td, "竞彩足球_2026-11-07.csv"),
                         [{"联赛": "法乙", "开赛日期": "2026-11-07", "开赛时间": "03:00:00"},
                          {"联赛": "法乙", "开赛日期": "2026-11-07", "开赛时间": "03:00:00"}])
            picked = [make_fixture("法乙", "2026-11-06T19:00:00+00:00", fid=1),
                      make_fixture("法乙", "2026-11-06T19:00:00+00:00", fid=2)]
            kept, stats = gm.exclude_jc_matches(picked, "2026-11-07", td)
            self.assertEqual(kept, [])
            self.assertEqual(stats["excluded"], 2)

    def test_ambiguous_counts_keeps_all(self):
        """竞彩只卖 1 场、本环境同桶 3 场，无法判断是哪场，全部保留。"""
        with tempfile.TemporaryDirectory() as td:
            write_jc_csv(os.path.join(td, "竞彩足球_2026-11-07.csv"),
                         [{"联赛": "法乙", "开赛日期": "2026-11-07",
                           "开赛时间": "03:00:00"}])
            picked = [make_fixture("法乙", "2026-11-06T19:00:00+00:00", fid=1),
                      make_fixture("法乙", "2026-11-06T19:00:00+00:00", fid=2),
                      make_fixture("法乙", "2026-11-06T19:00:00+00:00", fid=3)]
            kept, stats = gm.exclude_jc_matches(picked, "2026-11-07", td)
            self.assertEqual(len(kept), 3)
            self.assertEqual(stats["ambiguous"], 1)
            self.assertEqual(stats["excluded"], 0)

    def test_missing_main_file_keeps_everything(self):
        with tempfile.TemporaryDirectory() as td:
            picked = [make_fixture("英超", "2026-11-06T20:00:00+00:00")]
            kept, stats = gm.exclude_jc_matches(picked, "2026-11-07", td)
            self.assertEqual(len(kept), 1)
            self.assertEqual(stats["sold"], 0)


class TestFlashscoreExclusion(unittest.TestCase):
    """Apify 版竞彩排除：入参为标准化记录（cfg.name + kickoff_dt）。

    逻辑与 API 版同口径：同联赛 + 开球 5 分钟分桶，同桶数量相等才整组剔除。
    """

    @staticmethod
    def rec(league, kickoff, mid="1"):
        return {"cfg": {"name": league}, "kickoff_dt": kickoff, "match_id": mid}

    @staticmethod
    def jc(league, kickoff):
        return {"联赛": league,
                "开赛日期": kickoff.strftime("%Y-%m-%d"),
                "开赛时间": kickoff.strftime("%H:%M:%S")}

    def test_one_to_one_excluded(self):
        picked = [self.rec("英超", gmf.datetime(2026, 11, 7, 4, 0))]
        kept, excluded, stats = gmf.exclude_jc(
            picked, [self.jc("英超", gmf.datetime(2026, 11, 7, 4, 0))])
        self.assertEqual(kept, [])
        self.assertEqual(len(excluded), 1)
        self.assertEqual(stats["excluded"], 1)

    def test_time_outside_window_kept(self):
        picked = [self.rec("英超", gmf.datetime(2026, 11, 7, 4, 7))]
        kept, _, stats = gmf.exclude_jc(
            picked, [self.jc("英超", gmf.datetime(2026, 11, 7, 4, 0))])
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats["excluded"], 0)

    def test_ambiguous_counts_keeps_all(self):
        sold = [self.jc("法乙", gmf.datetime(2026, 11, 7, 3, 0))]
        picked = [self.rec("法乙", gmf.datetime(2026, 11, 7, 3, 0), str(i))
                  for i in range(3)]
        kept, _, stats = gmf.exclude_jc(picked, sold)
        self.assertEqual(len(kept), 3)
        self.assertEqual(stats["ambiguous"], 1)
        self.assertEqual(stats["excluded"], 0)


class TestResolveLeagueCfg(unittest.TestCase):
    def test_exact_and_normalized(self):
        cfg = gmf.resolve_league_cfg("ENGLAND: Premier League")
        self.assertEqual(cfg["name"], "英超")
        self.assertEqual(gmf.resolve_league_cfg("england premier league")["name"], "英超")

    def test_serie_c_group_alias(self):
        self.assertEqual(gmf.resolve_league_cfg("ITALY: Serie C - Group A")["api_id"], 138)

    def test_czechia_alias(self):
        self.assertEqual(
            gmf.resolve_league_cfg("CZECHIA: Chance Liga")["name"], "捷甲")

    def test_unknown_returns_none(self):
        self.assertIsNone(gmf.resolve_league_cfg("ITALY: Serie B Women"))


class TestSettleFindResult(unittest.TestCase):
    def test_prefers_league_home_away_key(self):
        sc = {"联赛": "英超", "home": "A", "away": "B", "matchId": "old"}
        results = {"英超|A|B": {"ft_home": 2}, "old": {"ft_home": 0}}
        self.assertEqual(settle_all.find_result(results, sc)["ft_home"], 2)

    def test_falls_back_to_match_id(self):
        sc = {"联赛": "英超", "home": "A", "away": "B", "matchId": "old"}
        results = {"old": {"ft_home": 0}}
        self.assertEqual(settle_all.find_result(results, sc)["ft_home"], 0)

    def test_missing_returns_none(self):
        self.assertIsNone(settle_all.find_result({}, None))


if __name__ == "__main__":
    unittest.main()
