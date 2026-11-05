# -*- coding: utf-8 -*-
"""v2 让球推导机制测试：净胜球五类概率分布 → 让球胜平负概率 → 主推/次选/风险。

覆盖任务书第四节用例 1-7（含被截断的第 7 条：概率缺失降级不得伪造、非法概率拒绝、
模型与赔率冲突、尾部桶跨档、版本隔离）。v1 首选比分单点函数的既有断言保留在
test_lottery_rules.py / test_compare_report.py，本文件不重复其全部用例。
"""
import unittest

from lottery_rules import (
    NET_GOAL_BUCKETS,
    HANDICAP_VERSION_V1,
    HANDICAP_VERSION_V2,
    MARKET_STATE_CONFLICT,
    MARKET_STATE_NO_CONFLICT,
    MARKET_STATE_INDETERMINATE,
    validate_net_goal_probabilities,
    validate_core_script,
    map_net_goal_probs_to_handicap,
    select_handicap_picks,
    format_handicap_pick,
    derive_handicap_recommendation,
    format_net_goal_probabilities,
    format_handicap_probabilities,
    parse_net_goal_probabilities_text,
    run_consistency_checks,
)


def bucket_probs(**kw):
    """构造五桶概率 dict，默认全 0。"""
    probs = {b: 0.0 for b in NET_GOAL_BUCKETS}
    probs.update(kw)
    return probs


def single_bucket(bucket):
    return bucket_probs(**{bucket: 1.0})


class TestTaskMandatedMapping(unittest.TestCase):
    """任务书第四节 1-6：让球数取自参数，临界净胜球 = -让球数。"""

    # 1. 主队让 -1、主胜 1 球 → 让平
    def test_minus1_home_win1_is_handicap_draw(self):
        m = map_net_goal_probs_to_handicap(single_bucket("主胜1"), -1)
        self.assertTrue(m["exact"])
        self.assertEqual(m["probabilities"], {"主胜": 0.0, "平": 1.0, "客胜": 0.0})
        self.assertEqual(format_handicap_pick(-1, "平"), "让-1平")
        # 与首选比分单点口径在单点情形下必须一致
        self.assertEqual(derive_handicap_recommendation("1:0", -1), "让-1平")

    # 2. 主队让 -1、主胜 2 球 → 让胜
    def test_minus1_home_win2_is_handicap_win(self):
        m = map_net_goal_probs_to_handicap(single_bucket("主胜2+"), -1)
        self.assertTrue(m["exact"])
        self.assertEqual(m["probabilities"], {"主胜": 1.0, "平": 0.0, "客胜": 0.0})
        self.assertEqual(format_handicap_pick(-1, "主胜"), "让-1主胜")
        self.assertEqual(derive_handicap_recommendation("2:0", -1), "让-1主胜")

    # 3. 主队让 -1、平局或客胜 → 让负
    def test_minus1_draw_or_away_is_handicap_loss(self):
        for bucket in ("平", "客胜1", "客胜2+"):
            with self.subTest(bucket=bucket):
                m = map_net_goal_probs_to_handicap(single_bucket(bucket), -1)
                self.assertTrue(m["exact"])
                self.assertEqual(m["probabilities"]["客胜"], 1.0)
        self.assertEqual(derive_handicap_recommendation("1:1", -1), "让-1客胜")
        self.assertEqual(derive_handicap_recommendation("0:1", -1), "让-1客胜")
        self.assertEqual(derive_handicap_recommendation("0:3", -1), "让-1客胜")

    # 4. 主队受让 +1：主胜→让胜；平局→让胜（受让后赢盘）；客胜 1 球→让平；客胜 2+→让负
    def test_plus1_home_draw_away_mapping(self):
        cases = [
            ("主胜2+", "2:0", "主胜"),
            ("主胜1", "2:1", "主胜"),
            ("平", "1:1", "主胜"),       # 受让 +1：平局经让球后为主胜
            ("客胜1", "0:1", "平"),     # 客胜 1 球恰好走盘 → 让平
            ("客胜2+", "0:2", "客胜"),
        ]
        for bucket, score, label in cases:
            with self.subTest(bucket=bucket):
                m = map_net_goal_probs_to_handicap(single_bucket(bucket), "+1")
                self.assertTrue(m["exact"], f"{bucket} 在 +1 下应为单一结果")
                self.assertEqual(m["probabilities"][label], 1.0)
                self.assertEqual(derive_handicap_recommendation(score, "+1"),
                                 format_handicap_pick(1, label))

    # 5. 不同让球数的临界净胜球测试（d = -h 必须落在含「平」的桶结果集合内）
    def test_critical_margin_across_handicaps(self):
        for hc in (-3, -2, -1, 1, 2, 3):
            with self.subTest(handicap=hc):
                critical = -hc
                bucket = ("主胜2+" if critical >= 2 else
                          "主胜1" if critical == 1 else
                          "平" if critical == 0 else
                          "客胜1" if critical == -1 else "客胜2+")
                m = map_net_goal_probs_to_handicap(single_bucket(bucket), hc)
                self.assertIn("平", _labels_with_mass(m))

    def test_critical_explicit_outcomes(self):
        # 临界净胜球 d=-h 的结果必须恰为让球平（含在尾部跨档集合中）
        for hc, expected in ((-1, ["平"]), (-2, ["主胜", "平"]), (-3, ["主胜", "平", "客胜"]),
                             (1, ["平"]), (2, ["平", "客胜"]), (3, ["主胜", "平", "客胜"])):
            d = -hc
            bucket = ("主胜2+" if d >= 2 else "主胜1" if d == 1 else
                      "平" if d == 0 else "客胜1" if d == -1 else "客胜2+")
            from lottery_rules import _bucket_outcome_labels
            self.assertEqual(_bucket_outcome_labels(bucket, hc), expected)

    # 6. 五类概率合计 100% → 映射后三类让球概率合计 100%（精确映射情形）
    def test_five_bucket_sum_one_implies_three_sum_one(self):
        distributions = [
            {"主胜2+": 0.30, "主胜1": 0.25, "平": 0.20, "客胜1": 0.15, "客胜2+": 0.10},
            {"主胜2+": 0.05, "主胜1": 0.10, "平": 0.30, "客胜1": 0.30, "客胜2+": 0.25},
            {"主胜2+": 0.50, "主胜1": 0.20, "平": 0.15, "客胜1": 0.10, "客胜2+": 0.05},
        ]
        for probs in distributions:
            self.assertAlmostEqual(sum(probs.values()), 1.0, places=12)
            for hc in (-1, 1):  # |h|=1 时五桶均不跨档，映射精确
                with self.subTest(handicap=hc):
                    m = map_net_goal_probs_to_handicap(probs, hc)
                    self.assertTrue(m["exact"])
                    self.assertAlmostEqual(sum(m["probabilities"].values()), 1.0, places=12)
                    for v in m["probabilities"].values():
                        self.assertGreaterEqual(v, 0.0)
                        self.assertLessEqual(v, 1.0)


