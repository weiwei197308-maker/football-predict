"""修正对照报告生成器：对比 LLM 原始预测与规则引擎修正后的结果。

数据来源：
- output/竞彩分析_<date>.csv ：赛前预测（若由旧版单阶段流程生成，即为修正前原始预测）
- output/每日记录_<date>.json：真实赛果（可选；存在时追加修正前后命中统计）

修正引擎复用 analyze.post_process_prediction（入口深拷贝，不覆盖原始预测）。
本脚本只读取上述文件并新写 修正对照报告_<date>.txt，绝不修改原始 CSV / JSON。

重要口径：修正率不等于预测准确率。没有真实赛果时，不输出任何命中率结论。
"""
import copy
import csv
import json
import os
import re
import sys

from analyze import post_process_prediction
from lottery_rules import (
    derive_handicap_recommendation,
    parse_hafu_codes,
    run_consistency_checks,
    parse_net_goal_probabilities_text,
    validate_net_goal_probabilities,
    HANDICAP_VERSION_V2,
)


def _run_consistency_checks(prediction, handicap):
    """兼容规则模块的二元/三元返回值，统一为 (hard, soft, unjudgeable)。"""
    result = run_consistency_checks(prediction, handicap)
    if not isinstance(result, (tuple, list)):
        raise TypeError("run_consistency_checks 必须返回 tuple/list")
    if len(result) == 3:
        return result[0], result[1], result[2]
    if len(result) == 2:
        return result[0], result[1], []
    raise ValueError(
        f"run_consistency_checks 返回项数异常：期望 2 或 3，实际 {len(result)}"
    )

SNAPSHOT_FILE = "原始预测快照"
SNAPSHOT_REQUIRED_KEYS = ("原始玩法输出", "锁定核心剧本", "修正后预测")
# 玩法字段（变化统计维度）；净胜球分布/核心剧本属剧本字段，仅展示不计入玩法统计
SNAPSHOT_PLAY_FIELDS = ("胜平负", "让球", "半全场", "总进球", "比分")
CHANGE_CAUSES = ("核心剧本锁定覆盖", "玩法规则后处理", "净胜球分布规则推导(v2)", "共同作用", "无法归因")


# ============ 基础工具 ============

def split_rec(value):
    """把 '2球 / 3球' 或 list 统一成 list[str]，去空白项。"""
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [p.strip() for p in re.split(r"[/,，、\n]+", str(value or "")) if p.strip()]


def canon_ttg(tokens):
    """总进球选项规范化：'4球'/'4'/'4+' → '4球'；>=7 → '7+球'；无法识别原样保留。"""
    out = []
    for t in tokens:
        m = re.match(r"^(\d+)", str(t).strip())
        if m:
            n = int(m.group(1))
            label = "7+球" if n >= 7 else f"{n}球"
        else:
            label = str(t).strip()
        if label and label not in out:
            out.append(label)
    return out


def parse_teams(duizhen):
    """'乌德勒支[荷甲15] vs 威廉二世[荷甲18]' → ('乌德勒支', '威廉二世')。"""
    parts = re.split(r"\s+vs\s+", str(duizhen or ""))
    home = re.sub(r"\[.*?\]", "", parts[0]).strip() if parts else ""
    away = re.sub(r"\[.*?\]", "", parts[1]).strip() if len(parts) > 1 else ""
    return home, away


def rq_direction(rec):
    """从让球推荐字符串提取方向：主胜 / 平 / 客胜；无法识别返回 None。"""
    s = str(rec or "")
    if "主胜" in s:
        return "主胜"
    if "客胜" in s:
        return "客胜"
    if "平" in s:
        return "平"
    return None


def row_to_prediction(row):
    """把竞彩分析 CSV 的一行还原成 post_process_prediction 的输入 dict。"""
    pred = {
        "胜平负推荐": row.get("胜平负推荐", ""),
        "胜平负理由": row.get("胜平负理由", ""),
        "让球推荐": row.get("让球推荐", ""),
        "让球理由": row.get("让球理由", ""),
        "总进球推荐": split_rec(row.get("总进球推荐", "")),
        "总进球理由": row.get("总进球理由", ""),
        "半全场推荐": split_rec(row.get("半全场推荐", "")),
        "半全场理由": row.get("半全场理由", ""),
        "比分推荐": split_rec(row.get("比分推荐", "")),
        "比分理由": row.get("比分理由", ""),
        "净胜球分布": row.get("净胜球分布", ""),
        "核心剧本": row.get("核心剧本", ""),
        "数据缺失说明": row.get("数据缺失说明", ""),
        "让球推导版本": str(row.get("让球推导版本", "") or "").strip(),
    }
    # v2 行：从 CSV 文本反解析净胜球概率，使复盘重演得到与赛前一致的 v2 推导；
    # 反解析失败（如旧版 CSV 缺列）不带内部键，自动落回 v1 单点口径。
    if str(row.get("让球推导版本", "") or "").strip() == HANDICAP_VERSION_V2:
        probs = parse_net_goal_probabilities_text(row.get("净胜球概率分布", ""))
        if probs:
            pred["_net_goal_probabilities"] = probs
    return pred


# ============ 单场对照 ============

