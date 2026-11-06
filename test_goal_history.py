"""goal-api h2h 适配层与换源开关的离线测试（HTTP 全部 mock，不联网）。"""

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

# 其他测试可能为便携运行注入 football_history 桩；这里恢复真实模块后再导入适配层。
import football_history
if not hasattr(football_history, "summarize_h2h"):
    sys.modules.pop("football_history", None)
    football_history = importlib.import_module("football_history")

import goal_history
import get_matches_api as gma


class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("bad json")
        return self._payload


def _env(**updates):
    base = {"GOAL_API_KEY": "test-goal-key-123456"}
    base.update(updates)
    return patch.dict(os.environ, base, clear=False)


def _h2h_payload():
    return {
        "success": True,
        "data": {
            "team1Id": "g-home", "team1Name": "Home",
            "team2Id": "g-away", "team2Name": "Away",
            "directMatches": [
                {"match_date": "2025-06-11", "match_time": "23:30",
                 "match_hometeam_id": "200", "match_hometeam_name": "Away",
                 "match_hometeam_score": "1", "match_awayteam_id": "100",
                 "match_awayteam_name": "Home", "match_awayteam_score": "2"},
            ],
            "team1Recent": [
                {"match_date": "2026-11-04", "match_time": "19:30",
                 "match_hometeam_id": "100", "match_hometeam_name": "Home",
                 "match_hometeam_score": "2", "match_awayteam_id": "300",
                 "match_awayteam_name": "OppA", "match_awayteam_score": "0"},
                {"match_date": "2026-11-01", "match_time": "12:00",
                 "match_hometeam_id": "301", "match_hometeam_name": "OppB",
                 "match_hometeam_score": "1", "match_awayteam_id": "100",
                 "match_awayteam_name": "Home", "match_awayteam_score": "1"},
            ],
            "team2Recent": [
                {"match_date": "2026-11-02", "match_time": "20:00",
                 "match_hometeam_id": "200", "match_hometeam_name": "Away",
                 "match_hometeam_score": "0", "match_awayteam_id": "302",
                 "match_awayteam_name": "OppC", "match_awayteam_score": "3"},
            ],
        },
    }


class TestGoalKey(unittest.TestCase):
    def test_missing_key_fail_fast(self):
        with patch.dict(os.environ, {"GOAL_API_KEY": ""}, clear=False):
            with self.assertRaises(goal_history.GoalApiError) as ctx:
                goal_history.get_goal_api_key()
        self.assertEqual(ctx.exception.kind, "config")
        self.assertIn("GOAL_API_KEY", str(ctx.exception))

    def test_key_with_whitespace_rejected(self):
        with patch.dict(os.environ, {"GOAL_API_KEY": "abc def ghi jkl"}, clear=False):
            with self.assertRaises(goal_history.GoalApiError):
                goal_history.get_goal_api_key()

    def test_short_key_rejected(self):
        with patch.dict(os.environ, {"GOAL_API_KEY": "short"}, clear=False):
            with self.assertRaises(goal_history.GoalApiError):
                goal_history.get_goal_api_key()

    def test_error_message_never_contains_key(self):
        secret = "secret-key-value-123456"
        with patch.dict(os.environ, {"GOAL_API_KEY": ""}, clear=False):
            try:
                goal_history.get_goal_api_key()
            except goal_history.GoalApiError as e:
                self.assertNotIn(secret, str(e))