def _labels_with_mass(mapping):
    """从 bounds 反推有概率质量的结果标签（临界测试辅助）。"""
    return [k for k, pair in mapping["bounds"].items() if pair[1] > 0]


class TestTailBucketStraddle(unittest.TestCase):
    """|让球|>=2 时尾部桶（主胜2+/客胜2+）可能同时对应多个让球结果：不得平均拆分。"""

    def test_minus2_straddle_bounds_and_mass_conservation(self):
        probs = {"主胜2+": 0.30, "主胜1": 0.20, "平": 0.20, "客胜1": 0.10, "客胜2+": 0.20}
        m = map_net_goal_probs_to_handicap(probs, -2)
        self.assertFalse(m["exact"])
        self.assertEqual(m["straddles"], [{"bucket": "主胜2+", "mass": 0.30,
                                           "labels": ["主胜", "平"]}])
        # 下界：单点桶全部计入客胜（0.2+0.2+0.1+0.2=0.7）；主胜2+ 跨 主胜/平，
        # 不含客胜，故客胜上界仍为 0.7
        self.assertEqual(m["bounds"]["客胜"], [0.7, 0.7])
        self.assertEqual(m["bounds"]["主胜"], [0.0, 0.3])
        self.assertEqual(m["bounds"]["平"], [0.0, 0.3])
        # 质量守恒：Σ下界 = 1-M，Σ上界 = 1+M（M=跨档质量）
        self.assertAlmostEqual(sum(p[0] for p in m["bounds"].values()), 0.7, places=12)
        self.assertAlmostEqual(sum(p[1] for p in m["bounds"].values()), 1.3, places=12)
        # 审计明细：跨档桶不得被静默赋值
        self.assertTrue(all(v is None for v in m["bucket_allocation"]["主胜2+"].values()))

    def test_minus3_three_way_straddle_marked_uncertain(self):
        probs = {"主胜2+": 0.30, "主胜1": 0.20, "平": 0.20, "客胜1": 0.10, "客胜2+": 0.20}
        m = map_net_goal_probs_to_handicap(probs, -3)
        self.assertFalse(m["exact"])
        self.assertEqual(m["straddles"][0]["labels"], ["主胜", "平", "客胜"])
        d = select_handicap_picks(m)
        self.assertTrue(d["double"])
        self.assertTrue(any("尾部净胜球桶" in u for u in d["uncertainties"]))
        # 非精确情形禁止给出精确三概率文本
        self.assertIn("~", format_handicap_probabilities(
            {"exact": False, "bounds": m["bounds"]}))

    def test_plus2_away_tail_straddle(self):
        m = map_net_goal_probs_to_handicap(single_bucket("客胜2+"), 2)
        self.assertFalse(m["exact"])
        self.assertEqual(m["straddles"][0]["labels"], ["平", "客胜"])