def build_match_comparison(row, actual=None):
    """构建一场比赛的修正对照。不修改传入的 row（原始预测保留）。

    Args:
        row: 竞彩分析 CSV 的一行（dict）
        actual: 每日记录中同编号场次（dict），无真实赛果时为 None

    Returns:
        对照结果 dict（含修正前后各玩法、冲突项、一致性检查、命中情况）
    """
    handicap = str(row.get("体彩让球数", "") or "").strip()
    pred = row_to_prediction(row)
    original = copy.deepcopy(pred)

    corrected, pp_report = post_process_prediction(pred, handicap)
    pre_hard, pre_soft, pre_unjudgeable = _run_consistency_checks(original, handicap)

    scores_before = split_rec(original.get("比分推荐"))
    first_score = scores_before[0] if scores_before else ""

    # --- 让球 ---
    rq_before = str(original.get("让球推荐", "") or "").strip()
    rq_after = str(corrected.get("让球推荐", "") or "").strip()
    rq_correct = derive_handicap_recommendation(first_score, handicap)
    rq_changed = rq_before != rq_after

    # --- 半全场 ---
    hafu_before = parse_hafu_codes(split_rec(original.get("半全场推荐")))
    hafu_after = [str(x).strip() for x in corrected.get("半全场推荐", []) if str(x).strip()]
    hafu_removed = [c for c in hafu_before if c not in hafu_after]
    hafu_added = [c for c in hafu_after if c not in hafu_before]
    hafu_changed = hafu_before != hafu_after

    # --- 总进球 ---
    ttg_before = canon_ttg(split_rec(original.get("总进球推荐")))
    ttg_after = canon_ttg(corrected.get("总进球推荐", []))
    ttg_added = [t for t in ttg_after if t not in ttg_before]
    ttg_changed = ttg_before != ttg_after

    # --- 备选比分冲突（来自修正前一致性检查的软性提示/硬性错误） ---
    alt_conflicts = [m for m in pre_soft if "备选比分" in m]
    alt_hard = [m for m in pre_hard if "备选比分" in m]

    # --- 修正后状态 ---
    status_line = pp_report.splitlines()[0] if pp_report else ""
    if "仍有冲突" in status_line:
        post_status = "仍有冲突"
    elif "无法判定" in status_line:
        post_status = "无法判定"
    elif "已修正" in status_line:
        post_status = "已修正，复查通过"
    else:
        post_status = "通过"

    home, away = parse_teams(row.get("对阵", ""))
    comp = {
        "编号": row.get("编号", ""),
        "联赛": row.get("联赛", ""),
        "主队": home,
        "客队": away,
        "核心剧本": original.get("核心剧本", ""),
        "原始主比分": first_score,
        "原始净胜球分布": original.get("净胜球分布", ""),
        "体彩让球数": handicap,
        "让球": {
            "修正前": rq_before, "修正后": rq_after,
            "规则计算正确结果": rq_correct, "被修正": rq_changed,
            "推导版本": str(corrected.get("让球推导版本", "") or ""),
            "让球次选": str(corrected.get("让球次选", "") or ""),
            "净胜球概率分布": str(row.get("净胜球概率分布", "") or ""),
            "让球结果概率": str(row.get("让球结果概率", "") or ""),
        },
        "半全场": {
            "修正前": hafu_before, "修正后": hafu_after,
            "删除项": hafu_removed, "新增项": hafu_added, "被修正": hafu_changed,
        },
        "总进球": {
            "修正前": ttg_before, "修正后": ttg_after,
            "补入项": ttg_added, "被修正": ttg_changed,
        },
        "备选比分": split_rec(original.get("比分推荐"))[1:],
        "备选比分冲突": alt_conflicts + alt_hard,
        "一致性检查": {
            "修正前硬性错误": pre_hard,
            "修正前软性冲突": pre_soft,
            "修正前无法判定": pre_unjudgeable,
            "修正后状态": post_status,
            "修正报告": pp_report,
        },
        "数据缺失说明": original.get("数据缺失说明", ""),
        "命中": None,
    }

    # --- 真实赛果：修正前后命中 ---
    if actual:
        sg = actual.get("赛果", {}) or {}
        actual_rq = str(sg.get("让球", "") or "").strip()
        actual_hafu = str(sg.get("半全场", "") or "").strip().upper()
        actual_ttg = canon_ttg([sg.get("总进球", "")])
        comp["实际赛果"] = {
            "全场": sg.get("全场", ""), "半场": sg.get("半场", ""),
            "胜平负": sg.get("胜平负", ""), "让球": actual_rq,
            "半全场": actual_hafu, "总进球": sg.get("总进球", ""),
            "比分": sg.get("比分", ""),
        }
        comp["命中"] = {
            "让球": {
                "修正前": (rq_direction(rq_before) == actual_rq) if (rq_direction(rq_before) and actual_rq) else None,
                "修正后": (rq_direction(rq_after) == actual_rq) if (rq_direction(rq_after) and actual_rq) else None,
            },
            "半全场": {
                "修正前": (actual_hafu in hafu_before) if actual_hafu else None,
                "修正后": (actual_hafu in hafu_after) if actual_hafu else None,
            },
            "总进球": {
                "修正前": (actual_ttg[0] in ttg_before) if actual_ttg else None,
                "修正后": (actual_ttg[0] in ttg_after) if actual_ttg else None,
            },
        }
    return comp


def _snapshot_list(value):
    """快照列表字段轻量规范化：str 也兼容为 list。"""
    return split_rec(value)


def _safe_str_list(value):
    """安全提取字符串列表：仅接受 list/tuple；str/None/其他类型一律返回 []。

    用于锁定核心剧本地段：字段类型异常时宁可证据不足（无法归因），不得逐字符迭代误判。
    """
    if isinstance(value, (list, tuple)):
        return [str(s).strip() for s in value if str(s).strip()]
    return []


