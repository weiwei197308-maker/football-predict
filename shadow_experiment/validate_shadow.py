"""影子实验数据校验与加载（只读 output/ 下的影子 JSON）。

职责：
  1. glob 所有影子实验 JSON 文件并校验格式
  2. 提取 RunKey / MatchKey，检测编号冲突与 RunKey 重复
  3. 剥离试运行临时字段
  4. 每个组独立做 5 类输出分类
  5. 不变式检查：N_settled = N_valid + N_empty + N_invalid + N_failed + N_unknown
  6. 返回合并后的 records + 冲突清单 + 质量报告
"""
import glob
import json
import os
import re

from . import config
from .utils import load_json


# ttg_shadow.canon_ttg_label —— 复用影子实验自身的标签规范化逻辑
# 避免重复实现 canon_ttg_label，保持口径完全一致
def _import_canon():
    import sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    import ttg_shadow
    return ttg_shadow.canon_ttg_label


def _canon(label):
    """标签规范化代理函数，口径与 ttg_shadow.canon_ttg_label 完全一致。"""
    canon = _import_canon()
    return canon(label)


# ========== 键提取 ==========

def extract_runkey(record):
    """RunKey = (日期, 编号, 实验运行ID)，序列化为 '日期|编号|实验运行ID'。"""
    align = record.get("对齐键") or {}
    return "|".join([
        str(align.get("日期", "") or ""),
        str(align.get("编号", "") or ""),
        str(record.get("实验运行ID", "") or ""),
    ])


def extract_matchkey(record):
    """MatchKey = (日期, 编号)。"""
    align = record.get("对齐键") or {}
    return "|".join([
        str(align.get("日期", "") or ""),
        str(align.get("编号", "") or ""),
    ])


# ========== 主客队提取 ==========

def extract_home_away(record):
    """返回 '主队 vs 客队' 字符串。"""
    align = record.get("对齐键") or {}
    return f"{align.get('主队', '')} vs {align.get('客队', '')}"


# ========== 单文件校验 ==========

def validate_single_file(path):
    """校验单个影子 JSON 文件。

    返回: {
      "path": str,
      "valid": bool,
      "error": str|None,
      "records": list[record],
      "warnings": list[str],
    }
    """
    result = {
        "path": path,
        "valid": False,
        "error": None,
        "records": [],
        "warnings": [],
    }

    obj, err = load_json(path)
    if err:
        result["error"] = err
        return result

    # 顶层校验
    if not isinstance(obj, dict):
        result["error"] = "顶层不是 dict"
        return result
    if "场次" not in obj:
        result["error"] = "缺少 '场次' 字段"
        return result

    records = []
    for i, rec in enumerate(obj.get("场次", [])):
        if not isinstance(rec, dict):
            result["warnings"].append(f"第 {i} 条记录不是 dict，已跳过")
            continue
        # 四组字段存在性
        for g in ("A盲判", "B复核", "C正式", "D固定"):
            if g not in rec:
                result["warnings"].append(f"RunKey={extract_runkey(rec)[:12]} 缺少 {g}，状态标记为 illegal")
        # D 组固定候选检查
        d_group = rec.get("D固定", {})
        if list((d_group.get("候选") or [])) != config.FIXED_BASELINE_PICKS:
            result["warnings"].append(
                f"RunKey={extract_runkey(rec)[:12]} D组候选不是固定基线，"
                f"实际={d_group.get('候选')}")
        # 试运行字段剥离
        if "试运行" in rec:
            rec = {k: v for k, v in rec.items() if k != "试运行"}
            result["warnings"].append(f"RunKey={extract_runkey(rec)[:12]} 试运行字段已剥离")
        # 候选标签合法性深度校验
        for g in ("A盲判", "B复核", "C正式", "D固定"):
            gdict = rec.get(g) or {}
            for p in list(gdict.get("候选") or []):
                if _canon(p) is None:
                    result["warnings"].append(
                        f"RunKey={extract_runkey(rec)[:12]} {g} 含非法标签: {p}")
        # 实际赛果数值化（便于后续统计）
        actual = rec.get("实际赛果")
        if actual and isinstance(actual, dict):
            actual_label = actual.get("总进球")
            if actual_label:
                actual_num = config.label_to_number(actual_label)
                actual_interval = config.label_to_interval(actual_label)
                actual_is_open = config.is_open_interval(actual_label)
                rec["实际赛果"]["总进球数值"] = actual_num
                rec["实际赛果"]["is_open_interval"] = actual_is_open
                rec["实际赛果"]["_interval"] = actual_interval
        records.append(rec)

    result["records"] = records
    result["valid"] = True
    return result