class TestProbabilityValidation(unittest.TestCase):
    """第 7 条（补全）：非法概率拒绝；百分数换算；合计容差与归一化。"""

    def test_missing_degrades(self):
        for raw in (None, {}, []):
            with self.subTest(raw=raw):
                r = validate_net_goal_probabilities(raw)
                self.assertFalse(r["ok"])
                self.assertIsNone(r["probabilities"])
                self.assertTrue(r["reason"])

    def test_illegal_inputs_rejected(self):
        bad = [
            "not-a-dict",
            bucket_probs(主胜1=0.5),  # 缺桶
            {"主胜2+": -0.1, "主胜1": 0.4, "平": 0.3, "客胜1": 0.2, "客胜2+": 0.2},
            {"主胜2+": "x", "主胜1": 0.4, "平": 0.3, "客胜1": 0.2, "客胜2+": 0.1},
            {"主胜2+": 120, "主胜1": -10, "平": 0, "客胜1": 0, "客胜2+": 0},  # 百分数下 >100
            {"主胜2+": 0.5, "主胜1": 0.4, "平": 0.3, "客胜1": 0.2, "客胜2+": 0.1},  # 合计 1.5
            {"主胜2+": 0.3, "主胜2球": 0.1, "主胜1": 0.2, "平": 0.2,
             "客胜1": 0.1, "客胜2+": 0.1},  # 桶重复（别名）
            {"未知桶": 1.0},
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                r = validate_net_goal_probabilities(raw)
                self.assertFalse(r["ok"], f"应拒绝：{raw}")
                self.assertIsNone(r["probabilities"])
                self.assertTrue(r["reason"])

    def test_sum_outside_tolerance_rejected_boundary(self):
        base = {"主胜2+": 0.30, "主胜1": 0.20, "平": 0.20, "客胜1": 0.10, "客胜2+": 0.20}
        ok = dict(base); ok["主胜2+"] = 0.31  # 合计 1.01：边界内容许并归一
        r = validate_net_goal_probabilities(ok)
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(sum(r["probabilities"].values()), 1.0, places=12)
        bad = dict(base); bad["主胜2+"] = 0.32  # 合计 1.02：拒绝
        self.assertFalse(validate_net_goal_probabilities(bad)["ok"])

    def test_percentage_scale_accepted(self):
        r = validate_net_goal_probabilities(
            {"主胜2+": 30, "主胜1": 25, "平": 20, "客胜1": 15, "客胜2+": 10})
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["probabilities"]["主胜2+"], 0.30, places=12)
        self.assertAlmostEqual(sum(r["probabilities"].values()), 1.0, places=12)

    def test_normalized_distribution_sums_exactly_one(self):
        r = validate_net_goal_probabilities(
            {"主胜2+": 0.333, "主胜1": 0.333, "平": 0.0, "客胜1": 0.0, "客胜2+": 0.334})
        self.assertTrue(r["ok"])
        self.assertEqual(sum(r["probabilities"].values()), 1.0)

    def test_alternatives_counting_is_never_probability(self):
        # 即使备选比分有 4 个，校验器也不会凭空生成概率（无输入 → 明确缺失）
        r = validate_net_goal_probabilities(None)
        self.assertFalse(r["ok"])
        self.assertIn("未提供", r["reason"])

    def test_nan_and_inf_rejected(self):
        # NaN/Inf 必须被显式拒绝，不得进入归一化
        for bad_val in (float("nan"), float("inf"), float("-inf")):
            raw = {"主胜2+": bad_val, "主胜1": 0.3, "平": 0.2, "客胜1": 0.2, "客胜2+": 0.3}
            with self.subTest(bad_val=bad_val):
                r = validate_net_goal_probabilities(raw)
                self.assertFalse(r["ok"])
                self.assertIsNone(r["probabilities"])
                self.assertIn("NaN/Inf", r["reason"])

    def test_extra_field_rejected(self):
        # 五桶齐全但夹带未识别字段：整体拒绝，不静默忽略
        raw = {"主胜2+": 0.2, "主胜1": 0.2, "平": 0.2, "客胜1": 0.2, "客胜2+": 0.2,
               "confidence_score": 0.9}
        r = validate_net_goal_probabilities(raw)
        self.assertFalse(r["ok"])
        self.assertIn("无法识别", r["reason"])


class TestCoreValidatorIntegration(unittest.TestCase):
    def _base_script(self, **over):
        s = {
            "home_team": "主队", "away_team": "客队", "full_time_result": "主胜",
            "net_goal_interval": "主胜1", "primary_score": "2:1",
            "alternative_scores": ["1:0"], "match_script": "剧本",
            "net_goal_reasoning": "一球小胜", "uncertainties": [], "confidence": "中",
        }
        s.update(over)
        return s

    def test_valid_probs_pass_into_normalized(self):
        probs = {"主胜2+": 0.2, "主胜1": 0.5, "平": 0.2, "客胜1": 0.05, "客胜2+": 0.05}
        r = validate_core_script(self._base_script(net_goal_probabilities=probs))
        self.assertTrue(r["ok"])
        self.assertIsNotNone(r["normalized"]["net_goal_probabilities"])
        self.assertAlmostEqual(
            sum(r["normalized"]["net_goal_probabilities"].values()), 1.0, places=12)

    def test_invalid_probs_do_not_block_but_degrade(self):
        r = validate_core_script(self._base_script(net_goal_probabilities={"平": 0.9}))
        self.assertTrue(r["ok"])  # 非法概率不阻止核心剧本，仅降级
        self.assertIsNone(r["normalized"]["net_goal_probabilities"])
        self.assertTrue(any("降级" in w and "net_goal_probabilities" in w for w in r["warnings"]))

    def test_missing_probs_silent_none(self):
        r = validate_core_script(self._base_script())
        self.assertTrue(r["ok"])
        self.assertIsNone(r["normalized"]["net_goal_probabilities"])
        self.assertFalse(any("net_goal_probabilities" in w for w in r["warnings"]))


