"""影子实验统计计算层（四组指标 + A→B 配对 + 极端桶 + D/市场四格表）。

核心约束：
  - 所有公式引用 config.py 常量，不硬编码
  - 7+球开放区间用 interval_distance 而非数值误差
  - 分母不变式：N_settled = N_valid + N_empty + N_invalid + N_failed + N_unknown
  - A→B 三个配对群体分母分离
  - 实验层 RunKey 独立统计，比赛层不自动选代表
"""
import os
import sys

# 确保项目根目录在 sys.path（import ttg_shadow）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from . import config
from .validate_shadow import (
    extract_runkey, extract_matchkey, _canon, load_all_shadow_files,
)


def _entry_for_group(record, group_name):
    """从 record 提取某组的 entry dict，用于 group_metrics 复用。"""
    g = record.get(group_name) or {}
    picks = list(g.get("候选", []) or [])
    ordered = bool(g.get("有序", True))
    state = g.get("状态", "ok")
    return {
        "候选": picks,
        "有序": ordered,
        "状态": state,
        "实际": None,  # evaluate_records 会填
        "市场前三": record.get("市场前三") or [],
        "_interval": None,
        "_num": None,
    }


def _entries_for_runkey(record):
    """单个 RunKey → 四组 entries dict，带实际赛果和数值/区间预计算。"""
    actual = (record.get("实际赛果") or {})
    actual_label = actual.get("总进球") if actual else None
    actual_interval = actual.get("_interval") if actual else None
    actual_num = actual.get("总进球数值") if actual else None
    actual_is_open = actual.get("is_open_interval", False) if actual else False

    entries = {}
    for g in ("A盲判", "B复核", "C正式", "D固定"):
        e = _entry_for_group(record, g)
        e["实际"] = actual_label
        e["实际_num"] = actual_num
        e["实际_interval"] = actual_interval
        e["实际_is_open"] = actual_is_open

        # 首选数值 / 区间预计算（有序有效预测时用）
        if e["有序"] and e["候选"]:
            top_label = e["候选"][0]
            e["首选_num"] = config.label_to_number(top_label)
            e["首选_interval"] = config.label_to_interval(top_label)
        else:
            e["首选_num"] = None
            e["首选_interval"] = None

        # 每个候选的区间预计算
        e["候选_intervals"] = [
            config.label_to_interval(p) for p in e["候选"]
        ] if e["候选"] else []

        # 分类（用 _canon 校验候选合法性）
        group_dict = record.get(g) or {}
        cat, cat_reason = config.classify_output(group_dict, canon_fn=_canon)
        e["分类"] = cat
        e["分类理由"] = cat_reason
        entries[g] = e

    # 市场前三（E 组，按同一框架）
    market = list(record.get("市场前三") or [])
    market_valid = [p for p in market if _canon(p) is not None]
    me = {
        "候选": market_valid,
        "有序": True,
        "状态": "ok" if market_valid else "empty",
        "实际": actual_label,
        "实际_num": actual_num,
        "实际_interval": actual_interval,
        "实际_is_open": actual_is_open,
        "首选_num": config.label_to_number(market_valid[0]) if market_valid else None,
        "首选_interval": config.label_to_interval(market_valid[0]) if market_valid else None,
        "候选_intervals": [config.label_to_interval(p) for p in market_valid],
        "分类": config.CAT_VALID if market_valid else config.CAT_EMPTY,
        "分类理由": f"市场有效候选数={len(market_valid)}",
        "市场有效候选数": len(market_valid),
    }
    entries["市场前三"] = me
    return entries


# ========== 单组指标计算 ==========

