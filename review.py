import csv
import json
import os
import re
import sys
from datetime import date, timedelta

import requests

from get_results import fetch_tecai_results, parse_score, get_result_from_win_flag, get_result_code_from_win_flag
from analyze import validate_api_key
from lottery_rules import (
    get_result_label as rules_get_result_label,
    cn_to_code,
    parse_handicap,
    compute_handicap_result,
    parse_hafu_picks_strict,
    parse_score as rules_parse_score,
    parse_total_goals_list,
    extract_handicap_direction,
)


DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
LESSONS_FILE = "lessons.txt"
LESSONS_MAX_LINES = 200


def has_formal_prediction(analysis_row):
    """仅当存在胜平负正式推荐时，才把场次计入玩法命中率统计。"""
    return bool(str((analysis_row or {}).get("胜平负推荐", "") or "").strip())


def append_to_lessons(date_str, num, home, away, attribution):
    if not attribution or attribution.startswith("(归因调用失败"):
        return
    lesson_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LESSONS_FILE)
    line = f"[{date_str}] {num} {home} vs {away}：{attribution}\n"

    existing_lines = []
    if os.path.exists(lesson_path):
        with open(lesson_path, "r", encoding="utf-8") as f:
            existing_lines = f.readlines()

    match_prefix = f"[{date_str}] {num} "
    found = any(l.startswith(match_prefix) for l in existing_lines)
    if found:
        print(f"    ⏭️ {num} 已有归因记录，跳过写入")
        return

    existing_lines.append(line)

    if len(existing_lines) > LESSONS_MAX_LINES:
        existing_lines = existing_lines[-LESSONS_MAX_LINES:]

    with open(lesson_path, "w", encoding="utf-8") as f:
        f.writelines(existing_lines)


