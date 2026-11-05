#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
总进球预测诊断（一次性分析脚本，只读 CSV，不写回任何数据文件）

回答四类问题：
  1. 热门翻车：首选是否为最低赔率、错场中热门翻车的占比
  2. 选 2 / 选 3 / 选 4 的覆盖率（第 4 选用市场最低剩余赔率做代理）
  3. 按联赛拆解命中率
  4. 极端进球区间（含 4+ vs 小比分）的命中率

用法：python3 analyze_total_goals.py
"""

import csv
import glob
import os
from collections import Counter, defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GOALS_FILE = os.path.join(BASE_DIR, "竞彩总进球模拟盘.csv")
ODDS_GLOB = os.path.join(BASE_DIR, "output", "竞彩足球_*.csv")

OPTIONS = [f"{i}球" for i in range(7)] + ["7+球"]


def goal_num(label):
    """把 '3球' / '7+球' 转成整数；'7+球' 记为 7。非法返回 None。"""
    label = label.strip()
    if label == "7+球":
        return 7
    if label.endswith("球"):
        try:
            return int(label[:-1])
        except ValueError:
            return None
    return None


def norm_actual(label):
    """实际赛果 7/8/9 球统一折算到体彩选项 7+球。"""
    n = goal_num(label)
    if n is None:
        return label.strip()
    return "7+球" if n >= 7 else f"{n}球"


def load_goal_rows():
    with open(GOALS_FILE, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_all_odds():
    """按 (MM-DD, 编号) 索引每日赛程赔率。"""
    table = {}
    for path in glob.glob(ODDS_GLOB):
        date = os.path.basename(path)[len("竞彩足球_"):-4][5:]
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                table[(date, row["编号"])] = row
    return table


def pct(a, b):
    return f"{a / b * 100:.1f}%" if b else "—"


def build_dataset(rows, odds_table):
    data, skipped = [], []
    align_checked = align_mismatch = 0
    for r in rows:
        actual_raw = r["实际总进球数"].strip()
        preds = [p.strip() for p in r["AI总进球预测"].split(" / ") if p.strip()]
        if not actual_raw or len(preds) < 3:
            skipped.append(r)
            continue
        key = (r["日期"], r["编号"])
        odds_row = odds_table.get(key)
        if odds_row is None:
            skipped.append(r)
            continue
        pred_odds = []
        odds_raw = [x.strip() for x in r["各选项赔率"].split("|")]
        for idx, p in enumerate(preds[:3]):
            try:
                pred_odds.append(float(odds_raw[idx]))
            except (ValueError, IndexError):
                pred_odds = []
                break
        if len(pred_odds) != 3:
            skipped.append(r)
            continue

        # 用每日赛程表的完整赔率复核预测赔率是否对齐
        full_odds = {}
        for opt in OPTIONS:
            try:
                full_odds[opt] = float(odds_row[f"总进球-{opt}"])
            except (KeyError, ValueError):
                pass
        for p, o in zip(preds[:3], pred_odds):
            align_checked += 1
            if abs(full_odds.get(p, -1) - o) > 0.02:
                align_mismatch += 1

        actual = norm_actual(actual_raw)
        data.append(
            {
                "date": r["日期"],
                "num": r["编号"],
                "league": r["联赛"],
                "match": r["对阵"],
                "confidence": r["信心指数"].strip(),
                "preds": preds[:3],
                "odds": pred_odds,
                "actual": actual,
                "full_odds": full_odds,
                "baseline_profit": float(r["模拟盈亏"].replace("+", ""))
                if r["模拟盈亏"].strip()
                else 0.0,
            }
        )
    print(
        f"数据：总行 {len(rows)}｜已开奖且可分析 {len(data)}｜剔除 {len(skipped)}"
        f"（未开奖/无推荐/缺赔率）"
    )
    print(
        f"赔率回填校验：{align_checked} 个预测赔率与赛程表比对，"
        f"{align_mismatch} 处不一致"
    )
    return data


def section_title(title):
    print(f"\n================ {title} ================")


def q1_hot_favorite(data):
    section_title("Q1 热门翻车")
    n = len(data)
    rank1 = sum(1 for d in data if d["actual"] == d["preds"][0])
    misses = [d for d in data if d["actual"] not in d["preds"]]
    print(
        f"首选命中 {rank1}/{n}（{pct(rank1, n)}）｜"
        f"三选全错 {len(misses)}/{n}（{pct(len(misses), n)}）"
    )

    # 定义 A：AI 三选内部赔率最低者 = 首选
    a_first_lowest = [d for d in data if d["preds"][0] == d["preds"][d["odds"].index(min(d["odds"]))]]
    a_lowest_hit = sum(1 for d in a_first_lowest if d["actual"] == d["preds"][0])
    a_miss_lowest = sum(
        1 for d in misses if d["preds"][0] == d["preds"][d["odds"].index(min(d["odds"]))]
    )

    # 定义 B：AI 首选恰为全场最低赔率（真·市场热门）
    b_fav = [d for d in data if d["full_odds"] and d["preds"][0] == min(d["full_odds"], key=d["full_odds"].get)]
    b_hit = sum(1 for d in b_fav if d["actual"] == d["preds"][0])
    b_miss = sum(
        1 for d in misses
        if d["full_odds"] and d["preds"][0] == min(d["full_odds"], key=d["full_odds"].get)
    )

    # 全场热门本身的命中率（与 AI 是否推荐无关）
    fav_hit = 0
    for d in data:
        if not d["full_odds"]:
            continue
        fav = min(d["full_odds"], key=d["full_odds"].get)
        if d["actual"] == fav:
            fav_hit += 1
    fav_total = sum(1 for d in data if d["full_odds"])

    print(
        "| 口径 | 场次 | 命中 | 命中率 |\n|---|---|---|---|"
    )
    print(f"| AI 首选（排名第一） | {n} | {rank1} | {pct(rank1, n)} |")
    print(
        f"| AI 首选=三选内最低赔率 | {len(a_first_lowest)} | {a_lowest_hit} | {pct(a_lowest_hit, len(a_first_lowest))} |"
    )
    print(
        f"| AI 首选=全场最低赔率（真热门） | {len(b_fav)} | {b_hit} | {pct(b_hit, len(b_fav))} |"
    )
    print(f"| 全场最低赔率选项本身命中 | {fav_total} | {fav_hit} | {pct(fav_hit, fav_total)} |")

    print(
        f"\n错场 {len(misses)} 场中：首选为三选内最低赔率 **{a_miss_lowest} 场"
        f"（{pct(a_miss_lowest, len(misses))}）**；首选为全场真热门 **{b_miss} 场"
        f"（{pct(b_miss, len(misses))}）**。"
    )
    print(
        f"首选恰为全场热门的场次共 {len(b_fav)} 场（占 {pct(len(b_fav), n)}），"
        f"这些场次里首选自身失败 {len(b_fav)-b_hit} 场（{pct(len(b_fav)-b_hit, len(b_fav))}）——"
        "热门首选翻车是常态，只是三选里的第 2/3 选偶尔接住了。"
    )
    print(
        f"参考：AI 三选整体覆盖 {sum(1 for d in data if d['actual'] in d['preds'])}/{n}"
        f"（{pct(sum(1 for d in data if d['actual'] in d['preds']), n)}）；"
        "上一轮策略报告的 42.9% 是“只买前 2 选”的覆盖率，不是三选命中率。"
    )
    print("注：以上“最低赔率”只依据模拟盘记录的 3 个推荐赔率；全场热门依据每日赛程表的 8 个总进球选项。")


def q2_coverage(data):
    section_title("Q2 选 2 / 选 3 / 选 4 覆盖率")
    n = len(data)

    def hit2(d):
        return d["actual"] in d["preds"][:2]

    def hit3(d):
        return d["actual"] in d["preds"]

    def proxy4(d):
        """AI 三选 + 第 4 选（取剩余选项中市场赔率最低者）。"""
        if hit3(d):
            return True
        rest = {k: v for k, v in d["full_odds"].items() if k not in d["preds"]}
        return bool(rest) and d["actual"] == min(rest, key=rest.get)

    def market_topk(d, k):
        ranked = sorted(d["full_odds"], key=d["full_odds"].get)
        return d["actual"] in ranked[:k]

    rows = [
        ("AI 选 2（去掉第 3 选）", sum(hit2(d) for d in data)),
        ("AI 选 3（现状）", sum(hit3(d) for d in data)),
        ("AI 选 3 + 市场补第 4 选（代理）", sum(proxy4(d) for d in data)),
        ("市场赔率前 2", sum(market_topk(d, 2) for d in data)),
        ("市场赔率前 3", sum(market_topk(d, 3) for d in data)),
        ("市场赔率前 4", sum(market_topk(d, 4) for d in data)),
    ]
    print("| 选法 | 命中 | 命中率 | 相对选 3 变化 |\n|---|---|---|---|")
    base = rows[1][1]
    for label, hit in rows:
        delta = f"{(hit - base) / n * 100:+.1f}pp" if label != "AI 选 3（现状）" else "—"
        print(f"| {label} | {hit}/{n} | {pct(hit, n)} | {delta} |")

    rank = Counter()
    for d in data:
        if d["actual"] in d["preds"]:
            rank[d["preds"].index(d["actual"]) + 1] += 1
        else:
            rank["3 选外"] += 1
    print(
        "\n实际结果落在 AI 排名："
        f"第 1 选 {rank[1]}｜第 2 选 {rank[2]}｜第 3 选 {rank[3]}｜三选外 {rank['3 选外']}"
    )
    print("说明：数据里 AI 只输出 3 个总进球选项，没有真实“第 4 选”；第 4 选用市场最低剩余赔率代理，仅用于估算扩容上限。")

    # AI 三选与市场赔率前三的重合度：判断 AI 是否只是复述市场
    same_set = same2 = 0
    overlap = Counter()
    for d in data:
        market = sorted(d["full_odds"], key=d["full_odds"].get)
        same_set += set(d["preds"]) == set(market[:3])
        same2 += set(d["preds"][:2]) == set(market[:2])
        overlap[len(set(d["preds"]) & set(market[:3]))] += 1
    print(
        f"\nAI 三选与市场赔率前三：集合完全相同 {same_set}/{n}（{pct(same_set, n)}）；"
        f"AI 前二与市场前二相同 {same2}/{n}（{pct(same2, n)}）。"
    )
    print(
        "重合个数分布："
        + "｜".join(f"重合 {k} 个 {overlap[k]} 场" for k in sorted(overlap, reverse=True))
    )


def q3_leagues(data):
    section_title("Q3 按联赛拆解")
    groups = defaultdict(list)
    for d in data:
        groups[d["league"]].append(d)
    stats = []
    for league, items in groups.items():
        hits = sum(1 for d in items if d["actual"] in d["preds"])
        profit = sum(d["baseline_profit"] for d in items)
        stats.append((league, len(items), hits, hits / len(items), profit))
    stats.sort(key=lambda x: (-x[3], -x[1]))

    print("| 联赛 | 场次 | 命中 | 命中率 | 盈亏(三选均注) | 回报率 | 样本判断 |\n|---|---|---|---|---|---|---|")
    for league, n, hits, rate, profit in stats:
        if n >= 10:
            flag = "可参考"
        elif n >= 5:
            flag = "样本偏小"
        else:
            flag = "样本过小"
        roi = profit / (n * 100) * 100
        print(
            f"| {league} | {n} | {hits} | {rate*100:.1f}% | {profit:+.0f} | "
            f"{roi:+.1f}% | {flag} |"
        )

    high = [s for s in stats if s[1] >= 10 and s[3] > 0.60]
    low = [s for s in stats if s[1] >= 10 and s[3] < 0.30]
    print(
        "\n命中率 >60%（场次≥10）："
        + ("、".join(f"{x[0]} {x[3]*100:.0f}%({x[1]}场, {x[4]:+.0f}元)" for x in high) or "无")
    )
    print(
        "命中率 <30%（场次≥10）："
        + ("、".join(f"{x[0]} {x[3]*100:.0f}%({x[1]}场, {x[4]:+.0f}元)" for x in low) or "无")
    )
    if high:
        hn = sum(x[1] for x in high)
        hh = sum(x[2] for x in high)
        hp = sum(x[4] for x in high)
        print(
            f"高命中联赛合计：{hn} 场，命中 {hh}（{pct(hh, hn)}），盈亏 {hp:+.0f} 元"
            f"（ROI {hp/(hn*100)*100:+.1f}%）；全部样本 {len(data)} 场，命中率 "
            f"{pct(sum(1 for d in data if d['actual'] in d['preds']), len(data))}，"
            f"盈亏 {sum(d['baseline_profit'] for d in data):+.0f} 元。"
        )
    print("注：每场胜负是独立样本，10 场规模的 95% 置信区间约 ±30pp，联赛差异很容易是噪声。")


def q4_extremes(data):
    section_title("Q4 极端进球区间")

    def group_of(d, key):
        if key == "max":
            n = max(goal_num(p) for p in d["preds"])
        else:
            n = goal_num(d["preds"][0])
        if n is None:
            return "无法判定"
        if n <= 2:
            return "小比分（≤2球）"
        if n == 3:
            return "3球"
        return "含 4+ 球"

    for key, title in (("max", "按“三选最高进球数”分组"), ("first", "按“首选进球数”分组")):
        groups = defaultdict(list)
        for d in data:
            groups[group_of(d, key)].append(d)
        print(
            f"\n{title}：\n| 分组 | 场次 | 命中 | 命中率 | 实际打出 4+ 球占比 | 盈亏(三选均注) | 回报率 |"
            "\n|---|---|---|---|---|---|---|"
        )
        for name in ["小比分（≤2球）", "3球", "含 4+ 球", "无法判定"]:
            items = groups.get(name, [])
            if not items:
                continue
            hits = sum(1 for d in items if d["actual"] in d["preds"])
            big = sum(1 for d in items if goal_num(d["actual"]) >= 4)
            profit = sum(d["baseline_profit"] for d in items)
            print(
                f"| {name} | {len(items)} | {hits} | {hits/len(items)*100:.1f}% | "
                f"{big/len(items)*100:.1f}% | {profit:+.0f} | {profit/(len(items)*100)*100:+.1f}% |"
            )

    # AI 推荐本身的集中度：判断是不是“模板化推荐”
    first_dist = Counter(d["preds"][0] for d in data)
    includes_3 = sum(1 for d in data if "3球" in d["preds"])
    print("\n首选分布：" + "｜".join(f"{k} {v} 场" for k, v in first_dist.most_common()))
    print(
        f"三选必含 3球：{includes_3}/{len(data)}（{pct(includes_3, len(data))}）；"
        f"首选为 3球或 2球：{first_dist['3球'] + first_dist['2球']}/{len(data)}"
        f"（{pct(first_dist['3球'] + first_dist['2球'], len(data))}）"
    )

    # 反向视角：按实际进球区间看覆盖率——定位“准/不准”发生在哪一段
    actual_bucket = defaultdict(list)
    for d in data:
        k = goal_num(d["actual"])
        bucket = "0-1 球" if k <= 1 else ("2-3 球" if k <= 3 else "4+ 球")
        actual_bucket[bucket].append(d)
    print("\n按实际进球区间反向看覆盖率：\n| 实际区间 | 场次 | 命中 | 覆盖率 |\n|---|---|---|---|")
    for bucket in ["0-1 球", "2-3 球", "4+ 球"]:
        items = actual_bucket[bucket]
        hits = sum(1 for d in items if d["actual"] in d["preds"])
        print(f"| {bucket} | {len(items)} | {hits} | {hits/len(items)*100:.1f}% |")

    miss_bucket = Counter()
    for d in data:
        if d["actual"] in d["preds"]:
            continue
        k = goal_num(d["actual"])
        miss_bucket["0-1 球" if k <= 1 else ("2-3 球" if k <= 3 else "4+ 球")] += 1
    total_miss = sum(miss_bucket.values())
    print(
        f"\n错场 {total_miss} 场的实际落点：0-1 球 {miss_bucket['0-1 球']} 场"
        f"（{pct(miss_bucket['0-1 球'], total_miss)}）｜2-3 球 {miss_bucket['2-3 球']} 场"
        f"（{pct(miss_bucket['2-3 球'], total_miss)}）｜4+ 球 {miss_bucket['4+ 球']} 场"
        f"（{pct(miss_bucket['4+ 球'], total_miss)}）"
    )

    # 实际总进球分布与预测最大值的关系（诊断“看不准”的方向）
    groups = defaultdict(Counter)
    for d in data:
        n = max(goal_num(p) for p in d["preds"])
        bucket = "≤2" if n <= 2 else ("3" if n == 3 else "≥4")
        groups[bucket][d["actual"]] += 1
    print("\n实际结果分布（按三选最高进球数分组，取前三）：")
    for bucket in ["≤2", "3", "≥4"]:
        top = groups[bucket].most_common(3)
        print(f"  预测最高 {bucket}：{', '.join(f'{k} {v}场' for k, v in top)}")


def main():
    rows = load_goal_rows()
    odds = load_all_odds()
    data = build_dataset(rows, odds)
    q1_hot_favorite(data)
    q2_coverage(data)
    q3_leagues(data)
    q4_extremes(data)


if __name__ == "__main__":
    main()
