"""get_results_api.py — zucai_all 全量样本环境赛果回填（API-Football 版）。

从 output/竞彩足球_{date}_raw.json 取本销售日 matchId 集合，
GET fixtures?date={date}（1 次请求）取全天比赛，按 matchId 过滤，
仅收录 status 为 FT/AET 的场次，写 output/赛果_{date}.json：
  {matchId字符串: {"home","away","status","ft_home","ft_away","ht_home","ht_away"}}

用法：
  python get_results_api.py [--date YYYY-MM-DD] [--output-dir output]
  默认日期为昨天（赛后回填场景）。
"""

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta

import requests

BASE = "https://v3.football.api-sports.io"
FINISHED_STATUS = ("FT", "AET")

_req_count = 0  # 计额请求数（/status 免费，不计入）


def _key():
    key = os.environ.get("FOOTBALL_API_KEY", "").strip()
    if not key:
        sys.exit("错误：未设置 FOOTBALL_API_KEY 环境变量（请先 source ~/.zshrc）")
    return key


def api_get(path, params=None):
    global _req_count
    resp = None
    for attempt in range(4):
        resp = requests.get(
            f"{BASE}/{path}", params=params or {},
            headers={"x-apisports-key": _key()}, timeout=20,
        )
        if resp.status_code != 429:
            break
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


def load_match_ids(raw_path):
    """从 _raw.json 提取 {matchId字符串: 编号}。兼容 fixture 为完整对象或内层对象。"""
    with open(raw_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
    id_to_num = {}
    for item in raw_data:
        fx = item.get("fixture") or {}
        fid = (fx.get("fixture") or {}).get("id") or fx.get("id")
        if fid is None:
            continue
        id_to_num[str(fid)] = item.get("number", "")
    return id_to_num


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date",
                    default=(date.today() - timedelta(days=1)).strftime("%Y-%m-%d"))
    ap.add_argument("--output-dir", default="output")
    args = ap.parse_args()

    raw_path = os.path.join(args.output_dir, f"竞彩足球_{args.date}_raw.json")
    if not os.path.exists(raw_path):
        sys.exit(f"错误：找不到 {raw_path}，请先运行 get_matches_api.py")

    id_to_num = load_match_ids(raw_path)
    if not id_to_num:
        sys.exit(f"错误：{raw_path} 中没有任何 matchId")

    remaining = get_remaining()
    print(f"API 当日剩余请求：{remaining}（/status 不计额）")

    fixtures = api_get("fixtures", {"date": args.date})
    print(f"fixtures?date={args.date} 返回 {len(fixtures)} 场（全联赛），"
          f"本地待回填 {len(id_to_num)} 场")

    results, unfinished = {}, []
    for fx in fixtures:
        fid = str((fx.get("fixture") or {}).get("id", ""))
        if fid not in id_to_num:
            continue
        status = (fx.get("fixture") or {}).get("status", {}).get("short", "")
        home = (fx.get("teams") or {}).get("home", {}).get("name", "")
        away = (fx.get("teams") or {}).get("away", {}).get("name", "")
        if status not in FINISHED_STATUS:
            unfinished.append(f"{id_to_num[fid]} {home} vs {away}（{status}）")
            continue
        goals = fx.get("goals") or {}
        ht = ((fx.get("score") or {}).get("halftime")) or {}
        if goals.get("home") is None or goals.get("away") is None:
            unfinished.append(f"{id_to_num[fid]} {home} vs {away}（{status} 但无比分）")
            continue
        results[fid] = {
            "home": home,
            "away": away,
            "status": status,
            "ft_home": int(goals["home"]),
            "ft_away": int(goals["away"]),
            "ht_home": int(ht["home"]) if ht.get("home") is not None else None,
            "ht_away": int(ht["away"]) if ht.get("away") is not None else None,
        }

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, f"赛果_{args.date}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n收录完赛 {len(results)} 场 → {out_path}")
    for fid, r in sorted(results.items(), key=lambda kv: id_to_num.get(kv[0], "")):
        ht = (f"（半场 {r['ht_home']}:{r['ht_away']}）"
              if r["ht_home"] is not None else "")
        print(f"  {id_to_num.get(fid, fid)} {r['home']} {r['ft_home']}:{r['ft_away']} "
              f"{r['away']}{ht} [{r['status']}]")
    if unfinished:
        print(f"尚未完赛 {len(unfinished)} 场：")
        for line in unfinished:
            print(f"  ⏳ {line}")
    missing = set(id_to_num) - set(results) - {
        str((fx.get("fixture") or {}).get("id", "")) for fx in fixtures
        if str((fx.get("fixture") or {}).get("id", "")) in id_to_num}
    for fid in sorted(missing):
        print(f"  ⚠️ {id_to_num[fid]}（matchId={fid}）未出现在当日 fixtures 返回中")
    print(f"\n本次计额请求共 {_req_count} 次（/status 不计额）。")


if __name__ == "__main__":
    main()
