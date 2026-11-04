#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
半全场 / 总进球 双选对冲回测（一次性分析脚本，只读 CSV，不写回任何数据文件）

策略：
  取 AI 推荐的前 2 个选项，赔率 O1 / O2。
  S = 1/O1 + 1/O2
  - S < 1：荷兰式对冲，固定总投入 100
      金额1 = 100/(S*O1)，金额2 = 100/(S*O2)
      任一命中，回收恒为 100/S（>100）
  - S >= 1：放弃对冲，100 全仓买第 1 选项
  首选、次选都未命中 → 亏 100。

用法：python3 backtest_hedge_2pick.py
"""

import csv
import os

TOTAL_STAKE = 100.0
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_float(text):
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_signed(text):
    """解析带正负号的金额/盈亏，非法或空值返回 None。"""
    try:
        return float(str(text).strip().replace("+", ""))
    except (TypeError, ValueError):
        return None


def load_rows(filename, pred_col, actual_col):
    path = os.path.join(BASE_DIR, filename)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def backtest(filename, pred_col, actual_col, play_name):
    rows = load_rows(filename, pred_col, actual_col)
    settled = [r for r in rows if r[actual_col].strip()]
    unsettled = len(rows) - len(settled)

    valid = []
    invalid = []
    for r in rows:
        actual = r[actual_col].strip()
        if not actual:
            continue
        preds = [p.strip() for p in r[pred_col].split(" / ") if p.strip()]
        odds_raw = [o.strip() for o in r["各选项赔率"].split("|") if o.strip()]
        o1 = parse_float(odds_raw[0]) if len(odds_raw) >= 1 else None
        o2 = parse_float(odds_raw[1]) if len(odds_raw) >= 2 else None
        if len(preds) < 2 or o1 is None or o2 is None:
            invalid.append(r)
            continue
        valid.append((r, preds, o1, o2, actual))

    results = []
    for r, preds, o1, o2, actual in valid:
        s = 1.0 / o1 + 1.0 / o2
        if s < 1.0:
            path = "对冲"
            st1 = TOTAL_STAKE / (s * o1)
            st2 = TOTAL_STAKE / (s * o2)
            payout_if_hit = TOTAL_STAKE / s
            hit = actual in preds[:2]
            payout = payout_if_hit if hit else 0.0
            hit_option = preds[0] if actual == preds[0] else (preds[1] if actual == preds[1] else "")
        else:
            path = "单选"
            st1 = TOTAL_STAKE
            st2 = 0.0
            hit = actual == preds[0]
            payout_if_hit = TOTAL_STAKE * o1
            payout = payout_if_hit if hit else 0.0
            hit_option = preds[0] if hit else ""
        results.append(
            {
                "date": r["日期"],
                "num": r["编号"],
                "match": r["对阵"],
                "path": path,
                "s": s,
                "o1": o1,
                "o2": o2,
                "st1": st1,
                "st2": st2,
                "payout": payout,
                "payout_if_hit": payout_if_hit,
                "profit": payout - TOTAL_STAKE,
                "hit": hit,
                "hit_option": hit_option,
                "actual": actual,
                "pred1": preds[0],
                "pred2": preds[1],
                "baseline_profit": parse_signed(r["模拟盈亏"]) or 0.0,
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
            "baseline": sum(x["baseline_profit"] for x in items),
            "mean_s": sum(x["s"] for x in items) / len(items) if items else 0.0,
            # 假设各场命中率相同，总盈亏为 0 时所需的命中率
            "breakeven": staked / payout_if_hit if payout_if_hit else 0.0,
        }

    overall = summary(results)
    hedge = summary([x for x in results if x["path"] == "对冲"])
    single = summary([x for x in results if x["path"] == "单选"])

    # 极端区间：S < 0.95 才算明显有对冲价值
    strong_hedge = summary([x for x in results if x["path"] == "对冲" and x["s"] < 0.95])

    print(f"\n================ {play_name} ================")
    print(
        f"总比赛场次 {len(rows)}｜已开奖 {len(settled)}｜未开奖 {unsettled}｜"
        f"有效下注 {overall['n']}｜剔除缺赔率/不足2选 {len(invalid)}"
    )

    header = (
        "| 口径 | 场次 | 命中 | 命中率 | 平均S | 保本命中率 | "
        "总投入 | 总回收 | 盈亏 | 回报率 | 原3等分盈亏 |"
    )
    sep = "|---|---|---|---|---|---|---|---|---|---|---|"
    print(header)
    print(sep)

    def line(label, s):
        print(
            f"| {label} | {s['n']} | {s['hits']} | {s['hits']/s['n']*100 if s['n'] else 0:.1f}% | "
            f"{s['mean_s']:.3f} | {s['breakeven']*100:.1f}% | "
            f"{s['staked']:.0f} | {s['returned']:.2f} | {s['profit']:+.2f} | "
            f"{s['roi']*100:+.1f}% | {s['baseline']:+.2f} |"
        )

    line("全部有效", overall)
    line("对冲路径(S<1)", hedge)
    line("单选路径(S>=1)", single)
    line("强对冲(S<0.95)", strong_hedge)

    if invalid:
        print("\n剔除样例（最多 5 条）：")
        for r in invalid[:5]:
            print(
                f"  {r['日期']} {r['编号']} 预测={r[pred_col]!r} 赔率={r['各选项赔率']!r}"
            )

    return {
        "play": play_name,
        "overall": overall,
        "hedge": hedge,
        "single": single,
        "strong_hedge": strong_hedge,
        "invalid": len(invalid),
        "unsettled": unsettled,
        "results": results,
    }


def main():
    half = backtest("竞彩半全场模拟盘.csv", "AI半全场预测", "实际半全场", "半全场")
    goals = backtest("竞彩总进球模拟盘.csv", "AI总进球预测", "实际总进球数", "总进球")

    print("\n================ 汇总对比 ================")
    print(
        "| 玩法 | 有效场次 | 命中 | 命中率 | 保本命中率 | "
        "总投入 | 总回收 | 盈亏 | 回报率 | 原策略盈亏 | 差异 |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in (half, goals):
        s = r["overall"]
        print(
            f"| {r['play']} | {s['n']} | {s['hits']} | {s['hits']/s['n']*100:.1f}% | "
            f"{s['breakeven']*100:.1f}% | "
            f"{s['staked']:.0f} | {s['returned']:.2f} | {s['profit']:+.2f} | "
            f"{s['roi']*100:+.1f}% | {s['baseline']:+.2f} | {s['profit']-s['baseline']:+.2f} |"
        )

    print("\n说明：")
    print("- 有效下注 = 已开奖、且前 2 个选项均有有效赔率；两边统一按有效场次对比原策略盈亏。")
    print("- 原策略为现有模拟盘的 100 元三等分均注（模拟盈亏列），仅作对照基线。")
    print("- 对冲路径命中 = 实际结果落在首选/次选；单选路径命中 = 实际结果等于首选。")


if __name__ == "__main__":
    main()