class TestGoalGet(unittest.TestCase):
    def test_bearer_header_and_data(self):
        with _env():
            with patch.object(goal_history.requests, "get",
                              return_value=FakeResponse(200, {"success": True, "data": [1]})) as g:
                out = goal_history.goal_get("h2h/a/b")
        self.assertEqual(out, {"success": True, "data": [1]})
        headers = g.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-goal-key-123456")

    def test_401_auth_error(self):
        with _env():
            with patch.object(goal_history.requests, "get",
                              return_value=FakeResponse(401, {})):
                with self.assertRaises(goal_history.GoalApiError) as ctx:
                    goal_history.goal_get("h2h/a/b")
        self.assertEqual(ctx.exception.kind, "auth")

    def test_429_quota_error(self):
        with _env():
            with patch.object(goal_history.requests, "get",
                              return_value=FakeResponse(429, {})):
                with self.assertRaises(goal_history.GoalApiError) as ctx:
                    goal_history.goal_get("h2h/a/b")
        self.assertEqual(ctx.exception.kind, "quota")

    def test_bad_json_is_parse_error(self):
        with _env():
            with patch.object(goal_history.requests, "get",
                              return_value=FakeResponse(200, bad_json=True)):
                with self.assertRaises(goal_history.GoalApiError) as ctx:
                    goal_history.goal_get("h2h/a/b")
        self.assertEqual(ctx.exception.kind, "parse")

    def test_success_false_is_parse_error(self):
        with _env():
            with patch.object(goal_history.requests, "get",
                              return_value=FakeResponse(200, {"success": False, "data": {}})):
                with self.assertRaises(goal_history.GoalApiError):
                    goal_history.goal_get("h2h/a/b")