def compute_group_metrics(entries_list, group_name):
    """对一组 entries（已结算场次）计算完整指标。

    不复用 ttg_shadow.group_metrics——那个函数只算基础覆盖，
    新模块需要区间距离、开放区间标记、精确误差（仅闭区间对闭区间）等。
    """
    n_total = len(entries_list)
    if n_total == 0:
        return _empty_metrics(group_name)

    # 5 类分类计数
    cat_counts = {c: 0 for c in [
        config.CAT_VALID, config.CAT_EMPTY, config.CAT_ILLEGAL,
        config.CAT_RUN_FAILED, config.CAT_UNKNOWN,
    ]}
    valid_entries = []
    for e in entries_list:
        cat_counts[e["分类"]] += 1
        if e["分类"] == config.CAT_VALID:
            valid_entries.append(e)

    n_valid = len(valid_entries)
    n_empty = cat_counts[config.CAT_EMPTY]
    n_invalid = cat_counts[config.CAT_ILLEGAL]
    n_failed = cat_counts[config.CAT_RUN_FAILED]
    n_unknown = cat_counts[config.CAT_UNKNOWN]

    # 分母不变式断言（这里只是记录，真断言在测试里）
    invariant_ok = (n_total == n_valid + n_empty + n_invalid + n_failed + n_unknown)

    # 有序/无序
    ordered_entries = [e for e in valid_entries if e["有序"]]
    unordered_entries = [e for e in valid_entries if not e["有序"]]
    n_order = len(ordered_entries)
    n_unordered = len(unordered_entries)

    # 首选命中率（区间相交）
    first_hits = 0
    for e in ordered_entries:
        if e["首选_interval"] is None or e["实际_interval"] is None:
            continue
        if config.interval_distance(e["首选_interval"], e["实际_interval"]) == 0:
            first_hits += 1

    # 原始候选集合覆盖率（区间相交）
    raw_hits = 0
    for e in valid_entries:
        if e["实际_interval"] is None:
            continue
        for ci in e["候选_intervals"]:
            if ci is not None and config.interval_distance(ci, e["实际_interval"]) == 0:
                raw_hits += 1
                break

    # 等宽三档覆盖率（仅有序，窄不补齐，宽按原序截前 3）
    eq3_hits = 0
    for e in ordered_entries:
        if e["实际_interval"] is None:
            continue
        first3 = e["候选_intervals"][:3]  # 窄集合自然少，宽集合截前 3
        for ci in first3:
            if ci is not None and config.interval_distance(ci, e["实际_interval"]) == 0:
                eq3_hits += 1
                break

    # 首选区间距离（任何情况可算）
    first_distances = []
    first_dist_open_flags = []
    for e in ordered_entries:
        if e["首选_interval"] is None or e["实际_interval"] is None:
            continue
        d = config.interval_distance(e["首选_interval"], e["实际_interval"])
        if d is not None:
            first_distances.append(d)
            first_dist_open_flags.append(e["实际_is_open"])

    # 首选精确误差（仅双侧闭区间）
    first_exact_errors = []
    for e in ordered_entries:
        if e["首选_num"] is None or e["实际_num"] is None:
            continue
        if e["实际_is_open"] or config.is_open_interval(
                (config.label_to_interval(
                    config.TTG_LABELS[0])) if False else None):  # 占位，下面替代
            continue
        # 实际闭区间 → 可以算精确误差
        # 首选是 7+球 时首选_num=7.0（开放），不算精确误差
        if e["首选_interval"] is not None and e["首选_interval"][2]:
            continue  # 首选开放区间
        first_exact_errors.append(abs(e["首选_num"] - e["实际_num"]))

    # 候选集合最短区间距离
    set_distances = []
    for e in valid_entries:
        if e["实际_interval"] is None:
            continue
        ds = []
        for ci in e["候选_intervals"]:
            if ci is None:
                continue
            d = config.interval_distance(ci, e["实际_interval"])
            if d is not None:
                ds.append(d)
        if ds:
            set_distances.append(min(ds))

    # 候选平均数量
    avg_picks = (sum(len(e["候选"]) for e in valid_entries) / n_valid) if n_valid > 0 else None

    # 窄/宽集合
    narrow = sum(1 for e in valid_entries if len(e["候选"]) < 3)
    wide = sum(1 for e in valid_entries if len(e["候选"]) > 3)

    return {
        "组": group_name,
        "已结算场次数 N_settled": n_total,
        "有效预测场次 N_valid": n_valid,
        "有序预测场次 N_order": n_order,
        "无序预测场次 N_unordered": n_unordered,
        "空输出场次 N_empty": n_empty,
        "非法输出场次 N_invalid": n_invalid,
        "运行失败场次 N_failed": n_failed,
        "未知状态场次 N_unknown": n_unknown,
        "分母不变式成立": invariant_ok,
        "首选命中率": first_hits,
        "首选命中率_分母": n_order,
        "首选命中率_率": round(100.0 * first_hits / n_order, 2) if n_order else None,
        "原始候选集合覆盖率": raw_hits,
        "原始候选集合覆盖率_分母": n_valid,
        "原始候选集合覆盖率_率": round(100.0 * raw_hits / n_valid, 2) if n_valid else None,
        "等宽三档覆盖率": eq3_hits,
        "等宽三档覆盖率_分母": n_order,
        "等宽三档覆盖率_率": round(100.0 * eq3_hits / n_order, 2) if n_order else None,
        "首选区间距离均值": round(sum(first_distances) / len(first_distances), 2) if first_distances else None,
        "首选精确误差均值": round(sum(first_exact_errors) / len(first_exact_errors), 2) if first_exact_errors else None,
        "首选精确误差_分母": len(first_exact_errors),
        "候选集合最短区间距离均值": round(sum(set_distances) / len(set_distances), 2) if set_distances else None,
        "候选平均数量": round(avg_picks, 2) if avg_picks is not None else None,
        "窄集合数": narrow,
        "宽集合数": wide,
        "空输出率": round(100.0 * n_empty / n_total, 2) if n_total else None,
        "非法输出率": round(100.0 * n_invalid / n_total, 2) if n_total else None,
        "运行失败率": round(100.0 * n_failed / n_total, 2) if n_total else None,
    }


