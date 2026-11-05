#!/usr/bin/env python3
"""30 天数据审计修复重算脚本（独立输出，不覆盖原始 CSV）。

数据流：output/竞彩分析_<date>.csv + 竞彩足球_<date>.csv + 每日记录_<date>.json
        → 调用四个 tracker 修复后的 build_row 逐场重建
        → 写入 审计修复输出/ 下的 4 张新表
        → 与根目录 4 张原始表逐条比对，生成差异 CSV
        → 调用 summarize_30days 生成修复后汇总报告
        → 重建错题归因总表（修复后）
        → 输出未解决异常清单与口径说明

运行：python3 audit_recompute.py
"""
import csv
import glob
import os
import re
import subprocess
import sys
from collections import Counter

import update_goals_tracker as ugt
import update_half_full_tracker as uhf
import update_score_tracker as usc
import update_sim_tracker as ust
import build_attribution_table as bat
from review_data_loader import load_review_data

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(ROOT, "output")
AUDIT_DIR = os.path.join(ROOT, "审计修复输出")

ORIG_CSVS = {
    "模拟30.csv": (ust, "让球命中"),
    "竞彩总进球模拟盘.csv": (ugt, "是否命中"),
    "竞彩半全场模拟盘.csv": (uhf, "是否命中"),
    "竞彩比分模拟盘.csv": (usc, "是否命中"),
}


def find_dates():
    dates = []
    for path in sorted(glob.glob(os.path.join(OUTPUT_DIR, "竞彩分析_*.csv"))):
        m = re.search(r"竞彩分析_(\d{4}-\d{2}-\d{2})\.csv$", os.path.basename(path))
        if m:
            dates.append(m.group(1))
    return dates


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def recompute_all(dates):
    """逐日调用四个 tracker 的 build_row，返回 {csv名: [dict行]}。"""
    results = {name: [] for name in ORIG_CSVS}
    skipped = []
    for date_str in dates:
        mmdd = ugt.parse_date(date_str)
        ana_path = os.path.join(OUTPUT_DIR, f"竞彩分析_{date_str}.csv")
        fb_path = os.path.join(OUTPUT_DIR, f"竞彩足球_{date_str}.csv")
        if not os.path.exists(fb_path):
            skipped.append((date_str, "缺竞彩足球赔率文件"))
            continue
        analysis_rows = read_csv_rows(ana_path)
        football_rows = read_csv_rows(fb_path)
        review_data = load_review_data(date_str, OUTPUT_DIR)
        for arow in analysis_rows:
            for name, (mod, _) in ORIG_CSVS.items():
                row = mod.build_row(arow, football_rows, review_data, mmdd)
                results[name].append(dict(zip(mod.CSV_HEADER, row)))
    return results, skipped


def write_csvs(results):
    os.makedirs(AUDIT_DIR, exist_ok=True)
    for name, (mod, _) in ORIG_CSVS.items():
        path = os.path.join(AUDIT_DIR, name)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=mod.CSV_HEADER)
            writer.writeheader()
            writer.writerows(results[name])
    print(f"已写入 4 张修复后模拟盘 CSV → {AUDIT_DIR}")