class TestFixtureIndexAndTeamMapping(unittest.TestCase):
    def test_build_index_groups_by_kickoff_minute(self):
        fixtures = [
            {"kickoffUtc": "2026-11-05T17:45:00.000Z",
             "homeTeam": {"id": "g-100", "name": "AC Milan"},
             "awayTeam": {"id": "g-200", "name": "Ferencvaros"},
             "leagueName": "UEFA Europa League - League Phase"},
            {"kickoffUtc": "2026-11-05T20:00:00Z", "homeTeamId": "g-300",
             "awayTeamId": "g-400", "homeTeamName": "H2", "awayTeamName": "A2"},
            {"kickoffUtc": "bad", "homeTeamId": "x", "awayTeamId": "y"},
        ]
        idx = goal_history.build_fixture_index(fixtures)
        self.assertEqual(set(idx), {"2026-11-05T17:45", "2026-11-05T20:00"})
        self.assertEqual(idx["2026-11-05T17:45"][0]["home_team_id"], "g-100")
        self.assertEqual(idx["2026-11-05T20:00"][0]["away_team_id"], "g-400")

    def test_resolve_team_ids_fuzzy_names_and_kickoff(self):
        idx = {"2026-11-05T17:45": [
            {"home_team_id": "g-100", "away_team_id": "g-200",
             "home_name": "AC Milan", "away_name": "Ferencvaros",
             "league_name": "UEFA Europa League - League Phase"}]}
        home, away = goal_history.resolve_team_ids(
            "2026-11-05T17:45:00+00:00", "AC Milan", "Ferencvarosi TC",
            "UEFA Europa League", idx)
        self.assertEqual((home, away), ("g-100", "g-200"))

    def test_resolve_team_ids_league_breaks_tie(self):
        idx = {"2026-11-05T17:45": [
            {"home_team_id": "x1", "away_team_id": "x2", "home_name": "Milan",
             "away_name": "Inter", "league_name": "Serie A"},
            {"home_team_id": "g-100", "away_team_id": "g-200", "home_name": "AC Milan",
             "away_name": "Inter", "league_name": "UEFA Europa League"}]}
        home, _ = goal_history.resolve_team_ids(
            "2026-11-05T17:45:00Z", "AC Milan", "Inter", "UEFA Europa League", idx)
        self.assertEqual(home, "g-100")

    def test_resolve_team_ids_unmapped_returns_none(self):
        self.assertEqual(goal_history.resolve_team_ids(
            "2026-11-05T17:45:00Z", "Home", "Away", "L", {}), (None, None))

    def test_index_page_limit_within_real_api_max(self):
        # 实测 /fixtures/date 的 limit 上限为 100（文档 500 会返回 400）
        self.assertLessEqual(goal_history.INDEX_PAGE_LIMIT, 100)

    def test_goal_api_id_is_not_used_as_key(self):
        # 实测 goal-api apiId 与 API-Football fixture id 不同源，索引不再按 apiId
        idx = goal_history.build_fixture_index(
            [{"apiId": "1636208", "kickoffUtc": "2026-11-05T17:45:00Z",
              "homeTeam": {"id": "g1", "name": "A"}, "awayTeam": {"id": "g2", "name": "B"}}])
        self.assertNotIn("1636208", idx)
        self.assertIn("2026-11-05T17:45", idx)

    def test_resolve_team_ids_within_kickoff_window(self):
        # 两数据源开球时间差 2 分钟，容差窗内应命中
        idx = {"2026-11-05T17:47": [
            {"home_team_id": "g-100", "away_team_id": "g-200",
             "home_name": "AC Milan", "away_name": "Ferencvaros",
             "league_name": "UEFA Europa League"}]}
        home, away = goal_history.resolve_team_ids(
            "2026-11-05T17:45:00+00:00", "AC Milan", "Ferencvaros",
            "UEFA Europa League", idx)
        self.assertEqual((home, away), ("g-100", "g-200"))

    def test_resolve_team_ids_window_boundary_and_outside(self):
        entry = {"home_team_id": "g-100", "away_team_id": "g-200",
                 "home_name": "AC Milan", "away_name": "Ferencvaros",
                 "league_name": "L"}
        idx_in = {"2026-11-05T18:00": [entry]}   # 恰好 15 分钟，边界内
        idx_out = {"2026-11-05T18:01": [entry]}  # 16 分钟，窗外
        self.assertEqual(goal_history.resolve_team_ids(
            "2026-11-05T17:45:00Z", "AC Milan", "Ferencvaros", "L", idx_in),
            ("g-100", "g-200"))
        self.assertEqual(goal_history.resolve_team_ids(
            "2026-11-05T17:45:00Z", "AC Milan", "Ferencvaros", "L", idx_out),
            (None, None))

    def test_resolve_team_ids_swapped_home_away(self):
        # goal-api 主客顺序相反时应按查询侧视角换回 id
        idx = {"2026-11-05T17:45": [
            {"home_team_id": "g-200", "away_team_id": "g-100",
             "home_name": "Ferencvaros", "away_name": "AC Milan",
             "league_name": "UEFA Europa League"}]}
        home, away = goal_history.resolve_team_ids(
            "2026-11-05T17:45:00Z", "AC Milan", "Ferencvarosi TC",
            "UEFA Europa League", idx)
        self.assertEqual((home, away), ("g-100", "g-200"))

    def test_resolve_team_ids_prefers_direct_over_swapped(self):
        # 互换候选（0 分钟）与正向候选（5 分钟）并存时，正向优先
        idx = {
            "2026-11-05T17:45": [
                {"home_team_id": "s-away", "away_team_id": "s-home",
                 "home_name": "Ferencvaros", "away_name": "AC Milan",
                 "league_name": "UEFA Europa League"}],
            "2026-11-05T17:50": [
                {"home_team_id": "g-100", "away_team_id": "g-200",
                 "home_name": "AC Milan", "away_name": "Ferencvaros",
                 "league_name": "UEFA Europa League"}],
        }
        home, away = goal_history.resolve_team_ids(
            "2026-11-05T17:45:00Z", "AC Milan", "Ferencvaros",
            "UEFA Europa League", idx)
        self.assertEqual((home, away), ("g-100", "g-200"))

    def test_resolve_team_ids_prefers_closer_kickoff_on_score_tie(self):
        # 同为正向同分时，取时间差更小者
        far = {"home_team_id": "far-h", "away_team_id": "far-a",
               "home_name": "AC Milan", "away_name": "Ferencvaros",
               "league_name": "Other League"}
        near = {"home_team_id": "g-100", "away_team_id": "g-200",
                "home_name": "AC Milan", "away_name": "Ferencvaros",
                "league_name": "Other League"}
        idx = {"2026-11-05T17:55": [far], "2026-11-05T17:46": [near]}
        home, away = goal_history.resolve_team_ids(
            "2026-11-05T17:45:00Z", "AC Milan", "Ferencvaros", "L", idx)
        self.assertEqual((home, away), ("g-100", "g-200"))