def _base_parsed(**over):
    p = {
        "胜平负推荐": "主胜", "净胜球分布": "主胜1",
        "让球推荐": "让-1平", "让球理由": "",
        "比分推荐": ["2:1"], "比分理由": "",
        "总进球推荐": ["3球"], "半全场推荐": ["HH"],
    }
    p.update(over)
    return p


class TestPostProcessDualPath(unittest.TestCase):
    def _pp(self, parsed, hc="-1", match_data=None):
        from analyze import post_process_prediction
        return post_process_prediction(parsed, hc, match_data)

    def test_v1_degraded_path_unchanged_and_marked(self):
        result, report = self._pp(_base_parsed(让球推荐="让-1主胜"))
        self.assertEqual(result["让球推荐"], "让-1平")  # v1 单点覆盖行为不变
        self.assertIn("[程序校验修正]", result["让球理由"])
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V1)
        self.assertIsNone(result["净胜球概率分布"])
        self.assertIsNone(result["让球结果概率"])
        self.assertEqual(result["让球次选"], "")
        self.assertTrue(result["概率来源"].startswith("缺失"))
        self.assertIn("首选比分单点推导（v1）", result["概率来源"])
        # 旧报告不混入 v2 概率措辞
        self.assertNotIn("v2", report)

    def test_v1_never_fabricates_from_alternative_count(self):
        p = _base_parsed(比分推荐=["2:1", "1:0", "2:0", "3:0", "1:1"])
        result, _ = self._pp(p)
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V1)
        self.assertIsNone(result["净胜球概率分布"])

    def test_v2_distribution_changes_recommendation_away_from_single_point(self):
        # 首选 2:1 的单点结果是让-1平，但分布概率显示客胜方向占优 → 让-1客胜
        # 让-1 映射：主胜2+→让胜(0.10)，主胜1→让平(0.20)，平/客胜1/客胜2+→让负(0.70)
        probs = {"主胜2+": 0.10, "主胜1": 0.20, "平": 0.20, "客胜1": 0.20, "客胜2+": 0.30}
        p = _base_parsed(_net_goal_probabilities=probs, 让球推荐="让-1平")
        result, report = self._pp(p)
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V2)
        self.assertEqual(result["让球推荐"], "让-1客胜")
        self.assertEqual(result["让球次选"], "让-1平")  # 平 0.20 > 主胜 0.10
        self.assertEqual(result["净胜球概率分布"], probs)
        self.assertTrue(result["让球结果概率"]["exact"])
        self.assertAlmostEqual(result["让球结果概率"]["probabilities"]["客胜"], 0.70, places=10)
        self.assertIn("净胜球概率", result["让球理由"])
        # 与首选比分单点不同 → 软性跨剧本提示，不是硬性错误
        self.assertNotIn("硬性错误", report)
        self.assertIn("v2 净胜球分布口径", report)

    def test_v2_dominant_draw_still_recommends_draw(self):
        # 不设让平配额：当分布真的集中在净胜 1 球时，让平仍是主推
        probs = {"主胜2+": 0.05, "主胜1": 0.80, "平": 0.10, "客胜1": 0.03, "客胜2+": 0.02}
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=probs))
        self.assertEqual(result["让球推荐"], "让-1平")
        self.assertFalse(result["让球结果概率"]["double"])

    def test_v2_close_race_marks_double_pick(self):
        # 让-1：主胜2+ 0.45→让胜，主胜1 0.40→让平，其余 0.15→让负；差距 5% < 8%
        probs = {"主胜2+": 0.45, "主胜1": 0.40, "平": 0.10, "客胜1": 0.03, "客胜2+": 0.02}
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=probs))
        self.assertEqual(result["让球推荐"], "让-1主胜")
        self.assertEqual(result["让球次选"], "让-1平")
        self.assertTrue(result["让球结果概率"]["double"])
        self.assertIn("双选", result["让球理由"])

    def test_v2_straddle_carries_interval_not_fake_split(self):
        probs = {"主胜2+": 0.30, "主胜1": 0.20, "平": 0.20, "客胜1": 0.10, "客胜2+": 0.20}
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=probs, 让球推荐="让-2客胜"), "-2")
        self.assertEqual(result["让球推荐"], "让-2客胜")
        payload = result["让球结果概率"]
        self.assertFalse(payload["exact"])
        self.assertEqual(payload["straddles"][0]["bucket"], "主胜2+")
        self.assertIn("无法精确拆分", result["让球理由"])

    def test_v2_alternative_scenario_labeled_no_weight(self):
        probs = {"主胜2+": 0.10, "主胜1": 0.70, "平": 0.10, "客胜1": 0.05, "客胜2+": 0.05}
        p = _base_parsed(_net_goal_probabilities=probs, 比分推荐=["2:1", "1:1"])
        result, _ = self._pp(p)
        self.assertIn("替代剧本：备选比分 1:1（平）", result["让球理由"])
        self.assertIn("让-1客胜", result["让球理由"])
        self.assertIn("无概率权重", result["让球理由"])

    def test_v2_market_conflict_flagged_but_not_flipped(self):
        # 让-1：主胜2+ 0.45→让胜；主胜1 0.15→让平；平 0.25→让负；客队两桶 0.15→让负
        # 模型主推让胜 0.45，市场倾向让负（去水约 0.556）→ 标记分歧但不翻转
        probs = {"主胜2+": 0.45, "主胜1": 0.15, "平": 0.25, "客胜1": 0.10, "客胜2+": 0.05}
        odds = {"主胜": 5.0, "平": 5.0, "客胜": 2.0}
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=probs),
                             match_data={"让球赔率": odds})
        self.assertEqual(result["让球推荐"], "让-1主胜")
        self.assertTrue(result["让球结果概率"]["market_conflict"])
        self.assertEqual(result["让球结果概率"]["market_conflict_state"], MARKET_STATE_CONFLICT)
        self.assertIn("分歧", result["让球理由"])

    def test_v2_missing_odds_noted(self):
        probs = {"主胜2+": 0.10, "主胜1": 0.70, "平": 0.10, "客胜1": 0.05, "客胜2+": 0.05}
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=probs))
        self.assertIn("未做市场概率交叉验证", result["让球理由"])

    def test_unparseable_handicap_cannot_map_even_with_probs(self):
        probs = {"主胜2+": 0.2, "主胜1": 0.5, "平": 0.2, "客胜1": 0.05, "客胜2+": 0.05}
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=probs), "abc")
        self.assertEqual(result["让球推荐"], "让球无法判定")
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V1)