def _locked_covers_scores(locked, c_scores):
    """最终比分列表是否完全由锁定核心剧本派生（首选=锁定主比分，其余 ⊆ 锁定集合）。

    apply_locked_core_script 会按'同方向同区间'过滤备选比分，因此最终列表通常是
    锁定集合的子集而非全等；该判定同时覆盖'锁定值与原始不同'与'锁定过滤生效'两种情形。
    锁定主比分缺失、备选字段类型异常、最终列表为空时返回 False（证据不足）。
    """
    raw_primary = locked.get("primary_score")
    primary = raw_primary.strip() if isinstance(raw_primary, str) else ""
    if not primary or not c_scores:
        return False
    allowed = {primary} | set(_safe_str_list(locked.get("alternative_scores")))
    return c_scores[0] == primary and all(s in allowed for s in c_scores)


def build_snapshot_comparison(row, entry, actual=None):
    """快照口径对照：修正前=快照'原始玩法输出'，修正后=竞彩分析 CSV 行（最终输出）。

    不对 CSV 重跑 post_process_prediction 模拟历史；post_process 仅用于计算
    '仅规则后处理'参考值以支撑归因。结构与 build_match_comparison 兼容，
    另含 '归因' / '玩法变化' / '快照问题' / '剧本字段' / '比分'。
    不修改传入的 row 与 entry。
    """
    original = entry.get("原始玩法输出") or {}
    locked = entry.get("锁定核心剧本") or {}
    snap_fixed = entry.get("修正后预测") or {}
    handicap = str(row.get("体彩让球数", "") or "").strip()

    orig_pred = copy.deepcopy(original)
    for key in ("总进球推荐", "半全场推荐", "比分推荐"):
        orig_pred[key] = _snapshot_list(orig_pred.get(key))
    final_pred = row_to_prediction(row)

    # 一致性判定（纯函数只读）
    pre_hard, pre_soft, pre_unjudgeable = _run_consistency_checks(copy.deepcopy(orig_pred), handicap)
    post_hard, _, post_unjudgeable2 = _run_consistency_checks(copy.deepcopy(final_pred), handicap)
    # 仅规则后处理参考（归因证据，不作为历史重演；入口深拷贝不改 orig_pred）
    post_only, _ = post_process_prediction(copy.deepcopy(orig_pred), handicap)

    issues = []
    # 快照'修正后预测'与 CSV 一致性抽查（异常显式报告，不阻止报告）
    for label, key, norm in (
        ("让球", "让球推荐", lambda v: str(v or "").strip()),
        ("半全场", "半全场推荐", lambda v: parse_hafu_codes(split_rec(v))),
        ("总进球", "总进球推荐", lambda v: canon_ttg(split_rec(v))),
        ("胜平负", "胜平负推荐", lambda v: str(v or "").strip()),
        ("比分", "比分推荐", split_rec),
    ):
        if isinstance(snap_fixed.get(key), (str, list)) and norm(snap_fixed.get(key)) != norm(row.get(key)):
            issues.append(f"快照修正后预测与CSV不一致（{label}）：快照={snap_fixed.get(key)!r}，CSV={row.get(key)!r}")

    # 锁定核心剧本字段类型异常：显式记录（含场次编号与字段名），并按证据不足处理
    mid = str(row.get("编号", "") or "").strip()
    raw_primary = locked.get("primary_score")
    if raw_primary is not None and not isinstance(raw_primary, str):
        issues.append(f"快照场次 {mid} 锁定核心剧本字段类型异常：primary_score"
                      f"（期望字符串，实际 {type(raw_primary).__name__}），按证据不足处理")
    raw_alts = locked.get("alternative_scores")
    if raw_alts is not None and not isinstance(raw_alts, (list, tuple)):
        issues.append(f"快照场次 {mid} 锁定核心剧本字段类型异常：alternative_scores"
                      f"（期望列表，实际 {type(raw_alts).__name__}），按证据不足处理")

    # v2 归因证据：锁定核心剧本带净胜球概率时，用同一规则引擎重演分布口径结果。
    # 旧快照无该字段/为 None → post_only_v2 为 None，归因口径与旧版完全一致；
    # 快照内概率非法（损坏/被改）显式记入 issues，不静默按伪造概率归因。
    locked_probs = locked.get("net_goal_probabilities")
    post_only_v2 = None
    if isinstance(locked_probs, dict):
        vc = validate_net_goal_probabilities(locked_probs)
        if vc["ok"]:
            v2_input = copy.deepcopy(orig_pred)
            v2_input["_net_goal_probabilities"] = vc["probabilities"]
            post_only_v2, _ = post_process_prediction(v2_input, handicap)
        else:
            issues.append(f"快照场次 {mid} 锁定核心剧本字段异常：net_goal_probabilities "
                          f"非法（{vc['reason']}），v2 归因证据缺失，按证据不足处理")

    # ---- 原始/锁定/最终三视角取值 ----
    o_spf = str(original.get("胜平负推荐") or "").strip()
    l_spf = str(locked.get("full_time_result") or "").strip()
    c_spf = str(row.get("胜平负推荐") or "").strip()
    spf_overridden = bool(l_spf) and l_spf != o_spf
    # 胜平负锁定是否生效于最终输出（post_process 半全场过滤方向仅取自胜平负推荐，
    # 见 analyze.py post_process_prediction，故此为半全场"共同作用"的唯一锁定证据）
    spf_lock_effective = spf_overridden and l_spf == c_spf

    o_scores = _snapshot_list(original.get("比分推荐"))
    l_primary = str(raw_primary).strip() if isinstance(raw_primary, str) else ""
    l_scores = ([l_primary] + _safe_str_list(locked.get("alternative_scores")))
    l_scores = [s for s in l_scores if s]
    c_scores = split_rec(row.get("比分推荐"))
    # 三个相互独立的判断，不得混为一个变量：
    # 1) 比分列表是否因核心剧本锁定机制变化（锁定列表与原始列表不同：主推替换或备选增删）
    score_locked_list_differs = bool(l_scores) and l_scores != o_scores
    # 2) 原始主推比分是否被锁定主推比分改变、且锁定主推真实生效于最终输出
    #    （让球/总进球规则推导基准变化的直接证据；仅凭备选比分列表变化不算）
    o_primary = o_scores[0] if o_scores else ""
    primary_lock_effective = (bool(l_primary) and bool(o_primary)
                              and l_primary != o_primary
                              and bool(c_scores) and c_scores[0] == l_primary)
    # 3) 最终比分是否属于锁定剧本允许的比分集合：见 _locked_covers_scores(locked, c_scores)

    first_score = c_scores[0] if c_scores else ""

    # ---- 胜平负 ----
    spf_changed = o_spf != c_spf
    if not spf_changed:
        spf_cause = "无变化"
    elif spf_lock_effective:
        spf_cause = "核心剧本锁定覆盖"
    else:
        spf_cause = "无法归因"

    # ---- 比分 ----
    score_changed = o_scores != c_scores
    if not score_changed:
        score_cause = "无变化"
    elif _locked_covers_scores(locked, c_scores):
        score_cause = "核心剧本锁定覆盖"
    else:
        score_cause = "无法归因"

    # ---- 让球 ----
    rq_before = str(original.get("让球推荐") or "").strip()
    rq_after = str(row.get("让球推荐") or "").strip()
    rq_changed = rq_before != rq_after
    rq_correct = derive_handicap_recommendation(first_score, handicap)
    rq_version = str(row.get("让球推导版本") or "").strip()
    if not rq_changed:
        rq_cause = "无变化"
    elif (rq_version == HANDICAP_VERSION_V2 and post_only_v2 is not None
          and str(post_only_v2.get("让球推荐") or "").strip() == rq_after):
        rq_cause = "净胜球分布规则推导(v2)"
    elif str(post_only.get("让球推荐") or "").strip() == rq_after:
        rq_cause = "玩法规则后处理"
    elif primary_lock_effective:
        rq_cause = "共同作用"
    else:
        rq_cause = "无法归因"

    # ---- 半全场（中文与 HDA 等价规范化后比较） ----
    hafu_before = parse_hafu_codes(_snapshot_list(original.get("半全场推荐")))
    hafu_after = parse_hafu_codes(split_rec(row.get("半全场推荐")))
    hafu_removed = [c for c in hafu_before if c not in hafu_after]
    hafu_added = [c for c in hafu_after if c not in hafu_before]
    hafu_changed = hafu_before != hafu_after
    if not hafu_changed:
        hafu_cause = "无变化"
    elif parse_hafu_codes(post_only.get("半全场推荐")) == hafu_after:
        hafu_cause = "玩法规则后处理"
    elif spf_lock_effective:
        hafu_cause = "共同作用"
    else:
        hafu_cause = "无法归因"

    # ---- 总进球 ----
    ttg_before = canon_ttg(_snapshot_list(original.get("总进球推荐")))
    ttg_after = canon_ttg(split_rec(row.get("总进球推荐")))
    ttg_added = [t for t in ttg_after if t not in ttg_before]
    ttg_changed = ttg_before != ttg_after
    if not ttg_changed:
        ttg_cause = "无变化"
    elif canon_ttg(post_only.get("总进球推荐")) == ttg_after:
        ttg_cause = "玩法规则后处理"
    elif primary_lock_effective:
        ttg_cause = "共同作用"
    else:
        ttg_cause = "无法归因"

    causes = {"胜平负": spf_cause, "让球": rq_cause, "半全场": hafu_cause,
              "总进球": ttg_cause, "比分": score_cause}
    play_changes = {"胜平负": spf_changed, "让球": rq_changed, "半全场": hafu_changed,
                    "总进球": ttg_changed, "比分": score_changed}
    any_change = any(play_changes.values())

    # 修正后状态（快照口径：基于最终 CSV 的一致性复查）
    if post_hard:
        post_status = "仍有冲突"
    elif post_unjudgeable2:
        post_status = "无法判定"
    elif any_change:
        post_status = "已修正，复查通过"
    else:
        post_status = "通过"

    alt_conflicts = [m for m in pre_soft if "备选比分" in m]
    alt_hard = [m for m in pre_hard if "备选比分" in m]

    home, away = parse_teams(row.get("对阵", ""))
    comp = {
        "编号": row.get("编号", ""),
        "联赛": row.get("联赛", ""),
        "主队": home,
        "客队": away,
        "核心剧本": str(original.get("核心剧本") or ""),
        "原始主比分": _snapshot_list(original.get("比分推荐"))[:1][0] if _snapshot_list(original.get("比分推荐")) else "",
        "原始净胜球分布": str(original.get("净胜球分布") or ""),
        "体彩让球数": handicap,
        "让球": {"修正前": rq_before, "修正后": rq_after,
                 "规则计算正确结果": rq_correct, "被修正": rq_changed,
                 "推导版本": rq_version, "归因": rq_cause,
                 "让球次选": str(row.get("让球次选", "") or ""),
                 "净胜球概率分布": str(row.get("净胜球概率分布", "") or ""),
                 "让球结果概率": str(row.get("让球结果概率", "") or "")},
        "半全场": {"修正前": hafu_before, "修正后": hafu_after,
                   "删除项": hafu_removed, "新增项": hafu_added, "被修正": hafu_changed},
        "总进球": {"修正前": ttg_before, "修正后": ttg_after,
                   "补入项": ttg_added, "被修正": ttg_changed},
        "比分": {"修正前": o_scores, "修正后": c_scores, "被修正": score_changed,
                 "锁定机制": (("锁定列表替换原始输出" if score_locked_list_differs
                            else "锁定列表与原始一致，按同方向同区间过滤")
                           if score_cause == "核心剧本锁定覆盖" else "")},
        "备选比分": _snapshot_list(original.get("比分推荐"))[1:],
        "备选比分冲突": alt_conflicts + alt_hard,
        "剧本字段": {
            "净胜球分布": {"修正前": str(original.get("净胜球分布") or ""),
                           "修正后": str(row.get("净胜球分布") or ""),
                           "锁定值": str(locked.get("net_goal_interval") or "")},
            "核心剧本": {"修正前": str(original.get("核心剧本") or ""),
                         "修正后": str(row.get("核心剧本") or ""),
                         "锁定值": str(locked.get("match_script") or "")},
        },
        "一致性检查": {
            "修正前硬性错误": pre_hard,
            "修正前软性冲突": pre_soft,
            "修正前无法判定": pre_unjudgeable,
            "修正后状态": post_status,
            "修正报告": "",
        },
        "数据缺失说明": str(original.get("数据缺失说明") or row.get("数据缺失说明") or ""),
        "归因": causes,
        "玩法变化": play_changes,
        "快照问题": issues,
        "命中": None,
    }

    # --- 真实赛果：修正前后命中（与旧口径同一判定逻辑） ---
    if actual:
        sg = actual.get("赛果", {}) or {}
        actual_rq = str(sg.get("让球", "") or "").strip()
        actual_hafu = str(sg.get("半全场", "") or "").strip().upper()
        actual_ttg = canon_ttg([sg.get("总进球", "")])
        comp["实际赛果"] = {
            "全场": sg.get("全场", ""), "半场": sg.get("半场", ""),
            "胜平负": sg.get("胜平负", ""), "让球": actual_rq,
            "半全场": actual_hafu, "总进球": sg.get("总进球", ""),
            "比分": sg.get("比分", ""),
        }
        comp["命中"] = {
            "让球": {
                "修正前": (rq_direction(rq_before) == actual_rq) if (rq_direction(rq_before) and actual_rq) else None,
                "修正后": (rq_direction(rq_after) == actual_rq) if (rq_direction(rq_after) and actual_rq) else None,
            },
            "半全场": {
                "修正前": (actual_hafu in hafu_before) if actual_hafu else None,
                "修正后": (actual_hafu in hafu_after) if actual_hafu else None,
            },
            "总进球": {
                "修正前": (actual_ttg[0] in ttg_before) if actual_ttg else None,
                "修正后": (actual_ttg[0] in ttg_after) if actual_ttg else None,
            },
        }
    return comp


