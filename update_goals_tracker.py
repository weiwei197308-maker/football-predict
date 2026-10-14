import csv
import re
import sys
import os
from datetime import date

from review_data_loader import load_review_data

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
TARGET_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "竞彩总进球模拟盘.csv")

CSV_HEADER = [
    "日期", "编号", "联赛", "对阵", "信心指数",
    "AI总进球预测", "各选项赔率",
    "实际总进球数", "是否命中", "模拟投注额", "模拟盈亏",
]


def parse_date(date_str):
    ym = date_str.split("-")
    return f"{ym[1]}-{ym[2]}"


def load_analysis(date_str):
    path = os.path.join(OUTPUT_DIR, f"竞彩分析_{date_str}.csv")
    if not os.path.exists(path):
        print(f"[错误] 找不到分析文件: {path}")
        sys.exit(1)
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_football(date_str):
    path = os.path.join(OUTPUT_DIR, f"竞彩足球_{date_str}.csv")
    if not os.path.exists(path):
        print(f"[错误] 找不到足球赔率文件: {path}")
        sys.exit(1)
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def parse_goals_reco(reco_str):
    """从 '2球 / 3球 / 4球' 解析出 [2, 3, 4]。"""
    items = [x.strip() for x in reco_str.split("/")]
    nums = []
    for item in items:
        m = re.match(r"(\d+)(?:\+|球)?", item)
        if m:
            nums.append(int(m.group(1)))
    return nums


def goal_col(goal_num):
    if goal_num >= 7:
        return "总进球-7+球"
    return f"总进球-{goal_num}球"


def find_goal_odds(match_num, goal_nums, football_rows):
    for row in football_rows:
        if row["编号"].strip() == match_num.strip():
            odds = []
            for g in goal_nums:
                col = goal_col(g)
                odds.append(row.get(col, "").strip())
            return odds
    return [""] * len(goal_nums)


def clean_matchup(s):
    return re.sub(r"\[.*?\]", "", s).strip()


def build_row(analysis_row, football_rows, review_data, mmdd):
    num = analysis_row["编号"].strip()
    league = analysis_row["联赛"].strip()
    matchup = clean_matchup(analysis_row["对阵"])
    confidence = analysis_row["信心指数"].strip()
    goals_reco_raw = analysis_row["总进球推荐"].strip()

    goal_nums = parse_goals_reco(goals_reco_raw)
    odds_list = find_goal_odds(num, goal_nums, football_rows)

    # 实际总进球数：直接读 JSON 的 "赛果.总进球" 字段，已经是 "4球" 形式
    rv = review_data.get(num, {})
    actual_goals_str = rv.get("总进球", "")
    hit_ttg = rv.get("hit_ttg")

    # 提取数字用于定位赔率索引
    actual_goals_num = None
    if actual_goals_str:
        m = re.match(r"(\d+)(?:\+|球)?", actual_goals_str)
        if m:
            actual_goals_num = int(m.group(1))

    hit = ""
    pl = ""
    bet_amount = 100

    if actual_goals_str:
        # 命中判定：直接读 JSON 布尔值
        hit = "是" if hit_ttg is True else "否"
        per_option = bet_amount / 3.0
        if hit == "是":
            # 定位命中赔率
            if actual_goals_num is not None:
                if actual_goals_num >= 7 and 7 in goal_nums:
                    idx = goal_nums.index(7)
                    odds_str = odds_list[idx]
                elif actual_goals_num in goal_nums:
                    idx = goal_nums.index(actual_goals_num)
                    odds_str = odds_list[idx]
                else:
                    odds_str = ""
            else:
                odds_str = ""
            try:
                odds_val = float(odds_str)
                raw = odds_val * per_option - bet_amount
                pl = f"{raw:+.2f}"
            except (ValueError, IndexError):
                pl = "-100"
        else:
            pl = "-100"

    return [
        mmdd, num, league, matchup, confidence,
        goals_reco_raw, "|".join(odds_list),
        actual_goals_str, hit,
        bet_amount, pl,
    ]


def load_existing_rows():
    if not os.path.exists(TARGET_CSV):
        return []
    with open(TARGET_CSV, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def save_rows(rows):
    with open(TARGET_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    mmdd = parse_date(date_str)

    print(f"=== 竞彩总进球模拟盘更新脚本 ===")
    print(f"目标日期: {date_str}  (CSV中显示: {mmdd})")
    print(f"输出文件: {TARGET_CSV}")
    print()

    analysis_rows = load_analysis(date_str)
    football_rows = load_football(date_str)
    review_data = load_review_data(date_str, OUTPUT_DIR)
    print(f"读取: 分析 {len(analysis_rows)} 场, 足球赔率 {len(football_rows)} 场, 复盘 {len(review_data)} 场")
    print()

    existing = load_existing_rows()
    key_map = {(r["日期"], r["编号"]): idx for idx, r in enumerate(existing)}

    new_count = 0
    update_count = 0

    for arow in analysis_rows:
        num = arow["编号"].strip()
        new_row = build_row(arow, football_rows, review_data, mmdd)
        record = dict(zip(CSV_HEADER, new_row))
        key = (mmdd, num)
        goals_display = record["实际总进球数"] if record["实际总进球数"] != "" else "待定"
        if key in key_map:
            existing[key_map[key]] = record
            update_count += 1
            print(f"  [更新] {num} {record['对阵']} → 实际 {goals_display}  命中 {record['是否命中'] or '-'}  盈亏 {record['模拟盈亏'] or '-'}")
        else:
            existing.append(record)
            key_map[key] = len(existing) - 1
            new_count += 1
            print(f"  [新增] {num} {record['对阵']} → 实际 {goals_display}  命中 {record['是否命中'] or '-'}  盈亏 {record['模拟盈亏'] or '-'}")

    save_rows(existing)
    print()
    print(f"完成: 新增 {new_count} 场, 更新 {update_count} 场, 总计 {len(existing)} 场")


if __name__ == "__main__":
    main()