class TestConsistencyVersionGate(unittest.TestCase):
    def test_v2_divergence_is_soft_v1_divergence_is_hard(self):
        probs = {"主胜2+": 0.10, "主胜1": 0.20, "平": 0.20, "客胜1": 0.20, "客胜2+": 0.30}
        from analyze import post_process_prediction
        v2, _ = post_process_prediction(_base_parsed(_net_goal_probabilities=probs), "-1")
        hard, soft, _ = run_consistency_checks(v2, "-1")
        self.assertFalse(any("让球" in e for e in hard))
        self.assertTrue(any("v2 净胜球分布口径" in w for w in soft))

        v1, _ = post_process_prediction(
            _base_parsed(让球推荐="让-1客胜", 比分推荐=["2:1"]), "-1")
        # v1 下让球与首选比分不一致必须仍是硬性错误
        hard1, _, _ = run_consistency_checks(
            {"比分推荐": ["2:1"], "让球推荐": "让-1客胜", "胜平负推荐": "主胜",
             "净胜球分布": "主胜1", "总进球推荐": ["3球"]}, "-1")
        self.assertTrue(any("让球" in e for e in hard1))
        self.assertEqual(v1["让球推导版本"], HANDICAP_VERSION_V1)


class TestFormattersAndReplay(unittest.TestCase):
    def test_bucket_text_round_trip(self):
        probs = {"主胜2+": 0.30, "主胜1": 0.25, "平": 0.20, "客胜1": 0.15, "客胜2+": 0.10}
        text = format_net_goal_probabilities(probs)
        self.assertEqual(text, "主胜2+ 30.0% / 主胜1 25.0% / 平 20.0% / 客胜1 15.0% / 客胜2+ 10.0%")
        back = parse_net_goal_probabilities_text(text)
        for b in NET_GOAL_BUCKETS:
            self.assertAlmostEqual(back[b], probs[b], places=6)
        self.assertIsNone(parse_net_goal_probabilities_text(""))
        self.assertIsNone(parse_net_goal_probabilities_text("主胜2+ 90.0% / 主胜1 10.0%"))

    def test_handicap_probability_text(self):
        exact = format_handicap_probabilities(
            {"exact": True, "probabilities": {"主胜": 0.5, "平": 0.3, "客胜": 0.2}})
        self.assertEqual(exact, "主胜 50.0% / 平 30.0% / 客胜 20.0%")
        rng = format_handicap_probabilities(
            {"exact": False,
             "bounds": {"主胜": [0.0, 0.3], "平": [0.0, 0.3], "客胜": [0.7, 1.0]}})
        self.assertIn("主胜 0.0%~30.0%", rng)
        self.assertIn("客胜 70.0%~100.0%", rng)
        self.assertEqual(format_handicap_probabilities(None), "")

    def test_compare_row_round_trip_reproduces_v2(self):
        from analyze import post_process_prediction
        from compare_report import row_to_prediction
        probs = {"主胜2+": 0.10, "主胜1": 0.20, "平": 0.20, "客胜1": 0.20, "客胜2+": 0.30}
        result, _ = post_process_prediction(
            _base_parsed(_net_goal_probabilities=probs), "-1")
        # 模拟赛后 CSV 行（v2 五列齐全）
        row = {
            "胜平负推荐": result["胜平负推荐"], "让球推荐": result["让球推荐"],
            "净胜球分布": "主胜1", "比分推荐": "2:1",
            "总进球推荐": "3球", "半全场推荐": "HH",
            "让球次选": result["让球次选"],
            "净胜球概率分布": format_net_goal_probabilities(probs),
            "让球结果概率": format_handicap_probabilities(result["让球结果概率"]),
            "让球推导版本": HANDICAP_VERSION_V2,
        }
        pred = row_to_prediction(row)
        self.assertEqual(pred["_net_goal_probabilities"]["客胜1"], 0.20)
        replay, _ = post_process_prediction(pred, "-1")
        self.assertEqual(replay["让球推荐"], result["让球推荐"])
        self.assertEqual(replay["让球推导版本"], HANDICAP_VERSION_V2)

        # 旧版 CSV 行（无新列）：回退 v1，不携带内部概率键
        old_pred = row_to_prediction({"比分推荐": "2:1", "让球推荐": "让-1客胜",
                                      "胜平负推荐": "主胜", "净胜球分布": "主胜1",
                                      "总进球推荐": "3球"})
        self.assertNotIn("_net_goal_probabilities", old_pred)
        old_result, _ = post_process_prediction(old_pred, "-1")
        self.assertEqual(old_result["让球推荐"], "让-1平")
        self.assertEqual(old_result["让球推导版本"], HANDICAP_VERSION_V1)


