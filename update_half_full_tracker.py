import csv
import json
import re
import sys
import os
from datetime import date

from review_data_loader import load_review_data
from lottery_rules import (
    HAFU_SOURCE_CLEARED,
    finalize_hafu_picks,
    parse_hafu_picks_strict,
)
from settle_core import (
    STATUS_HIT, STATUS_MISS, STATUS_NO_PREDICTION, STATUS_NO_DATA,
    STATUS_UNSETTLED, STATUS_INVALID_CODE,
    settle_multi_pick,
)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
TARGET_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "竞彩半全场模拟盘.csv")

CSV_HEADER = [
    "日期", "编号", "联赛", "对阵", "信心指数",
    "AI半全场预测", "各选项赔率",
    "实际半全场", "是否命中", "模拟投注额", "模拟盈亏",
    "记录状态", "备注",
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


def load_snapshot(date_str):
    """读取当日原始预测快照，返回 {编号: 快照条目}；文件缺失/损坏返回空 dict。"""
    path = os.path.join(OUTPUT_DIR, f"原始预测快照_{date_str}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        print(f"[警告] 原始预测快照无法解析，按历史口径回溯: {path}")
        return {}
    entries = data.get("场次") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        print(f"[警告] 原始预测快照结构异常，按历史口径回溯: {path}")
        return {}
    index = {}
    for entry in entries:
        if isinstance(entry, dict):
            key = str(entry.get("编号", "")).strip()
            if key:
                index[key] = entry
    return index


def recompute_hafu_from_snapshot(analysis_row, snapshot):
    """用原始预测快照的模型原始半全场输出重新规范化。

    仅供「分析 CSV 缺来源列且推荐为空」的历史行走快照优先口径；
    快照缺少原始玩法输出时返回 None，由调用方退回理由回溯。
    """
    if not isinstance(snapshot, dict):
        return None
    raw_play = snapshot.get("原始玩法输出")
    if not isinstance(raw_play, dict) or "半全场推荐" not in raw_play:
        return None
    return finalize_hafu_picks(
        raw_play.get("半全场推荐", ""),
        analysis_row.get("胜平负推荐", ""),
    )


def build_row(analysis_row, football_rows, review_data, mmdd, snapshot=None):
    num = analysis_row["编号"].strip()
    league = analysis_row["联赛"].strip()
    matchup = clean_matchup(analysis_row["对阵"])
    confidence = analysis_row["信心指数"].strip()
    hf_reco_raw = analysis_row["半全场推荐"].strip()
    # 半全场来源由 analyze.py 写入；旧 CSV 无该列时 .get 回退空串
    hf_source = str(analysis_row.get("半全场来源", "") or "").strip()
    # 历史 CSV 不回填来源列：仅当来源缺失时，用 analyze.py 写入的清空理由回溯分类。
    # 理由为程序生成文本，只能证明曾被程序清空，原始 token 未在推荐字段留存。
    hf_reason = str(analysis_row.get("半全场理由", "") or "").strip()
    legacy_cleared = (not hf_source
                      and "[程序校验修正]" in hf_reason
                      and "已清空" in hf_reason)

    # 快照优先：来源缺失且推荐为空时按原始预测快照重算，避免把有效输出误判为清空/无预测；
    # 快照缺失或结构不符才退回上面的理由回溯口径。
    snapshot_used = False
    if not hf_source and not hf_reco_raw and snapshot:
        outcome = recompute_hafu_from_snapshot(analysis_row, snapshot)
        if outcome is not None:
            hf_reco_raw = " / ".join(outcome["推荐"])
            hf_source = outcome["来源"]
            snapshot_used = True

    # 统一由 lottery_rules 严格解析；非法 token（如 HP）不静默丢弃，记入备注
    hf_codes, invalids = parse_hafu_picks_strict(hf_reco_raw)
    odds_list = find_hf_odds(num, hf_codes, football_rows)

    # 实际半全场：直接读 JSON 的 "赛果.半全场"
    rv = review_data.get(num, {})
    actual = rv.get("半全场", "")

    hit = ""
    pl = ""
    status = ""
    note = f"含非法编码:{'|'.join(invalids)}" if invalids else ""

    if not actual:
        # 无赛果：有合法预测→未结算；已清空→含非法编码；其余无预测→数据缺失
        if hf_codes:
            status, bet_amount = STATUS_UNSETTLED, 100
        elif not hf_reco_raw and hf_source == "非法输出已清空":
            # 来源字段只能证明 analyze.py 曾清空输出，原始 token 未在推荐字段留存
            status, bet_amount = STATUS_INVALID_CODE, 0
            note = "非法输出已清空:程序已按校验规则清空，原始内容未在推荐字段保留"
        else:
            status, bet_amount = STATUS_NO_DATA, 0
            # 无赛果且无合法预测：备注不得残留入口处的非法编码标记
            note = ""
    elif not hf_codes:
        # 有赛果但无合法预测：无预测输出、全部非法、或已被程序清空
        if not hf_reco_raw and hf_source == "非法输出已清空":
            # 来源字段只能证明 analyze.py 曾清空输出，原始 token 未在推荐字段留存
            status, bet_amount = STATUS_INVALID_CODE, 0
            note = "非法输出已清空:程序已按校验规则清空，原始内容未在推荐字段保留"
        elif not hf_reco_raw and legacy_cleared:
            # 旧 CSV 缺来源列：按程序写入的清空理由回溯，不改动历史数据
            status, bet_amount = STATUS_INVALID_CODE, 0
            note = ("非法输出已清空(来源列缺失，按历史理由回溯):"
                    "程序已按校验规则清空，原始内容未在推荐字段保留")
        else:
            status = STATUS_NO_PREDICTION if not hf_reco_raw else STATUS_INVALID_CODE
            bet_amount = 0
            if invalids:
                note = f"全部选项非法:{'|'.join(invalids)}"
    elif invalids:
        # 只要含任何非法编码，整条预测即无效：即使实际命中合法项也不结算
        status, bet_amount = STATUS_INVALID_CODE, 0
    else:
        bet_amount = 100
        hit_idx = hf_codes.index(actual) if actual in hf_codes else -1
        st, pl_val = settle_multi_pick(bet_amount, hf_codes, odds_list, hit_idx)
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

    if snapshot_used:
        # 备注必须如实反映数据来源：原始快照内容已恢复，不能再声称"未保留"
        if not hf_codes and hf_source == HAFU_SOURCE_CLEARED:
            note = ("非法输出已清空(依据快照重算):"
                    "原始快照半全场输出不含任何合法编码，程序按校验规则清空")
        elif not hf_codes:
            note = ""
        elif note:
            note = f"依据快照重算:{note}"
        else:
            note = "依据快照重算:原始快照半全场输出经程序规则重算"

    return [
        mmdd, num, league, matchup, confidence,
        hf_reco_raw, "|".join(odds_list),
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

    print(f"=== 竞彩半全场模拟盘更新脚本 ===")
    print(f"目标日期: {date_str}  (CSV中显示: {mmdd})")
    print(f"输出文件: {TARGET_CSV}")
    print()

    analysis_rows = load_analysis(date_str)
    football_rows = load_football(date_str)
    review_data = load_review_data(date_str, OUTPUT_DIR)
    snapshots = load_snapshot(date_str)
    print(f"读取: 分析 {len(analysis_rows)} 场, 足球赔率 {len(football_rows)} 场, "
          f"复盘 {len(review_data)} 场, 原始预测快照 {len(snapshots)} 场")
    print()

    existing = load_existing_rows()
    key_map = {(r["日期"], r["编号"]): idx for idx, r in enumerate(existing)}

    new_count = 0
    update_count = 0

    for arow in analysis_rows:
        num = arow["编号"].strip()
        new_row = build_row(arow, football_rows, review_data, mmdd,
                            snapshots.get(num))
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