# ============ 统计 ============

def _pct(part, total):
    return f"{part / total * 100:.1f}%" if total else "0.0%"


def summarize(comparisons):
    """汇总统计。命中统计只覆盖有真实赛果的场次；无赛果场次不参与命中率。"""
    total = len(comparisons)
    stats = {
        "总场数": total,
        "让球被修正": sum(1 for c in comparisons if c["让球"]["被修正"]),
        "半全场被修正": sum(1 for c in comparisons if c["半全场"]["被修正"]),
        "总进球被修正": sum(1 for c in comparisons if c["总进球"]["被修正"]),
        "备选比分冲突": sum(1 for c in comparisons if c["备选比分冲突"]),
        "数据缺失": sum(1 for c in comparisons if str(c["数据缺失说明"]).strip()),
        "修正前硬性错误": sum(1 for c in comparisons if c["一致性检查"]["修正前硬性错误"]),
        "规则检查失败": sum(1 for c in comparisons
                       if c["一致性检查"]["修正后状态"] in ("仍有冲突", "无法判定")),
    }
    judged = [c for c in comparisons if c.get("命中")]
    stats["有赛果场数"] = len(judged)
    for dim in ("让球", "半全场", "总进球"):
        for tag in ("修正前", "修正后"):
            vals = [c["命中"][dim][tag] for c in judged]
            valid = [v for v in vals if v is not None]
            stats[f"{dim}{tag}命中"] = sum(1 for v in valid if v)
            stats[f"{dim}{tag}样本"] = len(valid)
    # 快照口径扩展维度（旧口径 comp 无"玩法变化"键时保持全 0，不在报告中展示）
    stats["玩法变化"] = {f: 0 for f in SNAPSHOT_PLAY_FIELDS}
    stats["变化原因"] = {c: 0 for c in CHANGE_CAUSES}
    stats["有变化场数"] = 0
    for c in comparisons:
        pc = c.get("玩法变化")
        if not pc:
            continue
        if any(pc.values()):
            stats["有变化场数"] += 1
        for f, v in pc.items():
            if v:
                stats["玩法变化"][f] += 1
        seen_causes = set()
        for cause in c.get("归因", {}).values():
            if cause in stats["变化原因"] and cause not in seen_causes:
                stats["变化原因"][cause] += 1
                seen_causes.add(cause)
    return stats