class TestParseH2H(unittest.TestCase):
    def test_contract_keys_and_values(self):
        out = goal_history.parse_h2h_payload(_h2h_payload())
        self.assertEqual(set(out), {"home_recent", "away_recent", "h2h",
                                    "home_recent_text", "away_recent_text",
                                    "h2h_text", "available"})
        self.assertTrue(out["available"])
        self.assertIn("历史交锋", out["h2h_text"])
        self.assertIn("近2场", out["home_recent_text"])
        self.assertIn("近1场", out["away_recent_text"])

    def test_h2h_score_orientation_and_cst_date(self):
        out = goal_history.parse_h2h_payload(_h2h_payload())
        # 23:30 UTC + 8h -> 次日
        self.assertEqual(out["h2h"][0]["date"], "2025-06-12")
        self.assertEqual(out["h2h"][0]["score"], "1-2")

    def test_recent_home_away_and_result(self):
        out = goal_history.parse_h2h_payload(_h2h_payload())
        rec = {r["opponent"]: r for r in out["home_recent"]}
        self.assertEqual(rec["OppA"]["home_away"], "H")
        self.assertEqual(rec["OppA"]["result"], "W")
        self.assertEqual(rec["OppB"]["home_away"], "A")
        self.assertEqual(rec["OppB"]["result"], "D")
        self.assertEqual(out["away_recent"][0]["result"], "L")

    def test_recent_team_name_normalization(self):
        payload = {"success": True, "data": {
            "team1Id": "g1", "team1Name": "El Nacional", "team2Id": "g2",
            "team2Name": "9 de Octubre", "directMatches": [],
            "team1Recent": [{"match_date": "2026-11-01", "match_time": "10:00",
                             "match_hometeam_name": "EL NACIONAL",
                             "match_hometeam_score": "1",
                             "match_awayteam_name": "Barcelona SC",
                             "match_awayteam_score": "0"}],
            "team2Recent": [],
        }}
        out = goal_history.parse_h2h_payload(payload)
        self.assertEqual(out["home_recent"][0]["opponent"], "Barcelona SC")
        self.assertEqual(out["home_recent"][0]["result"], "W")

    def test_empty_lists_available_false(self):
        payload = {"success": True, "data": {"directMatches": [], "team1Recent": [],
                                             "team2Recent": []}}
        out = goal_history.parse_h2h_payload(payload)
        self.assertFalse(out["available"])
        self.assertEqual(out["h2h_text"], "")

    def test_missing_fields_raise_parse_error(self):
        with self.assertRaises(goal_history.GoalApiError) as ctx:
            goal_history.parse_h2h_payload({"success": True, "data": {}})
        self.assertEqual(ctx.exception.kind, "parse")


KICKOFF = "2026-11-05T11:00:00+00:00"


class TestResolveHistoryFallback(unittest.TestCase):
    def test_goal_success_does_not_touch_api(self):
        with patch("get_matches_api.goal_history.fetch_history_goal",
                   return_value={"available": True, "home_recent_text": "g"}), \
             patch("get_matches_api.fetch_history",
                   side_effect=AssertionError("不应回退")):
            entry, src, err, _ = gma.resolve_history(KICKOFF, "Home", "Away", "L", 100, 200, {}, "goal", True)
        self.assertEqual(src, "goal")
        self.assertTrue(entry["available"])
        self.assertIsNone(err)

    def test_goal_failure_falls_back_to_api(self):
        with patch("get_matches_api.goal_history.fetch_history_goal",
                   side_effect=goal_history.GoalApiError("x", kind="quota")), \
             patch("get_matches_api.fetch_history",
                   return_value={"available": True, "home_recent_text": "api"}):
            entry, src, err, _ = gma.resolve_history(KICKOFF, "Home", "Away", "L", 100, 200, {}, "goal", True)
        self.assertEqual(src, "api")
        self.assertEqual(err, "quota")
        self.assertEqual(entry["home_recent_text"], "api")

    def test_goal_empty_result_falls_back_to_api(self):
        empty = {"available": False, "home_recent_text": "", "away_recent_text": "",
                 "h2h_text": ""}
        with patch("get_matches_api.goal_history.fetch_history_goal", return_value=empty), \
             patch("get_matches_api.fetch_history",
                   return_value={"available": True, "home_recent_text": "api"}):
            entry, src, err, goal_raw = gma.resolve_history(KICKOFF, "Home", "Away", "L", 100, 200, {}, "goal", True)
        self.assertEqual(src, "api")
        self.assertEqual(err, "empty")
        self.assertFalse(goal_raw["available"])
        self.assertEqual(entry["home_recent_text"], "api")

    def test_no_budget_returns_none(self):
        with patch("get_matches_api.goal_history.fetch_history_goal",
                   side_effect=goal_history.GoalApiError("x", kind="timeout")), \
             patch("get_matches_api.fetch_history",
                   side_effect=AssertionError("不应回退")):
            entry, src, err, _ = gma.resolve_history(KICKOFF, "Home", "Away", "L", 100, 200, {}, "goal", False)
        self.assertEqual(src, "none")
        self.assertEqual(err, "timeout")
        self.assertFalse(entry["available"])

    def test_api_mode_uses_api_only(self):
        with patch("get_matches_api.goal_history.fetch_history_goal",
                   side_effect=AssertionError("不应调用")), \
             patch("get_matches_api.fetch_history",
                   return_value={"available": True}):
            entry, src, err, _ = gma.resolve_history(KICKOFF, "Home", "Away", "L", 100, 200, {}, "api", True)
        self.assertEqual(src, "api")

    def test_unexpected_goal_error_is_contained(self):
        with patch("get_matches_api.goal_history.fetch_history_goal",
                   side_effect=KeyError("boom")), \
             patch("get_matches_api.fetch_history", return_value={"available": True}):
            entry, src, err, _ = gma.resolve_history(KICKOFF, "Home", "Away", "L", 100, 200, {}, "goal", True)
        self.assertEqual(src, "api")
        self.assertIn("unexpected:KeyError", err)