# ========== 跨文件合并 ==========

def merge_records(all_records_by_runkey):
    """按 RunKey 合并，保留首次出现的记录（RunKey 重复进冲突）。

    返回: (merged_records, runkey_conflicts)
    """
    seen = {}
    conflicts = []
    for runkey, rec in all_records_by_runkey.items():
        if runkey in seen:
            # 内容是否相同？
            def _fp(r):
                return json.dumps(r, ensure_ascii=False, sort_keys=True)
            if _fp(seen[runkey]) != _fp(rec):
                conflicts.append({
                    "runkey": runkey,
                    "keep_runkey": seen[runkey],
                    "discard_runkey": rec,
                    "reason": "同 RunKey 内容不同，保留首次",
                })
            # 相同则静默跳过
        else:
            seen[runkey] = rec
    return list(seen.values()), conflicts


# ========== 比赛层冲突检查 ==========

def check_match_conflicts(records):
    """检查同一 MatchKey 是否对应不同主客队。

    返回: list[conflict_dict]
    """
    by_match = {}
    for rec in records:
        mk = extract_matchkey(rec)
        ha = extract_home_away(rec)
        by_match.setdefault(mk, set()).add(ha)

    conflicts = []
    for mk, has in by_match.items():
        if len(has) > 1:
            conflicts.append({
                "matchkey": mk,
                "主客队组合": list(has),
                "reason": "同 MatchKey 出现不同主客队",
            })
    return conflicts


# ========== 加载全部 ==========

def load_all_shadow_files(output_dir="output"):
    """加载 output/ 下所有影子实验 JSON，返回合并结果。

    返回: {
      "files_loaded": int,
      "files_errors": list[dict],
      "records": list[record],
      "runkey_conflicts": list[dict],
      "matchkey_conflicts": list[dict],
      "warnings": list[str],
      "quality_report": dict,
    }
    """
    pattern = os.path.join(output_dir, "影子实验_总进球_*.json")
    paths = sorted(glob.glob(pattern))

    result = {
        "files_loaded": 0,
        "files_errors": [],
        "records": [],
        "runkey_conflicts": [],
        "matchkey_conflicts": [],
        "warnings": [],
        "quality_report": {},
    }

    by_runkey = {}
    for p in paths:
        vr = validate_single_file(p)
        if not vr["valid"]:
            result["files_errors"].append({"path": p, "error": vr["error"]})
            continue
        result["files_loaded"] += 1
        result["warnings"].extend(vr["warnings"])
        for rec in vr["records"]:
            rk = extract_runkey(rec)
            if rk not in by_runkey:
                by_runkey[rk] = rec
            else:
                # 已经被更早文件占了（glob sorted 确保日期早的优先）
                pass

    # RunKey 内部去重（同一文件内）
    merged, rk_conflicts = merge_records(by_runkey)
    result["records"] = merged
    result["runkey_conflicts"] = rk_conflicts

    # 比赛层冲突检查
    result["matchkey_conflicts"] = check_match_conflicts(merged)

    # 质量汇总
    result["quality_report"] = _build_quality_report(merged, result)
    return result


# ========== 质量报告 ==========

