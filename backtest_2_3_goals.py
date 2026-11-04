#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
“盲狙 2-3 球”策略回测（一次性分析脚本，只读 CSV，不写回任何数据文件）

策略：
  不看 AI 推荐，直接锚定 2球 / 3球，赔率取自每日赛程 CSV。
  S = 1/O2 + 1/O3
  - S < 1：荷兰式对冲，总投入 100，任一命中回收 100/S
  - S >= 1：全仓 100 买两者中赔率较高者
  2球、3球都没中 → 亏 100。

用法：python3 backtest_2_3_goals.py
"""

import csv
import glob
import os
from collections import Counter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GOALS_FILE = os.path.join(BASE_DIR, "竞彩总进球模拟盘.csv")
ODDS_GLOB = os.path.join(BASE_DIR, "output", "竞彩足球_*.csv")
TOTAL_STAKE = 100.0
TARGETS = ("2球", "3球")


def norm_actual(label):
    """7/8/9 球折算为体彩选项 7+球；其余 X球 原样返回。"""
    label = label.strip()
    if label == "7+球":
        return label
    if label.endswith("球"):
        try:
            n = int(label[:-1])
        except ValueError:
            return label
        return "7+球" if n >= 7 else f"{n}球"
    return label


def load_rows():
    with open(GOALS_FILE, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_odds():
    table = {}
    for path in glob.glob(ODDS_GLOB):
        date = os.path.basename(path)[len("竞彩足球_"):-4][5:]
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                table[(date, row["编号"])] = row
    return table


def main():
    rows = load_rows()
    odds = load_odds()
    settled = [r for r in rows if r["实际总进球数"].strip()]
    unsettled = len(rows) - len(settled)

    bets, skipped = [], 0
    for r in rows:
        actual_raw = r["实际总进球数"].strip()
        if not actual_raw:
            continue
        o = odds.get((r["日期"], r["编号"]))
        if o is None:
            skipped += 1
            continue
        try:
            o2 = float(o["总进球-2球"])
            o3 = float(o["总进球-3球"])
        except (KeyError, ValueError):
            skipped += 1
            continue
        if o2 <= 0 or o3 <= 0:
            skipped += 1
            continue
        actual = norm_actual(actual_raw)
        s = 1 / o2 + 1 / o3
        if s < 1:
            path = "对冲"
            # 荷兰式：两注合计 100，命中时回收 100/S
            st2 = TOTAL_STAKE / (s * o2)
            st3 = TOTAL_STAKE / (s * o3)
            hit = actual in TARGETS
            payout_if_hit = TOTAL_STAKE / s
        else:
            path = "单选"
            # 全仓买赔率较高者
            pick = "2球" if o2 >= o3 else "3球"
            st2 = TOTAL_STAKE if pick == "2球" else 0.0
            st3 = TOTAL_STAKE if pick == "3球" else 0.0
            hit = actual == pick
            payout_if_hit = TOTAL_STAKE * (o2 if pick == "2球" else o3)
        bets.append(
            {
                "date": r["日期"],
                "num": r["编号"],
                "league": r["联赛"],
                "o2": o2,
                "o3": o3,
                "s": s,
                "path": path,
                "st2": st2,
                "st3": st3,
                "hit": hit,
                "payout": payout_if_hit if hit else 0.0,
                "payout_if_hit": payout_if_hit,
                "profit": (payout_if_hit - TOTAL_STAKE) if hit else -TOTAL_STAKE,
                "baseline_profit": float(r["模拟盈亏"].replace("+", ""))
                if r["模拟盈亏"].strip()
                else 0.0,
                "actual": actual,
            }
        )

    def summary(items):
        staked = TOTAL_STAKE * len(items)
        returned = sum(x["payout"] for x in items)
        profit = returned - staked
        hits = sum(1 for x in items if x["hit"])
        payout_if_hit = sum(x["payout_if_hit"] for x in items)
        return {
            "n": len(items),
            "hits": hits,
            "staked": staked,
            "returned": returned,
            "profit": profit,
            "roi": profit / staked if staked else 0.0,
            "mean_s": sum(x["s"] for x in items) / len(items) if items else 0.0,
            "breakeven": staked / payout_if_hit if payout_if_hit else 0.0,
            "baseline": sum(x["baseline_profit"] for x in items),
        }

    overall = summary(bets)
    hedge = summary([x for x in bets if x["path"] == "对冲"])
    single = summary([x for x in bets if x["path"] == "单选"])

    print("================ 盲狙 2-3 球 ================")
    print(
        f"总比赛场次 {len(rows)}｜已开奖 {len(settled)}｜未开奖 {unsettled}｜"
        f"有效下注 {overall['n']}｜剔除缺赔率 {skipped}"
    )
    print(
        "| 口径 | 场次 | 命中 | 命中率 | 平均S | 保本命中率 | 总投入 | 总回收 | 盈亏 | 回报率 | 对比AI三选盈亏 |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")

    def line(label, s):
        print(
            f"| {label} | {s['n']} | {s['hits']} | {s['hits']/s['n']*100 if s['n'] else 0:.1f}% | "
            f"{s['mean_s']:.3f} | {s['breakeven']*100:.1f}% | "
            f"{s['staked']:.0f} | {s['returned']:.2f} | {s['profit']:+.2f} | "
            f"{s['roi']*100:+.1f}% | {s['profit']-s['baseline']:+.2f} |"
        )

    line("全部有效", overall)
    line("对冲路径(S<1)", hedge)
    line("单选路径(S>=1)", single)

    print(
        f"\n路径分布：对冲 {hedge['n']} 场｜单选 {single['n']} 场；"
        f"S 区间 [{min(x['s'] for x in bets):.3f}, {max(x['s'] for x in bets):.3f}]"
    )

    actual_bucket = Counter()
    for x in bets:
        n = int(x["actual"].replace("7+球", "7").replace("球", ""))
        actual_bucket["0-1 球" if n <= 1 else ("2-3 球" if n <= 3 else "4+ 球")] += 1
    print(
        "实际落点："
        + "｜".join(f"{k} {actual_bucket[k]} 场" for k in ["0-1 球", "2-3 球", "4+ 球"])
    )

    print(
        "\n按联赛拆分（仅列场次≥10）：\n| 联赛 | 场次 | 命中 | 命中率 | 盈亏 | 回报率 |"
        "\n|---|---|---|---|---|---|"
    )
    league_stats = {}
    for x in bets:
        s = league_stats.setdefault(x["league"], {"n": 0, "hits": 0, "profit": 0.0})
        s["n"] += 1
        s["hits"] += 1 if x["hit"] else 0
        s["profit"] += x["profit"]
    for league, s in sorted(league_stats.items(), key=lambda kv: -kv[1]["profit"]):
        if s["n"] < 10:
            continue
        print(
            f"| {league} | {s['n']} | {s['hits']} | {s['hits']/s['n']*100:.1f}% | "
            f"{s['profit']:+.0f} | {s['profit']/(s['n']*100)*100:+.1f}% |"
        )

    print(
        f"\n结论：总投入 {overall['staked']:.0f} 元，总回收 {overall['returned']:.2f} 元，"
        f"盈亏 {overall['profit']:+.2f} 元，回报率 {overall['roi']*100:+.1f}%。"
    )
    print(
        f"保本需要 {overall['breakeven']*100:.1f}% 命中率，实际 {overall['hits']/overall['n']*100:.1f}%；"
        f"同期 AI 三选策略盈亏 {overall['baseline']:+.2f} 元。"
    )


if __name__ == "__main__":
    main()