class TestBudgetModel(unittest.TestCase):
    def test_goal_mode_one_request_per_match(self):
        self.assertEqual(gma.max_matches_for_budget(10, "goal", 0), 10)

    def test_api_mode_two_requests_per_match(self):
        self.assertEqual(gma.max_matches_for_budget(10, "api", 0), 5)

    def test_compare_reserves_first_n_extra_requests(self):
        self.assertEqual(gma.max_matches_for_budget(10, "compare", 3), 7)

    def test_compare_small_budget(self):
        self.assertEqual(gma.max_matches_for_budget(4, "compare", 3), 2)

    def test_zero_budget(self):
        self.assertEqual(gma.max_matches_for_budget(0, "goal", 0), 0)


class TestCompareArtifact(unittest.TestCase):
    def _record(self):
        goal = {"available": True, "home_recent_text": "g", "away_recent_text": "",
                "h2h_text": "g"}
        api = {"available": True, "home_recent_text": "a", "away_recent_text": "",
               "h2h_text": "a"}
        return gma.build_compare_record("PL01", "746486", "Home", "Away",
                                        goal, api, None, None)

    def test_record_structure(self):
        rec = self._record()
        self.assertEqual(set(rec), {"number", "matchId", "home", "away", "goal",
                                    "api", "goal_error", "api_error", "diff"})
        self.assertEqual(rec["matchId"], "746486")
        self.assertFalse(rec["diff"]["home_recent_text"])

    def test_save_report_structure_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            path = gma.save_compare_report([self._record()], "2026-11-05", td)
            self.assertEqual(os.path.basename(path), "shadow_h2h_compare_2026-11-05.json")
            with open(path, encoding="utf-8") as fp:
                payload = json.load(fp)
            self.assertEqual(payload["date"], "2026-11-05")
            self.assertEqual(payload["summary"]["matches"], 1)
            self.assertEqual(len(payload["items"]), 1)
            # 同日重跑不得覆盖既有产物
            path2 = gma.save_compare_report([self._record()], "2026-11-05", td)
            self.assertNotEqual(path, path2)