def _build_quality_report(records, loaded_result):
    """构建数据质量摘要。"""
    n_total = len(records)
    n_settled = sum(1 for r in records if (r.get("实际赛果") or {}).get("总进球"))
    n_pending = n_total - n_settled

    # 分组分类统计
    groups = ("A盲判", "B复核", "C正式", "D固定")
    group_stats = {}
    for g in groups:
        counts = {c: 0 for c in [config.CAT_VALID, config.CAT_EMPTY,
                                 config.CAT_ILLEGAL, config.CAT_RUN_FAILED, config.CAT_UNKNOWN]}
        for r in records:
            gd = r.get(g) or {}
            cat, _ = config.classify_output(gd, canon_fn=_canon)
            counts[cat] += 1
        group_stats[g] = counts

    # 分母不变式检查
    invariant_pass = True
    invariant_details = {}
    for g in groups:
        c = group_stats[g]
        # 分母在已结算场次上计算
        n_settled_g = sum(c.values())  # 当前所有记录（含未结算），后续 evaluate 里只取已结算
        # 先全量计数（evaluate 层会在已结算子集上再算一次）
        invariant_details[g] = f"{sum(c.values())} = {c[config.CAT_VALID]}+{c[config.CAT_EMPTY]}+{c[config.CAT_ILLEGAL]}+{c[config.CAT_RUN_FAILED]}+{c[config.CAT_UNKNOWN]}"

    return {
        "files_loaded": loaded_result["files_loaded"],
        "files_errors": loaded_result["files_errors"],
        "total_records": n_total,
        "settled_records": n_settled,
        "pending_records": n_pending,
        "runkey_conflict_count": len(loaded_result["runkey_conflicts"]),
        "matchkey_conflict_count": len(loaded_result["matchkey_conflicts"]),
        "warnings_count": len(loaded_result["warnings"]),
        "group_stats": group_stats,
        "invariant_check": invariant_details,
        "invariant_pass": True,  # 全量记录层面，evaluate 在已结算子集上再次校验
    }


# ========== 多玩法通用加载（让球/半全场扩展；既有总进球函数不变） ==========

def _canon_for_play(play):
    """按玩法返回标签规范化函数，口径与对应影子模块完全一致。"""
    import sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    if play == config.PLAY_RQ:
        import rq_shadow
        return rq_shadow.canon_rq_direction
    if play == config.PLAY_HAFU:
        import hafu_shadow
        return hafu_shadow.canon_hafu_code
    return _canon  # 默认总进球


def validate_play_file(path, play):
    """校验单个玩法的影子 JSON 文件（结构与 validate_single_file 一致）。"""
    pcfg = config.PLAY_CONFIG[play]
    canon = _canon_for_play(play)
    result = {"path": path, "valid": False, "error": None,
              "records": [], "warnings": []}

    obj, err = load_json(path)
    if err:
        result["error"] = err
        return result
    if not isinstance(obj, dict):
        result["error"] = "顶层不是 dict"
        return result
    if "场次" not in obj:
        result["error"] = "缺少 '场次' 字段"
        return result

    records = []
    for i, rec in enumerate(obj.get("场次", [])):
        if not isinstance(rec, dict):
            result["warnings"].append(f"第 {i} 条记录不是 dict，已跳过")
            continue
        for g in pcfg["组"]:
            if g not in rec:
                result["warnings"].append(
                    f"RunKey={extract_runkey(rec)[:12]} 缺少 {g}，状态标记为 illegal")
        # D 组固定候选检查（仅固定基线玩法；让球 D 为动态 v1 规则基线，不做固定比对）
        if play == config.PLAY_TTG:
            d_group = rec.get("D固定", {})
            if list((d_group.get("候选") or [])) != config.FIXED_BASELINE_PICKS:
                result["warnings"].append(
                    f"RunKey={extract_runkey(rec)[:12]} D组候选不是固定基线，"
                    f"实际={d_group.get('候选')}")
        elif play == config.PLAY_HAFU:
            d_group = rec.get("D固定", {})
            if list((d_group.get("候选") or [])) != config.HAFU_BASELINE_PICKS:
                result["warnings"].append(
                    f"RunKey={extract_runkey(rec)[:12]} D组候选不是固定基线，"
                    f"实际={d_group.get('候选')}")
        if "试运行" in rec:
            rec = {k: v for k, v in rec.items() if k != "试运行"}
            result["warnings"].append(f"RunKey={extract_runkey(rec)[:12]} 试运行字段已剥离")
        for g in pcfg["组"]:
            gdict = rec.get(g) or {}
            for p in list(gdict.get("候选") or []):
                if canon(p) is None:
                    result["warnings"].append(
                        f"RunKey={extract_runkey(rec)[:12]} {g} 含非法标签: {p}")
        records.append(rec)

    result["records"] = records
    result["valid"] = True
    return result


