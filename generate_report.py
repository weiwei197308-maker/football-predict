import csv
import os
import re
from datetime import date

WIDTH = 72
PADDING = 4


def find_latest_file(prefix, output_dir="output"):
    if not os.path.isdir(output_dir):
        return None
    files = [f for f in os.listdir(output_dir)
             if f.startswith(prefix) and f.endswith(".csv")]
    if not files:
        return None
    files.sort(reverse=True)
    return os.path.join(output_dir, files[0])


def load_csv(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def match_date_from_path(path):
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
    return m.group(1) if m else date.today().strftime("%Y-%m-%d")


def top_n_by_odds(row, prefix, n=3):
    items = []
    for col, val in row.items():
        if col.startswith(prefix) and val:
            try:
                odds = float(val)
                label = col[len(prefix):]
                items.append((odds, label))
            except ValueError:
                pass
    items.sort(key=lambda x: x[0])
    return items[:n]


def format_num(val):
    try:
        v = float(val)
        if v == int(v):
            return str(int(v))
        return f"{v:.2f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return str(val)


def build_match_block(idx, row, analysis_row):
    lines = []

    num = row.get("编号", "")
    league = row.get("联赛", "")
    home = row.get("主队", "")
    home_rank = row.get("主队排名", "")
    away = row.get("客队", "")
    away_rank = row.get("客队排名", "")
    date_ = row.get("开赛日期", "")
    time_ = row.get("开赛时间", "")

    home_full = f"{home}{home_rank}" if home_rank else home
    away_full = f"{away}{away_rank}" if away_rank else away

    header_top = f"  [{idx}] {num}  {league}"
    header_match = f"  {home_full}  vs  {away_full}"
    header_time = f"  开赛：{date_} {time_}"

    lines.append("+" + "-" * (WIDTH - 2) + "+")
    lines.append("|" + header_top.center(WIDTH - 2) + "|")
    lines.append("|" + header_match.center(WIDTH - 2) + "|")
    lines.append("|" + header_time.center(WIDTH - 2) + "|")
    lines.append("+" + "-" * (WIDTH - 2) + "+")

    lines.append("|  [原始赔率]" + " " * (WIDTH - 16) + "|")

    spf_h = format_num(row.get("胜平负-主胜", ""))
    spf_d = format_num(row.get("胜平负-平", ""))
    spf_a = format_num(row.get("胜平负-客胜", ""))
    line1 = f"胜平负：主胜 {spf_h} | 平 {spf_d} | 客胜 {spf_a}"
    lines.append("|" + "  " + line1 + " " * (WIDTH - 4 - len(line1)) + "|")

    rq_goal = row.get("让球数", "")
    if rq_goal:
        rq_h = format_num(row.get("让球-主胜", ""))
        rq_d = format_num(row.get("让球-平", ""))
        rq_a = format_num(row.get("让球-客胜", ""))
        line2 = f"让球{rq_goal}：主胜 {rq_h} | 平 {rq_d} | 客胜 {rq_a}"
        lines.append("|" + "  " + line2 + " " * (WIDTH - 4 - len(line2)) + "|")

    ttg_top = top_n_by_odds(row, "总进球-", 3)
    if ttg_top:
        ttg_str = " > ".join(f"{label}({format_num(odds)})" for odds, label in ttg_top)
        line3 = f"总进球热门：{ttg_str}"
        lines.append("|" + "  " + line3 + " " * (WIDTH - 4 - len(line3)) + "|")

    crs_top = top_n_by_odds(row, "比分-", 3)
    if crs_top:
        crs_str = " > ".join(f"{label}({format_num(odds)})" for odds, label in crs_top)
        line4 = f"比分热门：  {crs_str}"
        lines.append("|" + "  " + line4 + " " * (WIDTH - 4 - len(line4)) + "|")

    history_text = analysis_row.get("历史数据", "") if analysis_row else ""
    if history_text:
        lines.append("|" + "  [历史数据]" + " " * (WIDTH - 16) + "|")
        for hline in history_text.split("\n"):
            if hline.strip():
                wrapped = wrap_text(hline, WIDTH - 6)
                for wl in wrapped:
                    lines.append("|" + "  " + wl + " " * (WIDTH - 4 - len(wl)) + "|")
        lines.append("+" + "-" * (WIDTH - 2) + "+")

    if analysis_row:
        lines.append("|  [AI 五大推荐]" + " " * (WIDTH - 17) + "|")

        spf_rec = analysis_row.get("胜平负推荐", "")
        spf_reason = analysis_row.get("胜平负理由", "")
        lines.append(build_recommend_line("🏆 胜平负", spf_rec, spf_reason))

        rq_rec = analysis_row.get("让球推荐", "")
        rq_reason = analysis_row.get("让球理由", "")
        lines.append(build_recommend_line("⚽ 让球  ", rq_rec, rq_reason))

        ttg_rec = analysis_row.get("总进球推荐", "")
        ttg_reason = analysis_row.get("总进球理由", "")
        lines.append(build_recommend_line("🎯 总进球", ttg_rec, ttg_reason))

        hafu_rec_raw = analysis_row.get("半全场推荐", "")
        hafu_rec = expand_hafu(hafu_rec_raw)
        hafu_reason = analysis_row.get("半全场理由", "")
        lines.append(build_recommend_line("⏱ 半全场", hafu_rec, hafu_reason))

        crs_rec = analysis_row.get("比分推荐", "")
        crs_reason = analysis_row.get("比分理由", "")
        lines.append(build_recommend_line("📊 比分  ", crs_rec, crs_reason))

        confidence = analysis_row.get("信心指数", "")
        zonghe = analysis_row.get("综合分析", "")

        lines.append("+" + "-" * (WIDTH - 2) + "+")

        if confidence:
            conf_line = f"信心指数：{'★' * int(confidence)}{'☆' * (10 - int(confidence))}  {confidence}/10"
            lines.append("|" + "  " + conf_line + " " * (WIDTH - 4 - len(conf_line)) + "|")

        if zonghe:
            wrapped = wrap_text(zonghe, WIDTH - 6)
            for j, wl in enumerate(wrapped):
                lines.append("|" + "  " + wl + " " * (WIDTH - 4 - len(wl)) + "|")

    else:
        lines.append("|  [AI 五大推荐] 暂无分析（运行 analyze.py 后自动补上）" +
                     " " * 4 + "|")

    lines.append("+" + "-" * (WIDTH - 2) + "+")
    return lines


def build_recommend_line(label, rec, reason):
    full = f"{label}：{rec}"
    if reason:
        full += f"   （{reason}）"
    if len(full) <= WIDTH - 4:
        return "|" + "  " + full + " " * (WIDTH - 4 - len(full)) + "|"

    first = f"{label}：{rec}"
    return "|" + "  " + first + " " * (WIDTH - 4 - len(first)) + "|"


def expand_hafu(raw):
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    result = []
    code_map = {"H": "主胜", "D": "平", "A": "客胜"}
    for p in parts:
        p = p.strip()
        if len(p) == 2 and p[0] in code_map and p[1] in code_map:
            half = code_map[p[0]]
            full = code_map[p[1]]
            result.append(f"{half}→{full}")
        else:
            result.append(p)
    return " / ".join(result)


def wrap_text(text, max_width):
    if not text:
        return []
    if len(text) <= max_width:
        return [text]
    words = list(text)
    lines = []
    current = ""
    for ch in words:
        current += ch
        if len(current) >= max_width:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines


def build_report(match_rows, analysis_rows, target_date):
    analysis_by_num = {}
    for a in analysis_rows:
        n = a.get("编号", "")
        if n:
            analysis_by_num[n] = a

    all_lines = []

    all_lines.append("=" * WIDTH)
    all_lines.append("竞彩足球每日分析报告".center(WIDTH))
    all_lines.append(f"日期：{target_date}".center(WIDTH))
    all_lines.append(f"共 {len(match_rows)} 场比赛".center(WIDTH))
    all_lines.append("=" * WIDTH)

    has_analysis = len(analysis_rows) > 0
    if has_analysis:
        def _is_blocked(a):
            # 核心剧本校验未通过的被阻止行不计为已完成预测
            return str(a.get("综合分析", "")).startswith("核心剧本校验未通过")

        blocked = sum(1 for a in analysis_rows if _is_blocked(a))
        done = sum(1 for r in match_rows
                   if r.get("编号", "") in analysis_by_num
                   and not _is_blocked(analysis_by_num[r.get("编号", "")]))
        line = f"AI 分析：已完成 {done}/{len(match_rows)}"
        if blocked:
            line += f"（{blocked} 场校验未通过，未生成预测）"
        all_lines.append(line.center(WIDTH))
    else:
        all_lines.append("AI 分析：尚未运行（建议执行 analyze.py）".center(WIDTH))
    all_lines.append("=" * WIDTH)
    all_lines.append("")

    for i, row in enumerate(match_rows, 1):
        num = row.get("编号", "")
        ana = analysis_by_num.get(num)
        block = build_match_block(i, row, ana)
        all_lines.extend(block)
        all_lines.append("")

    all_lines.append("=" * WIDTH)
    all_lines.append(f"生成时间：{date.today().strftime('%Y-%m-%d')}".center(WIDTH))
    all_lines.append("祝你好运！".center(WIDTH))
    all_lines.append("=" * WIDTH)

    return "\n".join(all_lines)


def main():
    print("竞彩足球报告生成器")
    print("=" * 40)

    match_csv = find_latest_file("竞彩足球_")
    if not match_csv:
        print("找不到赛程 CSV，请先运行 get_matches.py")
        return

    target_date = match_date_from_path(match_csv)
    match_rows = load_csv(match_csv)
    print(f"赛程数据：{match_csv}（{len(match_rows)} 场）")

    analysis_csv = find_latest_file("竞彩分析_")
    analysis_rows = []
    if analysis_csv:
        analysis_rows = load_csv(analysis_csv)
        print(f"分析数据：{analysis_csv}（{len(analysis_rows)} 场）")
    else:
        print("分析数据：暂无（只显示赔率）")

    report = build_report(match_rows, analysis_rows, target_date)

    print("\n")
    print(report)

    os.makedirs("output", exist_ok=True)
    txt_path = f"output/竞彩报告_{target_date}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n报告已保存：{txt_path}")


if __name__ == "__main__":
    main()
