#!/usr/bin/env python3
"""数据清洗：用修复后的让球逻辑重算所有历史复盘报告 + 四张模拟盘CSV"""

import csv
import json
import os
import re
import subprocess
import sys

from review import (
    load_tecai_data, get_result_label, get_result_after_handicap,
    check_spf, check_rq, check_ttg, check_crs, check_hafu, generate_report,
)
from get_results import (
    fetch_tecai_results, parse_score,
    get_result_from_win_flag, get_result_code_from_win_flag,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

DATES = [
    "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30",
    "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
]

TRACKER_SCRIPTS = [
    "update_sim_tracker.py",
    "update_goals_tracker.py",
    "update_half_full_tracker.py",
    "update_score_tracker.py",
]

CSV_FILES = [
    "模拟30.csv",
    "竞彩总进球模拟盘.csv",
    "竞彩半全场模拟盘.csv",
    "竞彩比分模拟盘.csv",
]


def parse_existing_attribution(date_str):
    """从旧复盘报告中提取已有的AI归因文本（避免重新调用DeepSeek）"""
    path = os.path.join(OUTPUT_DIR, f"竞彩复盘_{date_str}.txt")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read()
    attributions = {}
    blocks = re.split(r"🏟️", text)
    for block in blocks[1:]:
        m = re.search(r"([周][一二三四五六日]\d{3})", block)
        if not m:
            continue
        num = m.group(1)
        am = re.search(r"💡 AI 归因[：:]\s*(.+)", block)
        if am:
            attributions[num] = am.group(1).strip()
    return attributions


def process_date(date_str):
    """重算单个日期的复盘数据，生成复盘报告和每日记录"""
    tecai_matches, analysis_rows = load_tecai_data(date_str)
    if not tecai_matches:
        print(f"  ⚠️ {date_str} 无体彩数据，跳过")
        return None, None

    api_results = fetch_tecai_results(date_str)
    if not api_results:
        print(f"  ⚠️ {date_str} API 无赛果，跳过")
        return None, None

    result_map = {r["match_num"]: r for r in api_results}
    existing_attr = parse_existing_attribution(date_str)

    results = []
    stats = {"total": 0, "spf_hit": 0, "rq_hit": 0,
              "ttg_hit": 0, "hafu_hit": 0, "crs_hit": 0}

    for ana in analysis_rows:
        num = ana.get("编号", "")
        tecai = tecai_matches.get(num, {})
        api_res = result_map.get(num)
        if not tecai or not api_res:
            continue

        home_cn = api_res["home"]
        away_cn = api_res["away"]
        goal_line = api_res["goal_line"] or tecai.get("goal_line", "")

        h_full, a_full = parse_score(api_res["full_score_raw"])
        h_half, a_half = parse_score(api_res["half_score_raw"])
        win_flag = api_res["win_flag"]

        if h_full is None or a_full is None:
            continue

        actual_spf_label = get_result_from_win_flag(win_flag) or get_result_label(h_full, a_full)[0]
        actual_rq_label, _ = get_result_after_handicap(h_full, a_full, goal_line)
        actual_total = h_full + a_full
        half_code = get_result_label(h_half, a_half)[1] if h_half is not None else "?"
        full_code = get_result_label(h_full, a_full)[1]

        pred_spf = ana.get("胜平负推荐", "")
        pred_rq = ana.get("让球推荐", "")
        pred_ttg = ana.get("总进球推荐", "")
        pred_hafu = ana.get("半全场推荐", "")
        pred_crs = ana.get("比分推荐", "")
        conf = ana.get("信心指数", "?")

        spf_hit, spf_actual = check_spf(pred_spf, actual_spf_label)
        rq_hit, rq_actual = check_rq(pred_rq, actual_rq_label)
        ttg_hit, ttg_actual = check_ttg(pred_ttg, actual_total)
        crs_hit, crs_actual = check_crs(pred_crs, h_full, a_full)
        hafu_hit, hafu_actual = check_hafu(pred_hafu, half_code, full_code)

        stats["total"] += 1
        if spf_hit: stats["spf_hit"] += 1
        if rq_hit: stats["rq_hit"] += 1
        if ttg_hit: stats["ttg_hit"] += 1
        if crs_hit: stats["crs_hit"] += 1
        if hafu_hit: stats["hafu_hit"] += 1

        result = {
            "num": num,
            "league": api_res["league"] or tecai.get("league", ""),
            "home_cn": home_cn,
            "away_cn": away_cn,
            "goal_line": goal_line,
            "half_score": f"{h_half}-{a_half}" if h_half is not None else "-",
            "full_score": f"{h_full}-{a_full}",
            "half_score_raw": api_res["half_score_raw"],
            "full_score_raw": api_res["full_score_raw"],
            "win_flag": win_flag,
            "conf": conf,
            "pred_spf": pred_spf, "actual_spf": spf_actual, "spf_hit": spf_hit,
            "pred_rq": pred_rq, "actual_rq": rq_actual, "rq_hit": rq_hit,
            "pred_ttg": pred_ttg, "actual_ttg": f"{actual_total}球", "ttg_hit": ttg_hit,
            "pred_hafu": pred_hafu, "actual_hafu": hafu_actual, "hafu_hit": hafu_hit,
            "pred_crs": pred_crs, "actual_crs": f"{h_full}:{a_full}", "crs_hit": crs_hit,
            "attribution": existing_attr.get(num),
        }
        results.append(result)

    if not results:
        print(f"  ⚠️ {date_str} 无有效比赛，跳过")
        return None, None

    # 生成复盘报告
    report_lines = generate_report(date_str, results, stats)
    report_text = "\n".join(report_lines)
    report_path = os.path.join(OUTPUT_DIR, f"竞彩复盘_{date_str}.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    # 保存每日记录
    daily_record = []
    for r in results:
        rec = {
            "编号": r["num"], "联赛": r["league"],
            "对阵": f"{r['home_cn']} vs {r['away_cn']}",
            "信心指数": r["conf"],
            "预判": {"胜平负": r["pred_spf"], "让球": r["pred_rq"],
                     "总进球": r["pred_ttg"], "半全场": r["pred_hafu"], "比分": r["pred_crs"]},
            "赛果": {"半场": r["half_score"], "全场": r["full_score"],
                     "胜平负": r["actual_spf"], "让球": r["actual_rq"],
                     "总进球": r["actual_ttg"], "半全场": r["actual_hafu"], "比分": r["actual_crs"]},
            "命中": {"胜平负": r["spf_hit"], "让球": r["rq_hit"],
                     "总进球": r["ttg_hit"], "半全场": r["hafu_hit"], "比分": r["crs_hit"]},
            "错误归因": r.get("attribution"),
        }
        daily_record.append(rec)

    daily_path = os.path.join(OUTPUT_DIR, f"每日记录_{date_str}.json")
    with open(daily_path, "w", encoding="utf-8") as f:
        json.dump({"复盘日期": date_str, "命中统计": stats, "场次": daily_record},
                  f, ensure_ascii=False, indent=2)

    return results, stats


def clear_csvs():
    """删除四张CSV以便从零重建"""
    for name in CSV_FILES:
        path = os.path.join(BASE_DIR, name)
        if os.path.exists(path):
            os.remove(path)
            print(f"  🗑️ 已删除 {name}")


def run_trackers(date_str):
    """对单个日期运行四个tracker脚本"""
    for script in TRACKER_SCRIPTS:
        script_path = os.path.join(BASE_DIR, script)
        result = subprocess.run(
            [sys.executable, script_path, date_str],
            capture_output=True, text=True, cwd=BASE_DIR,
        )
        if result.returncode != 0:
            print(f"  ⚠️ {script} {date_str} 失败: {result.stderr[:200]}")


def collect_csv_stats():
    """从四张CSV中收集命中率和盈亏统计"""
    stats = {}

    # 模拟30.csv: 让球命中 + 胜平负命中 + 盈亏
    sim_path = os.path.join(BASE_DIR, "模拟30.csv")
    if os.path.exists(sim_path):
        with open(sim_path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        rq_total = sum(1 for r in rows if r.get("让球命中(1/0)") in ("0", "1"))
        rq_hit = sum(1 for r in rows if r.get("让球命中(1/0)") == "1")
        spf_total = sum(1 for r in rows if r.get("胜平负命中(1/0)") in ("0", "1"))
        spf_hit = sum(1 for r in rows if r.get("胜平负命中(1/0)") == "1")
        total_pl = 0.0
        for r in rows:
            pl = r.get("模拟盈亏", "").strip()
            if pl:
                try:
                    total_pl += float(pl)
                except ValueError:
                    pass
        stats["模拟30"] = {
            "total_rows": len(rows),
            "rq_hit": rq_hit, "rq_total": rq_total,
            "spf_hit": spf_hit, "spf_total": spf_total,
            "total_pl": total_pl,
        }

    # 其他三张CSV的盈亏
    for name, key in [("竞彩总进球模拟盘.csv", "总进球"),
                      ("竞彩半全场模拟盘.csv", "半全场"),
                      ("竞彩比分模拟盘.csv", "比分")]:
        path = os.path.join(BASE_DIR, name)
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            hit = sum(1 for r in rows if r.get("是否命中") == "是")
            total = sum(1 for r in rows if r.get("是否命中") in ("是", "否"))
            total_pl = 0.0
            for r in rows:
                pl = r.get("模拟盈亏", "").strip()
                if pl:
                    try:
                        total_pl += float(pl)
                    except ValueError:
                        pass
            stats[key] = {
                "total_rows": len(rows),
                "hit": hit, "total": total,
                "total_pl": total_pl,
            }

    return stats


def main():
    print("=" * 70)
    print("  🔄 数据清洗：用修复后的让球逻辑重算所有历史数据")
    print("=" * 70)

    # Phase 1: 重算复盘报告
    print("\n📐 Phase 1: 重算复盘报告（用修复后的 check_rq 逻辑）\n")
    all_stats = {}
    for date_str in DATES:
        print(f"📅 {date_str}")
        results, stats = process_date(date_str)
        if stats:
            all_stats[date_str] = stats
            pct = stats["rq_hit"] * 100 // stats["total"] if stats["total"] else 0
            print(f"   ✅ {stats['total']}场 | 让球命中 {stats['rq_hit']}/{stats['total']} ({pct}%)")
            print(f"   复盘报告已覆盖: output/竞彩复盘_{date_str}.txt")

    # Phase 2: 清空并重建四张CSV
    print("\n📐 Phase 2: 清空并重建四张模拟盘CSV\n")
    clear_csvs()
    for date_str in DATES:
        print(f"📅 {date_str} 运行四个tracker...")
        run_trackers(date_str)

    # Phase 3: 统计汇总
    print("\n" + "=" * 70)
    print("  📊 数据清洗完成 — 统计汇总")
    print("=" * 70)

    # 复盘报告让球命中率
    print("\n📈 各日期让球命中率（复盘报告）")
    print("-" * 50)
    grand_rq = 0
    grand_total = 0
    for date_str in DATES:
        s = all_stats.get(date_str)
        if s:
            pct = s["rq_hit"] * 100 // s["total"] if s["total"] else 0
            print(f"  {date_str}: 让球 {s['rq_hit']}/{s['total']} ({pct}%)")
            grand_rq += s["rq_hit"]
            grand_total += s["total"]
    if grand_total:
        print(f"  {'—' * 40}")
        print(f"  总计:    让球 {grand_rq}/{grand_total} ({grand_rq*100//grand_total}%)")

    # CSV统计
    print("\n📈 四张模拟盘统计")
    print("-" * 50)
    csv_stats = collect_csv_stats()

    grand_pl = 0.0
    for key in ["模拟30", "总进球", "半全场", "比分"]:
        s = csv_stats.get(key)
        if not s:
            continue
        if key == "模拟30":
            rq_pct = s["rq_hit"] * 100 // s["rq_total"] if s["rq_total"] else 0
            spf_pct = s["spf_hit"] * 100 // s["spf_total"] if s["spf_total"] else 0
            print(f"  模拟30.csv     | {s['total_rows']}行 | 让球 {s['rq_hit']}/{s['rq_total']} ({rq_pct}%) | 胜平负 {s['spf_hit']}/{s['spf_total']} ({spf_pct}%) | 盈亏 {s['total_pl']:+.0f}")
        else:
            hit_pct = s["hit"] * 100 // s["total"] if s["total"] else 0
            print(f"  {key}模拟盘    | {s['total_rows']}行 | 命中 {s['hit']}/{s['total']} ({hit_pct}%) | 盈亏 {s['total_pl']:+.0f}")
        grand_pl += s["total_pl"]

    print(f"  {'—' * 40}")
    print(f"  四张表总盈亏:  {grand_pl:+.0f} 元")

    print("\n💾 复盘报告: output/竞彩复盘_*.txt")
    print("💾 每日记录: output/每日记录_*.json")
    print("💾 模拟盘CSV: 模拟30.csv, 竞彩总进球模拟盘.csv, 竞彩半全场模拟盘.csv, 竞彩比分模拟盘.csv")


if __name__ == "__main__":
    main()