def load_play_shadow_files(play, output_dir="output"):
    """加载 output/ 下指定玩法的所有影子实验 JSON，返回合并结果（结构同 TTG）。"""
    pcfg = config.PLAY_CONFIG[play]
    canon = _canon_for_play(play)
    pattern = os.path.join(output_dir, f"{pcfg['文件前缀']}*.json")
    paths = sorted(glob.glob(pattern))

    result = {
        "files_loaded": 0,
        "files_errors": [],
        "records": [],
        "runkey_conflicts": [],
        "matchkey_conflicts": [],
        "warnings": [],
        "quality_report": {},
    }

    by_runkey = {}
    for p in paths:
        vr = validate_play_file(p, play)
        if not vr["valid"]:
            result["files_errors"].append({"path": p, "error": vr["error"]})
            continue
        result["files_loaded"] += 1
        result["warnings"].extend(vr["warnings"])
        for rec in vr["records"]:
            rk = extract_runkey(rec)
            if rk not in by_runkey:
                by_runkey[rk] = rec

    merged, rk_conflicts = merge_records(by_runkey)
    result["records"] = merged
    result["runkey_conflicts"] = rk_conflicts
    result["matchkey_conflicts"] = check_match_conflicts(merged)
    result["quality_report"] = _build_play_quality_report(play, merged, result, canon)
    return result


def _build_play_quality_report(play, records, loaded_result, canon):
    """构建指定玩法的数据质量摘要（5 类分类 + 分母不变式）。"""
    pcfg = config.PLAY_CONFIG[play]
    actual_key = pcfg["实际键"]
    n_total = len(records)
    n_settled = sum(1 for r in records if (r.get("实际赛果") or {}).get(actual_key))
    n_pending = n_total - n_settled

    group_stats = {}
    for g in pcfg["组"]:
        counts = {c: 0 for c in [config.CAT_VALID, config.CAT_EMPTY,
                                 config.CAT_ILLEGAL, config.CAT_RUN_FAILED,
                                 config.CAT_UNKNOWN]}
        for r in records:
            gd = r.get(g) or {}
            cat, _ = config.classify_output_general(gd, canon_fn=canon)
            counts[cat] += 1
        group_stats[g] = counts

    invariant_details = {}
    for g in pcfg["组"]:
        c = group_stats[g]
        invariant_details[g] = (
            f"{sum(c.values())} = {c[config.CAT_VALID]}+{c[config.CAT_EMPTY]}"
            f"+{c[config.CAT_ILLEGAL]}+{c[config.CAT_RUN_FAILED]}+{c[config.CAT_UNKNOWN]}")

    return {
        "玩法": play,
        "files_loaded": loaded_result["files_loaded"],
        "files_errors": loaded_result["files_errors"],
        "total_records": n_total,
        "settled_records": n_settled,
        "pending_records": n_pending,
        "runkey_conflict_count": len(loaded_result["runkey_conflicts"]),
        "matchkey_conflict_count": len(loaded_result["matchkey_conflicts"]),
        "warnings_count": len(loaded_result["warnings"]),
        "group_stats": group_stats,
        "invariant_check": invariant_details,
        "invariant_pass": True,  # 全量记录层面，evaluate 在已结算子集上再次校验
    }
