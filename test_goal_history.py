"""goal-api h2h 适配层与换源开关的离线测试（HTTP 全部 mock，不联网）。"""

import importlib
import json
import os
import sys
import tempfile
import unittest
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
    def test_build_index_prefers_nested_team_ids(self):
        fixtures = [
            {"apiId": "746486",
             "homeTeam": {"id": "g-100", "apiId": "100", "name": "Home"},
             "awayTeam": {"id": "g-200", "apiId": "200", "name": "Away"}},
            {"apiId": "746490", "homeTeamId": "g-300", "awayTeamId": "g-400",
             "homeTeamName": "H2", "awayTeamName": "A2"},
            {"apiId": "", "homeTeamId": "x", "awayTeamId": "y"},
        ]
        idx = goal_history.build_fixture_index(fixtures)
        self.assertEqual(set(idx), {"746486", "746490"})
        self.assertEqual(idx["746486"]["home_team_id"], "g-100")
        self.assertEqual(idx["746490"]["away_team_id"], "g-400")

    def test_resolve_team_ids_from_index_uses_api_fixture_id(self):
        idx = {"746486": {"home_team_id": "g-100", "away_team_id": "g-200"}}
        with patch.object(goal_history, "fetch_single_fixture",
                          side_effect=AssertionError("不应发请求")):
            home, away = goal_history.resolve_team_ids("746486", idx)
        self.assertEqual((home, away), ("g-100", "g-200"))

    def test_resolve_team_ids_unmapped_returns_none(self):
        with patch.object(goal_history, "fetch_single_fixture", return_value=None):
            self.assertEqual(goal_history.resolve_team_ids("999", {}), (None, None))


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


class TestResolveHistoryFallback(unittest.TestCase):
    def test_goal_success_does_not_touch_api(self):
        with patch("get_matches_api.goal_history.fetch_history_goal",
                   return_value={"available": True, "home_recent_text": "g"}), \
             patch("get_matches_api.fetch_history",
                   side_effect=AssertionError("不应回退")):
            entry, src, err, _ = gma.resolve_history("1", 100, 200, {}, "goal", True)
        self.assertEqual(src, "goal")
        self.assertTrue(entry["available"])
        self.assertIsNone(err)

    def test_goal_failure_falls_back_to_api(self):
        with patch("get_matches_api.goal_history.fetch_history_goal",
                   side_effect=goal_history.GoalApiError("x", kind="quota")), \
             patch("get_matches_api.fetch_history",
                   return_value={"available": True, "home_recent_text": "api"}):
            entry, src, err, _ = gma.resolve_history("1", 100, 200, {}, "goal", True)
        self.assertEqual(src, "api")
        self.assertEqual(err, "quota")
        self.assertEqual(entry["home_recent_text"], "api")

    def test_goal_empty_result_falls_back_to_api(self):
        empty = {"available": False, "home_recent_text": "", "away_recent_text": "",
                 "h2h_text": ""}
        with patch("get_matches_api.goal_history.fetch_history_goal", return_value=empty), \
             patch("get_matches_api.fetch_history",
                   return_value={"available": True, "home_recent_text": "api"}):
            entry, src, err, goal_raw = gma.resolve_history("1", 100, 200, {}, "goal", True)
        self.assertEqual(src, "api")
        self.assertEqual(err, "empty")
        self.assertFalse(goal_raw["available"])
        self.assertEqual(entry["home_recent_text"], "api")

    def test_no_budget_returns_none(self):
        with patch("get_matches_api.goal_history.fetch_history_goal",
                   side_effect=goal_history.GoalApiError("x", kind="timeout")), \
             patch("get_matches_api.fetch_history",
                   side_effect=AssertionError("不应回退")):
            entry, src, err, _ = gma.resolve_history("1", 100, 200, {}, "goal", False)
        self.assertEqual(src, "none")
        self.assertEqual(err, "timeout")
        self.assertFalse(entry["available"])

    def test_api_mode_uses_api_only(self):
        with patch("get_matches_api.goal_history.fetch_history_goal",
                   side_effect=AssertionError("不应调用")), \
             patch("get_matches_api.fetch_history",
                   return_value={"available": True}):
            entry, src, err, _ = gma.resolve_history("1", 100, 200, {}, "api", True)
        self.assertEqual(src, "api")

    def test_unexpected_goal_error_is_contained(self):
        with patch("get_matches_api.goal_history.fetch_history_goal",
                   side_effect=KeyError("boom")), \
             patch("get_matches_api.fetch_history", return_value={"available": True}):
            entry, src, err, _ = gma.resolve_history("1", 100, 200, {}, "goal", True)
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
                patch("get_matches_api.goal_history.fetch_fixture_index",
                      return_value={"746486": {"home_team_id": "g-100",
                                               "away_team_id": "g-200"}}), \
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


if __name__ == "__main__":
    unittest.main()