# ============ 报告文本 ============

def _fmt_list(lst):
    return " / ".join(str(x) for x in lst) if lst else "（空）"


def _fmt_hit(v):
    return {True: "命中", False: "未命中", None: "无法判定"}[v]


def format_match_block(idx, c):
    L = []
    L.append("=" * 72)
    L.append(f"[{idx}] {c['编号']}  {c['联赛']}  {c['主队']} vs {c['客队']}")
    L.append("-" * 72)
    L.append(f"原始核心剧本：{c['核心剧本'] or '（缺失）'}")
    L.append(f"原始主比分：{c['原始主比分'] or '（缺失）'}    原始净胜球分布：{c['原始净胜球分布'] or '（缺失）'}")
    L.append(f"体彩让球数：{c['体彩让球数'] or '（缺失）'}")

    rq = c["让球"]
    flag = "【已修正】" if rq["被修正"] else "【未变】"
    L.append(f"让球胜平负 {flag} 修正前：{rq['修正前'] or '（空）'} → 修正后：{rq['修正后'] or '（空）'}")
    rq_version = rq.get("推导版本", "")
    if rq_version == HANDICAP_VERSION_V2:
        L.append(f"  推导口径：{rq_version}（净胜球概率分布→让球概率确定性映射）"
                 + (f"，归因：{rq.get('归因', '')}" if rq.get("归因") else ""))
        if rq.get("让球次选"):
            L.append(f"  让球次选：{rq['让球次选']}")
        if rq.get("净胜球概率分布"):
            L.append(f"  净胜球概率：{rq['净胜球概率分布']}")
        if rq.get("让球结果概率"):
            L.append(f"  让球结果概率：{rq['让球结果概率']}")
        L.append(f"  v1 单点参考（首选比分+让球数，仅历史口径）：{rq['规则计算正确结果']}")
    else:
        L.append(f"  规则计算正确结果（主比分+让球数）：{rq['规则计算正确结果']}")

    hf = c["半全场"]
    flag = "【已修正】" if hf["被修正"] else "【未变】"
    L.append(f"半全场 {flag} 修正前：{_fmt_list(hf['修正前'])} → 修正后：{_fmt_list(hf['修正后'])}")
    if hf["删除项"]:
        L.append(f"  删除冲突选项：{_fmt_list(hf['删除项'])}")
    if hf["新增项"]:
        L.append(f"  程序补入/兜底：{_fmt_list(hf['新增项'])}")

    tg = c["总进球"]
    flag = "【已修正】" if tg["被修正"] else "【未变】"
    L.append(f"总进球 {flag} 修正前：{_fmt_list(tg['修正前'])} → 修正后：{_fmt_list(tg['修正后'])}")
    if tg["补入项"]:
        L.append(f"  补入项：{_fmt_list(tg['补入项'])}（仅为一致性修正，不代表概率更高）")

    if c["备选比分"]:
        L.append(f"备选比分：{_fmt_list(c['备选比分'])}")
    if c["备选比分冲突"]:
        L.append("备选比分与核心剧本冲突：")
        for m in c["备选比分冲突"]:
            L.append(f"  - {m}")
    else:
        L.append("备选比分与核心剧本冲突：无")

    chk = c["一致性检查"]
    L.append(f"一致性检查（修正前）：硬性错误 {len(chk['修正前硬性错误'])} 项，"
             f"软性冲突 {len(chk['修正前软性冲突'])} 项，无法判定 {len(chk['修正前无法判定'])} 项")
    for m in chk["修正前硬性错误"]:
        L.append(f"  硬性错误：{m}")
    for m in chk["修正前软性冲突"]:
        L.append(f"  软性冲突：{m}")
    for m in chk["修正前无法判定"]:
        L.append(f"  无法判定：{m}")
    L.append(f"一致性检查（修正后状态）：{chk['修正后状态']}")

    miss = str(c["数据缺失说明"]).strip()
    L.append(f"数据缺失说明：{miss if miss else '无'}")

    # 快照口径扩展信息（旧口径 comp 无以下键，自动跳过）
    if c.get("比分") and c["比分"]["被修正"]:
        L.append(f"比分推荐【已变化】 修正前：{_fmt_list(c['比分']['修正前'])} → 修正后：{_fmt_list(c['比分']['修正后'])}")
    sf = c.get("剧本字段")
    if sf:
        for name, d in sf.items():
            if d["修正前"] != d["修正后"]:
                L.append(f"  [剧本字段变化] {name}：修正前 {d['修正前'] or '（空）'} → 修正后 {d['修正后'] or '（空）'}（锁定值：{d['锁定值'] or '（无）'}）")
    if c.get("归因"):
        cause_txt = "；".join(f"{k}：{v}" for k, v in c["归因"].items() if v != "无变化")
        L.append(f"变化归因：{cause_txt if cause_txt else '无变化'}")
    for q in c.get("快照问题", []):
        L.append(f"  [快照异常] {q}")

    if c.get("实际赛果"):
        sg = c["实际赛果"]
        L.append(f"实际赛果：全场 {sg['全场']}，半场 {sg['半场']}，"
                 f"胜平负 {sg['胜平负']}，让球 {sg['让球']}，半全场 {sg['半全场']}，"
                 f"总进球 {sg['总进球']}，比分 {sg['比分']}")
        hit = c["命中"]
        for dim in ("让球", "半全场", "总进球"):
            h = hit[dim]
            L.append(f"  {dim}命中：修正前 {_fmt_hit(h['修正前'])} / 修正后 {_fmt_hit(h['修正后'])}")
    else:
        L.append("实际赛果：未获取（不统计命中）")
    L.append("")
    return "\n".join(L)