def load_tecai_data(date_str):
    raw_path = f"output/竞彩足球_{date_str}_raw.json"
    analysis_path = f"output/竞彩分析_{date_str}.csv"

    tecai_matches = {}
    if os.path.exists(raw_path):
        with open(raw_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        for item in raw_data:
            num = item.get("matchNumStr", "")
            hhad = item.get("hhad", {})
            goal_line = hhad.get("goalLine", "") or hhad.get("goalLineValue", "")
            tecai_matches[num] = {
                "goal_line": goal_line,
                "home": item.get("homeTeamAllName", ""),
                "away": item.get("awayTeamAllName", ""),
                "league": item.get("leagueAbbName", ""),
                "match_time": item.get("matchTime", ""),
            }

    analysis_rows = []
    if os.path.exists(analysis_path):
        with open(analysis_path, "r", encoding="utf-8-sig") as f:
            analysis_rows = list(csv.DictReader(f))

    return tecai_matches, analysis_rows


def get_result_after_handicap(home_goals, away_goals, goal_line):
    """让球胜平负实际结果：委托 lottery_rules.compute_handicap_result 判定。

    让球数缺失或无法解析时返回 "无法判定"（不按无让球默认处理，避免默认通过）。
    """
    hc = parse_handicap(goal_line)
    if hc is None:
        return "无法判定", None
    return compute_handicap_result(home_goals, away_goals, hc), None


def check_spf(pred, actual_label):
    if not pred:
        return None, "无预测"
    return pred.strip() == actual_label, actual_label


def check_rq(pred, actual_label_rq):
    """让球命中：方向由 lottery_rules.extract_handicap_direction 统一提取。"""
    if not pred:
        return None, "无预测"
    pred_label = extract_handicap_direction(pred.strip())
    if pred_label is None:
        return None, actual_label_rq
    return pred_label == actual_label_rq, actual_label_rq


def check_ttg(pred_str, actual_total):
    recs = parse_total_goals_list(pred_str)
    if not recs:
        return None, actual_total
    return actual_total in recs, actual_total


def check_crs(pred_str, actual_home, actual_away):
    recs = []
    for part in re.split(r"[/,，、\s]+", str(pred_str or "")):
        pair = rules_parse_score(part.strip())
        if pair:
            recs.append(pair)
    if not recs:
        return None, f"{actual_home}:{actual_away}"
    return (actual_home, actual_away) in recs, f"{actual_home}:{actual_away}"


def check_hafu(pred_str, half_code, full_code):
    """半全场命中：严格解析，含任何非法编码的整条预测视为无效（返回 None）。

    统一口径：非法 token（如 HP）不得被静默丢弃；只要出现一个，就不判中、不判错，
    因此该场不进命中率分母、不进错误清单、不触发归因。
    """
    recs, invalids = parse_hafu_picks_strict(pred_str)
    actual_combo = f"{half_code}{full_code}"
    # 半场/全场赛果无法解析（编码含 "?"）时不可判定，不得当作普通未命中
    if "?" in actual_combo or not recs or invalids:
        return None, actual_combo
    return actual_combo in recs, actual_combo


# 半全场来源/状态标记（CSV「半全场来源」列与每日记录共用）
HAFU_SOURCE_INVALID_CLEARED = "非法输出已清空"
HAFU_STATUS_VALID = "有效预测"
HAFU_STATUS_INVALID = "非法输出已清空"
HAFU_STATUS_NO_PREDICTION = "模型未输出"


def resolve_hafu_status(source, hafu_hit):
    """区分半全场三类状态：有效预测 / 非法输出已清空 / 模型未输出。

    后两类都不进命中率分母，但必须保留可追溯的状态差异：
    非法清空是"模型给过输出但被程序清空"，无预测是"模型本来没给"。
    旧 CSV 没有「半全场来源」列时按命中结果回退，行为与修复前一致。
    """
    if str(source or "").strip() == HAFU_SOURCE_INVALID_CLEARED:
        return HAFU_STATUS_INVALID
    if hafu_hit is None:
        return HAFU_STATUS_NO_PREDICTION
    return HAFU_STATUS_VALID


def call_deepseek_for_attribution(match_info, errors):
    if not errors:
        return None

    # 密钥缺失或格式明显错误时在调用 API 前停止，不回显完整密钥
    key_ok, key_err = validate_api_key(DEEPSEEK_API_KEY)
    if not key_ok:
        print(f"错误归因已跳过：{key_err}")
        return None

    error_summary = ""
    for dim, pred, actual in errors:
        dim_cn = {"spf": "胜平负", "rq": "让球胜平负", "ttg": "总进球数",
                  "hafu": "半全场", "crs": "比分"}.get(dim, dim)
        error_summary += f"  - {dim_cn}：预测「{pred}」，实际「{actual}」\n"

    prompt = f"""你是一个竞彩足球复盘专家。请分析以下比赛 AI 预测错误的可能原因。

比赛：{match_info.get('num', '')} {match_info.get('league', '')}
对阵：{match_info.get('home', '')} vs {match_info.get('away', '')}
实际比分：{match_info.get('score', '')}（半场 {match_info.get('half_score', '')}）
AI 信心指数：{match_info.get('conf', '?')}/10

预测错误：
{error_summary}

请用 1-2 句话分析可能错在哪里（比如：高估了主队状态、忽略了客队防守韧性、低估了平局概率等）。只输出分析结论，不要重复问题。"""

    try:
        resp = requests.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 200,
            },
            timeout=30,
        )
        result = resp.json()
        return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(归因调用失败：{e})"


