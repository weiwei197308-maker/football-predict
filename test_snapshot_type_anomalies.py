import sys, types, unittest
# Current uploaded analyze.py imports football_history, which is not part of this upload.
fh = types.ModuleType("football_history")
fh.build_history_for_match = lambda *args, **kwargs: ""
sys.modules.setdefault("football_history", fh)

import compare_report as cr

def make_row(**kw):
    row = {
        "编号":"周日001", "联赛":"测试联赛", "对阵":"主队A vs 客队B", "体彩让球数":"-1",
        "胜平负推荐":"主胜", "让球推荐":"让-1主胜", "半全场推荐":"胜胜",
        "总进球推荐":"2球 / 3球", "比分推荐":"1:0 / 2:1", "净胜球分布":"主胜1",
        "核心剧本":"测试剧本", "数据缺失说明":""
    }
    row.update(kw)
    return row

def make_entry(locked):
    return {
        "原始玩法输出": {
            "胜平负推荐":"主胜", "让球推荐":"让-1平", "半全场推荐":["平平"],
            "总进球推荐":["1球"], "比分推荐":["0:0"], "净胜球分布":"平",
            "核心剧本":"原始剧本", "数据缺失说明":""
        },
        "锁定核心剧本": locked,
        "修正后预测": {}
    }

class SnapshotTypeAnomalyTests(unittest.TestCase):
    def test_alternative_scores_string_is_reported_and_not_attributed(self):
        c = cr.build_snapshot_comparison(make_row(), make_entry({
            "full_time_result":"主胜", "net_goal_interval":"主胜1",
            "primary_score":"1:0", "alternative_scores":"2:1", "match_script":"s"
        }))
        self.assertTrue(any("alternative_scores" in q and "周日001" in q for q in c["快照问题"]))
        self.assertEqual(c["归因"]["比分"], "无法归因")

    def test_primary_score_list_is_reported_and_not_attributed(self):
        c = cr.build_snapshot_comparison(make_row(), make_entry({
            "full_time_result":"主胜", "net_goal_interval":"主胜1",
            "primary_score":["1:0"], "alternative_scores":["2:1"], "match_script":"s"
        }))
        self.assertTrue(any("primary_score" in q and "周日001" in q for q in c["快照问题"]))
        self.assertEqual(c["归因"]["比分"], "无法归因")

    def test_locked_covers_scores_rejects_non_string_primary(self):
        self.assertFalse(cr._locked_covers_scores(
            {"primary_score":["1:0"], "alternative_scores":["2:1"]}, ["1:0"]
        ))

if __name__ == "__main__":
    unittest.main(verbosity=2)