def format_summary(stats, context=None):
    L = []
    total = stats["总场数"]
    L.append("=" * 72)
    L.append("统计汇总")
    L.append("=" * 72)
    if context:
        L.append(f"数据来源口径：{context['source_note']}")
    L.append(f"总比赛场数：{total}")
    L.append(f"让球推荐被修正：{stats['让球被修正']} 场（{_pct(stats['让球被修正'], total)}）")
    L.append(f"半全场推荐被修正：{stats['半全场被修正']} 场（{_pct(stats['半全场被修正'], total)}）")
    L.append(f"总进球推荐被修正：{stats['总进球被修正']} 场（{_pct(stats['总进球被修正'], total)}）")
    L.append(f"备选比分冲突场数：{stats['备选比分冲突']}")
    L.append(f"数据缺失场数：{stats['数据缺失']}")
    L.append(f"修正前存在硬性错误场数：{stats['修正前硬性错误']}")
    L.append(f"规则检查失败场数（修正后仍有冲突或无法判定）：{stats['规则检查失败']}")
    if context and context.get("snapshot_mode"):
        pv = stats["玩法变化"]
        L.append("")
        L.append("按玩法字段统计变化场数（一场比赛可涉及多个字段，各字段独立计数）：")
        for f in SNAPSHOT_PLAY_FIELDS:
            L.append(f"  {f}：{pv[f]} 场")
        cr = stats["变化原因"]
        L.append("")
        L.append("按变化原因统计场数（一场比赛可计入多个原因类；原因分类与玩法分类为不同维度，两组数字不得相加；"
                 "变化场数 ≠ 修正正确，修正率 ≠ 命中率）：")
        for c in CHANGE_CAUSES:
            L.append(f"  {c}：{cr[c]} 场")
        L.append("  注：'核心剧本锁定覆盖'含两种机制——锁定值直接替换原始输出，以及锁定剧本按")
        L.append("      '同方向同区间'过滤备选比分导致列表缩短；两者均为锁定机制派生，非模型自由生成。")
        L.append(f"  存在任一变化的唯一比赛数：{stats['有变化场数']} 场")
        if context.get("csv_only"):
            L.append("")
            L.append("CSV 中存在但快照缺失的场次（未纳入快照对照统计）：")
            for q in context["csv_only"]:
                L.append(f"  - {q}")
        if context.get("snap_only"):
            L.append("")
            L.append("快照中存在但 CSV 缺失的场次：")
            for q in context["snap_only"]:
                L.append(f"  - {q}")
        if context.get("issues"):
            L.append("")
            L.append("异常清单：")
            for q in context["issues"]:
                L.append(f"  - {q}")
    n = stats["有赛果场数"]
    if n:
        L.append("")
        L.append(f"修正前后实际命中率（仅统计有真实赛果的 {n} 场）：")
        for dim in ("让球", "半全场", "总进球"):
            b, bs = stats[f"{dim}修正前命中"], stats[f"{dim}修正前样本"]
            a, as_ = stats[f"{dim}修正后命中"], stats[f"{dim}修正后样本"]
            L.append(f"  {dim}：修正前 {b}/{bs}（{_pct(b, bs)}） → 修正后 {a}/{as_}（{_pct(a, as_)}）")
        L.append("")
        L.append("注意：修正率不等于预测准确率。以上命中对比仅说明规则修正前后与实际赛果的")
        L.append("吻合差异，样本量小，不构成“修正后预测准确率提高”的结论。")
    else:
        L.append("")
        L.append("未获取真实赛果，不统计命中率。修正率不等于预测准确率。")
    return "\n".join(L)