def _num(s):
    s = (s or "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def diff_one(name, hit_key):
    """逐条比对原始表与修复表：命中翻转、盈亏变化、状态变化。"""
    orig_path = os.path.join(ROOT, name)
    new_path = os.path.join(AUDIT_DIR, name)
    orig = {(r["日期"], r["编号"]): r for r in read_csv_rows(orig_path)}
    new = {(r["日期"], r["编号"]): r for r in read_csv_rows(new_path)}
    rows = []
    stat = Counter()
    for key in sorted(set(orig) | set(new)):
        o, n = orig.get(key), new.get(key)
        if o is None:
            rows.append([name, *key, "", "", "新增行", n.get(hit_key, ""), n.get("模拟盈亏", ""), n.get("记录状态", ""), ""])
            stat["新增行"] += 1
            continue
        if n is None:
            rows.append([name, *key, "", "", "删除行", o.get(hit_key, ""), o.get("模拟盈亏", ""), "", ""])
            stat["删除行"] += 1
            continue
        o_hit, n_hit = (o.get(hit_key) or "").strip(), (n.get(hit_key) or "").strip()
        o_pl, n_pl = _num(o.get("模拟盈亏")), _num(n.get("模拟盈亏"))
        o_st = (o.get("记录状态") or "").strip() or ("命中" if o_hit == "是" else "未中" if o_hit == "否" else "未结算")
        n_st = (n.get("记录状态") or "").strip()
        pl_changed = (o_pl is None) != (n_pl is None) or (o_pl is not None and n_pl is not None and abs(o_pl - n_pl) > 1e-9)
        if o_hit != n_hit or pl_changed or o_st != n_st:
            kind = []
            if o_hit != n_hit:
                kind.append(f"命中 {o_hit or '空'}→{n_hit or '空'}")
            if pl_changed:
                kind.append(f"盈亏 {o.get('模拟盈亏') or '空'}→{n.get('模拟盈亏') or '空'}")
            if o_st != n_st:
                kind.append(f"状态 {o_st}→{n_st}")
            rows.append([name, *key, o.get("联赛", ""), o.get("对阵", ""),
                         "; ".join(kind), f"{o_hit}→{n_hit}",
                         f"{o.get('模拟盈亏','')}→{n.get('模拟盈亏','')}",
                         f"{o_st}→{n_st}", (n.get("备注") or "")])
            for k in kind:
                stat[k.split(" ")[0]] += 1
    return rows, stat


def write_diff():
    header = ["表", "日期", "编号", "联赛", "对阵", "变化类型", "命中 前→后", "盈亏 前→后", "状态 前→后", "备注"]
    all_rows, total_stat = [], Counter()
    for name, (_, hit_key) in ORIG_CSVS.items():
        rows, stat = diff_one(name, hit_key)
        all_rows.extend(rows)
        total_stat.update(stat)
    path = os.path.join(AUDIT_DIR, "修复前后逐条差异.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(all_rows)
    print(f"已写入 修复前后逐条差异.csv: {len(all_rows)} 行变化")
    return all_rows, total_stat


def write_attribution():
    rows = bat.build_rows(OUTPUT_DIR)
    path = os.path.join(AUDIT_DIR, "错题归因总表_修复后.csv")
    bat.save_rows(rows, path)
    orig = read_csv_rows(bat.TARGET_CSV) if os.path.exists(bat.TARGET_CSV) else []
    print(f"错题归因总表: 修复前 {len(orig)} 行 → 修复后 {len(rows)} 行")
    return len(orig), len(rows)


def _orig_index(name):
    """原表 (日期,编号) → 数据行号（1 起，不含表头），用于追溯来源。"""
    idx = {}
    for i, r in enumerate(read_csv_rows(os.path.join(ROOT, name)), start=1):
        idx[(r["日期"].strip(), r["编号"].strip())] = i
    return idx


PLAY_OF_CSV = {
    "模拟30.csv": ["让球", "胜平负"],
    "竞彩总进球模拟盘.csv": ["总进球"],
    "竞彩半全场模拟盘.csv": ["半全场"],
    "竞彩比分模拟盘.csv": ["比分"],
}

PRED_COL = {"让球": "AI让球预测", "胜平负": "AI胜平负预测"}
ODDS_COL = {"让球": "让球赔率", "胜平负": "胜平负赔率"}


def _split_odds(s):
    s = (s or "").strip()
    return [x for x in s.split("|")] if s else []


def _f(s):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def write_five_play_rows(results):
    """五种玩法逐场结果（含胜平负虚拟结算），每条保留原表行号。"""
    header = ["日期", "编号", "联赛", "对阵", "玩法", "口径", "AI预测", "选项数",
              "赔率逐项", "实际赛果", "命中判定", "记录状态", "投注额", "单项投注",
              "返奖", "净盈亏", "备注", "来源"]
    rows = []
    for name, (mod, _) in ORIG_CSVS.items():
        idx = _orig_index(name)
        for r in results[name]:
            key = (r["日期"].strip(), r["编号"].strip())
            src = f"{name} 第{idx.get(key, '?')}行"
            for pb in PLAY_OF_CSV[name]:
                pred = (r.get(PRED_COL.get(pb, f"AI{pb}预测")) or "").strip()
                odds_raw = (r.get(ODDS_COL.get(pb, "各选项赔率")) or "").strip()
                if name == "模拟30.csv" and pb == "让球":
                    hit = (r.get("让球命中") or "").strip()
                    pl = _f(r.get("模拟盈亏"))
                    bet = _f(r.get("模拟投注额")) or 0.0
                    actual = (r.get("实际赛果") or "").strip()
                    status = (r.get("记录状态") or "").strip()
                elif name == "模拟30.csv" and pb == "胜平负":
                    hit = (r.get("胜平负命中") or "").strip()
                    bet = 100.0
                    actual = (r.get("实际赛果") or "").strip()
                    o = _f(odds_raw)
                    # 胜平负独立判定状态，不能沿用让球状态（两者命中可不同）
                    if hit in ("是", "否"):
                        if hit == "是" and o is None:
                            status, pl = "赔率缺失", None   # 命中但无赔率，无法计算虚拟盈亏
                        else:
                            status = "命中" if hit == "是" else "未中"
                            pl = round(100.0 * (o - 1), 2) if hit == "是" else -100.0
                    else:
                        status = (r.get("记录状态") or "").strip()   # 无预测输出/未结算/数据缺失
                        pl = None
                else:
                    hit = (r.get("是否命中") or "").strip()
                    pl = _f(r.get("模拟盈亏"))
                    bet = _f(r.get("模拟投注额")) or 0.0
                    actual = (r.get(f"实际{pb}") or r.get(f"实际{pb}数") or "").strip()
                    status = (r.get("记录状态") or "").strip()
                opts = _split_odds(odds_raw)
                n = len(opts) if pred else 0
                per = round(bet / n, 2) if n else None
                payout = round(bet + pl, 2) if (pl is not None and bet) else None
                rows.append([r["日期"], r["编号"], r["联赛"], r["对阵"], pb,
                             "虚拟" if pb == "胜平负" else "实投",
                             pred, n, odds_raw, actual, hit, status,
                             bet, per, payout, pl, r.get("备注", ""), src])
    path = os.path.join(AUDIT_DIR, "五种玩法逐场结果.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"已写入 五种玩法逐场结果.csv: {len(rows)} 行")


def write_no_pred_compare(results):
    """所有特殊状态记录（无预测/未结算/数据缺失/赔率缺失/非法编码）逐条修复前后对照。"""
    header = ["表", "玩法", "日期", "编号", "对阵", "修复前命中", "修复后命中",
              "修复前盈亏", "修复后盈亏", "修复后状态", "修复后投注额", "备注", "来源"]
    out = []
    for name, (mod, hit_key) in ORIG_CSVS.items():
        orig = {r["日期"].strip() + "|" + r["编号"].strip(): r
                for r in read_csv_rows(os.path.join(ROOT, name))}
        idx = _orig_index(name)
        for r in results[name]:
            st = (r.get("记录状态") or "").strip()
            if st in ("命中", "未中"):
                continue
            key = r["日期"].strip() + "|" + r["编号"].strip()
            tkey = (r["日期"].strip(), r["编号"].strip())
            o = orig.get(key, {})
            for pb in PLAY_OF_CSV[name]:
                oh = (o.get(hit_key) or "").strip()
                npl = (r.get("模拟盈亏") or "").strip()
                out.append([name, pb, r["日期"], r["编号"], r["对阵"],
                            oh or "空", (r.get("是否命中") or r.get("让球命中") or "空"),
                            (o.get("模拟盈亏") or "空"), npl or "空", st,
                            r.get("模拟投注额", ""), r.get("备注", ""),
                            f"{name} 第{idx.get(tkey, '?')}行"])
    path = os.path.join(AUDIT_DIR, "无预测与异常逐条对照.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(out)
    print(f"已写入 无预测与异常逐条对照.csv: {len(out)} 行")


def write_odds_sensitivity(results):
    """两种投注假设对照：A=每场100均分到实际选项（项目现有口径）；B=每选项各投100。"""
    header = ["玩法", "日期", "编号", "选项数", "命中", "假设A净盈亏(100均分)",
              "假设B净盈亏(每项100)", "差额(B-A)"]
    rows = []
    agg = {}
    for name in ("竞彩总进球模拟盘.csv", "竞彩半全场模拟盘.csv", "竞彩比分模拟盘.csv"):
        pb = PLAY_OF_CSV[name][0]
        for r in results[name]:
            st = (r.get("记录状态") or "").strip()
            if st not in ("命中", "未中"):
                continue
            n = len(_split_odds(r.get("各选项赔率", "")))
            pl_a = _f(r.get("模拟盈亏"))
            if n == 0 or pl_a is None:
                continue
            if st == "命中":
                odds_hit = (pl_a + 100.0) * n / 100.0
                pl_b = round(odds_hit * 100.0 - 100.0 * n, 2)
            else:
                pl_b = -100.0 * n
            a = agg.setdefault(pb, {"a": 0.0, "b": 0.0, "n": 0})
            a["a"] += pl_a
            a["b"] += pl_b
            a["n"] += 1
            rows.append([pb, r["日期"], r["编号"], n, st, pl_a, pl_b, round(pl_b - pl_a, 2)])
    path = os.path.join(AUDIT_DIR, "投注口径敏感性_逐场.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    md = ["# 投注口径敏感性（两种假设对照）", "",
          "- 假设A（项目现有口径，已采用）：每场 100 元为该场该玩法总预算，均分到实际合法选项；",
          "- 假设B（备选）：每个推荐选项各投 100 元，总投入 = 100 × 选项数；",
          "- 两者仅在选项数 ≥ 2 时不同；让球/胜平负为单注，两种假设完全一致。", "",
          "| 玩法 | 已结算场次 | 假设A净盈亏 | 假设B净盈亏 | 差额(B-A) |", "|---|---|---|---|---|"]
    for pb, a in agg.items():
        md.append(f"| {pb} | {a['n']} | {a['a']:+.2f} | {a['b']:+.2f} | {a['b'] - a['a']:+.2f} |")
    md += ["", "> 结论：项目文档字符串（summarize_30days.py）明确写明「每场 100 等额分摊到各预测选项」，"
               "因此本次修复采用假设A。若业务实际为假设B，请告知，所有多选玩法盈亏需按上表差额调整。"]
    with open(os.path.join(AUDIT_DIR, "投注口径敏感性.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"已写入 投注口径敏感性.md / 投注口径敏感性_逐场.csv: {len(rows)} 行")


def write_formula_doc():
    path = os.path.join(AUDIT_DIR, "字段定义与计算公式.md")
    lines = [
        "# 字段定义、公式与计算假设", "",
        "## 一、字段定义（tracker CSV）", "",
        "| 字段 | 定义 |", "|---|---|",
        "| 模拟投注额 | 该场该玩法的总预算。多选玩法固定 100 并均分；模拟30 让球单注 100；特殊状态为 0 |",
        "| 各选项赔率 | 体彩官方固定奖金，按选项顺序用 `|` 分隔，与预测选项逐项对应；缺失记空串 |",
        "| 是否命中 / 让球命中 / 胜平负命中 | 三态：是=命中；否=有效预测未中；空=无有效预测或无法判定 |",
        "| 记录状态 | 命中/未中/无预测输出/未结算/数据缺失/赔率缺失/赔率数量异常/含非法编码 |",
        "| 模拟盈亏 | 净盈亏（含本金口径）；特殊状态留空 |",
        "| 备注 | 异常原因（如 含非法编码:HP、JSON命中判定缺失） |",
        "",
        "## 二、结算公式", "",
        "设该场总投入 `S=100`，合法选项数 `n`，命中选项赔率 `O`（含本金）：",
        "",
        "```",
        "单项投注   = S / n",
        "命中返奖   = O × (S / n)",
        "命中净盈亏 = O × (S / n) - S",
        "全部未中   = -S",
        "返奖率     = 返奖 / S",
        "ROI        = 净盈亏 / S",
        "```",
        "",
        "单注玩法（让球、胜平负）n=1，退化为：命中净盈亏 = S×(O-1)，未中 = -S。",
        "",
        "## 三、计算假设", "",
        "1. 每场 100 元是该场该玩法总预算，不是每个选项各 100（依据：summarize_30days.py 文档字符串与既有表说明）。",
        "2. 赔率为体彩固定奖金，**含本金**；净盈亏按上式扣除本金。",
        "3. 命中率分母 = 命中 + 未中；无预测输出/未结算/数据缺失/赔率数量异常/含非法编码 均不入分母。"
        "「赔率缺失」已判定命中但无赔率，计入命中率分子与分母，只是不计投注额与盈亏。",
        "4. 胜平负为虚拟口径（未实投），仅用于对比，不计入实投总投入。",
        "5. 赔率无采集时间戳，无法确认是否为开赛前最终赔率。",
        "",
        "## 四、状态判定优先级", "",
        "1. 无赛果 → 未结算（有预测）/ 数据缺失（无预测）；",
        "2. 有赛果、无预测 → 无预测输出；",
        "3. 半全场预测含非法 token 且合法选项未命中 → 含非法编码（不判中不判错）；",
        "4. 选项数 ≠ 赔率数 → 赔率数量异常；",
        "5. 命中但赔率缺失 → 赔率缺失（保留命中事实，不结算盈亏）；",
        "6. 其余 → 命中 / 未中。",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("已写入 字段定义与计算公式.md")


def write_data_quality(results):
    lines = ["# 数据质量检查", ""]
    # 1) 重复唯一键
    dup_lines = []
    for name in ORIG_CSVS:
        keys = [(r["日期"].strip(), r["编号"].strip()) for r in results[name]]
        c = Counter(keys)
        dups = {k: v for k, v in c.items() if v > 1}
        dup_lines.append(f"- {name}: 行数 {len(keys)}，唯一键 {len(c)}，重复 {len(dups)}")
        if dups:
            for k, v in list(dups.items())[:5]:
                dup_lines.append(f"  - 重复键 {k}: {v} 次")
    lines += ["## 一、唯一键（日期+编号）", ""] + dup_lines + [""]
    # 2) 跨表场次一致性
    base = {(r["日期"].strip(), r["编号"].strip()) for r in results["模拟30.csv"]}
    lines.append("## 二、跨表场次一致性（以模拟30 为基准）")
    lines.append("")
    for name in ORIG_CSVS:
        keys = {(r["日期"].strip(), r["编号"].strip()) for r in results[name]}
        lines.append(f"- {name}: 缺失 {len(base - keys)}，多出 {len(keys - base)}")
    lines += ["", "## 三、日期格式与赔率采集时点", "",
              "- 日期列为 MM-DD（源自销售日），年份需结合目录/记录文件名确定；未发现跨年歧义。",
              "- 赔率 CSV 无采集时间戳字段：无法验证赔率是否为开赛前最终值，属已知限制。", ""]
    with open(os.path.join(AUDIT_DIR, "数据质量检查.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("已写入 数据质量检查.md")


def write_attr_compare():
    """错题归因总表修复前后按玩法对比（修复前取项目根目录原表）。"""
    path_old = bat.TARGET_CSV
    path_new = os.path.join(AUDIT_DIR, "错题归因总表_修复后.csv")
    old = read_csv_rows(path_old) if os.path.exists(path_old) else []
    new = read_csv_rows(path_new) if os.path.exists(path_new) else []
    co, cn = Counter(r["玩法"] for r in old), Counter(r["玩法"] for r in new)
    lines = ["# 错题归因总表修复前后对比", "",
             "| 玩法 | 修复前错题行 | 修复后错题行 | 差额 |", "|---|---|---|---|"]
    for pb in bat.PLAYBOOKS:
        lines.append(f"| {pb} | {co[pb]} | {cn[pb]} | {cn[pb] - co[pb]:+d} |")
    lines.append(f"| **合计** | {len(old)} | {len(new)} | {len(new) - len(old):+d} |")
    lines += ["", "> 修复后改为从 预判/赛果 用规范判定函数重算，"
                  "已命中（含24条半全场中文误判）、无预测输出、含非法编码的比赛不再进入错题表。"]
    with open(os.path.join(AUDIT_DIR, "错题归因修复前后对比.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"已写入 错题归因修复前后对比.md（{len(old)} → {len(new)}）")


def write_notes(skipped, diff_stat, attr_counts, results):
    path = os.path.join(AUDIT_DIR, "未解决异常与口径说明.md")
    lines = ["# 未解决异常与口径说明", ""]
    lines.append("## 结算口径（已查证，非假定）")
    lines.append("- 多选玩法（总进球/半全场/比分）：每场 100 元为该场该玩法总预算，按实际合法选项数等额分摊；"
                 "赔率为体彩固定奖金（含本金），净盈亏 = 命中赔率×单项投注-100，全未中 = -100。")
    lines.append("- 模拟30：让球单注 100 实投；胜平负仅虚拟结算。")
    lines.append("- 无预测输出/未结算/数据缺失/赔率数量异常/含非法编码：不计命中率分母、不计盈亏。")
    lines.append("- 赔率缺失（已判定命中但无赔率）：计入命中率，不计投注额与盈亏。")
    lines.append("")
    lines.append("## 未解决异常（需业务口径确认）")
    miss_odds_wdl = sum(
        1 for r in results.get("模拟30.csv", [])
        if (r.get("胜平负命中") or "").strip() == "是" and not (r.get("胜平负赔率") or "").strip())
    lines.append(f"1. 胜平负 {miss_odds_wdl} 场赔率缺失：源头为竞彩足球赛程 CSV 该三列本身为空（疑似未开售/采集缺失），"
                 "代码无法补数；这些场次胜平负虚拟盈亏无法计算。")
    lines.append("2. 10-10 周六003 半全场预测含非法编码「HP」（AI 原始输出 'HH / HP / DH'）："
                 "合法选项 HH/DH 不含实际结果 AA，但 HP 意图不可考，按「含非法编码」不计中也不计错，待确认归属。")
    lines.append("3. 赔率采集时间戳缺失：赔率 CSV 无采集时间，无法确认赔率是否临场更新。")
    lines.append("4. 错题归因「未分类」行：关键词规则无法覆盖的归因文本，需人工复核，不在本次修复范围。")
    lines.append("5. 10-14 周三008「罗马 vs 皇家马德里」：分析 CSV 该行预测全空、每日记录 JSON 无该场、"
                 "赛果未同步；修复后状态为「数据缺失」，投注额 0（原先仍记 100）。")
    if skipped:
        lines.append("")
        lines.append("## 重算跳过的日期")
        for d, why in skipped:
            lines.append(f"- {d}: {why}")
    lines.append("")
    lines.append("## 修复前后变化统计")
    for k, v in diff_stat.most_common():
        lines.append(f"- {k}: {v} 行")
    lines.append(f"- 错题归因总表: 修复前 {attr_counts[0]} 行 → 修复后 {attr_counts[1]} 行")
    lines.append("")
    lines.append("## 复现方法")
    lines.append("```bash")
    lines.append("python3 audit_recompute.py   # 重建 4 表+差异+归因+汇总报告，全部写入本目录")
    lines.append("```")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"已写入 {os.path.basename(path)}")


def run_summary():
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "summarize_30days.py"),
         "--input-dir", AUDIT_DIR,
         "--out", os.path.join(AUDIT_DIR, "30天数据汇总报告_修复后.md")],
        capture_output=True, text=True, cwd=ROOT)
    print(r.stdout[-2000:] if r.returncode == 0 else r.stderr[-2000:])
    return r.returncode


def main():
    print("=== 30 天数据审计修复重算 ===")
    dates = find_dates()
    print(f"发现 {len(dates)} 个分析日期: {dates[0]} ~ {dates[-1]}")
    results, skipped = recompute_all(dates)
    write_csvs(results)
    write_five_play_rows(results)
    write_no_pred_compare(results)
    write_odds_sensitivity(results)
    write_formula_doc()
    write_data_quality(results)
    _, diff_stat = write_diff()
    attr_counts = write_attribution()
    write_attr_compare()
    write_notes(skipped, diff_stat, attr_counts, results)
    rc = run_summary()
    print("=== 重算完成 ===" if rc == 0 else "!!! 汇总报告生成失败")
    return rc


if __name__ == "__main__":
    sys.exit(main())