class TestMainFlowOffline(unittest.TestCase):
    """compare 模式主流程离线跑通：CSV 列不变、raw 标注来源、对照产物落盘。"""

    FIXTURE = {
        "fixture": {"id": 746486, "date": "2026-11-05T11:00:00+00:00",
                    "status": {"short": "NS"}},
        "league": {"id": 39, "name": "Premier League", "season": 2025},
        "teams": {"home": {"id": 100, "name": "Home"},
                  "away": {"id": 200, "name": "Away"}},
    }
    ODDS = [{"bookmakers": [{"id": 8, "bets": [
        {"name": "Match Winner", "values": [
            {"value": "Home", "odd": "1.50"},
            {"value": "Draw", "odd": "3.20"},
            {"value": "Away", "odd": "5.00"}]}]}]}]

    def test_compare_mode_writes_contract_and_shadow(self):
        import csv
        import tempfile

        def fake_api_get(path, params):
            return [self.FIXTURE] if path == "fixtures" else self.ODDS

        goal_entry = {"available": True, "home_recent_text": "g-home",
                      "away_recent_text": "g-away", "h2h_text": "g-h2h"}
        api_entry = {"available": True, "home_recent_text": "a-home",
                     "away_recent_text": "a-away", "h2h_text": "a-h2h"}

        with tempfile.TemporaryDirectory() as td, _env(), \
                patch.object(sys, "argv", ["get_matches_api.py", "--date", "2026-11-05",
                                           "--output-dir", td]), \
                patch.object(gma, "get_remaining", return_value=100), \
                patch.object(gma, "api_get", side_effect=fake_api_get), \
                patch.object(gma, "_req_count", 0), \
                patch.object(gma, "load_or_fetch_goal_rosters", return_value={}), \
                patch("get_matches_api.goal_history.fetch_fixture_index",
                      return_value={"2026-11-05T11:00": [
                          {"home_team_id": "g-100", "away_team_id": "g-200",
                           "home_name": "Home", "away_name": "Away",
                           "league_name": "Premier League"}]}), \
                patch("get_matches_api.goal_history.fetch_history_goal",
                      return_value=goal_entry), \
                patch("get_matches_api.fetch_history", return_value=api_entry):
            gma._req_count = 0
            gma.main()
            with open(os.path.join(td, "竞彩足球_2026-11-05.csv"),
                      encoding="utf-8-sig") as fp:
                header = next(csv.reader(fp))
            with open(os.path.join(td, "竞彩足球_2026-11-05_raw.json"),
                      encoding="utf-8") as fp:
                raws = json.load(fp)
            with open(os.path.join(td, "history_cache_2026-11-05.json"),
                      encoding="utf-8") as fp:
                cache = json.load(fp)
            with open(os.path.join(td, "shadow_h2h_compare_2026-11-05.json"),
                      encoding="utf-8") as fp:
                cmp = json.load(fp)

        self.assertNotIn("history_source", header)
        self.assertIn("matchId", header)
        self.assertEqual(raws[0]["history_source"], "goal")
        self.assertEqual(cache["746486"]["home_recent_text"], "g-home")
        self.assertEqual(cmp["items"][0]["matchId"], "746486")
        self.assertEqual(cmp["summary"]["matches"], 1)
        self.assertFalse(cmp["items"][0]["diff"]["home_recent_text"])


class TestCsvContractUnchanged(unittest.TestCase):
    def test_build_row_has_no_history_source_column(self):
        fixture = {
            "fixture": {"id": 1, "date": "2026-11-05T11:00:00+00:00",
                        "status": {"short": "NS"}},
            "teams": {"home": {"name": "H"}, "away": {"name": "A"}},
        }
        mapped = {"spf": {"主胜": "1.5", "平": "3.2", "客胜": "5"},
                  "rq": {}, "ttg": {}, "hafu": {}, "crs": {}}
        row = gma.build_row(fixture, mapped, "PL01",
                            {"name": "L", "full_name": "LF"})
        self.assertNotIn("history_source", row)
        self.assertIn("matchId", row)
        self.assertEqual(row["让球数"], "")


class TestApiThrottle(unittest.TestCase):
    def test_first_call_no_sleep(self):
        with patch.object(gma, "_last_api_call_at", 0.0), \
                patch.object(gma.time, "monotonic", return_value=100.0), \
                patch.object(gma.time, "sleep") as sl:
            gma._throttle_api()
        sl.assert_not_called()

    def test_second_call_sleeps_remaining_gap(self):
        with patch.object(gma, "API_MIN_INTERVAL_SECONDS", 1.0), \
                patch.object(gma, "_last_api_call_at", 100.0), \
                patch.object(gma.time, "monotonic", return_value=100.5), \
                patch.object(gma.time, "sleep") as sl:
            gma._throttle_api()
        sl.assert_called_once()
        self.assertAlmostEqual(sl.call_args.args[0], 0.5)

    def test_no_sleep_when_gap_already_elapsed(self):
        with patch.object(gma, "API_MIN_INTERVAL_SECONDS", 1.0), \
                patch.object(gma, "_last_api_call_at", 100.0), \
                patch.object(gma.time, "monotonic", return_value=101.5), \
                patch.object(gma.time, "sleep") as sl:
            gma._throttle_api()
        sl.assert_not_called()

    def test_disabled_when_zero(self):
        with patch.object(gma, "API_MIN_INTERVAL_SECONDS", 0), \
                patch.object(gma.time, "sleep") as sl:
            gma._throttle_api()
        sl.assert_not_called()