# ============ 数据加载与主流程 ============

def load_analysis_rows(csv_path):
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_actual_results(json_path):
    """每日记录 JSON → {编号: 场次记录}；文件不存在返回 {}。"""
    if not json_path or not os.path.isfile(json_path):
        return {}
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {m.get("编号", ""): m for m in data.get("场次", []) if m.get("编号")}


def load_snapshot(date_str, output_dir="output"):
    """读取原始预测快照，返回 (index, issues)。

    - index: {编号: 场次记录}；仅当快照文件**确实不存在**时为 None（允许旧口径回退）。
    - issues: 逐条结构/字段/重复编号异常（显式报告，不静默）。
    - 文件损坏或顶层结构错误：抛 ValueError（不静默回退旧口径）。
    """
    path = os.path.join(output_dir, f"{SNAPSHOT_FILE}_{date_str}.json")
    if not os.path.isfile(path):
        return None, []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"原始预测快照损坏，无法解析：{path}（{e}）")
    if not isinstance(data, dict) or not isinstance(data.get("场次"), list):
        raise ValueError(f"原始预测快照结构错误：{path}（顶层缺'场次'列表）")
    index, issues = {}, []
    for i, m in enumerate(data["场次"], 1):
        if not isinstance(m, dict):
            issues.append(f"快照第 {i} 条不是对象，已跳过")
            continue
        mid = str(m.get("编号") or "").strip()
        if not mid:
            issues.append(f"快照第 {i} 条缺'编号'，已跳过")
            continue
        if mid in index:
            issues.append(f"快照编号重复：{mid}（保留首条，后续条目忽略）")
            continue
        missing = [k for k in SNAPSHOT_REQUIRED_KEYS if not isinstance(m.get(k), dict)]
        if missing:
            issues.append(f"快照场次 {mid} 缺必要字段：{'、'.join(missing)}")
            if "原始玩法输出" in missing:
                issues.append(f"快照场次 {mid} 无原始玩法输出，无法做快照对照，已排除")
                continue
        index[mid] = m
    return index, issues


