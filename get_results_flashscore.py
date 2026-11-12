"""get_results_flashscore.py — 用 Apify Flashscore 回填赛果（替代 get_results_api.py）。

输入：
  output/竞彩足球_{date}.csv       当日赛程（编号/联赛/主队/客队/matchId）
  output/竞彩足球_{date}_raw.json  competition.display_name（用于精确复抓）
  data/odds/{date}.jsonl           兜底读取 league_display
输出：
  output/赛果_{date}.json          键为「联赛|主队|客队」，含全场/半场比分
  data/raw/{date}/apify_results_*.json  原始响应归档

只抓 status=finished 的比赛；未完赛/推迟的场次跳过并在控制台列出。
全程不发起任何 API-Football 请求。

用法：python get_results_flashscore.py [--date YYYY-MM-DD]，默认昨天。
"""

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta

import flashscore_odds as fo
from leagues_config import BY_FLASHSCORE


def load_schedule(path):
    with open(path, "r", encoding="utf-8-sig") as fp:
        return list(csv.DictReader(fp))


def pending_leagues(samples_path, d):
    """该日样本的结算情况，返回 (是否已有样本, 未结算联赛短名集合)。

    已全部结算时集合为空 —— 说明这场日期的回填已完成，无需再请求 Actor。
    """
    if not os.path.exists(samples_path):
        return False, set()
    rows = []
    with open(samples_path, "r", encoding="utf-8-sig") as fp:
        for rec in csv.DictReader(fp):
            if rec.get("date") == d:
                rows.append(rec)
    if not rows:
        return False, set()
    pending = {str(r.get("league") or "").strip() for r in rows
               if not str(r.get("ft_home") or "").strip()}
    return True, pending