class TestGoalGetRetry(unittest.TestCase):
    def test_timeout_then_success(self):
        ok = FakeResponse(200, {"success": True, "data": [1]})
        with _env(), patch.object(goal_history.time, "sleep"), \
                patch.object(goal_history.requests, "get",
                             side_effect=[goal_history.requests.Timeout("t"), ok]) as g:
            out = goal_history.goal_get("h2h/a/b")
        self.assertEqual(out, {"success": True, "data": [1]})
        self.assertEqual(g.call_count, 2)

    def test_retries_exhausted_raises_timeout(self):
        with _env(), patch.object(goal_history.time, "sleep"), \
                patch.object(goal_history.requests, "get",
                             side_effect=goal_history.requests.Timeout("t")) as g:
            with self.assertRaises(goal_history.GoalApiError) as ctx:
                goal_history.goal_get("h2h/a/b")
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertEqual(g.call_count, goal_history.HTTP_RETRIES)

    def test_auth_error_not_retried(self):
        with _env(), patch.object(goal_history.requests, "get",
                                  return_value=FakeResponse(401, {})) as g:
            with self.assertRaises(goal_history.GoalApiError):
                goal_history.goal_get("h2h/a/b")
        self.assertEqual(g.call_count, 1)


class TestLeagueGoalIdMap(unittest.TestCase):
    def test_all_whitelisted_leagues_have_goal_id(self):
        from leagues_config import LEAGUES
        for lg in LEAGUES:
            self.assertIsNotNone(goal_history.league_goal_id(lg["api_id"]),
                                 f"缺映射：{lg['name']}")

    def test_goal_ids_unique(self):
        ids = list(goal_history.GOAL_LEAGUE_IDS.values())
        self.assertEqual(len(ids), len(set(ids)))

    def test_unknown_league_returns_none(self):
        self.assertIsNone(goal_history.league_goal_id(99999))
        self.assertIsNone(goal_history.league_goal_id(None))


class TestRosterMapping(unittest.TestCase):
    ROSTER = [("Everton", "g-ev"), ("Coventry City", "g-cov"),
              ("Hamburger SV", "g-hsv"), ("Borussia M'gladbach", "g-bmg"),
              ("Derby County", "g-der"), ("Bolton Wanderers", "g-bol"),
              ("Red Star", "g-red"), ("Toulouse", "g-tou")]

    def test_contains_match_for_short_names(self):
        # 实测 API-Football 简称 Derby / Bolton，goal 全称 Derby County / Bolton Wanderers
        self.assertEqual(goal_history.resolve_team_ids_from_roster(
            "Derby", "Bolton", self.ROSTER), ("g-der", "g-bol"))

    def test_abbreviated_club_name_similarity(self):
        self.assertEqual(goal_history.resolve_team_ids_from_roster(
            "Borussia Mönchengladbach", "Hamburger SV", self.ROSTER),
            ("g-bmg", "g-hsv"))

    def test_suffix_variant(self):
        self.assertEqual(goal_history.resolve_team_ids_from_roster(
            "Everton", "Coventry", self.ROSTER), ("g-ev", "g-cov"))

    def test_same_team_rejected(self):
        self.assertEqual(goal_history.resolve_team_ids_from_roster(
            "Everton", "Everton", self.ROSTER), (None, None))

    def test_empty_roster(self):
        self.assertEqual(goal_history.resolve_team_ids_from_roster(
            "A", "B", []), (None, None))

    def test_low_similarity_rejected(self):
        self.assertEqual(goal_history.resolve_team_ids_from_roster(
            "Zenit", "Krasnodar", self.ROSTER), (None, None))