def _empty_metrics(group_name):
    return {
        "组": group_name,
        "已结算场次数 N_settled": 0,
        "有效预测场次 N_valid": 0,
        "有序预测场次 N_order": 0,
        "空输出率": None, "非法输出率": None, "运行失败率": None,
        "首选命中率_率": None, "原始候选集合覆盖率_率": None,
    }


# ========== A→B 配对分析 ==========

def compute_ab_pair_analysis(records):
    """对已结算场次的 A→B 三个配对群体分别计算。

    返回: {
      "p1_stats": {...},  # 首选误差配对
      "p2_stats": {...},  # 集合距离配对
      "p3_stats": {...},  # 覆盖变化配对
      "classifications": [list of (runkey, class_id, class_name)],
      "per_runkey_details": [逐场详情],
    }
    """
    settled = [r for r in records if (r.get("实际赛果") or {}).get("总进球")]

    p1_list = []  # 首选误差配对
    p2_list = []  # 集合距离配对
    p3_list = []  # 覆盖变化配对
    classifications = []
    per_runkey_details = []

    for rec in settled:
        entries = _entries_for_runkey(rec)
        ae = entries["A盲判"]
        be = entries["B复核"]
        actual = (rec.get("实际赛果") or {}).get("总进球")

        runkey = extract_runkey(rec)
        detail = {"runkey": runkey[:12], "编号": (rec.get("对齐键") or {}).get("编号"),
                  "实际": actual}

        # === P1 首选误差配对 ===
        if (ae["分类"] == config.CAT_VALID and ae["有序"] and ae["候选"]
                and be["分类"] == config.CAT_VALID and be["有序"] and be["候选"]):
            # 首选区间距离改善
            if (ae["首选_interval"] is not None and ae["实际_interval"] is not None
                    and be["首选_interval"] is not None):
                a_first_dist = config.interval_distance(ae["首选_interval"], ae["实际_interval"])
                b_first_dist = config.interval_distance(be["首选_interval"], ae["实际_interval"])
                if a_first_dist is not None and b_first_dist is not None:
                    improve = a_first_dist - b_first_dist  # + 表示 B 更好
                    detail["首选区间距离_A"] = round(a_first_dist, 2)
                    detail["首选区间距离_B"] = round(b_first_dist, 2)
                    detail["首选区间距离改善"] = round(improve, 2)

                    # 精确误差（仅双侧闭区间）
                    a_exact = b_exact = None
                    if (ae["首选_num"] is not None and ae["实际_num"] is not None
                            and not ae["实际_is_open"]
                            and (ae["首选_interval"] is None or not ae["首选_interval"][2])):
                        a_exact = abs(ae["首选_num"] - ae["实际_num"])
                    if (be["首选_num"] is not None and be["实际_num"] is not None
                            and not ae["实际_is_open"]  # 实际开放则两边都不算
                            and (be["首选_interval"] is None or not be["首选_interval"][2])):
                        b_exact = abs(be["首选_num"] - be["实际_num"])
                    detail["首选精确误差_A"] = round(a_exact, 2) if a_exact is not None else None
                    detail["首选精确误差_B"] = round(b_exact, 2) if b_exact is not None else None
                    if a_exact is not None and b_exact is not None:
                        detail["首选精确误差改善"] = round(a_exact - b_exact, 2)

                    # 方向判断
                    ideal = config.compute_ideal_direction(ae["首选_interval"], ae["实际_interval"])
                    b_move = config.compute_b_move_direction(ae["首选_num"], be["首选_num"])
                    detail["ideal_direction"] = ideal
                    detail["B_move_direction"] = b_move
                    detail["方向判定"] = _direction_label(ideal, b_move)
                    detail["首选开放区间_实际"] = ae["实际_is_open"]

                    p1_list.append({
                        "runkey": runkey,
                        "improve_interval": improve,
                        "improve_exact": (a_exact - b_exact) if (a_exact is not None and b_exact is not None) else None,
                        "ideal": ideal,
                        "b_move": b_move,
                        "actual_is_open": ae["实际_is_open"],
                    })

        # === P2 集合距离配对 ===
        if (ae["分类"] == config.CAT_VALID and ae["候选"]
                and be["分类"] == config.CAT_VALID and be["候选"]):
            def _min_dist(e):
                if e["实际_interval"] is None:
                    return None
                ds = [config.interval_distance(ci, e["实际_interval"])
                      for ci in e["候选_intervals"] if ci is not None]
                return min(ds) if ds else None
            a_min = _min_dist(ae)
            b_min = _min_dist(be)
            if a_min is not None and b_min is not None:
                set_improve = a_min - b_min
                detail["集合最短区间距离_A"] = round(a_min, 2)
                detail["集合最短区间距离_B"] = round(b_min, 2)
                detail["集合最短区间距离改善"] = round(set_improve, 2)
                p2_list.append({"runkey": runkey, "improve": set_improve})

        # === P3 覆盖变化配对 ===
        def _covered(e):
            if e["实际_interval"] is None:
                return None
            for ci in e["候选_intervals"]:
                if ci is not None and config.interval_distance(ci, e["实际_interval"]) == 0:
                    return True
            return False

        if (ae["分类"] == config.CAT_VALID and ae["候选"]
                and be["分类"] == config.CAT_VALID and be["候选"]):
            a_cover = _covered(ae)
            b_cover = _covered(be)
            detail["A覆盖"] = a_cover
            detail["B覆盖"] = b_cover
            if a_cover is not None and b_cover is not None:
                if not a_cover and b_cover:
                    detail["覆盖变化"] = "补全"
                elif a_cover and not b_cover:
                    detail["覆盖变化"] = "丧失"
                else:
                    detail["覆盖变化"] = "不变"
                p3_list.append({
                    "runkey": runkey, "a_cover": a_cover, "b_cover": b_cover,
                })

        # === A→B 分类 ===
        cls_id, cls_name = config.classify_ab_change(ae["候选"], be["候选"])
        classifications.append((runkey[:12], cls_id, cls_name))
        detail["AB分类"] = cls_id
        detail["AB分类名"] = cls_name

        per_runkey_details.append(detail)

    # === P1 汇总 ===
    p1_stats = _summarize_p1(p1_list)
    p2_stats = _summarize_p2(p2_list)
    p3_stats = _summarize_p3(p3_list)

    return {
        "p1_stats": p1_stats,
        "p2_stats": p2_stats,
        "p3_stats": p3_stats,
        "classifications": classifications,
        "per_runkey_details": per_runkey_details,
    }