class TestR1MarketConflictState(unittest.TestCase):
    """R1：市场分歧三态；区间场景只使用模型主推概率下界。"""

    def test_exact_confirmed_conflict_preserves_boolean_and_warning(self):
        probs = {"主胜2+": 0.45, "主胜1": 0.15, "平": 0.25, "客胜1": 0.10, "客胜2+": 0.05}
        mapping = map_net_goal_probs_to_handicap(probs, -1)
        d = select_handicap_picks(mapping, {"主胜": 5.0, "平": 5.0, "客胜": 2.0})
        self.assertTrue(d["market_conflict"])
        self.assertEqual(d["market_conflict_state"], MARKET_STATE_CONFLICT)
        self.assertTrue(any("明显分歧" in x for x in d["uncertainties"]))

    def test_market_same_as_model_is_no_conflict(self):
        probs = {"主胜2+": 0.10, "主胜1": 0.20, "平": 0.20, "客胜1": 0.20, "客胜2+": 0.30}
        mapping = map_net_goal_probs_to_handicap(probs, -1)
        d = select_handicap_picks(mapping, {"主胜": 5.0, "平": 5.0, "客胜": 2.0})
        self.assertFalse(d["market_conflict"])
        self.assertEqual(d["market_conflict_state"], MARKET_STATE_NO_CONFLICT)

    def test_missing_market_is_indeterminate(self):
        probs = {"主胜2+": 0.10, "主胜1": 0.20, "平": 0.20, "客胜1": 0.20, "客胜2+": 0.30}
        d = select_handicap_picks(map_net_goal_probs_to_handicap(probs, -1))
        self.assertFalse(d["market_conflict"])
        self.assertEqual(d["market_conflict_state"], MARKET_STATE_INDETERMINATE)

    def test_interval_conflict_uses_lower_bound_not_midpoint_or_upper(self):
        # -2 时客胜下界为 .70；市场主胜倾向超过 .40，方向相反，确认分歧。
        probs = {"主胜2+": 0.30, "主胜1": 0.10, "平": 0.05, "客胜1": 0.20, "客胜2+": 0.35}
        mapping = map_net_goal_probs_to_handicap(probs, -2)
        self.assertEqual(mapping["bounds"]["客胜"], [0.70, 0.70])
        d = select_handicap_picks(mapping, {"主胜": 1.7, "平": 4.5, "客胜": 4.5})
        self.assertEqual(d["primary"], "客胜")
        self.assertTrue(d["market_conflict"])
        self.assertEqual(d["market_conflict_state"], MARKET_STATE_CONFLICT)
        self.assertTrue(any("下界 70.0%" in x for x in d["uncertainties"]))

    def test_interval_market_opposes_but_model_lower_bound_insufficient(self):
        # 跨档质量 80%，主推下界为 0；不能把未分配的跨档质量当作模型方向概率。
        probs = {"主胜2+": 0.80, "主胜1": 0.05, "平": 0.05, "客胜1": 0.05, "客胜2+": 0.05}
        mapping = map_net_goal_probs_to_handicap(probs, -2)
        d = select_handicap_picks(mapping, {"主胜": 1.7, "客胜": 4.5, "平": 4.5})
        self.assertFalse(d["market_conflict"])
        self.assertEqual(d["market_conflict_state"], MARKET_STATE_INDETERMINATE)
        self.assertTrue(any("状态无法判断" in x for x in d["uncertainties"]))

    def test_interval_market_direction_below_threshold_not_conflict(self):
        probs = {"主胜2+": 0.30, "主胜1": 0.10, "平": 0.05, "客胜1": 0.20, "客胜2+": 0.35}
        mapping = map_net_goal_probs_to_handicap(probs, -2)
        # 三项赔率相同，市场最高隐含概率约 33.3%，低于 40% 门槛。
        d = select_handicap_picks(mapping, {"主胜": 3.0, "平": 3.0, "客胜": 3.0})
        self.assertFalse(d["market_conflict"])
        self.assertEqual(d["market_conflict_state"], MARKET_STATE_NO_CONFLICT)


