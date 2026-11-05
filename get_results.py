import json
import os
import re
from datetime import datetime, timedelta

import requests


TECAI_RESULT_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.lottery.gov.cn/jc/zqsgkj/",
    "Origin": "https://www.lottery.gov.cn",
}


def fetch_tecai_results(date_str, allowed_nums=None):
    """拉取指定销售日期的体彩赛果。

    关键背景：场次编号按销售日编排（如“周一001”），但深夜场次跨零点开球，
    官方 API 的 matchDate 记录的是实际开球日（次日凌晨）。例如销售日 2026-10-12
    的周一001-010 在 API 中 matchDate 全部为 2026-10-13。若按
    matchDate == date_str 严格过滤，会把这些跨零点场次全部误杀，导致复盘为空。

    过滤策略（双重边界，防止跨期回填）：
    1. 仅接受 matchDate ∈ {date_str, date_str+1} 的记录（查询窗口本身也是这两天）；
    2. allowed_nums 非空时，仅保留编号在白名单内的场次。白名单应由调用方从当日
       竞彩分析 CSV 的编号集合生成，从而精确圈定本销售日的全部场次，排除相邻
       销售日（如同窗口内的周二001）。
    """
    next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    params = {
        "matchBeginDate": date_str,
        "matchEndDate": next_day,
        "leagueId": "",
        "pageSize": "50",
        "pageNo": "1",
        "isFix": "0",
        "matchPage": "1",
        "pcOrWap": "1",
    }

    try:
        resp = requests.get(TECAI_RESULT_URL, params=params, headers=HEADERS, timeout=15)
        data = resp.json()
    except Exception as e:
        print(f"  体彩网赛果 API 请求失败：{e}")
        return []

    if not data.get("success"):
        print(f"  体彩网赛果 API 返回错误：{data.get('errorMessage', '未知错误')}")
        return []

    value = data.get("value", {})
    if isinstance(value, dict):
        results = value.get("matchResult", [])
    else:
        results = []

    allowed = {str(n).strip() for n in allowed_nums} if allowed_nums else None
    valid_match_dates = {date_str, next_day}
    seen_nums = set()
    parsed = []
    for m in results:
        num = m.get("matchNumStr", "")
        if not num or num in seen_nums:
            continue
        # 边界1：只接受销售日当天或次日（跨零点凌晨场次）的记录，禁止跨日期回填
        match_date = m.get("matchDate", "")
        if match_date and match_date not in valid_match_dates:
            continue
        # 边界2：白名单精确圈定本销售日期的场次
        if allowed is not None and num not in allowed:
            continue
        seen_nums.add(num)
        parsed.append({
            "match_num": num,
            "league": m.get("leagueName") or m.get("leagueNameAbbr") or "",
            "home": m.get("allHomeTeam", "") or m.get("homeTeam", ""),
            "away": m.get("allAwayTeam", "") or m.get("awayTeam", ""),
            "goal_line": m.get("goalLine", ""),
            "half_score_raw": m.get("sectionsNo1", ""),
            "full_score_raw": m.get("sectionsNo999", ""),
            "win_flag": m.get("winFlag", ""),
            "status": m.get("matchResultStatus", ""),
            "match_date": match_date,
            "home_team_id": m.get("homeTeamId"),
            "away_team_id": m.get("awayTeamId"),
            "match_id": m.get("matchId"),
        })

    return parsed


def parse_score(raw):
    if not raw or raw in ("无效场次", "取消"):
        return None, None
    parts = raw.split(":")
    if len(parts) == 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None, None
    return None, None


def get_result_from_win_flag(flag):
    mapping = {"H": "主胜", "D": "平", "A": "客胜"}
    return mapping.get(flag, "")


def get_result_code_from_win_flag(flag):
    mapping = {"H": "H", "D": "D", "A": "A"}
    return mapping.get(flag, "?")


def cache_results(date_str, results):
    cache_dir = "output"
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"体彩赛果_{date_str}.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"  💾 赛果缓存到 {cache_path}")


def load_cached_results(date_str):
    cache_path = f"output/体彩赛果_{date_str}.json"
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


if __name__ == "__main__":
    import sys
    from datetime import date, timedelta

    date_str = sys.argv[1] if len(sys.argv) > 1 else (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"📡 从体彩网获取 {date_str} 的竞彩足球赛果...")
    results = fetch_tecai_results(date_str)
    print(f"  ✅ 获取到 {len(results)} 场比赛赛果")

    for r in results:
        h_full, a_full = parse_score(r["full_score_raw"])
        h_half, a_half = parse_score(r["half_score_raw"])
        spf = get_result_from_win_flag(r["win_flag"])
        print(f"  {r['match_num']} | {r['home']} vs {r['away']} | "
              f"半场 {r['half_score_raw'] or '-'} | 全场 {r['full_score_raw'] or '-'} | "
              f"胜平负 {spf or '-'}")

    if results:
        cache_results(date_str, results)