def _direction_label(ideal, b_move):
    if ideal is None:
        return "不可判定（缺失数据）"
    if ideal == 0:
        return "方向不可判定（A首选已覆盖实际）"
    if b_move is None:
        return "方向不可判定（B首选缺失）"
    if b_move == 0:
        return "B 未移动"
    if ideal == b_move:
        return "方向正确移动"
    return "方向错误移动"


def _summarize_p1(p1_list):
    n = len(p1_list)
    if n == 0:
        return {"配对数": 0}

    improvements = [x["improve_interval"] for x in p1_list]
    exact_improvements = [x["improve_exact"] for x in p1_list if x["improve_exact"] is not None]

    ideal_nonzero = [x for x in p1_list if x["ideal"] not in (0, None)]
    dir_correct = sum(1 for x in ideal_nonzero if x["ideal"] == x["b_move"])
    dir_wrong = sum(1 for x in ideal_nonzero if x["ideal"] != x["b_move"])
    dir_undecidable = sum(1 for x in p1_list if x["ideal"] == 0)

    return {
        "配对数": n,
        "首选区间距离改善均值": round(sum(improvements) / n, 2),
        "首选区间距离改善为正": sum(1 for x in improvements if x > 0),
        "首选区间距离改善为负": sum(1 for x in improvements if x < 0),
        "首选区间距离改善为零": sum(1 for x in improvements if x == 0),
        "首选精确误差配对数": len(exact_improvements),
        "首选精确误差改善均值": round(sum(exact_improvements) / len(exact_improvements), 2) if exact_improvements else None,
        "方向理想≠0的场次": len(ideal_nonzero),
        "方向正确移动": dir_correct,
        "方向错误移动": dir_wrong,
        "方向不可判定(A已覆盖)": dir_undecidable,
    }


def _summarize_p2(p2_list):
    n = len(p2_list)
    if n == 0:
        return {"配对数": 0}
    improves = [x["improve"] for x in p2_list]
    return {
        "配对数": n,
        "集合最短区间距离改善均值": round(sum(improves) / n, 2),
        "改善为正": sum(1 for x in improves if x > 0),
        "改善为负": sum(1 for x in improves if x < 0),
        "改善为零": sum(1 for x in improves if x == 0),
    }