def run_review(date_str=None):
    if date_str is None:
        date_str = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"{'='*60}")
    print(f"  竞彩足球复盘  -  {date_str}")
    print(f"{'='*60}")

    tecai_matches, analysis_rows = load_tecai_data(date_str)
    if not tecai_matches:
        print(f"\n❌ 找不到 {date_str} 的体彩数据，请先运行 get_matches.py")
        return

    # 输入有效性保护：分析 CSV 缺失或没有任何数据行时禁止进入写入流程。
    # 否则空场次列表会静默覆盖既有 每日记录/竞彩复盘（output/ 未纳入 Git，无法恢复）。
    analysis_path = f"output/竞彩分析_{date_str}.csv"
    if not os.path.exists(analysis_path):
        print(f"\n❌ 找不到 {date_str} 的竞彩分析文件：{analysis_path}")
        print("   请先运行 analyze.py 生成预测；本次复盘已停止，未覆盖任何已有记录")
        return
    if not analysis_rows:
        print(f"\n❌ {analysis_path} 没有任何预测数据行（空文件或仅有表头）")
        print("   本次复盘已停止，未覆盖任何已有记录")
        return

    # 编号白名单：本销售日竞彩分析 CSV 中出现过的全部场次编号。
    # 体彩编号按销售日编排，但深夜场次跨零点开球，官方 API 的 matchDate 会记成次日，
    # 必须用编号白名单圈定场次，不能只靠 matchDate 过滤（否则跨零点场次全部漏抓）。
    allowed_nums = sorted({
        str(ana.get("编号", "") or "").strip()
        for ana in analysis_rows if str(ana.get("编号", "") or "").strip()
    })

    print(f"\n📡 从体彩网官方 API 获取 {date_str} 的赛果...")
    api_results = fetch_tecai_results(date_str, allowed_nums=allowed_nums)
    if not api_results:
        print(f"⚠️ 体彩网 {date_str} 窗口暂无本销售日赛果，试试查今天？")
        today_str = date.today().strftime("%Y-%m-%d")
        if today_str != date_str:
            api_results = fetch_tecai_results(today_str, allowed_nums=allowed_nums)
        if not api_results:
            print("❌ 仍然拿不到赛果，跳过复盘")
            return

    result_map = {}
    for r in api_results:
        result_map[r["match_num"]] = r

    print(f"  ✅ 体彩网返回 {len(api_results)} 场赛果")
    print(f"  � 本地体彩数据 {len(tecai_matches)} 场 | AI 分析 {len(analysis_rows)} 场\n")

    results = []
    stats = {"total": 0,
             "spf_hit": 0, "rq_hit": 0, "ttg_hit": 0, "hafu_hit": 0, "crs_hit": 0,
             "spf_valid": 0, "rq_valid": 0, "ttg_valid": 0, "hafu_valid": 0, "crs_valid": 0,
             "hafu_invalid": 0, "hafu_no_prediction": 0}

    for ana in analysis_rows:
        num = ana.get("编号", "")
        tecai = tecai_matches.get(num, {})
        api_res = result_map.get(num)

        if not tecai:
            print(f"  ⚠️ {num} 本地体彩数据缺失，跳过")
            continue
        if not api_res:
            print(f"  ⚠️ {num} 体彩网 API 没返回赛果（可能未开奖），跳过")
            continue

        # 核心剧本校验失败的场次不会生成正式玩法推荐，不应计入命中率分母。
        if not has_formal_prediction(ana):
            status = str(ana.get("综合分析", "") or "")
            print(f"  ⏭️ {num} 无有效玩法预测，跳过命中统计" + (f"（{status[:80]}）" if status else ""))
            continue

        home_cn = api_res["home"]
        away_cn = api_res["away"]
        goal_line = api_res["goal_line"] or tecai.get("goal_line", "")

        h_full, a_full = parse_score(api_res["full_score_raw"])
        h_half, a_half = parse_score(api_res["half_score_raw"])
        win_flag = api_res["win_flag"]

        if h_full is None or a_full is None:
            print(f"  ⏳ {num} {home_cn} vs {away_cn} - 比赛无效或未结束（{api_res['full_score_raw']}），跳过")
            continue

        actual_spf_label = get_result_from_win_flag(win_flag) or rules_get_result_label(h_full, a_full)
        actual_rq_label, _ = get_result_after_handicap(h_full, a_full, goal_line)
        actual_total = h_full + a_full
        half_code = get_result_code_from_win_flag(win_flag) if h_half is not None else "?"
        if h_half is not None:
            half_code = cn_to_code(rules_get_result_label(h_half, a_half))
        full_code = cn_to_code(rules_get_result_label(h_full, a_full))

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
        # 新增 CSV 列缺失（旧文件）时为空串，走旧逻辑，不因缺列崩溃
        hafu_source = str(ana.get("半全场来源", "") or "").strip()
        if hafu_source == HAFU_SOURCE_INVALID_CLEARED:
            hafu_hit = None
        hafu_status = resolve_hafu_status(hafu_source, hafu_hit)

        stats["total"] += 1
        # 三态命中：True 命中 / False 有效预测未中 / None 无预测（不进分母、不进归因）
        for key, val in (("spf", spf_hit), ("rq", rq_hit), ("ttg", ttg_hit),
                         ("crs", crs_hit), ("hafu", hafu_hit)):
            if val is not None:
                stats[f"{key}_valid"] += 1
                if val:
                    stats[f"{key}_hit"] += 1
        if hafu_status == HAFU_STATUS_INVALID:
            stats["hafu_invalid"] += 1
        elif hafu_status == HAFU_STATUS_NO_PREDICTION:
            stats["hafu_no_prediction"] += 1

        errors = []
        if spf_hit is False: errors.append(("spf", pred_spf, spf_actual))
        if rq_hit is False: errors.append(("rq", pred_rq, rq_actual))
        if ttg_hit is False: errors.append(("ttg", pred_ttg, f"{ttg_actual}球"))
        if hafu_hit is False: errors.append(("hafu", pred_hafu, hafu_actual))
        if crs_hit is False: errors.append(("crs", pred_crs, crs_actual))

        match_info = {
            "num": num,
            "league": api_res["league"] or tecai.get("league", ""),
            "home": home_cn,
            "away": away_cn,
            "score": f"{h_full}-{a_full}",
            "half_score": f"{h_half}-{a_half}" if h_half is not None else "-",
            "conf": conf,
        }

        attribution = None
        if errors:
            print(f"  🤖 {num} AI 归因中...")
            attribution = call_deepseek_for_attribution(match_info, errors)
            if attribution:
                append_to_lessons(date_str, num, home_cn, away_cn, attribution)

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
            "hafu_source": hafu_source, "hafu_status": hafu_status,
            "pred_crs": pred_crs, "actual_crs": f"{h_full}:{a_full}", "crs_hit": crs_hit,
            "attribution": attribution,
        }
        results.append(result)

    if not results:
        # 兜底保护：CSV 有行但全部无有效预测、或窗口内无匹配赛果时，
        # 同样不得用空场次覆盖既有每日记录与复盘报告。
        print(f"\n⚠️ {date_str} 没有可复盘场次（无有效预测或无匹配赛果），"
              f"已停止写入，保留已有记录")
        return

    report_lines = generate_report(date_str, results, stats)
    report_text = "\n".join(report_lines)
    print(report_text)

    report_path = f"output/竞彩复盘_{date_str}.txt"
    os.makedirs("output", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"\n💾 复盘报告已保存：{report_path}")

    daily_record = []
    for r in results:
        rec = {
            "编号": r["num"],
            "联赛": r["league"],
            "对阵": f"{r['home_cn']} vs {r['away_cn']}",
            "信心指数": r["conf"],
            "预判": {
                "胜平负": r["pred_spf"], "让球": r["pred_rq"],
                "总进球": r["pred_ttg"], "半全场": r["pred_hafu"], "比分": r["pred_crs"],
            },
            "赛果": {
                "半场": r["half_score"], "全场": r["full_score"],
                "胜平负": r["actual_spf"], "让球": r["actual_rq"],
                "总进球": r["actual_ttg"], "半全场": r["actual_hafu"], "比分": r["actual_crs"],
            },
            "命中": {
                "胜平负": r["spf_hit"], "让球": r["rq_hit"],
                "总进球": r["ttg_hit"], "半全场": r["hafu_hit"], "比分": r["crs_hit"],
            },
            "错误归因": r.get("attribution"),
            # 新增字段：半全场来源与三态状态（非法清空不得降级为普通空预测）
            "半全场来源": r["hafu_source"],
            "半全场状态": r["hafu_status"],
        }
        daily_record.append(rec)

    daily_path = f"output/每日记录_{date_str}.json"
    with open(daily_path, "w", encoding="utf-8") as f:
        json.dump({"复盘日期": date_str, "命中统计": stats, "场次": daily_record},
                  f, ensure_ascii=False, indent=2)
    print(f"💾 每日预判+赛果对照存档：{daily_path}")

    if results:
        lesson_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LESSONS_FILE)
        if os.path.exists(lesson_path):
            with open(lesson_path, "r", encoding="utf-8") as f:
                lesson_lines = f.readlines()
            today_count = sum(1 for l in lesson_lines if f"[{date_str}]" in l)
            total_count = len(lesson_lines)
            if today_count > 0:
                print(f"📚 今日新增 {today_count} 条错误经验 → lessons.txt（累计 {total_count} 条）")


