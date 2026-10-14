import requests
import csv
import json
import os
from datetime import date

API_URL = "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.lottery.gov.cn/",
    "Accept": "application/json, text/plain, */*",
}

WEEKDAY_PREFIX = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

TTG_KEYS = ["s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7"]
TTG_NAMES = {
    "s0": "0球", "s1": "1球", "s2": "2球", "s3": "3球",
    "s4": "4球", "s5": "5球", "s6": "6球", "s7": "7+球",
}

HAFU_KEYS = ["hh", "hd", "ha", "dh", "dd", "da", "ah", "ad", "aa"]
HAFU_NAMES = {
    "hh": "半主胜-全主胜", "hd": "半主胜-全平", "ha": "半主胜-全客胜",
    "dh": "半平-全主胜", "dd": "半平-全平", "da": "半平-全客胜",
    "ah": "半客胜-全主胜", "ad": "半客胜-全平", "aa": "半客胜-全客胜",
}

CRS_KEYS = []
for h in range(6):
    for a in range(6):
        CRS_KEYS.append(f"s0{h}s0{a}")


def parse_match(match):
    row = {}

    row["编号"] = match.get("matchNumStr", "")
    row["联赛"] = match.get("leagueAbbName", "")
    row["联赛全称"] = match.get("leagueAllName", "")
    row["主队"] = match.get("homeTeamAllName", "")
    row["主队排名"] = match.get("homeRank", "")
    row["客队"] = match.get("awayTeamAllName", "")
    row["客队排名"] = match.get("awayRank", "")
    row["开赛日期"] = match.get("matchDate", "")
    row["开赛时间"] = match.get("matchTime", "")
    row["matchId"] = match.get("matchId", "")
    row["matchStatus"] = match.get("matchStatus", "")

    had = match.get("had") or {}
    row["胜平负-主胜"] = had.get("h", "")
    row["胜平负-平"] = had.get("d", "")
    row["胜平负-客胜"] = had.get("a", "")

    hhad = match.get("hhad") or {}
    row["让球数"] = hhad.get("goalLine", "")
    row["让球-主胜"] = hhad.get("h", "")
    row["让球-平"] = hhad.get("d", "")
    row["让球-客胜"] = hhad.get("a", "")

    ttg = match.get("ttg") or {}
    for k in TTG_KEYS:
        row[f"总进球-{TTG_NAMES[k]}"] = ttg.get(k, "")

    hafu = match.get("hafu") or {}
    for k in HAFU_KEYS:
        row[f"半全场-{HAFU_NAMES[k]}"] = hafu.get(k, "")

    crs = match.get("crs") or {}
    for k in CRS_KEYS:
        num = crs.get(k, "")
        if num and num != "":
            h_goals = k[2]
            a_goals = k[5]
            row[f"比分-{h_goals}:{a_goals}"] = num

    return row


def fetch_matches(target_date=None):
    if target_date is None:
        target_date = date.today().strftime("%Y-%m-%d")

    all_matches_raw = []
    all_matches_parsed = []
    seen_match_ids = set()

    page = 1
    page_size = 50

    while True:
        params = {
            "matchPage": page,
            "matchDate": target_date,
            "leagueId": "",
            "pageSize": page_size,
        }

        print(f"正在获取第 {page} 页数据...")

        try:
            resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"请求失败：{e}")
            break
        except ValueError as e:
            print(f"解析 JSON 失败：{e}")
            break

        if not data.get("success"):
            print(f"接口返回错误：{data.get('errorMessage')}")
            break

        value = data.get("value") or {}
        match_info_list = value.get("matchInfoList") or []

        if not match_info_list:
            print("没有获取到任何比赛数据。")
            break

        new_count = 0

        for group in match_info_list:
            sub_list = group.get("subMatchList") or []

            for match in sub_list:
                match_id = str(match.get("matchId", ""))
                if match_id in seen_match_ids:
                    continue
                seen_match_ids.add(match_id)
                all_matches_raw.append(match)
                all_matches_parsed.append(parse_match(match))
                new_count += 1

        if new_count == 0:
            print(f"第 {page} 页无新增比赛（API 返回重复数据），停止翻页。")
            break

        page += 1
        if page > 20:
            print("翻页超过 20 页，停止继续获取。")
            break

    target_weekday = WEEKDAY_PREFIX[date.fromisoformat(target_date).weekday()]
    filtered_parsed = []
    filtered_raw = []
    dropped = 0

    for p, r in zip(all_matches_parsed, all_matches_raw):
        num = p.get("编号", "")
        if num.startswith(target_weekday):
            filtered_parsed.append(p)
            filtered_raw.append(r)
        else:
            dropped += 1

    if dropped > 0:
        print(f"\n🔍 期号过滤：保留 {len(filtered_parsed)} 场{target_weekday}（剔除 {dropped} 场非本期比赛）")

    return filtered_parsed, filtered_raw, target_date


def print_matches(matches, target_date):
    print()
    print("=" * 70)
    print(f"  竞彩足球赛程  {target_date}   共 {len(matches)} 场")
    print("=" * 70)

    for i, m in enumerate(matches, 1):
        print()
        print(f"  [{i}] 编号：{m['编号']}")
        print(f"      联赛：{m['联赛']}")
        home_display = f"{m['主队']}{m['主队排名']}" if m['主队排名'] else m['主队']
        away_display = f"{m['客队']}{m['客队排名']}" if m['客队排名'] else m['客队']
        print(f"      对阵：{home_display}  vs  {away_display}")
        print(f"      开赛：{m['开赛日期']}  {m['开赛时间']}")
        spf = f"主胜 {m['胜平负-主胜']} | 平 {m['胜平负-平']} | 客胜 {m['胜平负-客胜']}"
        print(f"      胜平负：{spf}")
        if m['让球数']:
            rq = (f"让{m['让球数']} 主胜 {m['让球-主胜']} | "
                  f"平 {m['让球-平']} | 客胜 {m['让球-客胜']}")
            print(f"      让球：  {rq}")

    print()
    print("=" * 70)


def save_csv(matches, target_date, output_dir="output"):
    if not matches:
        print("没有数据可保存。")
        return None

    os.makedirs(output_dir, exist_ok=True)
    filename = f"竞彩足球_{target_date}.csv"
    filepath = os.path.join(output_dir, filename)

    all_keys = []
    seen = set()
    for m in matches:
        for k in m.keys():
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    try:
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(matches)
        print(f"\nCSV 已保存：{filepath}")
        return filepath
    except OSError as e:
        print(f"保存 CSV 失败：{e}")
        return None


def save_json(raw_matches, target_date, output_dir="output"):
    if not raw_matches:
        return None

    os.makedirs(output_dir, exist_ok=True)
    filename = f"竞彩足球_{target_date}_raw.json"
    filepath = os.path.join(output_dir, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(raw_matches, f, ensure_ascii=False, indent=2)
        print(f"JSON 已保存：{filepath}")
        return filepath
    except OSError as e:
        print(f"保存 JSON 失败：{e}")
        return None


def main():
    print("竞彩足球每日赛程获取脚本（含全部五种玩法）")
    print("=" * 50)

    parsed, raw, target_date = fetch_matches()

    if not parsed:
        print("今天没有比赛，或接口暂时没有返回数据。")
        return

    print_matches(parsed, target_date)
    save_csv(parsed, target_date)
    save_json(raw, target_date)


if __name__ == "__main__":
    main()