def build_report_text(date_str, output_dir="output"):
    """生成对照报告文本（纯生成，不写文件）。返回报告文本。

    快照优先：存在原始预测快照时按快照口径对照；仅当快照确实不存在时回退旧口径。
    """
    csv_path = os.path.join(output_dir, f"竞彩分析_{date_str}.csv")
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f"找不到预测文件：{csv_path}")
    json_path = os.path.join(output_dir, f"每日记录_{date_str}.json")

    rows = load_analysis_rows(csv_path)
    actuals = load_actual_results(json_path)
    snapshot, snap_issues = load_snapshot(date_str, output_dir)

    snapshot_mode = snapshot is not None
    comparisons, issues, csv_only, snap_only = [], list(snap_issues), [], []
    seen = set()
    for row in rows:
        mid = str(row.get("编号", "") or "").strip()
        if mid in seen:
            issues.append(f"CSV 编号重复：{mid}（保留首条，后续忽略）")
            continue
        seen.add(mid)
        actual = actuals.get(mid)
        if snapshot_mode:
            entry = snapshot.get(mid)
            if entry is None:
                note = str(row.get("综合分析", "") or "")
                if note.startswith(("核心剧本校验未通过", "分析失败：")):
                    reason = "该场为核心剧本校验未通过/分析失败记录，无原始预测快照属预期"
                else:
                    reason = "该场在快照中缺失，原因未知，未纳入快照对照"
                csv_only.append(f"{mid}（{row.get('对阵', '')}）：{reason}")
                continue
            comparisons.append(build_snapshot_comparison(row, entry, actual))
        else:
            comparisons.append(build_match_comparison(row, actual))
    if snapshot_mode:
        for mid, m in snapshot.items():
            if mid not in seen:
                snap_only.append(f"{mid}（{m.get('对阵', '')}）")

    stats = summarize(comparisons)
    if snapshot_mode:
        source_note = (f"原始预测=原始预测快照_{date_str}.json（LLM 原始玩法输出/锁定核心剧本/修正后预测）；"
                       f"最终结果=竞彩分析_{date_str}.csv")
        mode_line = ("口径：修正前=快照中的 LLM 原始玩法输出，修正后=竞彩分析 CSV 最终输出；"
                     "不对已修正 CSV 重跑规则修正过程。")
    else:
        source_note = f"竞彩分析_{date_str}.csv（旧口径，无快照）"
        mode_line = ("旧口径：无法恢复原始模型输出（未找到原始预测快照）。本报告'修正前'取自 CSV，"
                     "修正统计为对 CSV 重跑规则修正的差异；CSV 可能已含运行时修正，不代表 LLM 原始输出。")
    context = {
        "snapshot_mode": snapshot_mode,
        "source_note": source_note,
        "issues": issues,
        "csv_only": csv_only,
        "snap_only": snap_only,
    }

    header = [
        f"竞彩足球预测修正对照报告（{date_str}）",
        f"数据来源：{os.path.basename(csv_path)}（{len(rows)} 场最终预测）"
        + (f" + {os.path.basename(json_path)}（{stats['有赛果场数']} 场真实赛果）" if actuals else "（无真实赛果）"),
        mode_line,
        "说明：本报告对比大模型原始预测与规则引擎修正后结果；原始 CSV / 快照 JSON 未被修改。",
        "命中判定口径：半全场推荐中的中文描述（如“半平-全平”）先转换为 HDA 代码再判定；",
        "review.py 复盘与本报告均已调用 lottery_rules 统一判定，两边口径一致。",
        "",
    ]
    body = [format_match_block(i, c) for i, c in enumerate(comparisons, 1)]
    return "\n".join(header) + "\n".join(body) + format_summary(stats, context) + "\n"


def generate_compare_report(date_str, output_dir="output", suffix=""):
    """生成指定日期的修正对照报告，返回报告文件路径。

    suffix：可选文件名后缀（如 "_v2"），用于在保留历史报告的同时另存新报告；
    后缀仅允许字母/数字/下划线/连字符，禁止路径分隔符。
    已有报告文件时拒绝覆盖并抛 FileExistsError（保护历史存档）。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]*", str(suffix)):
        raise ValueError(f"非法的报告文件名后缀：{suffix!r}（仅允许字母/数字/下划线/连字符）")
    text = build_report_text(date_str, output_dir)
    out_path = os.path.join(output_dir, f"修正对照报告_{date_str}{suffix}.txt")
    if os.path.exists(out_path):
        raise FileExistsError(f"报告已存在，拒绝覆盖：{out_path}。请先备份或删除已有文件。")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return out_path


def main():
    args = sys.argv[1:]
    date_str = args[0] if args else "2026-10-11"
    suffix = args[1] if len(args) > 1 else ""
    try:
        out_path = generate_compare_report(date_str, suffix=suffix)
    except FileNotFoundError as e:
        print(f"错误：{e}")
        sys.exit(1)
    except FileExistsError as e:
        print(f"保护：{e}")
        sys.exit(2)
    except ValueError as e:
        print(f"快照异常：{e}")
        sys.exit(3)
    print(f"修正对照报告已生成：{out_path}")


if __name__ == "__main__":
    main()
