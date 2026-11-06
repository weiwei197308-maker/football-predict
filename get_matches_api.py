"""get_matches_api.py — zucai_all 全量样本环境赛程抓取（API-Football 版）。

产出与 get_matches.py 完全同构的 output/竞彩足球_{date}.csv 与 _raw.json，
另写 output/history_cache_{date}.json（供 M1/M2 分析时零请求读取历史数据）。

请求预算（Free 100/天）：
  fixtures 1 次 + 每场 odds 1 次 + 历史（h2h 源决定，见下）；
  先查 /status（不计额），预留 RESULTS_RESERVE 给赛果回填，超额按联赛优先级截断。

h2h 源开关（环境变量 H2H_SOURCE，默认 compare）：
  goal    = 只用 goal-api（1 次/场 API-Football 配额，仅 odds）
  api     = 只用 API-Football h2h（2 次/场）
  compare = 主路径用 goal，前 H2H_COMPARE_N（默认 3）场额外双源对照，
            产物 output/shadow_h2h_compare_{date}.json；失败自动回退 API-Football。
  goal/compare 需要 GOAL_API_KEY（仅从环境变量读取，缺失即 fail-fast）。

用法：
  python get_matches_api.py [--date YYYY-MM-DD] [--dry-run] [--no-history] [--output-dir output]
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

import goal_history
from leagues_config import BY_API_ID

BASE = "https://v3.football.api-sports.io"
H2H_SOURCE_ENV = "H2H_SOURCE"
H2H_COMPARE_N_ENV = "H2H_COMPARE_N"
H2H_SOURCES = ("goal", "api", "compare")
DEFAULT_H2H_SOURCE = "compare"
DEFAULT_H2H_COMPARE_N = 3
BOOKMAKER_ID = 8  # bet365
RESULTS_RESERVE = 3
CST = timezone(timedelta(hours=8))

TTG_NAMES = ["0球", "1球", "2球", "3球", "4球", "5球", "6球", "7+球"]
HAFU_VALUE_MAP = {
    "Home/Home": "半主胜-全主胜", "Home/Draw": "半主胜-全平", "Home/Away": "半主胜-全客胜",
    "Draw/Home": "半平-全主胜", "Draw/Draw": "半平-全平", "Draw/Away": "半平-全客胜",
    "Away/Home": "半客胜-全主胜", "Away/Draw": "半客胜-全平", "Away/Away": "半客胜-全客胜",
}
HAFU_KEYS = list(HAFU_VALUE_MAP.values())

_req_count = 0
_last_api_call_at = 0.0
# Free 计划限 10 次/分钟（6s/次）。稳定优先：取 8s（约 7.5 次/分钟），
# 给计数窗口抖动留更大余量，宁可慢也不让白名单场次被 429 跳过。
# 每次请求（含 429 重试）前保证最小间隔。设为 0 可关闭节流。
API_MIN_INTERVAL_SECONDS = 8.0


def _throttle_api():
    """保证两次 API-Football 请求间隔不小于 API_MIN_INTERVAL_SECONDS。"""
    global _last_api_call_at
    if API_MIN_INTERVAL_SECONDS <= 0:
        return
    now = time.monotonic()
    wait = API_MIN_INTERVAL_SECONDS - (now - _last_api_call_at)
    if _last_api_call_at and wait > 0:
        time.sleep(wait)
    _last_api_call_at = time.monotonic()


def _key():
    key = os.environ.get("FOOTBALL_API_KEY", "").strip()
    if not key:
        sys.exit("错误：未设置 FOOTBALL_API_KEY 环境变量（请先 source ~/.zshrc）")
    return key


def api_get(path, params):
    global _req_count
    resp = None
    for attempt in range(4):
        _throttle_api()
        resp = requests.get(
            f"{BASE}/{path}", params=params,
            headers={"x-apisports-key": _key()}, timeout=20,
        )
        if resp.status_code != 429:
            break
        # Free 计划有每分钟速率限制；429 时退避重试
        wait = 20 * (attempt + 1)
        print(f"  ⚠️ 429 速率限制，{wait}s 后重试（第 {attempt + 1} 次）...")
        time.sleep(wait)
    _req_count += 1
    resp.raise_for_status()
    data = resp.json()
    errors = data.get("errors")
    if errors:
        raise RuntimeError(f"API 错误 {path}: {errors}")
    return data.get("response", [])


def get_remaining():
    resp = requests.get(
        f"{BASE}/status", headers={"x-apisports-key": _key()}, timeout=15)
    payload = resp.json()
    d = payload.get("response")
    if not isinstance(d, dict) or "requests" not in d:
        raise RuntimeError(f"API /status 返回异常: {payload}")
    return d["requests"]["limit_day"] - d["requests"]["current"]


# ---------------- h2h 源开关与 API-Football 预算 ----------------

def resolve_h2h_source():
    raw = (os.environ.get(H2H_SOURCE_ENV, "") or "").strip().lower()
    if not raw:
        return DEFAULT_H2H_SOURCE
    if raw not in H2H_SOURCES:
        sys.exit(f"错误：{H2H_SOURCE_ENV}={raw} 非法，可选值：goal / api / compare")
    return raw


def resolve_compare_n():
    raw = (os.environ.get(H2H_COMPARE_N_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_H2H_COMPARE_N
    try:
        n = int(raw)
    except ValueError:
        sys.exit(f"错误：{H2H_COMPARE_N_ENV}={raw} 非法，需为非负整数")
    if n < 0:
        sys.exit(f"错误：{H2H_COMPARE_N_ENV}={raw} 非法，需为非负整数")
    return n


def max_matches_for_budget(budget, source, compare_n):
    """API-Football 预算内最多跑多少场（odds 1 次/场；历史按源计）。"""
    if budget <= 0:
        return 0
    if source == "api":
        return budget // 2
    if source == "compare" and compare_n > 0:
        if budget <= 2 * compare_n:
            return budget // 2
        return budget - compare_n
    return budget


def _api_history_budget_ok(remaining):
    """回退 API-Football 前检查：至少给赛果回填留出 RESULTS_RESERVE。"""
    return remaining - RESULTS_RESERVE - _req_count >= 1


# ---------------- goal-api 联赛球队名单（team id 映射，带缓存） ----------------

GOAL_ROSTER_CACHE = "goal_rosters.json"
GOAL_ROSTER_TTL_DAYS = 7  # 联赛球队名单赛季内稳定，7 天后刷新


def _read_roster_cache(path):
    try:
        with open(path, encoding="utf-8") as fp:
            payload = json.load(fp)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _roster_entry_teams(entry):
    teams = (entry or {}).get("teams")
    if not isinstance(teams, list):
        return []
    out = []
    for t in teams:
        if isinstance(t, (list, tuple)) and len(t) == 2 and t[0] and t[1]:
            out.append((str(t[0]), str(t[1])))
    return out


def load_or_fetch_goal_rosters(picked, output_dir, refresh=False):
    """按联赛拉 goal 球队名单并缓存，返回 {str(API联赛id): [(队名, team_id)]}。

    命中核心：/teams?leagueId= 响应约 11KB（全天赛程索引 183KB），缓存后每日零索引请求；
    单个联赛失败不影响其他联赛，且失败时沿用旧缓存。
    """
    path = os.path.join(output_dir, GOAL_ROSTER_CACHE)
    cache = {} if refresh else _read_roster_cache(path)
    leagues = cache.get("leagues") if isinstance(cache.get("leagues"), dict) else {}
    try:
        age = (date.today() - date.fromisoformat(str(cache.get("updated")))).days
        fresh = 0 <= age < GOAL_ROSTER_TTL_DAYS
    except (TypeError, ValueError):
        fresh = False

    league_ids = []
    for _, fx in picked:
        lid = (fx.get("league") or {}).get("id")
        if lid is not None and int(lid) not in league_ids:
            league_ids.append(int(lid))

    rosters, changed = {}, False
    for lid in league_ids:
        key = str(lid)
        cached_teams = _roster_entry_teams(leagues.get(key))
        if fresh and cached_teams:
            rosters[key] = cached_teams
            continue
        goal_id = goal_history.league_goal_id(lid)
        if not goal_id:
            print(f"  ⚠️ 联赛 {lid} 未收录 goal 映射，跳过 team id 名单")
            continue
        try:
            teams = goal_history.fetch_league_roster(goal_id)
        except goal_history.GoalApiError as e:
            print(f"  ⚠️ goal-api 球队名单失败（联赛 {lid}，{e.kind}）：{e}")
            if cached_teams:
                rosters[key] = cached_teams
            continue
        if teams:
            rosters[key] = teams
            leagues[key] = {"goal_id": goal_id,
                            "teams": [[n, i] for n, i in teams]}
            changed = True

    if changed or not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"updated": str(date.today()), "leagues": leagues},
                          fp, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"  ⚠️ goal 球队名单缓存写入失败：{type(e).__name__}")
    return rosters


# ---------------- 赔率映射 ----------------

def _bet_list(odds_resp):
    for item in odds_resp:
        for bm in item.get("bookmakers", []):
            if bm.get("id") == BOOKMAKER_ID:
                return bm.get("bets", [])
    # bet365 缺失时退回第一家（raw 中会标注实际 bookmaker）
    for item in odds_resp:
        bms = item.get("bookmakers", [])
        if bms:
            return bms[0].get("bets", [])
    return []


def map_odds(odds_resp):
    """返回 (mapped, source_flags)；mapped 键与 CSV 列语义一致。"""
    bets = _bet_list(odds_resp)
    m = {"spf": {}, "rq": {}, "ttg": {}, "hafu": {}, "crs": {}}
    flags = {"odds_source": "api-football:bet365", "bets_found": [b.get("name") for b in bets]}

    for bet in bets:
        name = bet.get("name", "")
        values = bet.get("values", [])

        if name == "Match Winner":
            for v in values:
                k = {"Home": "主胜", "Draw": "平", "Away": "客胜"}.get(v.get("value"))
                if k:
                    m["spf"][k] = v.get("odd", "")

        elif name == "Exact Goals Number":
            # bet365 该市场为 0..6 共 7 档，最高档「6」实为 6+（隐含概率闭合），
            # 按用户确认口径映射到体彩「7+球」，「6球」留空并在 raw 标注。
            plain = {}
            for v in values:
                val = str(v.get("value", "")).strip()
                if val.endswith("+"):
                    m["ttg"]["7+球"] = v.get("odd", "")
                    flags["ttg_6plus_to_7plus"] = True
                elif val.isdigit() and int(val) <= 6:
                    plain[int(val)] = v.get("odd", "")
            if plain and "7+球" not in m["ttg"] and max(plain) == 6:
                m["ttg"]["7+球"] = plain.pop(6)
                flags["ttg_6plus_to_7plus"] = True
            for n, odd in plain.items():
                m["ttg"][f"{n}球"] = odd

        elif name == "HT/FT Double":
            for v in values:
                k = HAFU_VALUE_MAP.get(v.get("value", ""))
                if k:
                    m["hafu"][k] = v.get("odd", "")

        elif name in ("Correct Score", "Exact Score"):
            for v in values:
                sc = str(v.get("value", "")).strip()
                parts = sc.split(":")
                if len(parts) == 2 and all(p.isdigit() for p in parts):
                    h, a = int(parts[0]), int(parts[1])
                    if h <= 5 and a <= 5:
                        m["crs"][f"{h}:{a}"] = v.get("odd", "")

        # bet365 三向让球为 "Handicap Result"；必须排除角球/卡牌/半场/亚洲盘同名市场
        elif name in ("Handicap Result", "European Handicap") and len(values) >= 3:
            # 欧洲三向让球：值形如 "Home -1"。同一名下可能有多条盘口，按 handicap 分组
            groups = {}
            for v in values:
                g = re.match(r"(Home|Draw|Away)\s*\(?([+-]?\d+(?:\.\d+)?)\)?",
                             str(v.get("value", "")).strip())
                if not g:
                    continue
                side, hcap = g.group(1), float(g.group(2))
                if hcap != int(hcap):
                    continue  # 体彩只有整数让球
                groups.setdefault(int(hcap), {})[
                    {"Home": "主胜", "Draw": "平", "Away": "客胜"}[side]] = v.get("odd", "")
            # 选主客赔率最接近的一条作为主盘口
            best = None
            for hcap, odds in groups.items():
                if len(odds) < 3:
                    continue
                try:
                    diff = abs(float(odds["主胜"]) - float(odds["客胜"]))
                except (TypeError, ValueError):
                    continue
                if best is None or diff < best[0]:
                    best = (diff, hcap, odds)
            if best:
                m["rq"] = {"让球数": str(best[1]), **best[2]}
                flags["rq_bet_name"] = name

    return m, flags


# ---------------- 历史数据（h2h 直取，1 请求/场；近况受 Free 计划限制不可用） ----------------

def fetch_history(home_id, away_id, season=None):
    """Free 计划限制：last/from-to/当前season 均不可用，仅 h2h 不带 last 可用。
    近况战绩留空并在文本中标注；season 参数保留以兼容签名，暂未使用。"""
    from football_history import summarize_recent, summarize_h2h

    home_recent, away_recent = [], []
    h2h_raw = api_get("fixtures/headtohead", {"h2h": f"{home_id}-{away_id}"})
    h2h = []
    for m in h2h_raw:
        teams, goals = m.get("teams", {}), m.get("goals", {})
        if goals.get("home") is None or goals.get("away") is None:
            continue
        h2h.append({"date": m["fixture"]["date"][:10],
                    "home": teams.get("home", {}).get("name", ""),
                    "away": teams.get("away", {}).get("name", ""),
                    "score": f"{goals['home']}-{goals['away']}"})
    h2h.sort(key=lambda x: x["date"], reverse=True)

    return {
        "home_recent": home_recent, "away_recent": away_recent, "h2h": h2h[:5],
        "home_recent_text": summarize_recent(home_recent),
        "away_recent_text": summarize_recent(away_recent),
        "h2h_text": summarize_h2h(h2h[:5]),
        "available": bool(home_recent or away_recent or h2h),
    }


def _empty_history():
    return {"home_recent": [], "away_recent": [], "h2h": [],
            "home_recent_text": "", "away_recent_text": "", "h2h_text": "",
            "available": False}


def resolve_history(kickoff_utc, home_name, away_name, league_name,
                    home_api_id, away_api_id, index, source, can_spend_api, roster=None):
    """按开关取历史，返回 (entry, 来源标签, 错误类型, goal 原始结果)。

    来源标签写入 _raw.json 的 history_source：
      goal / api / none（none = 两种源都失败或预算不允许回退）。
    goal 解析为空（available=False）也视为失败并回退 API-Football。
    """
    err = None
    goal_entry = None
    if source in ("goal", "compare"):
        try:
            goal_entry = goal_history.fetch_history_goal(
                kickoff_utc, home_name, away_name, league_name, index, roster)
            if goal_entry.get("available"):
                return goal_entry, "goal", None, goal_entry
            err = "empty"
        except goal_history.GoalApiError as e:
            err = e.kind
        except Exception as e:  # 适配层意外异常也必须降级，不拖垮赛程主流程
            err = f"unexpected:{type(e).__name__}"

    if not can_spend_api:
        return _empty_history(), "none", err or "api_budget", goal_entry

    try:
        return fetch_history(home_api_id, away_api_id), "api", err, goal_entry
    except Exception as e:
        return _empty_history(), "none", err or f"api_failed:{type(e).__name__}", goal_entry


def _history_snapshot(entry):
    return {
        "available": bool(entry.get("available")),
        "home_recent_text": entry.get("home_recent_text", ""),
        "away_recent_text": entry.get("away_recent_text", ""),
        "h2h_text": entry.get("h2h_text", ""),
    }


def build_compare_record(number, match_id, home_name, away_name, goal_entry,
                         api_entry, goal_error, api_error):
    """compare 模式单场双源快照（只写独立 shadow 文件，不进正式链路）。"""
    g = _history_snapshot(goal_entry) if goal_entry else None
    a = _history_snapshot(api_entry) if api_entry else None
    diff = None
    if g and a:
        diff = {k: g[k] == a[k] for k in
                ("available", "home_recent_text", "away_recent_text", "h2h_text")}
    return {
        "number": number,
        "matchId": str(match_id),
        "home": home_name,
        "away": away_name,
        "goal": g,
        "api": a,
        "goal_error": goal_error,
        "api_error": api_error,
        "diff": diff,
    }


def save_compare_report(records, target_date, output_dir):
    """写 output/shadow_h2h_compare_{date}.json；已存在则加时间戳，绝不覆盖。"""
    path = os.path.join(output_dir, f"shadow_h2h_compare_{target_date}.json")
    if os.path.exists(path):
        path = os.path.join(
            output_dir,
            f"shadow_h2h_compare_{target_date}_{datetime.now().strftime('%H%M%S')}.json")
    summary = {
        "matches": len(records),
        "goal_available": sum(1 for r in records if (r.get("goal") or {}).get("available")),
        "api_available": sum(1 for r in records if (r.get("api") or {}).get("available")),
        "identical_text": sum(1 for r in records if r.get("diff") and all(r["diff"].values())),
    }
    payload = {
        "date": target_date,
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "compare_n": len(records),
        "summary": summary,
        "items": records,
    }
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    return path


# ---------------- 行构造与落盘 ----------------

def build_row(fixture, mapped, number, league_cfg):
    f = fixture["fixture"]
    dt = datetime.fromisoformat(f["date"].replace("Z", "+00:00")).astimezone(CST)
    row = {
        "编号": number,
        "联赛": league_cfg["name"],
        "联赛全称": league_cfg["full_name"],
        "主队": fixture["teams"]["home"]["name"],
        "主队排名": "",
        "客队": fixture["teams"]["away"]["name"],
        "客队排名": "",
        "开赛日期": dt.strftime("%Y-%m-%d"),
        "开赛时间": dt.strftime("%H:%M"),
        "matchId": str(f["id"]),
        "matchStatus": f.get("status", {}).get("short", ""),
        "胜平负-主胜": mapped["spf"].get("主胜", ""),
        "胜平负-平": mapped["spf"].get("平", ""),
        "胜平负-客胜": mapped["spf"].get("客胜", ""),
        "让球数": mapped["rq"].get("让球数", ""),
        "让球-主胜": mapped["rq"].get("主胜", ""),
        "让球-平": mapped["rq"].get("平", ""),
        "让球-客胜": mapped["rq"].get("客胜", ""),
    }
    for k in TTG_NAMES:
        row[f"总进球-{k}"] = mapped["ttg"].get(k, "")
    for k in HAFU_KEYS:
        row[f"半全场-{k}"] = mapped["hafu"].get(k, "")
    for sc, odd in mapped["crs"].items():
        row[f"比分-{sc}"] = odd
    return row


def save_csv(rows, target_date, output_dir):
    all_keys, seen = [], set()
    for r in rows:
        for k in r:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)
    path = os.path.join(output_dir, f"竞彩足球_{target_date}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=all_keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true",
                    help="只拉赛程+1场赔率样本，打印映射结果，不写文件")
    ap.add_argument("--no-history", action="store_true", help="跳过历史数据预取")
    ap.add_argument("--output-dir", default="output")
    args = ap.parse_args()

    source = resolve_h2h_source()
    compare_n = resolve_compare_n()
    if not args.no_history and source in ("goal", "compare"):
        try:
            goal_history.get_goal_api_key()
        except goal_history.GoalApiError as e:
            sys.exit(f"错误：{e}")
    print(f"h2h 源：{source}"
          + (f"（对照前 {compare_n} 场）" if source == "compare" else ""))

    remaining = get_remaining()
    print(f"API 当日剩余请求：{remaining}（预算控制：预留 {RESULTS_RESERVE} 给赛果回填）")

    fixtures = api_get("fixtures", {"date": args.date})
    print(f"fixtures?date={args.date} 返回 {len(fixtures)} 场（全联赛）")

    picked = []
    for fx in fixtures:
        cfg = BY_API_ID.get(fx.get("league", {}).get("id"))
        if not cfg:
            continue
        if fx.get("fixture", {}).get("status", {}).get("short") not in ("NS", "TBD"):
            continue
        picked.append((cfg, fx))
    picked.sort(key=lambda t: (t[0]["priority"], t[0]["api_id"],
                               t[1]["fixture"]["date"]))
    print(f"白名单过滤后 {len(picked)} 场（未开赛）")
    for cfg, fx in picked:
        lg = fx["league"]
        mark = "✓" if lg["id"] == cfg["api_id"] else "?"
        print(f"  [{mark}] {cfg['name']}(id={lg['id']}, api名={lg.get('name','')}) "
              f"{fx['teams']['home']['name']} vs {fx['teams']['away']['name']}")

    if args.dry_run:
        if not picked:
            print("无白名单比赛，dry-run 结束。")
            return
        cfg, fx = picked[0]
        print(f"\n--- dry-run 赔率样本：{fx['teams']['home']['name']} vs "
              f"{fx['teams']['away']['name']}（fixture {fx['fixture']['id']}）---")
        odds = api_get("odds", {"fixture": fx["fixture"]["id"], "bookmaker": BOOKMAKER_ID})
        mapped, flags = map_odds(odds)
        print(f"bets_found: {flags['bets_found']}")
        print(json.dumps(mapped, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in flags.items() if k != "bets_found"},
                         ensure_ascii=False))
        print(f"\ndry-run 共用 {_req_count} 次请求（未写任何文件）。")
        return

    if not picked:
        print("今天白名单内没有未开赛比赛。")
        return

    budget = remaining - RESULTS_RESERVE - _req_count
    budget_source = "goal" if args.no_history else source
    budget_compare_n = compare_n if (source == "compare" and not args.no_history) else 0
    max_matches = max_matches_for_budget(budget, budget_source, budget_compare_n)
    if len(picked) > max_matches:
        print(f"⚠️ 因 API-Football 预算截断：{len(picked)} 场按优先级仅可跑 "
              f"{max_matches} 场（可用预算 {budget}，h2h 源={source}，"
              f"预留 {RESULTS_RESERVE} 给赛果）。")
        picked = picked[:max_matches]

    goal_index, goal_rosters = {}, {}
    if not args.no_history and source in ("goal", "compare"):
        try:
            goal_rosters = load_or_fetch_goal_rosters(picked, args.output_dir)
        except Exception as e:
            print(f"⚠️ goal-api 球队名单异常：{type(e).__name__}；回退 fixture 索引")
            goal_rosters = {}
        if goal_rosters:
            print(f"goal-api 球队名单：{len(goal_rosters)} 个联赛"
                  f"（覆盖 {len(picked)} 场，零全天索引请求）")
        else:
            try:
                utc_dates = sorted({fx["fixture"]["date"][:10] for _, fx in picked})
                goal_index = goal_history.fetch_fixture_index(utc_dates)
                print(f"goal-api fixture 索引：{len(goal_index)} 场"
                      f"（UTC 日期 {', '.join(utc_dates)}）")
            except goal_history.GoalApiError as e:
                print(f"⚠️ goal-api fixture 索引失败（{e.kind}）：{e}；改为逐场解析 team id")
            except Exception as e:
                print(f"⚠️ goal-api fixture 索引异常：{type(e).__name__}；改为逐场解析 team id")

    rows, raws, history_cache = [], [], {}
    compare_records = []
    compare_done = 0
    counters = {}
    for cfg, fx in picked:
        fid = fx["fixture"]["id"]
        counters[cfg["prefix"]] = counters.get(cfg["prefix"], 0) + 1
        number = f"{cfg['prefix']}{counters[cfg['prefix']]:02d}"
        desc = f"{number} {fx['teams']['home']['name']} vs {fx['teams']['away']['name']}"

        try:
            odds = api_get("odds", {"fixture": fid, "bookmaker": BOOKMAKER_ID})
            mapped, flags = map_odds(odds)
        except Exception as e:
            print(f"  赔率失败 {desc}: {e}（跳过本场）")
            continue

        if not mapped["spf"]:
            print(f"  无胜平负赔率 {desc}（跳过本场）")
            continue

        hid = fx["teams"]["home"]["id"]
        aid = fx["teams"]["away"]["id"]
        history_entry, hist_source, hist_err, hist_goal = _empty_history(), "none", None, None
        hist_kickoff = fx["fixture"]["date"]
        hist_home = fx["teams"]["home"]["name"]
        hist_away = fx["teams"]["away"]["name"]
        hist_league = fx.get("league", {}).get("name", "")
        hist_league_id = (fx.get("league") or {}).get("id")
        if not args.no_history:
            history_entry, hist_source, hist_err, hist_goal = resolve_history(
                hist_kickoff, hist_home, hist_away, hist_league, hid, aid,
                goal_index, source, _api_history_budget_ok(remaining),
                roster=goal_rosters.get(str(hist_league_id)))
            history_cache[str(fid)] = history_entry

            if source == "compare" and compare_done < compare_n:
                compare_done += 1
                if hist_source == "api":
                    api_entry, api_err = history_entry, None
                elif hist_source != "goal" or not _api_history_budget_ok(remaining):
                    api_entry, api_err = None, "api_budget"
                else:
                    try:
                        api_entry, api_err = fetch_history(hid, aid), None
                    except Exception as e:
                        api_entry, api_err = _empty_history(), f"api_failed:{type(e).__name__}"
                goal_err = None if hist_source == "goal" else hist_err
                compare_records.append(build_compare_record(
                    number, fid, fx["teams"]["home"]["name"],
                    fx["teams"]["away"]["name"], hist_goal, api_entry,
                    goal_err, api_err))

        rows.append(build_row(fx, mapped, number, cfg))
        raws.append({"number": number, "fixture": fx, "odds_mapped": mapped,
                     "odds_flags": flags, "history_source": hist_source,
                     "history_error": hist_err})
        print(f"  ✓ {desc}（累计请求 {_req_count}，历史={hist_source}）")

    os.makedirs(args.output_dir, exist_ok=True)
    csv_path = save_csv(rows, args.date, args.output_dir)
    raw_path = os.path.join(args.output_dir, f"竞彩足球_{args.date}_raw.json")
    with open(raw_path, "w", encoding="utf-8") as fp:
        json.dump(raws, fp, ensure_ascii=False, indent=2)
    print(f"\nCSV 已保存：{csv_path}（{len(rows)} 场）")
    print(f"RAW 已保存：{raw_path}")

    if not args.no_history:
        hc_path = os.path.join(args.output_dir, f"history_cache_{args.date}.json")
        with open(hc_path, "w", encoding="utf-8") as fp:
            json.dump(history_cache, fp, ensure_ascii=False, indent=2)
        print(f"历史缓存已保存：{hc_path}（{len(history_cache)} 场）")

    if compare_records:
        cmp_path = save_compare_report(compare_records, args.date, args.output_dir)
        print(f"h2h 双源对照已保存：{cmp_path}（{len(compare_records)} 场）")

    print(f"本次共用 API-Football {_req_count} 次请求，"
          f"goal-api {goal_history.goal_request_count()} 次请求。")


if __name__ == "__main__":
    main()