def _summarize_p3(p3_list):
    n = len(p3_list)
    if n == 0:
        return {"配对数": 0}
    a_not_cover = [x for x in p3_list if not x["a_cover"]]
    a_cover = [x for x in p3_list if x["a_cover"]]
    b_补全 = sum(1 for x in a_not_cover if x["b_cover"])
    b_丧失 = sum(1 for x in a_cover if not x["b_cover"])
    return {
        "配对数": n,
        "A覆盖场次数": len(a_cover),
        "A未覆盖场次数": len(a_not_cover),
        "B补全覆盖(A未→B已)": b_补全,
        "B丧失覆盖(A已→B未)": b_丧失,
        "补全率_分母A未覆盖": len(a_not_cover),
        "补全率": round(100.0 * b_补全 / len(a_not_cover), 2) if a_not_cover else None,
        "丧失率_分母A覆盖": len(a_cover),
        "丧失率": round(100.0 * b_丧失 / len(a_cover), 2) if a_cover else None,
    }


# ========== 极端进球分桶 ==========

def compute_extreme_buckets(records):
    """6 个极端桶 × 每组统计。"""
    settled = [r for r in records if (r.get("实际赛果") or {}).get("总进球")]
    buckets = {bid: [] for bid, _, _ in config.EXTREME_BUCKETS}

    for rec in settled:
        actual_label = (rec.get("实际赛果") or {}).get("总进球")
        bid = config.bucket_for_actual(actual_label)
        if bid:
            buckets[bid].append(rec)

    result = {}
    for bid, bname, _ in config.EXTREME_BUCKETS:
        recs_in_bucket = buckets[bid]
        bucket_result = {"桶ID": bid, "桶名": bname, "场次数": len(recs_in_bucket)}
        if len(recs_in_bucket) <= 5 and len(recs_in_bucket) > 0:
            bucket_result["备注"] = "观察性，样本不足"

        # 每组统计：覆盖数/覆盖率 + 首选命中率 + 首选区间距离均值
        for g in ("A盲判", "B复核", "C正式", "D固定", "市场前三"):
            n = len(recs_in_bucket)
            if n == 0:
                bucket_result[f"{g}_覆盖数"] = 0
                bucket_result[f"{g}_覆盖率"] = None
                bucket_result[f"{g}_首选命中数"] = 0
                bucket_result[f"{g}_首选命中率"] = None
                bucket_result[f"{g}_首选区间距离均值"] = None
                continue

            hits = 0           # 原始候选集合覆盖数
            first_hits = 0     # 首选命中数
            first_dists = []   # 首选区间距离列表
            n_order_in_bucket = 0

            for rec in recs_in_bucket:
                entries = _entries_for_runkey(rec)
                e = entries[g]
                if e["实际_interval"] is None:
                    continue

                # 原始候选集合覆盖（任何候选区间与实际相交）
                for ci in e["候选_intervals"]:
                    if ci is not None and config.interval_distance(ci, e["实际_interval"]) == 0:
                        hits += 1
                        break

                # 首选命中率 + 首选区间距离（仅有序且首选非空）
                if e["有序"] and e["首选_interval"] is not None:
                    n_order_in_bucket += 1
                    d = config.interval_distance(e["首选_interval"], e["实际_interval"])
                    first_dists.append(d)
                    if d == 0:
                        first_hits += 1

            bucket_result[f"{g}_覆盖数"] = hits
            bucket_result[f"{g}_覆盖率"] = round(100.0 * hits / n, 2)
            bucket_result[f"{g}_首选命中数"] = first_hits
            bucket_result[f"{g}_首选命中率"] = round(100.0 * first_hits / n_order_in_bucket, 2) if n_order_in_bucket else None
            bucket_result[f"{g}_首选区间距离均值"] = round(sum(first_dists) / len(first_dists), 2) if first_dists else None
            bucket_result[f"{g}_首选命中率_分母"] = n_order_in_bucket
        result[bid] = bucket_result
    return result


# ========== D 组与市场四格表 ==========

def compute_d_market_table(records):
    """D 组 × 市场前三 四格表。"""
    settled = [r for r in records if (r.get("实际赛果") or {}).get("总进球")]
    a = b = c = d = 0
    for rec in settled:
        entries = _entries_for_runkey(rec)
        de = entries["D固定"]
        me = entries["市场前三"]
        actual_interval = de["实际_interval"]
        if actual_interval is None:
            continue

        def _covers(e):
            for ci in e["候选_intervals"]:
                if ci is not None and config.interval_distance(ci, actual_interval) == 0:
                    return True
            return False

        d_cov = _covers(de)
        m_cov = _covers(me)
        if d_cov and m_cov:
            a += 1
        elif d_cov and not m_cov:
            b += 1
        elif not d_cov and m_cov:
            c += 1
        else:
            d += 1

    n = a + b + c + d
    return {
        "a_D和市场都覆盖": a,
        "b_D覆盖市场未覆盖": b,
        "c_D未覆盖市场覆盖": c,
        "d_D和市场都未覆盖": d,
        "N": n,
        "D覆盖率": round(100.0 * (a + b) / n, 2) if n else None,
        "市场覆盖率": round(100.0 * (a + c) / n, 2) if n else None,
        "D相对市场覆盖率差": round(100.0 * (b - c) / n, 2) if n else None,
        "注意": "仅描述同一批比赛上的覆盖差异，不得认定 D 组预测能力更强",
    }


