"""Apify Flashscore 适配层测试：口径（总进球阶梯/整数让球）、列结构与落盘幂等。"""

import json
import os
import tempfile
import unittest

import flashscore_odds as fo
import get_results_flashscore as gr


def market(bet_type, selections, bookmaker="bet365"):
    return {"bet_type": bet_type, "bookmaker": {"name": bookmaker},
            "selections": selections}


def sel(selection, value, handicap=None, score=None, active=True):
    out = {"selection": selection, "value": value, "is_active": active}
    if handicap is not None:
        out["handicap"] = handicap
    if score is not None:
        out["score"] = score
    return out


def spf_market(home=1.8, draw=3.5, away=4.2, bookmaker="bet365"):
    return market("HOME_DRAW_AWAY", [
        sel("home", home), sel("draw", draw), sel("away", away)
    ], bookmaker)


def ou_market():
    """半球盘 0.5..6.5 阶梯，under 概率 0.08..0.85（单调递增）。"""
    cum = [0.08, 0.20, 0.33, 0.47, 0.61, 0.74, 0.85]
    sels = []
    for k, p in enumerate(cum):
        line = k + 0.5
        sels.append(sel("under", round(1.0 / p, 2), handicap=line))
        sels.append(sel("over", round(1.0 / (1.0 - p), 2), handicap=line))
    return market("OVER_UNDER", sels)


def ah_market():
    """整数盘：主队 -1 / 客队 +1 最平衡。"""
    return market("ASIAN_HANDICAP", [
        sel("home", 1.9, handicap=-1), sel("away", 1.95, handicap=1),
        sel("home", 2.2, handicap=1), sel("away", 1.75, handicap=-1),
    ])


def crs_market():
    return market("CORRECT_SCORE", [
        sel("1:0", 6.0, score="1:0"),
        sel("2:1", 8.0, score="2:1"),
        sel("3:2", 12.0, score="3:2"),
        sel("0:0", 9.0, score="0:0"),
        sel("0:1", 7.5, score="0:1"),
        sel("1:1", 5.0, score="1:1"),
        sel("2:0", 10.0, score="2:0"),
        sel("0:2", 15.0, score="0:2"),
    ])


def item(status="scheduled", odds=None):
    return {
        "id": "abc123",
        "status": status,
        "start_time": "2026-11-08T12:00:00Z",
        "home_score": 2 if status == "finished" else None,
        "away_score": 1 if status == "finished" else None,
        "periods": [{"name": "2nd Half", "home": 1, "away": 1}],
        "home": {"name": "Home FC"},
        "away": {"name": "Away FC"},
        "competition": {"display_name": "ENGLAND: Premier League",
                        "name": "Premier League", "country": "England"},
        "odds": {"markets": odds or []},
    }


class TestNormalizeItem(unittest.TestCase):
    def test_full_item(self):
        markets = [spf_market(), ou_market(), ah_market(),
                   market("HALF_FULL_TIME", [
                       sel("1/1", 3.0), sel("1/X", 12.0),
                       sel("1/2", 25.0), sel("X/1", 7.0),
                       sel("X/X", 6.0), sel("X/2", 10.0),
                       sel("2/1", 20.0), sel("2/X", 13.0),
                       sel("2/2", 5.0),
                   ]), crs_market()]
        norm = fo.normalize_item(item(odds=markets),
                                 {"name": "英超", "full_name": "英格兰超级联赛"})
        self.assertEqual(norm["match_id"], "abc123")
        self.assertEqual(norm["league_name"], "英超")
        self.assertEqual(norm["odds_mapped"]["spf"]["主胜"], 1.8)
        self.assertIn("0球", norm["odds_mapped"]["ttg"])
        self.assertIn("7+球", norm["odds_mapped"]["ttg"])
        self.assertEqual(norm["odds_mapped"]["rq"]["让球数"], "-1")
        self.assertTrue(norm["odds_mapped"]["rq"]["平"])
        self.assertEqual(len(norm["odds_mapped"]["hafu"]), 9)
        self.assertTrue(norm["odds_flags"]["rq_draw_derived_from_crs"])

    def test_no_spf_raises(self):
        with self.assertRaises(ValueError):
            fo.normalize_item(item(odds=[ah_market()]))


class TestBookmakerSelection(unittest.TestCase):
    def test_crs_prefers_full_coverage_over_bet365(self):
        sparse = crs_market()  # bet365，8 档
        full = market("CORRECT_SCORE", sparse["selections"] + [
            sel("5:0", 30.0, score="5:0")], bookmaker="BetMGM.uk")
        norm = fo.normalize_item(item(odds=[spf_market(), sparse, full]))
        self.assertEqual(norm["odds_flags"]["crs_bookmaker"], "BetMGM.uk")
        self.assertIn("5:0", norm["odds_mapped"]["crs"])
        # 其他玩法仍优先 bet365
        self.assertEqual(norm["odds_flags"]["spf_bookmaker"], "bet365")


class TestTotalGoals(unittest.TestCase):
    def test_ladder_bounds_and_normalization(self):
        odds, flags = fo.derive_total_goals(ou_market())
        self.assertEqual(flags["ttg_source"], "over_under_ladder")
        self.assertEqual(len(odds), 8)
        self.assertGreater(odds["0球"], odds["1球"])
        self.assertLess(odds["7+球"], odds["3球"])
        total = sum(1.0 / v for v in odds.values())
        self.assertAlmostEqual(total, 1.0, places=2)

    def test_missing_market(self):
        odds, flags = fo.derive_total_goals({})
        self.assertEqual(odds, {})
        self.assertEqual(flags["ttg_source"], "missing")

    def test_quarter_line_marks_fallback(self):
        quarter = market("OVER_UNDER", [
            sel("under", 1.9, handicap=2.25), sel("over", 1.9, handicap=2.25),
            sel("under", 1.8, handicap=3.5), sel("over", 2.0, handicap=3.5),
        ])
        _, flags = fo.derive_total_goals(quarter)
        self.assertTrue(flags.get("ttg_quarter_fallback"))