class TestR2PostProcessRevalidation(unittest.TestCase):
    """R2：post_process_prediction 的 V2 入口必须二次校验，失败时安全降级。"""

    def _pp(self, parsed, hc="-1"):
        from analyze import post_process_prediction
        return post_process_prediction(parsed, hc)

    def test_valid_distribution_enters_v2_after_revalidation(self):
        probs = {"主胜2+": 0.20, "主胜1": 0.50, "平": 0.20, "客胜1": 0.05, "客胜2+": 0.05}
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=probs))
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V2)
        self.assertEqual(sum(result["净胜球概率分布"].values()), 1.0)

    def test_invalid_negative_probability_falls_back_to_v1(self):
        probs = {"主胜2+": -0.1, "主胜1": 0.5, "平": 0.3, "客胜1": 0.2, "客胜2+": 0.1}
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=probs))
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V1)
        self.assertIsNone(result["净胜球概率分布"])
        self.assertTrue(result["概率来源"].startswith("二次校验失败："))

    def test_missing_bucket_falls_back_to_v1(self):
        probs = {"平": 1.0}
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=probs))
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V1)
        self.assertIn("二次校验失败", result["概率来源"])

    def test_sum_outside_tolerance_falls_back_to_v1(self):
        probs = {"主胜2+": 0.32, "主胜1": 0.20, "平": 0.20, "客胜1": 0.10, "客胜2+": 0.20}
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=probs))
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V1)
        self.assertIn("合计", result["概率来源"])

    def test_non_dict_probability_input_falls_back_to_v1(self):
        result, _ = self._pp(_base_parsed(_net_goal_probabilities=["bad", "input"]))
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V1)
        self.assertIn("二次校验失败", result["概率来源"])

    def test_invalid_distribution_without_primary_score_remains_unjudgeable(self):
        result, _ = self._pp(_base_parsed(_net_goal_probabilities={"平": 1.0}, 比分推荐=[]))
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V1)
        self.assertIn("二次校验失败", result["概率来源"])
        self.assertEqual(result["让球推荐"], "让-1平")  # 无首选比分时不凭概率伪造单点修正

    def test_missing_probability_is_normal_v1_fallback_not_recheck_failure(self):
        result, _ = self._pp(_base_parsed())
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V1)
        self.assertTrue(result["概率来源"].startswith("缺失："))
        self.assertNotIn("二次校验失败", result["概率来源"])

    def test_recheck_failure_visible_in_report_text(self):
        # 降级事件必须同时进入一致性报告文本，不能只在结构化字段里
        probs = {"主胜2+": -0.1, "主胜1": 0.5, "平": 0.3, "客胜1": 0.2, "客胜2+": 0.1}
        _, report = self._pp(_base_parsed(_net_goal_probabilities=probs))
        self.assertIn("净胜球概率二次校验失败", report)
        self.assertIn("已降级 v1 单点推导", report)

    def test_recheck_failure_without_score_visible_in_report(self):
        # 双缺失路径：报告同样必须包含失败原因，让球推荐不被概率伪造
        result, report = self._pp(
            _base_parsed(_net_goal_probabilities={"平": 1.0}, 比分推荐=[]))
        self.assertEqual(result["让球推导版本"], HANDICAP_VERSION_V1)
        self.assertIn("二次校验失败", report)
        self.assertIn("首选比分缺失", report)

    def test_normal_missing_probability_not_in_report_as_failure(self):
        # 正常缺失降级不得被误标为校验失败
        _, report = self._pp(_base_parsed())
        self.assertNotIn("二次校验失败", report)

    def test_unparseable_handicap_clears_stale_reason(self):
        # 让球数非法时推荐被置为无法判定，LLM 原让球理由不得原样残留
        result, _ = self._pp(
            _base_parsed(让球推荐="让-1主胜", 让球理由="模型看好主队大胜，让-1主胜稳出"),
            "abc")
        self.assertEqual(result["让球推荐"], "让球无法判定")
        self.assertIn("[程序校验修正]", result["让球理由"])
        self.assertIn("让球数 'abc' 无法解析", result["让球理由"])
        self.assertIn("原 AI 让球理由：模型看好主队大胜", result["让球理由"])

    def test_unparseable_handicap_clears_reason_even_without_pick(self):
        # 即使 LLM 未给让球推荐，孤立的旧让球理由也不得与「无法判定」并存
        result, _ = self._pp(
            _base_parsed(让球推荐="", 让球理由="单边看好主队"),
            "abc")
        self.assertEqual(result["让球推荐"], "让球无法判定")
        self.assertIn("[程序校验修正]", result["让球理由"])
        self.assertIn("原 AI 让球理由：单边看好主队", result["让球理由"])