# ========== 多玩法分类指标（让球/半全场扩展；既有总进球函数不变） ==========

def _canon_fn_for_play(play):
    """按玩法取标签规范化函数（委托 validate_shadow 的统一入口）。"""
    from .validate_shadow import _canon_for_play
    return _canon_for_play(play)


def _play_entry(record, play, group, canon):
    """从 record 提取某玩法某组的 entry dict（分类指标口径）。"""
    g = record.get(group) or {}
    picks = list(g.get("候选", []) or [])
    cat, cat_reason = config.classify_output_general(g, canon_fn=canon)
    return {
        "候选": picks,
        "有序": bool(g.get("有序", True)),
        "状态": g.get("状态", "ok"),
        "分类": cat,
        "分类理由": cat_reason,
        "实际": None,
    }


def compute_play_group_metrics(entries_list, group_name, labels):
    """方向/组合类玩法的单组指标（让球=3 方向，半全场=9 组合）。

    分母口径与总进球一致：
      N_settled = N_valid + N_empty + N_invalid + N_failed + N_unknown
      首选命中率/等宽3覆盖率 分母 = 有序且有效场次 N_order
      原始候选集合覆盖率 分母 = 有效场次 N_valid
    """
    n_total = len(entries_list)
    cat_counts = {c: 0 for c in [
        config.CAT_VALID, config.CAT_EMPTY, config.CAT_ILLEGAL,
        config.CAT_RUN_FAILED, config.CAT_UNKNOWN,
    ]}
    valid_entries = []
    for e in entries_list:
        cat_counts[e["分类"]] += 1
        if e["分类"] == config.CAT_VALID:
            valid_entries.append(e)

    n_valid = len(valid_entries)
    n_empty = cat_counts[config.CAT_EMPTY]
    n_invalid = cat_counts[config.CAT_ILLEGAL]
    n_failed = cat_counts[config.CAT_RUN_FAILED]
    n_unknown = cat_counts[config.CAT_UNKNOWN]
    invariant_ok = (n_total == n_valid + n_empty + n_invalid + n_failed + n_unknown)

    ordered_entries = [e for e in valid_entries if e["有序"]]
    n_order = len(ordered_entries)

    first_hits = sum(1 for e in ordered_entries
                     if e["实际"] is not None and e["候选"] and e["候选"][0] == e["实际"])
    raw_hits = sum(1 for e in valid_entries
                   if e["实际"] is not None and e["实际"] in e["候选"])
    eq3_hits = sum(1 for e in ordered_entries
                   if e["实际"] is not None and e["实际"] in e["候选"][:3])

    first_dist = {lab: sum(1 for e in ordered_entries
                           if e["候选"] and e["候选"][0] == lab) for lab in labels}
    pick_dist = {lab: sum(1 for e in valid_entries if lab in e["候选"]) for lab in labels}
    avg_picks = (sum(len(e["候选"]) for e in valid_entries) / n_valid) if n_valid else None

    def _pct(a, b):
        return round(100.0 * a / b, 2) if b else None

    return {
        "组": group_name,
        "已结算场次数 N_settled": n_total,
        "有效预测场次 N_valid": n_valid,
        "有序预测场次 N_order": n_order,
        "空输出场次 N_empty": n_empty,
        "非法输出场次 N_invalid": n_invalid,
        "运行失败场次 N_failed": n_failed,
        "未知状态场次 N_unknown": n_unknown,
        "分母不变式成立": invariant_ok,
        "首选命中率": first_hits,
        "首选命中率_分母": n_order,
        "首选命中率_率": _pct(first_hits, n_order),
        "原始候选集合覆盖率": raw_hits,
        "原始候选集合覆盖率_分母": n_valid,
        "原始候选集合覆盖率_率": _pct(raw_hits, n_valid),
        "等宽三档覆盖率": eq3_hits,
        "等宽三档覆盖率_分母": n_order,
        "等宽三档覆盖率_率": _pct(eq3_hits, n_order),
        "候选平均数量": round(avg_picks, 2) if avg_picks is not None else None,
        "首选标签分布": first_dist,
        "候选标签分布": pick_dist,
        "空输出率": _pct(n_empty, n_total),
        "非法输出率": _pct(n_invalid, n_total),
        "运行失败率": _pct(n_failed, n_total),
    }


