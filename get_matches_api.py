"""get_matches_api.py — zucai_all 全量样本环境赛程抓取（API-Football 版）。

产出与 get_matches.py 完全同构的 output/竞彩足球_{date}.csv 与 _raw.json，
另写 output/history_cache_{date}.json（供 M1/M2 分析时零请求读取历史数据）。

请求预算（Free 100/天）：
  fixtures 1 次 + 每场 odds 1 次 + 每场历史 3 次（两队 last=5 + h2h）；
  先查 /status（不计额），预留 RESULTS_RESERVE 给赛果回填，超额按联赛优先级截断。

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

from leagues_config import BY_API_ID

BASE = "https://v3.football.api-sports.io"
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


def _key():
    key = os.environ.get("FOOTBALL_API_KEY", "").strip()
    if not key:
        sys.exit("错误：未设置 FOOTBALL_API_KEY 环境变量（请先 source ~/.zshrc）")
    return key


def api_get(path, params):
    global _req_count
    resp = None
    for attempt in range(4):
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

    per_match = 1 if args.no_history else 2
    max_matches = max(0, (remaining - RESULTS_RESERVE - _req_count) // per_match)
    if len(picked) > max_matches:
        print(f"⚠️ 预算不足：{len(picked)} 场需 {len(picked)*per_match} 请求，"
              f"仅可跑 {max_matches} 场，按优先级截断。")
        picked = picked[:max_matches]

    rows, raws, history_cache = [], [], {}
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

        rows.append(build_row(fx, mapped, number, cfg))
        raws.append({"number": number, "fixture": fx, "odds_mapped": mapped,
                     "odds_flags": flags})

        if not args.no_history:
            try:
                hid = fx["teams"]["home"]["id"]
                aid = fx["teams"]["away"]["id"]
                season = fx.get("league", {}).get("season")
                history_cache[str(fid)] = fetch_history(hid, aid, season)
            except Exception as e:
                print(f"  历史失败 {desc}: {e}（本场无历史）")
                history_cache[str(fid)] = {"available": False}
        print(f"  ✓ {desc}（累计请求 {_req_count}）")

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

    print(f"本次共用 {_req_count} 次请求。")


if __name__ == "__main__":
    main()