def generate_report(date_str, results, stats):
    lines = []
    lines.append("=" * 70)
    lines.append(f"  竞彩足球复盘报告  -  {date_str}")
    lines.append("=" * 70)
    lines.append("  数据来源：中国体彩网官方 API（webapi.sporttery.cn）")
    lines.append("")

    lines.append("📈 整体命中率统计")
    lines.append("-" * 50)
    total = stats["total"]
    if total > 0:
        lines.append(f"  总分析场次：{total}")
        for label, key in (("胜平负", "spf"), ("让球胜平负", "rq"), ("总进球", "ttg"),
                           ("半全场", "hafu"), ("比分", "crs")):
            # 有效场次必须由统计方显式提供；缺失时不可用总场次冒充分母
            if f"{key}_valid" not in stats:
                lines.append(f"  {label}命中：不可计算（缺少有效场次统计）")
                continue
            valid = stats[f"{key}_valid"]
            hits = stats[f"{key}_hit"]
            if valid:
                lines.append(f"  {label}命中：{hits}/{valid}  ({hits*100//valid}%)")
            else:
                lines.append(f"  {label}命中：0/0（无有效预测）")
        # 半全场样本分布：非法清空与无预测都不进分母，但必须分开呈现
        if stats.get("hafu_invalid") or stats.get("hafu_no_prediction"):
            lines.append(
                f"  半全场样本：有效 {stats.get('hafu_valid', 0)} / "
                f"非法输出已清空 {stats.get('hafu_invalid', 0)} / "
                f"无预测 {stats.get('hafu_no_prediction', 0)}（后两类不计入分母）"
            )
        valid_keys = [f"{k}_valid" for k in ("spf", "rq", "ttg", "hafu", "crs")]
        if all(k in stats for k in valid_keys):
            total_hits = stats["spf_hit"] + stats["rq_hit"] + stats["ttg_hit"] + stats["hafu_hit"] + stats["crs_hit"]
            total_possible = sum(stats[k] for k in valid_keys)
            if total_possible:
                lines.append(f"  🏆 综合命中率：{total_hits}/{total_possible}  ({total_hits*100//total_possible}%)")
    lines.append("")

    lines.append("📋 逐场复盘详情")
    lines.append("-" * 50)

    for r in results:
        lines.append("")
        lines.append(f"  🏟️  {r['num']}  {r['league']}")
        lines.append(f"      {r['home_cn']} vs {r['away_cn']}  (置信 {r['conf']}/10)")
        lines.append(f"      半场 {r['half_score']}  →  全场 {r['full_score']}")
        lines.append(f"      让球数：{r['goal_line'] or '无'}  |  体彩开奖：{r['win_flag']}  H=主胜 D=平 A=客胜")
        lines.append("")

        dims = [
            ("胜平负", r['pred_spf'], r['actual_spf'], r['spf_hit']),
            ("让球", r['pred_rq'], r['actual_rq'], r['rq_hit']),
            ("总进球", r['pred_ttg'], r['actual_ttg'], r['ttg_hit']),
            ("半全场", r['pred_hafu'], r['actual_hafu'], r['hafu_hit']),
            ("比分", r['pred_crs'], r['actual_crs'], r['crs_hit']),
        ]
        for dim_name, pred, actual, hit in dims:
            if hit is None and dim_name == "半全场" and r.get("hafu_status") == HAFU_STATUS_INVALID:
                # 非法清空：既非命中、也非未中，更不能显示成普通无预测
                lines.append(f"      ⛔ {dim_name}：非法输出已清空（不计命中）  实际「{actual}」")
                continue
            if hit is None:
                mark = "➖"  # 无预测输出：既非命中也非预测错误
            else:
                mark = "✅" if hit else "❌"
            lines.append(f"      {mark} {dim_name}：预测「{pred}」  实际「{actual}」")

        if r.get("attribution"):
            lines.append("")
            lines.append(f"      💡 AI 归因：{r['attribution']}")

    lines.append("")
    lines.append("=" * 70)
    return lines


def main():
    date_str = None
    if len(sys.argv) > 1:
        date_str = sys.argv[1]

    if not os.path.isdir("output"):
        print("❌ 找不到 output 目录，请先运行 get_matches.py")
        return

    run_review(date_str)


if __name__ == "__main__":
    main()