def compute_play_ab_pair(records, play):
    """让球/半全场的 A→B 复核价值配对（首选命中四格 + 覆盖变化 + AB 分类）。

    复核首选改善率 = (A错B对 − A对B错) / 配对数；覆盖变化与总进球 P3 同口径。
    """
    pcfg = config.PLAY_CONFIG[play]
    actual_key = pcfg["实际键"]
    canon = _canon_fn_for_play(play)
    settled = [r for r in records if (r.get("实际赛果") or {}).get(actual_key)]

    n_pair = 0
    both_hit = a_only = b_only = neither = 0
    cover_补全 = cover_丧失 = cover_不变 = 0
    classifications = []
    per_runkey_details = []

    for rec in settled:
        actual = (rec.get("实际赛果") or {}).get(actual_key)
        ae = _play_entry(rec, play, "A盲判", canon)
        be = _play_entry(rec, play, "B复核", canon)
        ae["实际"] = be["实际"] = actual
        runkey = extract_runkey(rec)
        detail = {"runkey": runkey[:12],
                  "编号": (rec.get("对齐键") or {}).get("编号"),
                  "实际": actual}

        if (ae["分类"] == config.CAT_VALID and ae["候选"]
                and be["分类"] == config.CAT_VALID and be["候选"]):
            n_pair += 1
            a_first_hit = bool(ae["有序"]) and ae["候选"][0] == actual
            b_first_hit = bool(be["有序"]) and be["候选"][0] == actual
            if a_first_hit and b_first_hit:
                both_hit += 1
            elif a_first_hit:
                a_only += 1
            elif b_first_hit:
                b_only += 1
            else:
                neither += 1
            detail["A首选命中"] = a_first_hit
            detail["B首选命中"] = b_first_hit

            a_cover = actual in ae["候选"]
            b_cover = actual in be["候选"]
            detail["A覆盖"] = a_cover
            detail["B覆盖"] = b_cover
            if not a_cover and b_cover:
                cover_补全 += 1
                detail["覆盖变化"] = "补全"
            elif a_cover and not b_cover:
                cover_丧失 += 1
                detail["覆盖变化"] = "丧失"
            else:
                cover_不变 += 1
                detail["覆盖变化"] = "不变"

        cls_id, cls_name = config.classify_ab_change(ae["候选"], be["候选"])
        classifications.append((runkey[:12], cls_id, cls_name))
        detail["AB分类"] = cls_id
        detail["AB分类名"] = cls_name
        per_runkey_details.append(detail)

    return {
        "配对数": n_pair,
        "首选命中四格": {
            "A对B对": both_hit, "A对B错": a_only,
            "A错B对": b_only, "A错B错": neither,
        },
        "复核首选改善率": (round(100.0 * (b_only - a_only) / n_pair, 2)
                           if n_pair else None),
        "覆盖补全(A未→B已)": cover_补全,
        "覆盖丧失(A已→B未)": cover_丧失,
        "覆盖不变": cover_不变,
        "classifications": classifications,
        "per_runkey_details": per_runkey_details,
    }


def run_evaluate_play(play, output_dir="output"):
    """让球/半全场统计主入口：加载 → 五类分类 → 分类指标 → A→B 配对。"""
    from .validate_shadow import load_play_shadow_files

    pcfg = config.PLAY_CONFIG[play]
    actual_key = pcfg["实际键"]
    labels = pcfg["标签表"]
    groups = pcfg["组"]
    market_key = pcfg["市场键"]
    canon = _canon_fn_for_play(play)

    loaded = load_play_shadow_files(play, output_dir)
    records = loaded["records"]
    settled = [r for r in records if (r.get("实际赛果") or {}).get(actual_key)]

    group_entries = {g: [] for g in groups}
    market_entries = []
    for rec in settled:
        actual = (rec.get("实际赛果") or {}).get(actual_key)
        for g in groups:
            e = _play_entry(rec, play, g, canon)
            e["实际"] = actual
            group_entries[g].append(e)
        # 市场组按同一五分类框架统计（候选来自赔率排序，永远有序）
        mk = rec.get(market_key)
        m_picks = list((mk.get("候选") if isinstance(mk, dict) else mk) or [])
        m_picks = [p for p in m_picks if canon(p) is not None]
        market_entries.append({
            "候选": m_picks, "有序": True,
            "状态": "ok" if m_picks else "empty",
            "分类": config.CAT_VALID if m_picks else config.CAT_EMPTY,
            "分类理由": f"市场有效候选数={len(m_picks)}",
            "实际": actual,
        })

    group_metrics = {}
    for g in groups:
        group_metrics[g] = compute_play_group_metrics(group_entries[g], g, labels)
    group_metrics[market_key] = compute_play_group_metrics(market_entries, market_key, labels)

    ab_result = compute_play_ab_pair(records, play)
    phase_num, phase_desc = config.phase_for_n(len(settled))

    return {
        "玩法": play,
        "schema_version": config.SCHEMA_VERSION,
        "module_version": config.MODULE_VERSION,
        "phase_num": phase_num,
        "phase_desc": phase_desc,
        "total_records": len(records),
        "settled_records": len(settled),
        "pending_records": len(records) - len(settled),
        "group_metrics": group_metrics,
        "ab_pair": ab_result,
        "quality_report": loaded["quality_report"],
        "runkey_conflicts": loaded["runkey_conflicts"],
        "matchkey_conflicts": loaded["matchkey_conflicts"],
        "warnings": loaded["warnings"],
    }