class TestFormatterValidation(unittest.TestCase):
    """概率格式化器必须拒绝非法输入，不伪造概率。"""

    def test_format_net_goal_probs_rejects_nan(self):
        # NaN 不应变成 "nan%" 这样看似有效的字符串。
        import math
        probs = {"主胜2+": float("nan"), "主胜1": 0.2, "平": 0.2, "客胜1": 0.2, "客胜2+": 0.0}
        self.assertEqual(format_net_goal_probabilities(probs), "")

    def test_format_net_goal_probs_rejects_inf(self):
        probs = {"主胜2+": float("inf"), "主胜1": 0.2, "平": 0.2, "客胜1": 0.2, "客胜2+": -0.6}
        self.assertEqual(format_net_goal_probabilities(probs), "")

    def test_format_net_goal_probs_rejects_missing_bucket(self):
        # 缺失键不能用 0 冒充——缺失桶应整体拒绝。
        probs = {"主胜2+": 0.2, "主胜1": 0.2, "平": 0.2, "客胜1": 0.2}
        self.assertNotIn("客胜2+", probs)
        self.assertEqual(format_net_goal_probabilities(probs), "")

    def test_format_net_goal_probs_rejects_sum_outside_tolerance(self):
        # 合计 0.900（超出 1% 容差）不应被静默放行。
        probs = {"主胜2+": 0.3, "主胜1": 0.2, "平": 0.2, "客胜1": 0.2, "客胜2+": 0.0}
        self.assertAlmostEqual(sum(probs.values()), 0.900)
        self.assertEqual(format_net_goal_probabilities(probs), "")

    def test_format_net_goal_probs_rejects_negative(self):
        probs = {"主胜2+": -0.1, "主胜1": 0.3, "平": 0.3, "客胜1": 0.3, "客胜2+": 0.2}
        self.assertEqual(format_net_goal_probabilities(probs), "")

    def test_format_net_goal_probs_accepts_valid(self):
        probs = {"主胜2+": 0.25, "主胜1": 0.20, "平": 0.20, "客胜1": 0.20, "客胜2+": 0.15}
        text = format_net_goal_probabilities(probs)
        self.assertIn("主胜2+ 25.0%", text)
        self.assertIn("客胜2+ 15.0%", text)

    def test_format_handicap_probs_exact_rejects_missing_key(self):
        # 缺失结果键不应被默认补 0.0——必须整体拒绝。
        payload = {"exact": True, "probabilities": {"主胜": 0.6, "平": 0.3}}
        self.assertEqual(format_handicap_probabilities(payload), "")

    def test_format_handicap_probs_exact_rejects_nan(self):
        import math
        payload = {"exact": True, "probabilities": {"主胜": 0.6, "平": float("nan"), "客胜": 0.4}}
        self.assertEqual(format_handicap_probabilities(payload), "")

    def test_format_handicap_probs_exact_rejects_sum_bad(self):
        payload = {"exact": True, "probabilities": {"主胜": 0.5, "平": 0.4, "客胜": 0.3}}  # sum=1.2
        self.assertEqual(format_handicap_probabilities(payload), "")

    def test_format_handicap_probs_exact_accepts_valid(self):
        payload = {"exact": True, "probabilities": {"主胜": 0.45, "平": 0.15, "客胜": 0.40}}
        text = format_handicap_probabilities(payload)
        self.assertIn("主胜 45.0%", text)
        self.assertIn("客胜 40.0%", text)

    def test_format_handicap_probs_bounds_rejects_lo_gt_hi(self):
        # 区间下界大于上界：lo=0.7, hi=0.3 显然非法。
        payload = {"exact": False,
                   "bounds": {"主胜": [0.0, 0.3], "平": [0.0, 0.3], "客胜": [0.7, 0.3]}}
        self.assertEqual(format_handicap_probabilities(payload), "")

    def test_format_handicap_probs_bounds_rejects_negative(self):
        payload = {"exact": False,
                   "bounds": {"主胜": [-0.1, 0.3], "平": [0.0, 0.3], "客胜": [0.4, 0.7]}}
        self.assertEqual(format_handicap_probabilities(payload), "")

    def test_format_handicap_probs_bounds_rejects_gt_one(self):
        payload = {"exact": False,
                   "bounds": {"主胜": [0.0, 0.3], "平": [0.0, 0.3], "客胜": [0.4, 1.2]}}
        self.assertEqual(format_handicap_probabilities(payload), "")

    def test_format_handicap_probs_bounds_accepts_valid(self):
        payload = {"exact": False,
                   "bounds": {"主胜": [0.0, 0.3], "平": [0.0, 0.3], "客胜": [0.7, 0.7]}}
        text = format_handicap_probabilities(payload)
        self.assertIn("主胜 0.0%~30.0%", text)
        self.assertIn("客胜 70.0%~70.0%", text)

    def test_format_handicap_probs_bounds_rejects_missing_result_key(self):
        # 缺失结果键不应默认补 [0,0]。
        payload = {"exact": False,
                   "bounds": {"主胜": [0.0, 0.3], "平": [0.0, 0.3]}}  # 缺 客胜
        self.assertEqual(format_handicap_probabilities(payload), "")


class TestCSVSaveValidation(unittest.TestCase):
    """CSV 写入前字段校验；JSON 快照 allow_nan=False。"""

    def test_validate_fields_missing(self):
        # 借用 analyze._validate_result_row_fields 做纯函数测试（不写文件）。
        import analyze
        row = {"编号": "周三001"}  # 仅一个字段，缺 26 个
        issue = analyze._validate_result_row_fields(row, analyze.CSV_FIELDNAMES)
        self.assertIsNotNone(issue)
        self.assertIn("缺失字段", issue)

    def test_validate_fields_extra(self):
        import analyze
        row = {f: "" for f in analyze.CSV_FIELDNAMES}
        row["_internal_extra"] = "should_not_appear"
        issue = analyze._validate_result_row_fields(row, analyze.CSV_FIELDNAMES)
        self.assertIsNotNone(issue)
        self.assertIn("多余字段", issue)
        self.assertIn("_internal_extra", issue)

    def test_validate_fields_clean(self):
        import analyze
        row = {f: "" for f in analyze.CSV_FIELDNAMES}
        self.assertIsNone(analyze._validate_result_row_fields(row, analyze.CSV_FIELDNAMES))

    def test_json_dump_rejects_nan(self):
        # json.dump(allow_nan=False) 应抛 ValueError，不生成非法 JSON。
        import json, tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"val": float("nan")}, f, allow_nan=False)
            self.fail("allow_nan=False 未拦截 NaN")
        except ValueError:
            pass  # 预期
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