class TestHandicap(unittest.TestCase):
    def test_integer_line_priority_and_draw(self):
        mapped, flags = fo.derive_handicap(ah_market(), crs_market())
        self.assertEqual(mapped["让球数"], "-1")
        self.assertEqual(flags["rq_candidates"], [-1, 1])
        self.assertTrue(flags["rq_draw_derived_from_crs"])
        self.assertGreater(mapped["平"], 1.01)

    def test_no_crs_leaves_draw_missing(self):
        mapped, flags = fo.derive_handicap(ah_market(), None)
        self.assertEqual(mapped["平"], "")
        self.assertTrue(flags["rq_draw_missing"])

    def test_quarter_handicap_ignored(self):
        m = market("ASIAN_HANDICAP", [
            sel("home", 1.9, handicap=-1.25), sel("away", 1.9, handicap=1.25),
            sel("home", 1.85, handicap=-1), sel("away", 1.95, handicap=1),
            sel("home", 2.05, handicap=1), sel("away", 1.75, handicap=-1),
        ])
        pairs = fo._integer_pairs(m)
        self.assertEqual(sorted(pairs), [-1, 1])


class TestHalfTimeScores(unittest.TestCase):
    def test_second_half_only(self):
        it = item(status="finished")
        self.assertEqual(fo.half_time_scores(it), (1, 0))

    def test_first_half_preferred(self):
        it = item(status="finished")
        it["periods"] = [{"name": "1st Half", "home": 1, "away": 1},
                         {"name": "2nd Half", "home": 1, "away": 0}]
        self.assertEqual(fo.half_time_scores(it), (1, 1))

    def test_missing_score(self):
        it = {"home_score": None, "away_score": None, "periods": []}
        self.assertEqual(fo.half_time_scores(it), (None, None))


class TestScheduleRow(unittest.TestCase):
    def test_six_three_columns(self):
        norm = fo.normalize_item(item(odds=[spf_market(), ou_market(),
                                            ah_market(), crs_market()]))
        row = fo.build_schedule_row(norm, "PL01")
        self.assertEqual(len(row), 63)
        self.assertEqual(row["编号"], "PL01")
        self.assertEqual(row["开赛时间"], "20:00")  # 12:00Z -> CST
        self.assertEqual(row["让球数"], "-1")


class TestStorage(unittest.TestCase):
    def test_jsonl_idempotent_merge(self):
        with tempfile.TemporaryDirectory() as td:
            rec_a = {"match_id": "a", "home": "A", "away": "B"}
            rec_b = {"match_id": "b", "home": "C", "away": "D"}
            path = fo.write_odds_jsonl([rec_a, rec_b], "2026-11-08", td)
            self.assertTrue(os.path.exists(path))
            rec_a2 = dict(rec_a, home="A2")
            fo.write_odds_jsonl([rec_a2], "2026-11-08", td)
            with open(path, encoding="utf-8") as fp:
                rows = [json.loads(line) for line in fp if line.strip()]
            self.assertEqual(len(rows), 2)
            got = {r["match_id"]: r["home"] for r in rows}
            self.assertEqual(got, {"a": "A2", "b": "C"})

    def test_save_raw_never_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            meta = {"run_id": "run1", "input": {"days": ["2026-11-08"]}}
            p1 = fo.save_raw([{"id": 1}], meta, "2026-11-08", td)
            p2 = fo.save_raw([{"id": 2}], meta, "2026-11-08", td)
            self.assertNotEqual(p1, p2)
            self.assertTrue(os.path.exists(p1) and os.path.exists(p2))


class TestResultsHelpers(unittest.TestCase):
    def test_is_finished(self):
        self.assertTrue(gr._is_finished({"status": "finished"}))
        self.assertTrue(gr._is_finished({"status": "FT"}))
        self.assertFalse(gr._is_finished({"status": "scheduled"}))

    def test_competitions_fallback_to_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            jsonl = os.path.join(td, "2026-11-07.jsonl")
            with open(jsonl, "w", encoding="utf-8") as fp:
                fp.write(json.dumps({"match_id": "1",
                                      "league_display": "ITALY: Serie A"}) + "\n")
            comps = gr.competitions_from_raw(
                os.path.join(td, "missing_raw.json"), jsonl)
            self.assertEqual(comps, ["ITALY: Serie A"])

    def test_competitions_from_raw_dedupes(self):
        with tempfile.TemporaryDirectory() as td:
            raw = os.path.join(td, "raw.json")
            payload = {"items": [
                {"fixture": {"league": {"flashscore": "ENGLAND: Championship"}}},
                {"fixture": {"league": {"flashscore": "ENGLAND: Championship"}}},
                {"fixture": {"league": {"flashscore": "SPAIN: LaLiga"}}},
            ]}
            with open(raw, "w", encoding="utf-8") as fp:
                json.dump(payload, fp)
            comps = gr.competitions_from_raw(raw, os.path.join(td, "no.jsonl"))
            self.assertEqual(comps, ["ENGLAND: Championship", "SPAIN: LaLiga"])


if __name__ == "__main__":
    unittest.main()
