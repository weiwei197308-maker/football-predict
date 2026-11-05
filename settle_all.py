"""settle_all.py — M1（竞彩分析，带赔率）/ M2（独立分析，无赔率）同场对照结算。

竞彩口径五玩法命中判定，复用 lottery_rules 纯函数（与 review.py 同源）：
  胜平负 / 让球胜平负（让球数调整后）/ 总进球 0-7+ 分桶 / 半全场 HT-FT / 比分精确。
多候选推荐取「首推命中」与「任一候选命中」两种口径。

输入（--output-dir 默认 output）：
  竞彩分析_{date}.csv（M1，可缺）、独立分析_{date}.csv（M2，可缺）、
  赛果_{date}.json（必需，由 get_results_api.py 生成）、
  竞彩足球_{date}.csv（必需，取 编号→matchId 映射、让球数与对阵）
输出：
  控制台汇总 + 同场配对对照 + {output-dir}/对照报告_{date}.md
全程离线，无任何网络请求。

用法：python settle_all.py [--date YYYY-MM-DD] [--output-dir output]，默认今天。
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import date

from lottery_rules import (
    get_result_label, cn_to_code, code_to_cn, parse_handicap,
    compute_handicap_result, parse_hafu_picks_strict,
    parse_score as rules_parse_score, parse_total_goals_list,
    extract_handicap_direction, total_goals_label,
)

PLAYS = (("spf", "胜平负"), ("rq", "让球胜平负"), ("ttg", "总进球"),
         ("hafu", "半全场"), ("crs", "比分"))


# ---------------- 候选解析（首推 = 列表第 1 项，与 analyze 的 " / " 保序一致） ----------------

def spf_candidates(pred):
    codes = [c for c in (cn_to_code(p) for p in
                         re.split(r"[/,，、\s]+", str(pred or "")) if p.strip())
             if c]
    return codes or None


def rq_candidates(row):
    """让球推荐为首选，「让球次选」列（若存在）为次候选。"""
    cands = []
    for col in ("让球推荐", "让球次选"):
        d = extract_handicap_direction(str(row.get(col, "") or ""))
        if d and d not in cands:
            cands.append(d)
    return cands or None


def ttg_candidates(pred):
    return parse_total_goals_list(pred) or None


def hafu_candidates(pred):
    """与 review.check_hafu 同口径：含非法编码整条无效（返回 None）。"""
    codes, invalids = parse_hafu_picks_strict(pred)
    if not codes or invalids:
        return None
    return codes


def crs_candidates(pred):
    pairs = []
    for part in re.split(r"[/,，、\s]+", str(pred or "")):
        pair = rules_parse_score(part.strip())
        if pair and pair not in pairs:
            pairs.append(pair)
    return pairs or None


# ---------------- 实际赛果（竞彩口径） ----------------

def actuals(res, handicap_str):
    """返回 {play: 实际值}；让球无法解析时 rq 为 None（不可判定，不进分母）。"""
    fh, fa = res["ft_home"], res["ft_away"]
    hc = parse_handicap(handicap_str)
    half_code = None
    if res.get("ht_home") is not None and res.get("ht_away") is not None:
        half_code = cn_to_code(get_result_label(res["ht_home"], res["ht_away"]))
    return {
        "spf": cn_to_code(get_result_label(fh, fa)),
        "rq": compute_handicap_result(fh, fa, hc) if hc is not None else None,
        "ttg": 7 if fh + fa >= 7 else fh + fa,
        "hafu": f"{half_code}{cn_to_code(get_result_label(fh, fa))}" if half_code else None,
        "crs": (fh, fa),
    }


ACTUAL_TEXT = {
    "spf": lambda v: code_to_cn(v),
    "rq": lambda v: v,
    "ttg": lambda v: f"{total_goals_label(v)}球",
    "hafu": lambda v: v,
    "crs": lambda v: f"{v[0]}:{v[1]}",
}


def settle_row(row, act):
    """单场五玩法判定 → ({play: (first_hit, any_hit)}, {play: 候选}, {play: 原文})。

    无有效预测/不可判定为 (None, None)。
    """
    rq_raw = str(row.get("让球推荐", "") or "").strip()
    rq2 = str(row.get("让球次选", "") or "").strip()
    if rq2:
        rq_raw = f"{rq_raw} / {rq2}" if rq_raw else rq2
    preds = {
        "spf": spf_candidates(row.get("胜平负推荐")),
        "rq": rq_candidates(row),
        "ttg": ttg_candidates(row.get("总进球推荐")),
        "hafu": hafu_candidates(row.get("半全场推荐")),
        "crs": crs_candidates(row.get("比分推荐")),
    }
    raws = {
        "spf": str(row.get("胜平负推荐", "") or "").strip(),
        "rq": rq_raw,
        "ttg": str(row.get("总进球推荐", "") or "").strip(),
        "hafu": str(row.get("半全场推荐", "") or "").strip(),
        "crs": str(row.get("比分推荐", "") or "").strip(),
    }
    out = {}
    for play, _ in PLAYS:
        cands, actual = preds[play], act[play]
        if not cands or actual is None:
            out[play] = (None, None)
        else:
            out[play] = (cands[0] == actual, actual in cands)
    return out, preds, raws


# ---------------- 数据加载 ----------------

def load_schedule(path):
    """赛程 CSV → {编号: {matchId, 让球数, home, away}}。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    sched = {}
    for r in rows:
        num = str(r.get("编号", "") or "").strip()
        if num:
            sched[num] = {
                "matchId": str(r.get("matchId", "") or "").strip(),
                "让球数": str(r.get("让球数", "") or "").strip(),
                "home": r.get("主队", ""), "away": r.get("客队", ""),
            }
    return sched