def competitions_from_raw(raw_path, jsonl_path):
    comps = []
    if os.path.exists(raw_path):
        try:
            with open(raw_path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
            for item in payload.get("items", []):
                if not isinstance(item, dict):
                    continue
                league = ((item.get("fixture") or {}).get("league") or {})
                name = str(league.get("flashscore") or "").strip()
                if name and name not in comps:
                    comps.append(name)
        except (OSError, ValueError):
            pass
    if not comps and os.path.exists(jsonl_path):
        try:
            with open(jsonl_path, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    name = str(rec.get("league_display") or "").strip()
                    if name and name not in comps:
                        comps.append(name)
        except (OSError, ValueError):
            pass
    return comps


def _is_finished(item):
    return str(item.get("status") or "").strip().lower() in ("finished", "ft", "aet", "pen")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=(date.today() - timedelta(days=1)).strftime("%Y-%m-%d"))
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--output-dir", default="output")
    ap.add_argument("--max-results", type=int, default=0,
                    help="Actor 返回上限；0 = 自动（场次×3，至少 60）")
    ap.add_argument("--comps", default="",
                    help="逗号分隔的 Flashscore competition.display_name；"
                         "不传则从 _raw.json / jsonl 自动推断")
    args = ap.parse_args()
    d = args.date

    sched_path = os.path.join(args.output_dir, f"竞彩足球_{d}.csv")
    if not os.path.exists(sched_path):
        sys.exit(f"错误：找不到 {sched_path}（赛果回填需要当日赛程）")
    sched = load_schedule(sched_path)
    if args.comps.strip():
        comps = [c.strip() for c in args.comps.split(",") if c.strip()]
    else:
        comps = competitions_from_raw(
            os.path.join(args.output_dir, f"竞彩足球_{d}_raw.json"),
            os.path.join(args.data_dir, "odds", f"{d}.jsonl"))
    if not comps:
        sys.exit("错误：无法解析 competition.display_name（可传 --comps 手动指定）")

    # 已全部结算 → 不再请求（重复跑同一天时省掉整轮赛果额度）
    has_rows, pending = pending_leagues(
        os.path.join(args.data_dir, "samples.csv"), d)
    if has_rows and not pending:
        print(f"[跳过] {d} 的样本已全部结算，不再请求 Actor")
        return 0
    if has_rows:
        keep = [c for c in comps
                if str(BY_FLASHSCORE.get(c, {}).get("name") or "").strip() in pending]
        if keep and len(keep) < len(comps):
            print(f"  仅回填未结算联赛：{len(keep)}/{len(comps)} 个")
            comps = keep

    used = fo.monthly_usage(args.data_dir)
    print(f"  {fo.quota_message(used)}")
    if used >= fo.MONTHLY_QUOTA - fo.QUOTA_RESERVE:
        print(f"[跳过] 剩余额度不足 {fo.QUOTA_RESERVE} 场预留，本轮不回填赛果")
        return 0

    # Actor 的 days 以 UTC 日切分，且比赛的 UTC 归属日可能跨赛程的 CST 销售日：
    #   - 早开球（CST 上午/下午）→ UTC 同 d 的凌晨 → 用 prev,d
    #   - 晚开球（CST 凌晨）→ UTC 前一日傍晚 → 用 prev,d
    #   - 刚完赛的场次可能在 Actor 索引里滞后一天 → 补 next
    # 用三天 [prev, d, next] 覆盖所有可能的 UTC 归属；多余场次会在匹配阶段被过滤。
    prev = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    next_day = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    days = [prev, d, next_day]
    max_results = args.max_results or max(len(sched) * 5, 160)
    print(f"[1/3] 拉取 Flashscore 完赛结果：{d}，{len(comps)} 个联赛，"
          f"日期 {days}，上限 {max_results}")
    items, meta = fo.fetch_items(comps, days=days, status="finished",
                                 max_results=max_results)
    raw_path = fo.save_raw(items, meta, d, args.data_dir, prefix="apify_results")
    print(f"  Actor 返回 {len(items)} 场；原始响应：{raw_path}")

    by_id = {str(it.get("id")): it for it in items if it.get("id")}
    by_teams = {(fo.norm_team_name((it.get("home") or {}).get("name")),
                 fo.norm_team_name((it.get("away") or {}).get("name"))): it
                for it in items if it.get("home") and it.get("away")}

    results, missing, skipped = {}, [], []
    for row in sched:
        mid = str(row.get("matchId", "") or "").strip()
        item = by_id.get(mid) or by_teams.get(
            (fo.norm_team_name(row.get("主队")), fo.norm_team_name(row.get("客队"))))
        if not item:
            missing.append(f"{row.get('编号')} {row.get('主队')}-{row.get('客队')}")
            continue
        if not _is_finished(item) or item.get("home_score") is None:
            skipped.append(f"{row.get('编号')} {row.get('主队')}-{row.get('客队')}")
            continue
        ht_h, ht_a = fo.half_time_scores(item)
        key = f"{row.get('联赛', '')}|{row.get('主队', '')}|{row.get('客队', '')}"
        results[key] = {
            "matchId": mid,
            "league": row.get("联赛", ""),
            "home": row.get("主队", ""),
            "away": row.get("客队", ""),
            "ft_home": int(item["home_score"]),
            "ft_away": int(item["away_score"]),
            "ht_home": ht_h,
            "ht_away": ht_a,
            "status": str(item.get("status") or ""),
        }

    out_path = os.path.join(args.output_dir, f"赛果_{d}.json")
    existing = {}
    if os.path.exists(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as fp:
                existing = json.load(fp)
        except (OSError, ValueError):
            existing = {}
    existing.update(results)
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(existing, fp, ensure_ascii=False, indent=2)

    print(f"[2/3] 赛果写入 {out_path}：{len(results)} 场"
          f"（赛程 {len(sched)} 场，无对应比赛 {len(missing)}，未完赛 {len(skipped)}）")
    if missing:
        print("  无对应比赛：" + "；".join(missing[:8]) + (" …" if len(missing) > 8 else ""))
    if skipped:
        print("  未完赛/无比分：" + "；".join(skipped[:8]) + (" …" if len(skipped) > 8 else ""))
    print("[3/3] API-Football 请求：0")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