class TestFetchLeagueRoster(unittest.TestCase):
    def test_pagination_and_parse(self):
        pages = [
            FakeResponse(200, {"success": True, "pagination": {"hasMore": True},
                               "data": [{"name": "A", "id": "g-a"}]}),
            FakeResponse(200, {"success": True, "pagination": {"hasMore": False},
                               "data": [{"name": "B", "id": "g-b"}]}),
        ]
        with _env(), patch.object(goal_history.requests, "get", side_effect=pages):
            roster = goal_history.fetch_league_roster("lg-x")
        self.assertEqual(roster, [("A", "g-a"), ("B", "g-b")])

    def test_non_list_data_is_parse_error(self):
        with _env(), patch.object(goal_history.requests, "get",
                                  return_value=FakeResponse(200, {"success": True, "data": {}})):
            with self.assertRaises(goal_history.GoalApiError) as ctx:
                goal_history.fetch_league_roster("lg-x")
        self.assertEqual(ctx.exception.kind, "parse")


class TestFetchHistoryGoalRosterFirst(unittest.TestCase):
    def test_roster_hit_skips_index(self):
        roster = [("Home", "g-h"), ("Away", "g-a")]
        with _env(), patch.object(goal_history, "goal_get",
                                  return_value=_h2h_payload()) as g, \
                patch.object(goal_history, "resolve_team_ids",
                             side_effect=AssertionError("roster 命中时不应查索引")):
            out = goal_history.fetch_history_goal(KICKOFF, "Home", "Away", "L", None, roster)
        self.assertTrue(out["available"])
        self.assertEqual(g.call_args.args[0], "h2h/g-h/g-a")

    def test_roster_miss_falls_back_to_index(self):
        roster = [("Other", "g-o"), ("Another", "g-x")]
        idx = {"2026-11-05T11:00": [{"home_team_id": "i-h", "away_team_id": "i-a",
                                     "home_name": "Home XI", "away_name": "Away XI",
                                     "league_name": "L"}]}
        with _env(), patch.object(goal_history, "goal_get",
                                  return_value=_h2h_payload()) as g:
            goal_history.fetch_history_goal(KICKOFF, "Home XI", "Away XI", "L", idx, roster)
        self.assertEqual(g.call_args.args[0], "h2h/i-h/i-a")


class TestRosterCache(unittest.TestCase):
    def test_cache_hit_avoids_network(self):
        picked = [(None, {"league": {"id": 39}})]
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, gma.GOAL_ROSTER_CACHE), "w", encoding="utf-8") as fp:
                json.dump({"updated": str(date.today()),
                           "leagues": {"39": {"goal_id": "lg",
                                              "teams": [["Everton", "g-e"]]}}}, fp)
            with patch.object(gma.goal_history, "fetch_league_roster",
                              side_effect=AssertionError("缓存已命中，不应请求")):
                rosters = gma.load_or_fetch_goal_rosters(picked, td)
        self.assertEqual(rosters["39"], [("Everton", "g-e")])

    def test_cache_miss_fetches_and_writes(self):
        picked = [(None, {"league": {"id": 39}})]
        with tempfile.TemporaryDirectory() as td, \
                patch.object(gma.goal_history, "fetch_league_roster",
                             return_value=[("Everton", "g-e")]) as f:
            rosters = gma.load_or_fetch_goal_rosters(picked, td)
            self.assertEqual(rosters["39"], [("Everton", "g-e")])
            self.assertTrue(os.path.exists(os.path.join(td, gma.GOAL_ROSTER_CACHE)))
        self.assertEqual(f.call_count, 1)

    def test_stale_cache_refetches(self):
        picked = [(None, {"league": {"id": 39}})]
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, gma.GOAL_ROSTER_CACHE), "w", encoding="utf-8") as fp:
                json.dump({"updated": "2020-01-01",
                           "leagues": {"39": {"goal_id": "lg",
                                              "teams": [["Old", "g-old"]]}}}, fp)
            with patch.object(gma.goal_history, "fetch_league_roster",
                              return_value=[("New", "g-new")]):
                rosters = gma.load_or_fetch_goal_rosters(picked, td)
        self.assertEqual(rosters["39"], [("New", "g-new")])


if __name__ == "__main__":
    unittest.main()
