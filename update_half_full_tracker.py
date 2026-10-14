import csv
import re
import sys
import os
from datetime import date

from review_data_loader import load_review_data

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
TARGET_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "竞彩半全场模拟盘.csv")

CSV_HEADER = [
    "日期", "编号", "联赛", "对阵", "信心指数",
    "AI半全场预测", "各选项赔率",
    "实际半全场", "是否命中", "模拟投注额", "模拟盈亏",
]

HF_COL_MAP = {
    "HH": "半全场-半主胜-全主胜",
    "HD": "半全场-半主胜-全平",
    "HA": "半全场-半主胜-全客胜",
    "DH": "半全场-半平-全主胜",
    "DD": "半全场-半平-全平",
    "DA": "半全场-半平-全客胜",
    "AH": "半全场-半客胜-全主胜",
    "AD": "半全场-半客胜-全平",
    "AA": "半全场-半客胜-全客胜",
}


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


def to_hf_code(half_str, full_str):
    def cn_to_letter(s):
        s = s.strip()
        if "主胜" in s or "主" == s:
            return "H"
        if "客胜" in s or "客" == s:
            return "A"
        if "平" in s:
            return "D"
        if s.upper() in ("H", "D", "A"):
            return s.upper()
        return None

    half_l = cn_to_letter(half_str)
    full_l = cn_to_letter(full_str)
    if half_l and full_l:
        return half_l + full_l
    return None


def parse_hf_reco(reco_str):
    """从 'HH / DH / DD' 或 '半主胜-全主胜 / ...' 解析出代码列表。"""
    items = [x.strip() for x in reco_str.split("/")]
    codes = []
    for item in items:
        item = item.strip()
        m = re.match(r"^([HDA]{2})$", item.upper())
        if m:
            codes.append(m.group(1))
            continue

        m = re.match(r"(?:半)?([^\-]+?)-(?:全)?([^\-]+)$", item)
        if m:
            code = to_hf_code(m.group(1), m.group(2))
            if code:
                codes.append(code)
                continue

        h, f = item.split("-", 1) if "-" in item else (item, "")
        code = to_hf_code(h, f) if f else None
        if code:
            codes.append(code)

    return codes


def find_hf_odds(match_num, hf_codes, football_rows):
    for row in football_rows:
        if row["编号"].strip() == match_num.strip():
            odds = []
            for c in hf_codes:
                col = HF_COL_MAP.get(c, "")
                odds.append(row.get(col, "").strip() if col else "")
            return odds
    return [""] * len(hf_codes)


def clean_matchup(s):
    return re.sub(r"\[.*?\]", "", s).strip()


def build_row(analysis_row, football_rows, review_data, mmdd):
    num = analysis_row["编号"].strip()
    league = analysis_row["联赛"].strip()
    matchup = clean_matchup(analysis_row["对阵"])
    confidence = analysis_row["信心指数"].strip()
    hf_reco_raw = analysis_row["半全场推荐"].strip()

    hf_codes = parse_hf_reco(hf_reco_raw)
    odds_list = find_hf_odds(num, hf_codes, football_rows)

    # 实际半全场：直接读 JSON 的 "赛果.半全场"
    # 修复旧 bug：不再用正则解析 txt，半场数据缺失场景下 JSON 里会是 "?H" 这种，
    # hit_hafu 必为 False，所以会正确标记为"否"，不再空白
    rv = review_data.get(num, {})
    actual = rv.get("半全场", "")
    hit_hafu = rv.get("hit_hafu")

    hit = ""
    pl = ""
    bet_amount = 100

    if actual:
        # 命中判定：直接读 JSON 布尔值
        hit = "是" if hit_hafu is True else "否"
        per_option = bet_amount / 3.0
        if hit == "是":
            # 命中时定位赔率
            idx = hf_codes.index(actual) if actual in hf_codes else -1
            odds_str = odds_list[idx] if idx >= 0 else ""
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
        hf_reco_raw, "|".join(odds_list),
        actual, hit, bet_amount, pl,
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

    print(f"=== 竞彩半全场模拟盘更新脚本 ===")
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
        actual_display = record["实际半全场"] if record["实际半全场"] else "待定"
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
