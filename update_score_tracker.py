import csv
import re
import sys
import os
from datetime import date

from review_data_loader import load_review_data
from lottery_rules import parse_score as rules_parse_score
from settle_core import (
    STATUS_HIT, STATUS_MISS, STATUS_NO_PREDICTION, STATUS_NO_DATA,
    STATUS_UNSETTLED,
    settle_multi_pick,
)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
TARGET_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "竞彩比分模拟盘.csv")

CSV_HEADER = [
    "日期", "编号", "联赛", "对阵", "信心指数",
    "AI比分预测", "各选项赔率",
    "实际比分", "是否命中", "模拟投注额", "模拟盈亏",
    "记录状态", "备注",
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


def parse_score_reco(reco_str):
    """解析比分推荐，返回 (合法比分 token 列表, 非法 token 列表)。

    合法 = 能被 lottery_rules.parse_score 解析为 (主, 客) 整数对；
    非法 token 不静默丢弃，供"含非法编码"审计状态使用。
    """
    valid, invalids = [], []
    for x in reco_str.split("/"):
        x = x.strip()
        if not x:
            continue
        if rules_parse_score(x):
            if x not in valid:
                valid.append(x)
        else:
            invalids.append(x)
    return valid, invalids


def score_col(score_str):
    return f"比分-{score_str}"


def find_score_odds(match_num, scores, football_rows):
    for row in football_rows:
        if row["编号"].strip() == match_num.strip():
            odds = []
            for s in scores:
                col = score_col(s)
                odds.append(row.get(col, "").strip())
            return odds
    return [""] * len(scores)


def clean_matchup(s):
    return re.sub(r"\[.*?\]", "", s).strip()


def build_row(analysis_row, football_rows, review_data, mmdd):
    num = analysis_row["编号"].strip()
    league = analysis_row["联赛"].strip()
    matchup = clean_matchup(analysis_row["对阵"])
    confidence = analysis_row["信心指数"].strip()
    score_reco_raw = analysis_row["比分推荐"].strip()

    scores, invalids = parse_score_reco(score_reco_raw)
    odds_list = find_score_odds(num, scores, football_rows)

    # 实际比分：直接读 JSON 的 "赛果.比分"，已经是 "2:2" 形式（用冒号）
    rv = review_data.get(num, {})
    actual = rv.get("比分", "")

    hit = ""
    pl = ""
    status = ""
    note = f"含非法编码:{'|'.join(invalids)}" if invalids else ""

    if not actual:
        # 无赛果：有预测→未结算；无预测→数据缺失
        if scores:
            status, bet_amount = STATUS_UNSETTLED, 100
        else:
            status, bet_amount = STATUS_NO_DATA, 0
    elif not scores:
        # 有赛果但无合法预测输出：不记未中，不计 -100
        status, bet_amount = STATUS_NO_PREDICTION, 0
    else:
        bet_amount = 100
        hit_idx = scores.index(actual) if actual in scores else -1
        st, pl_val = settle_multi_pick(bet_amount, scores, odds_list, hit_idx)
        if st == STATUS_HIT:
            hit, pl = "是", f"{pl_val:+.2f}"
        elif st == STATUS_MISS:
            hit, pl = "否", f"{pl_val:+.2f}"
        else:
            # 命中选项赔率缺失等：命中事实保留，不结算
            hit = "是" if hit_idx >= 0 else "否"
            status = st
        if not status:
            status = st

    return [
        mmdd, num, league, matchup, confidence,
        score_reco_raw, "|".join(odds_list),
        actual, hit, bet_amount, pl,
        status, note,
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

    print(f"=== 竞彩比分模拟盘更新脚本 ===")
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
        actual_display = record["实际比分"] if record["实际比分"] else "待定"
        if key in key_map:
            existing[key_map[key]] = record
            update_count += 1
            print(f"  [更新] {num} {record['对阵']} → 实际 {actual_display}  命中 {record['是否命中'] or '-'}  盈亏 {record['模拟盈亏'] or '-'}")
        else:
            existing.append(record)
            key_map[key] = len(existing) - 1
            new_count += 1
            print(f"  [新增] {num} {record['对阵']} → 实际 {actual_display}  命中 {record['是否命中'] or '-'}  盈亏 {record['模拟盈亏'] or '-'}")

    save_rows(existing)
    print()
    print(f"完成: 新增 {new_count} 场, 更新 {update_count} 场, 总计 {len(existing)} 场")


if __name__ == "__main__":
    main()
