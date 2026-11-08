"""get_matches_flashscore.py — zucai_all 赛程入口（Apify Flashscore 版）。

流程：
  1. 调 Apify Actor 拉当日「未开赛」比赛（赔率 + 赛程），原始响应归档
  2. 按 Flashscore 显示名白名单过滤 → 只保留 CST 当日开球
  3. 沿用竞彩排除：只读主环境 output/竞彩足球_{date}.csv，
     「联赛中文名 + 开球 5 分钟分桶」同桶数量相等才整组剔除
  4. 按联赛优先级 + 开球时间排序，截断到每日上限（默认 60 场）
  5. goal-api 取 h2h/近况（联赛球队名单缓存优先，缺失回退全天索引）
  6. 写 output/竞彩足球_{date}.csv 与 _raw.json（列与主环境完全同构），
     history_cache_{date}.json；数据层写 data/odds/{date}.jsonl
  7. 对被排除的竞彩重合场次（默认 3 场）另写 output/双口径校验_{date}.json

API-Football 在本环境已隔离：本脚本不发起任何 API-Football 请求。

用法：
  python get_matches_flashscore.py [--date YYYY-MM-DD] [--max-matches 60]
                                  [--dry-run] [--no-history]
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from collections import defaultdict

import flashscore_odds as fo
import goal_history
from leagues_config import BY_FLASHSCORE, FLASHSCORE_COMPETITIONS

MAIN_ENV_OUTPUT_DIR = os.environ.get(
    "MAIN_ENV_OUTPUT_DIR", "/Users/mac/Desktop/zucai/output")
JC_KICKOFF_WINDOW_MINUTES = 5
DEFAULT_MAX_MATCHES = 60
CALIBRATION_N = 3  # 竞彩重合场次双口径校验条数（0 = 关闭）
ROSTER_CACHE = "goal_rosters.json"
ROSTER_TTL_DAYS = 7


# ---------------- 通用 ----------------

def _norm_text(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


_ALIAS = {"czechia": "czechrepublic"}


def _norm_display(display):
    text = _norm_text(display)
    for bad, good in _ALIAS.items():
        text = text.replace(bad, good)
    return text


_NORM_CFG = {}
for _display, _cfg in BY_FLASHSCORE.items():
    _NORM_CFG[_norm_display(_display)] = _cfg


def resolve_league_cfg(display):
    """Flashscore display_name -> 白名单 cfg；未收录返回 None。"""
    cfg = BY_FLASHSCORE.get(display)
    if cfg:
        return cfg
    return _NORM_CFG.get(_norm_display(display))


def _empty_history():
    return {"home_recent": [], "away_recent": [], "h2h": [],
            "home_recent_text": "", "away_recent_text": "", "h2h_text": "",
            "available": False}


def _utc_date(kickoff_utc):
    dt = datetime.fromisoformat(str(kickoff_utc).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _kickoff_bucket(kickoff):
    minute = kickoff.minute - kickoff.minute % JC_KICKOFF_WINDOW_MINUTES
    return kickoff.replace(minute=minute, second=0, microsecond=0)


# ---------------- 竞彩排除（只读主环境产物） ----------------

def _parse_kickoff(date_str, time_str):
    raw = f"{(date_str or '').strip()} {(time_str or '').strip()}"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


def load_jc_rows(target_date, main_dir=None):
    """读主环境当日竞彩 CSV（只读）；缺失返回 []（视为主环境尚未跑批）。"""
    path = os.path.join(main_dir or MAIN_ENV_OUTPUT_DIR,
                        f"竞彩足球_{target_date}.csv")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as fp:
            return list(csv.DictReader(fp))
    except OSError:
        return []


def exclude_jc(records, jc_rows):
    """同桶数量相等才整组剔除；返回 (保留, 被剔除, 统计)。"""
    sold_counts = defaultdict(int)
    for row in jc_rows:
        kickoff = _parse_kickoff(row.get("开赛日期"), row.get("开赛时间"))
        league = (row.get("联赛") or "").strip()
        if kickoff and league:
            sold_counts[(league, _kickoff_bucket(kickoff))] += 1

    groups = defaultdict(list)
    for rec in records:
        groups[(rec["cfg"]["name"], _kickoff_bucket(rec["kickoff_dt"]))].append(rec)

    kept, excluded, ambiguous = [], [], 0
    for key, items in groups.items():
        n_sold = sold_counts.get(key, 0)
        if n_sold == len(items):
            excluded.extend(items)
        else:
            if n_sold:
                ambiguous += 1
            kept.extend(items)
    stats = {"sold": len(jc_rows), "excluded": len(excluded),
             "ambiguous": ambiguous, "kept": len(kept)}
    return kept, excluded, stats


# ---------------- goal-api 历史 ----------------

def _read_roster_cache(path):
    try:
        with open(path, encoding="utf-8") as fp:
            payload = json.load(fp)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def load_rosters(goal_ids, data_dir="data"):
    """按 goal 联赛 id 拉球队名单并缓存（7 天）；单个失败不影响其他联赛。"""
    path = os.path.join(data_dir, ROSTER_CACHE)
    cache = _read_roster_cache(path)
    leagues = cache.get("leagues") if isinstance(cache.get("leagues"), dict) else {}
    try:
        age = (date.today() - date.fromisoformat(str(cache.get("updated")))).days
        fresh = 0 <= age < ROSTER_TTL_DAYS
    except (TypeError, ValueError):
        fresh = False

    rosters, changed = {}, False
    for gid in sorted({g for g in goal_ids if g}):
        key = str(gid)
        entry = leagues.get(key) or {}
        teams = entry.get("teams") or []
        if fresh and teams:
            rosters[key] = [(str(t[0]), str(t[1])) for t in teams
                            if isinstance(t, (list, tuple)) and len(t) == 2]
            continue
        try:
            fetched = goal_history.fetch_league_roster(gid)
        except goal_history.GoalApiError as e:
            print(f"  ⚠️ goal 球队名单失败（联赛 {gid}，{e.kind}）：{e}")
            if teams:
                rosters[key] = [(str(t[0]), str(t[1])) for t in teams
                                if isinstance(t, (list, tuple)) and len(t) == 2]
            continue
        if fetched:
            rosters[key] = fetched
            leagues[key] = {"goal_id": gid,
                            "teams": [[n, i] for n, i in fetched]}
            changed = True

    if changed or not os.path.exists(path):
        os.makedirs(data_dir, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"updated": str(date.today()), "leagues": leagues},
                          fp, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"  ⚠️ goal 球队名单缓存写入失败：{type(e).__name__}")
    return rosters


def goal_league_id(cfg):
    return cfg.get("goal_league_id") or goal_history.league_goal_id(cfg.get("api_id"))


def attach_history(records, data_dir="data"):
    """为每场补 h2h/近况，返回 {matchId: entry} 与来源统计。"""
    goal_ids = {goal_league_id(r["cfg"]) for r in records} - {None, ""}
    rosters = load_rosters(goal_ids, data_dir)

    need_index_dates = {_utc_date(r["kickoff_utc"]) for r in records
                        if not rosters.get(str(goal_league_id(r["cfg"])))} 
    index = {}
    if need_index_dates:
        try:
            index = goal_history.fetch_fixture_index(sorted(need_index_dates))
        except goal_history.GoalApiError as e:
            print(f"  ⚠️ goal 全天索引失败（{e.kind}），无名单联赛的历史将不可用：{e}")

    cache, stats = {}, defaultdict(int)
    for rec in records:
        gid = goal_league_id(rec["cfg"])
        roster = rosters.get(str(gid)) if gid else None
        entry, source, err = _empty_history(), "none", None
        try:
            entry = goal_history.fetch_history_goal(
                rec["kickoff_utc"], rec["home"], rec["away"],
                rec["league_display"], index, roster)
            source = "goal" if entry.get("available") else "none"
            if not entry.get("available"):
                err = "empty"
        except goal_history.GoalApiError as e:
            err = e.kind
        except Exception as e:  # 历史失败绝不能拖垮赛程主流程
            err = f"unexpected:{type(e).__name__}"
        cache[rec["match_id"]] = entry
        rec["history_source"] = source
        rec["history_error"] = err
        stats[source] += 1
    return cache, dict(stats)


# ---------------- 双口径校验（竞彩重合场次） ----------------

def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def jc_odds(row):
    """主环境竞彩 CSV 行 -> 五玩法赔率（与 odds_mapped 同结构）。"""
    def pick(prefix, keys):
        return {k: _float(row.get(f"{prefix}-{k}")) for k in keys
                if _float(row.get(f"{prefix}-{k}")) is not None}

    return {
        "spf": pick("胜平负", ("主胜", "平", "客胜")),
        "rq": {"让球数": (row.get("让球数") or "").strip(),
               **pick("让球", ("主胜", "平", "客胜"))},
        "ttg": pick("总进球", fo.TTG_NAMES),
        "hafu": pick("半全场", [f"半{a}-全{b}" for a in ("主胜", "平", "客胜")
                              for b in ("主胜", "平", "客胜")]),
        "crs": pick("比分", fo.CRS_SCORES),
    }


def _diff_block(jc, ap):
    diffs = {}
    for k, v in jc.items():
        if k == "让球数":
            continue
        w = ap.get(k)
        if isinstance(v, dict):
            sub = {kk: round(abs(vv - w[kk]), 3) for kk, vv in v.items()
                   if isinstance(w, dict) and isinstance(w.get(kk), (int, float))}
            if sub:
                diffs[k] = sub
        elif isinstance(v, (int, float)) and isinstance(w, (int, float)):
            diffs[k] = round(abs(v - w), 3)
    return diffs


def build_calibration(excluded, jc_rows, target_date, limit=CALIBRATION_N):
    """竞彩已售场次 vs Apify 推导赔率的并排对照（只校验，不进分析链路）。"""
    if limit <= 0 or not excluded:
        return None
    by_bucket = defaultdict(list)
    for rec in excluded:
        by_bucket[(rec["cfg"]["name"], _kickoff_bucket(rec["kickoff_dt"]))].append(rec)

    items = []
    for row in jc_rows:
        if len(items) >= limit:
            break
        kickoff = _parse_kickoff(row.get("开赛日期"), row.get("开赛时间"))
        league = (row.get("联赛") or "").strip()
        if not kickoff or not league:
            continue
        cands = by_bucket.get((league, _kickoff_bucket(kickoff)) , [])
        if len(cands) != 1:
            continue  # 同桶多场无法唯一配对，跳过而不猜
        rec = cands[0]
        jc, ap = jc_odds(row), rec["norm"]["odds_mapped"]
        items.append({
            "league": league,
            "home": rec["home"],
            "away": rec["away"],
            "kickoff": rec["kickoff_dt"].strftime("%Y-%m-%d %H:%M"),
            "jc_让球数": jc["rq"].get("让球数", ""),
            "apify_让球数": ap["rq"].get("让球数", ""),
            "rq_line_matched": jc["rq"].get("让球数", "").replace("+", "") ==
                               ap["rq"].get("让球数", "").replace("+", ""),
            "odds": {
                "jc": jc,
                "apify": ap,
                "abs_diff": {play: _diff_block(jc[play], ap[play])
                             for play in ("spf", "rq", "ttg", "hafu", "crs")},
            },
            "odds_flags": rec["norm"]["odds_flags"],
        })
    if not items:
        return None
    return {
        "date": target_date,
        "generated_at": datetime.now(fo.CST).isoformat(timespec="seconds"),
        "note": "竞彩重合场次双口径校验：只比对赔率/让球线，不进入 M1/M2 分析。",
        "items": items,
    }


# ---------------- 落盘 ----------------

def save_csv(rows, target_date, output_dir):
    fieldnames = list(rows[0].keys())
    path = os.path.join(output_dir, f"竞彩足球_{target_date}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def save_raw(records, target_date, output_dir, meta):
    raws = []
    for rec, number in records:
        cfg = rec["cfg"]
        raws.append({
            "number": number,
            "fixture": {
                "fixture": {"id": rec["match_id"], "date": rec["kickoff_utc"],
                            "status": {"short": "NS"}},
                "league": {"id": cfg.get("api_id"), "name": cfg["name"],
                           "flashscore": rec["league_display"]},
                "teams": {"home": {"name": rec["home"]},
                          "away": {"name": rec["away"]}},
            },
            "odds_mapped": rec["norm"]["odds_mapped"],
            "odds_flags": rec["norm"]["odds_flags"],
            "history_source": rec.get("history_source", "none"),
            "history_error": rec.get("history_error"),
        })
    path = os.path.join(output_dir, f"竞彩足球_{target_date}_raw.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump({"date": target_date,
                   "generated_at": datetime.now(fo.CST).isoformat(timespec="seconds"),
                   "source": "apify:flashscore",
                   "run_id": meta.get("run_id", ""),
                   "items": raws}, fp, ensure_ascii=False, indent=2)
    return path


def save_json(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    return path


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--max-matches", type=int, default=DEFAULT_MAX_MATCHES)
    ap.add_argument("--fetch-max", type=int, default=0,
                    help="Actor 返回上限；0 = 自动（分析上限×2，至少 60）")
    ap.add_argument("--calibration-n", type=int, default=CALIBRATION_N)
    ap.add_argument("--exclude-jc", action="store_true",
                    help="剔除竞彩已售同场（默认不剔除；本环境只要求白名单内、日限 60）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--output-dir", default="output")
    ap.add_argument("--main-env-output", default=MAIN_ENV_OUTPUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="只抓取/标准化，不写任何文件")
    ap.add_argument("--no-history", action="store_true", help="跳过 goal-api 历史")
    args = ap.parse_args()
    target_date = args.date

    if not args.no_history and not os.environ.get("GOAL_API_KEY", "").strip():
        sys.exit("错误：未设置 GOAL_API_KEY 环境变量（h2h 需要 goal-api；"
                 "或加 --no-history）")

    print(f"[1/6] 拉取 Apify Flashscore：{target_date}，"
          f"白名单 {len(FLASHSCORE_COMPETITIONS)} 个联赛")
    items, meta = fo.fetch_items(
        FLASHSCORE_COMPETITIONS, days=[target_date], status="scheduled",
        max_results=args.fetch_max or max(args.max_matches * 2, 60))
    print(f"  Actor 返回 {len(items)} 场（run_id={meta.get('run_id') or '-'}）")

    if not args.dry_run:
        raw_path = fo.save_raw(items, meta, target_date, args.data_dir)
        print(f"  原始响应已归档：{raw_path}")

    # 2) 白名单 + CST 当日过滤 + 标准化
    records, unknown_names, bad_items = [], defaultdict(int), 0
    for item in items:
        display = str((item.get("competition") or {}).get("display_name") or "")
        cfg = resolve_league_cfg(display)
        if not cfg:
            unknown_names[display] += 1
            continue
        try:
            norm = fo.normalize_item(item, cfg)
        except ValueError:
            bad_items += 1
            continue
        kickoff_dt = fo._cst_dt(norm["kickoff_utc"]).replace(tzinfo=None)
        if kickoff_dt.strftime("%Y-%m-%d") != target_date:
            continue
        records.append({
            "cfg": cfg, "norm": norm, "match_id": norm["match_id"],
            "home": norm["home"], "away": norm["away"],
            "league_display": norm["league_display"],
            "kickoff_utc": norm["kickoff_utc"], "kickoff_dt": kickoff_dt,
        })
    print(f"[2/6] 白名单命中 {len(records)} 场；无胜平负赔率跳过 {bad_items} 场")
    if unknown_names:
        top = sorted(unknown_names.items(), key=lambda x: -x[1])[:6]
        print("  未收录的 competition.display_name（如需纳入请补 leagues_config）：")
        for name, n in top:
            print(f"    - {name}（{n} 场）")

    if not records:
        sys.exit("今日白名单内无可分析比赛，结束。")

    # 3) 竞彩重合场次：默认**不剔除**（本环境只要求白名单内、日限 60，不追求与体彩一一对应）；
    #    仍用配对结果生成双口径校验；--exclude-jc 时才真正剔除。
    jc_rows = load_jc_rows(target_date, args.main_env_output)
    overlap, ex_stats = [], {"sold": len(jc_rows), "excluded": 0, "ambiguous": 0}
    if jc_rows:
        _, overlap, ex_stats = exclude_jc(records, jc_rows)
    if args.exclude_jc:
        kept = [r for r in records if r not in overlap]
        print(f"[3/6] 竞彩排除（--exclude-jc）：主环境 {ex_stats['sold']} 场 → 剔除 "
              f"{len(overlap)} 场，歧义保留 {ex_stats['ambiguous']} 组，剩余 {len(kept)} 场")
    else:
        kept = list(records)
        if jc_rows:
            print(f"[3/6] 竞彩重合 {len(overlap)} 场（仅用于双口径校验，不剔除）；"
                  f"主环境 {ex_stats['sold']} 场")
        else:
            print("[3/6] 未找到主环境当日赛程，跳过双口径校验")

    if not kept:
        sys.exit("无可分析场次，结束。")

    # 4) 优先级 + 开球时间排序，截断
    kept.sort(key=lambda r: (r["cfg"]["priority"], r["kickoff_dt"], r["match_id"]))
    capped = kept[:max(0, args.max_matches)]
    print(f"[4/6] 截断：{len(kept)} 场 → 每日上限 {args.max_matches} 场，"
          f"实际保留 {len(capped)} 场")
    if len(capped) < len(kept):
        dropped = kept[len(capped):]
        print(f"  超出上限未分析 {len(dropped)} 场（不消耗模型调用）")

    # 5) 历史
    if args.no_history:
        history_cache = {}
        for rec in capped:
            history_cache[rec["match_id"]] = _empty_history()
            rec["history_source"], rec["history_error"] = "none", "disabled"
        print("[5/6] 历史：已跳过（--no-history）")
    else:
        print("[5/6] goal-api 历史：h2h + 近况")
        history_cache, hist_stats = attach_history(capped, args.data_dir)
        print(f"  历史来源：{hist_stats}；goal-api 请求 {goal_history.goal_request_count()} 次")

    # 6) 落盘
    numbered = [(rec, f"{rec['cfg']['prefix']}{i + 1:02d}")
                for i, rec in enumerate(capped)]
    rows = [fo.build_schedule_row(rec["norm"], number)
            for rec, number in numbered]
    od_records = [{**rec["norm"],
                   "number": number,
                   "league": rec["cfg"]["name"],
                   "source_batch": "B",
                   "history_source": rec.get("history_source", "none"),
                   "history_error": rec.get("history_error"),
                   "run_id": meta.get("run_id", "")}
                  for rec, number in numbered]

    if args.dry_run:
        print("[6/6] --dry-run：不写文件。映射抽样：")
        for row in rows[:3]:
            print(f"  {row['编号']} {row['联赛']} {row['主队']} vs {row['客队']} "
                  f"{row['开赛日期']} {row['开赛时间']} | 让球 {row['让球数']} "
                  f"| 主胜 {row['胜平负-主胜']} | 0球 {row['总进球-0球']}")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    csv_path = save_csv(rows, target_date, args.output_dir)
    raw_path = save_raw(numbered, target_date, args.output_dir, meta)
    hc_path = save_json(
        os.path.join(args.output_dir, f"history_cache_{target_date}.json"),
        history_cache)
    jsonl_path = fo.write_odds_jsonl(od_records, target_date, args.data_dir)
    print(f"[6/6] CSV：{csv_path}（{len(rows)} 场）")
    print(f"      RAW：{raw_path}")
    print(f"      历史缓存：{hc_path}")
    print(f"      赔率 JSONL：{jsonl_path}")

    calib = build_calibration(overlap, jc_rows, target_date, args.calibration_n)
    if calib:
        cal_path = save_json(
            os.path.join(args.output_dir, f"双口径校验_{target_date}.json"), calib)
        print(f"      双口径校验：{cal_path}（{len(calib['items'])} 场）")
    print(f"\n每日上限 {args.max_matches} 场；API-Football 请求：0")


if __name__ == "__main__":
    main()