# ========== C2 独立判断对照 ==========

def compute_c2_comparison(records):
    """C2独立 vs C正式：仅统计 C2 已启用且双方均有效的已结算场次。

    C2 与 C 共用同一场次、同一赔率快照，差异只来自提示词，
    因此这组配对是判断“独立判断版提示词是否改变选球行为”的唯一干净证据。
    """
    enabled = 0
    pairs = []
    for rec in records:
        actual = (rec.get("实际赛果") or {})
        actual_label = actual.get("总进球")
        if not actual_label:
            continue
        c2 = rec.get(config.TTG_C2_GROUP)
        if not isinstance(c2, dict) or c2.get("状态") in (None, "", "disabled"):
            continue
        enabled += 1
        if c2.get("状态") != "ok":
            continue
        c = rec.get("C正式") or {}
        if c.get("状态") != "ok":
            continue
        c_picks = [p for p in (c.get("候选") or []) if _canon(p) is not None]
        c2_picks = [p for p in (c2.get("候选") or []) if _canon(p) is not None]
        if not c_picks or not c2_picks:
            continue
        pairs.append({"实际": actual_label, "C": c_picks, "C2": c2_picks})

    n = len(pairs)
    if n == 0:
        return {
            "启用场次数": enabled,
            "配对数": 0,
            "说明": "暂无 C2 已启用且双方均有效的已结算场次",
        }

    def _hit(key):
        return round(sum(1 for p in pairs if p[key][0] == p["实际"]) / n * 100, 2)

    def _cover(key):
        return round(sum(1 for p in pairs if p["实际"] in p[key]) / n * 100, 2)

    def _avg_len(key):
        return round(sum(len(p[key]) for p in pairs) / n, 2)

    def _dist(key):
        cnt = {}
        for p in pairs:
            top = p[key][0]
            cnt[top] = cnt.get(top, 0) + 1
        return {k: round(v / n * 100, 2) for k, v in sorted(cnt.items())}

    return {
        "启用场次数": enabled,
        "配对数": n,
        "C首选命中率": _hit("C"),
        "C2首选命中率": _hit("C2"),
        "C覆盖率": _cover("C"),
        "C2覆盖率": _cover("C2"),
        "C候选数均值": _avg_len("C"),
        "C2候选数均值": _avg_len("C2"),
        "C首选分布": _dist("C"),
        "C2首选分布": _dist("C2"),
        "C首选未进入C2候选的场次": sum(
            1 for p in pairs if p["C"][0] not in p["C2"]
        ),
    }


# ========== 入口 ==========

def run_evaluate(output_dir="output"):
    """主入口：加载所有影子文件 → 计算 → 返回完整结果字典。"""
    loaded = load_all_shadow_files(output_dir)
    records = loaded["records"]
    settled = [r for r in records if (r.get("实际赛果") or {}).get("总进球")]

    # 每组指标（已结算场次）
    group_entries = {g: [] for g in ("A盲判", "B复核", "C正式", "D固定", "市场前三")}
    for rec in settled:
        entries = _entries_for_runkey(rec)
        for g in group_entries:
            group_entries[g].append(entries[g])

    group_metrics = {}
    for g in group_entries:
        group_metrics[g] = compute_group_metrics(group_entries[g], g)

    # A→B 配对
    ab_result = compute_ab_pair_analysis(records)

    # 极端桶
    extreme = compute_extreme_buckets(records)

    # D × 市场四格表
    d_market = compute_d_market_table(records)

    # 阶段
    phase_num, phase_desc = config.phase_for_n(len(settled))

    return {
        "schema_version": config.SCHEMA_VERSION,
        "module_version": config.MODULE_VERSION,
        "phase_num": phase_num,
        "phase_desc": phase_desc,
        "total_records": len(records),
        "settled_records": len(settled),
        "pending_records": len(records) - len(settled),
        "group_metrics": group_metrics,
        "ab_pair": ab_result,
        "extreme_buckets": extreme,
        "d_market_table": d_market,
        "c2_comparison": compute_c2_comparison(records),
        "quality_report": loaded["quality_report"],
        "runkey_conflicts": loaded["runkey_conflicts"],
        "matchkey_conflicts": loaded["matchkey_conflicts"],
        "warnings": loaded["warnings"],
    }