def load_arm(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def pct(h, v):
    return f"{h*100//v}%" if v else "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--output-dir", default="output")
    args = ap.parse_args()
    od, d = args.output_dir, args.date

    results_path = os.path.join(od, f"赛果_{d}.json")
    sched_path = os.path.join(od, f"竞彩足球_{d}.csv")
    if not os.path.exists(results_path):
        sys.exit(f"错误：找不到 {results_path}，请先运行 get_results_api.py")
    if not os.path.exists(sched_path):
        sys.exit(f"错误：找不到 {sched_path}（需要 编号→matchId 映射与让球数）")

    with open(results_path, "r", encoding="utf-8") as f:
        results = json.load(f)
    sched = load_schedule(sched_path)

    arm_files = {"M1": os.path.join(od, f"竞彩分析_{d}.csv"),
                 "M2": os.path.join(od, f"独立分析_{d}.csv")}
    arms, notes = {}, []
    for arm, path in arm_files.items():
        if os.path.exists(path):
            arms[arm] = load_arm(path)
        else:
            notes.append(f"{arm} 文件缺失（{os.path.basename(path)}），仅结算存在的臂")

    if not arms:
        sys.exit("错误：M1/M2 分析 CSV 都不存在，无可结算内容")

    # 逐臂结算：settled[arm][编号] = {"hits": {play:(first,any)}, "preds":..., "res":...}
    settled, stats = {}, {}
    for arm, rows in arms.items():
        settled[arm] = {}
        stats[arm] = {f"{p}_{k}": 0 for p, _ in PLAYS
                      for k in ("first_hit", "first_valid", "any_hit", "any_valid")}
        skipped = 0
        for row in rows:
            num = str(row.get("编号", "") or "").strip()
            sc = sched.get(num)
            res = results.get(sc["matchId"]) if sc else None
            if not res:
                skipped += 1
                continue
            act = actuals(res, sc["让球数"])
            hits, preds, raws = settle_row(row, act)
            for play, _ in PLAYS:
                first, any_hit = hits[play]
                if first is not None:
                    stats[arm][f"{play}_first_valid"] += 1
                    stats[arm][f"{play}_any_valid"] += 1
                    stats[arm][f"{play}_first_hit"] += int(first)
                    stats[arm][f"{play}_any_hit"] += int(any_hit)
            settled[arm][num] = {"hits": hits, "raws": raws, "res": res,
                                 "act": act, "row": row, "sched": sc}
        if skipped:
            notes.append(f"{arm}：{skipped} 场无赛果或不在赛程中，已跳过")

    # 同场配对对照（首推口径，仅两臂均有有效预测的场次）
    common = sorted(set.intersection(*(set(s) for s in settled.values()))) \
        if len(settled) == 2 else []
    pair = {p: {"both": 0, "only_m1": 0, "only_m2": 0, "neither": 0, "skip": 0}
            for p, _ in PLAYS}
    for num in common:
        for play, _ in PLAYS:
            m1 = settled["M1"][num]["hits"][play][0]
            m2 = settled["M2"][num]["hits"][play][0]
            if m1 is None or m2 is None:
                pair[play]["skip"] += 1
            elif m1 and m2:
                pair[play]["both"] += 1
            elif m1:
                pair[play]["only_m1"] += 1
            elif m2:
                pair[play]["only_m2"] += 1
            else:
                pair[play]["neither"] += 1

    # ---------------- 报告 ----------------
    lines = [f"# 对照报告 {d}", ""]
    for n in notes:
        lines.append(f"> {n}")
    if notes:
        lines.append("")
    lines.append("## 每臂每玩法命中汇总")
    lines.append("")
    lines.append("| 臂 | 玩法 | 首推命中 | 首推命中率 | 任一候选命中 | 任一命中率 | 有效场次 |")
    lines.append("|---|---|---|---|---|---|---|")
    for arm in settled:
        for play, label in PLAYS:
            s = stats[arm]
            fh, fv = s[f"{play}_first_hit"], s[f"{play}_first_valid"]
            ah, av = s[f"{play}_any_hit"], s[f"{play}_any_valid"]
            lines.append(f"| {arm} | {label} | {fh}/{fv} | {pct(fh, fv)} | "
                         f"{ah}/{av} | {pct(ah, av)} | {fv} |")
        lines.append(f"| {arm} | 场次 | 共 **{len(settled[arm])}** 场结算 | | | | |")

    if common:
        lines += ["", "## 同场配对对照（首推口径，两臂均有有效预测）", "",
                  "| 玩法 | 双双命中 | 仅M1 | 仅M2 | 双双未中 | 无法比较 |",
                  "|---|---|---|---|---|---|"]
        for play, label in PLAYS:
            p = pair[play]
            lines.append(f"| {label} | {p['both']} | {p['only_m1']} | "
                         f"{p['only_m2']} | {p['neither']} | {p['skip']} |")

    lines += ["", "## 每场明细（首推口径）", ""]
    all_nums = sorted(set().union(*(set(s) for s in settled.values())))
    for num in all_nums:
        first_arm = next(a for a in settled if num in settled[a])
        info = settled[first_arm][num]
        res, sc = info["res"], info["sched"]
        ht = (f"半场 {res['ht_home']}:{res['ht_away']}，"
              if res.get("ht_home") is not None else "")
        lines.append(f"### {num} {res['home']} vs {res['away']}"
                     f"（{ht}全场 {res['ft_home']}:{res['ft_away']}，让球 {sc['让球数'] or '无'}）")
        lines.append("")
        lines.append("| 臂 | 玩法 | 预测 | 实际 | 首推 | 任一 |")
        lines.append("|---|---|---|---|---|---|")
        for arm in settled:
            if num not in settled[arm]:
                lines.append(f"| {arm} | - | 无此场 | - | - | - |")
                continue
            e = settled[arm][num]
            for play, label in PLAYS:
                pred_text = e["raws"][play] or "（无）"
                act_text = ACTUAL_TEXT[play](e["act"][play]) if e["act"][play] is not None else "不可判定"
                first, any_hit = e["hits"][play]
                mark = lambda h: "➖" if h is None else ("✅" if h else "❌")
                lines.append(f"| {arm} | {label} | {pred_text} | {act_text} | "
                             f"{mark(first)} | {mark(any_hit)} |")
        lines.append("")

    report = "\n".join(lines)
    print(report)
    out_path = os.path.join(od, f"对照报告_{d}.md")
    os.makedirs(od, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n💾 报告已保存：{out_path}")


if __name__ == "__main__":
    main()
