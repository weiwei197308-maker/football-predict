import csv
import re
import sys
import os
from datetime import date

from review_data_loader import load_review_data

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
TARGET_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "模拟30.csv")

CSV_HEADER = [
    "日期", "编号", "联赛", "对阵", "信心指数",
    "AI让球预测", "让球赔率", "AI胜平负预测", "胜平负赔率",
    "实际赛果", "让球命中", "胜平负命中",
    "模拟投注额", "模拟盈亏", "错题标签(归因)",
]


def parse_date(date_str):
    ym = date_str.split("-")
    return date_str, f"{ym[1]}-{ym[2]}"


def load_analysis(date_str):
    path = os.path.join(OUTPUT_DIR, f"竞彩分析_{date_str}.csv")
    if not os.path.exists(path):
        print(f"[错误] 找不到分析文件: {path}")
        sys.exit(1)
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def load_football(date_str):
    path = os.path.join(OUTPUT_DIR, f"竞彩足球_{date_str}.csv")
    if not os.path.exists(path):
        print(f"[错误] 找不到足球赔率文件: {path}")
        sys.exit(1)
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def parse_handicap(reco_str):
    """从 '让-1主胜' / '让+1客胜' 解析让球数和方向。"""
    m = re.match(r"让([+-]\d+)(主胜|客胜|平)", reco_str)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def find_odds(match_num, handicap_val, handicap_dir, spf_dir, football_rows):
    """根据编号和预测方向，到 竞彩足球_*.csv 查赔率。"""
    for row in football_rows:
        if row["编号"].strip() == match_num.strip():
            row_handicap = row.get("让球数", "").strip()
            if row_handicap != handicap_val:
                print(f"  [警告] {match_num} 让球数不匹配: 分析={handicap_val}, 足球CSV={row_handicap}")

            handicap_col_map = {"主胜": "让球-主胜", "平": "让球-平", "客胜": "让球-客胜"}
            handicap_col = handicap_col_map.get(handicap_dir)
            handicap_odds = row.get(handicap_col, "") if handicap_col else ""

            spf_col_map = {"主胜": "胜平负-主胜", "平": "胜平负-平", "客胜": "胜平负-客胜"}
            spf_col = spf_col_map.get(spf_dir)
            spf_odds = row.get(spf_col, "") if spf_col else ""

            return handicap_odds, spf_odds
    return "", ""


def clean_matchup(s):
    return re.sub(r"\[.*?\]", "", s).strip()


def build_row(analysis_row, football_rows, review_data, mmdd):
    num = analysis_row["编号"].strip()
    league = analysis_row["联赛"].strip()
    matchup = clean_matchup(analysis_row["对阵"])
    confidence = analysis_row["信心指数"].strip()
    spf_reco = analysis_row["胜平负推荐"].strip()
    handicap_reco = analysis_row["让球推荐"].strip()

    handicap_val, handicap_dir = parse_handicap(handicap_reco)
    if handicap_val is None or handicap_dir is None:
        print(f"  [警告] {num} 让球推荐格式异常: {handicap_reco}")
        handicap_odds, spf_odds = "", ""
    else:
        handicap_odds, spf_odds = find_odds(num, handicap_val, handicap_dir, spf_reco, football_rows)

    # 复盘数据从 review.py 写出的 JSON 读取，不再自己重算
    rv = review_data.get(num, {})
    full_score = rv.get("全场", "")
    actual_spf_label = rv.get("胜平负", "")
    hit_rq = rv.get("hit_rq")
    hit_spf = rv.get("hit_spf")
    attribution = rv.get("归因") or ""

    handicap_hit = ""
    spf_hit = ""
    result_display = ""
    pl = ""
    bet_amount = 100

    if full_score:
        # 显示 "X-Y (主胜)" 形式
        result_display = f"{full_score} ({actual_spf_label})" if actual_spf_label else full_score

        # 让球命中：直接读 JSON 的判定结果，转 是/否
        if hit_rq is True:
            handicap_hit = "是"
        elif hit_rq is False:
            handicap_hit = "否"
        # hit_rq 可能为 False 之外的状态（如未开奖时 None）→ 留空

        # 胜平负命中：同上
        if hit_spf is True:
            spf_hit = "是"
        elif hit_spf is False:
            spf_hit = "否"

        # 盈亏：让球单注 100 元，命中按赔率算，未中 -100
        if handicap_hit == "是" and handicap_odds:
            try:
                odds_val = float(handicap_odds)
                raw = odds_val * bet_amount - bet_amount
                pl = f"{raw:+.0f}"
            except ValueError:
                pl = ""
        elif handicap_hit == "否":
            pl = f"-{bet_amount}"

    return [
        mmdd, num, league, matchup, confidence,
        handicap_reco, handicap_odds, spf_reco, spf_odds,
        result_display, handicap_hit, spf_hit,
        bet_amount, pl, attribution,
    ]


def load_existing_rows():
    if not os.path.exists(TARGET_CSV):
        return []
    rows = []
    with open(TARGET_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def save_rows(rows):
    with open(TARGET_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    _, mmdd = parse_date(date_str)

    print(f"=== 竞彩30天模拟盘更新脚本 ===")
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
        if key in key_map:
            existing[key_map[key]] = record
            update_count += 1
            print(f"  [更新] {num} {record['对阵']} → 赛果 {record['实际赛果'] or '待定'}  盈亏 {record['模拟盈亏'] or '-'}")
        else:
            existing.append(record)
            key_map[key] = len(existing) - 1
            new_count += 1
            print(f"  [新增] {num} {record['对阵']} → 赛果 {record['实际赛果'] or '待定'}  盈亏 {record['模拟盈亏'] or '-'}")

    save_rows(existing)
    print()
    print(f"完成: 新增 {new_count} 场, 更新 {update_count} 场, 总计 {len(existing)} 场")


if __name__ == "__main__":
    main()
