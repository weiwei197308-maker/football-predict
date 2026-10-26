#!/usr/bin/env python3
"""30 天模拟盘汇总统计。

读取 4 张模拟盘 CSV（模拟30 / 竞彩总进球 / 竞彩半全场 / 竞彩比分），
纯 Python 客观计算，生成 30天数据汇总报告.md。不调用任何大模型。

结算口径（源自各表 tracker 的既有规则，报告中原样说明）：
- 模拟30：每场让球单注 100，让球命中=是 → 盈亏=100×(赔率-1)，否 → -100；
  胜平负仅记录命中、未实投，其"盈亏"为等额 100 按赔率的虚拟结算，报告中明确标注。
- 其余三表：每场 100 等额分摊到各预测选项，盈亏为表内"模拟盈亏"列原值。
"""
import csv
import glob
import os
import re
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "30天数据汇总报告.md")

MAIN_CSV = "模拟30.csv"
PLAY_CSVS = {
    "总进球": "竞彩总进球模拟盘.csv",
    "半全场": "竞彩半全场模拟盘.csv",
    "比分": "竞彩比分模拟盘.csv",
}
STAKE = 100.0


def read_csv(name):
    with open(os.path.join(ROOT, name), encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def to_f(s):
    s = (s or "").strip()
    try:
        return float(s)  # float 原生接受 "+89"/"-100"
    except ValueError:
        return None


def hit3(v):
    v = (v or "").strip()
    return True if v == "是" else False if v == "否" else None


def rate(hits):
    h, t = hits[True], hits[True] + hits[False]
    return (h / t) if t else None


def pct(x):
    return "—" if x is None else f"{x * 100:.1f}%"


def money(x):
    return "—" if x is None else f"{x:+.2f}".rstrip("0").rstrip(".")


def load_main():
    """主表：一行一场，让球实投 + 胜平负虚拟。"""
    rows = []
    for r in read_csv(MAIN_CSV):
        odds_h, odds_w = to_f(r.get("让球赔率")), to_f(r.get("胜平负赔率"))
        h_hcp, h_wdl = hit3(r.get("让球命中")), hit3(r.get("胜平负命中"))
        pl_hcp = to_f(r.get("模拟盈亏"))
        # 胜平负虚拟结算：等额 100 按赔率
        pl_wdl = None
        if h_wdl is not None and odds_w is not None:
            pl_wdl = round(STAKE * (odds_w - 1), 2) if h_wdl else -STAKE
        # 胜平负虚拟口径下，让球命中是否结算不影响该虚拟列；但未开奖场（让球未结算）排除
        settled = h_hcp is not None
        rows.append({
            "日期": r["日期"], "编号": r["编号"], "联赛": r["联赛"],
            "信心": (r.get("信心指数") or "").strip(),
            "hcp_hit": h_hcp, "hcp_pl": pl_hcp, "hcp_settled": settled,
            "wdl_hit": h_wdl, "wdl_pl": pl_wdl,
            "归因": (r.get("错题标签(归因)") or "").strip(),
        })
    return rows


def load_play(name, fname):
    """单玩法表：命中列='是否命中'，盈亏列='模拟盈亏'。"""
    rows = []
    for r in read_csv(fname):
        h = hit3(r.get("是否命中"))
        pl = to_f(r.get("模拟盈亏"))
        rows.append({
            "日期": r["日期"], "编号": r["编号"], "联赛": r["联赛"],
            "信心": (r.get("信心指数") or "").strip(),
            "hit": h, "pl": pl, "settled": h is not None,
        })
    return rows


def overall_block():
    lines = ["## 一、整体盈亏（4 张表）", ""]
    lines.append("| 表 | 场次 | 已结算 | 未开奖 | 总投入 | 总盈亏 | 回报率 |")
    lines.append("|---|---|---|---|---|---|---|")
    for label, rows, pl_key, st_key in [
        ("模拟30（让球实投）", main_rows, "hcp_pl", "hcp_settled"),
        ("竞彩总进球", play_rows["总进球"], "pl", "settled"),
        ("竞彩半全场", play_rows["半全场"], "pl", "settled"),
        ("竞彩比分", play_rows["比分"], "pl", "settled"),
    ]:
        settled = [r for r in rows if r[st_key]]
        inv = sum(STAKE for r in settled)
        pl = sum(r[pl_key] or 0 for r in settled)
        roi = pl / inv if inv else None
        lines.append(f"| {label} | {len(rows)} | {len(settled)} | {len(rows)-len(settled)} | {inv:.0f} | {money(pl)} | {pct(roi)} |")
    lines.append("")
    lines.append("> 口径：每场模拟投注 100；模拟30 实投玩法为让球（胜平负未实投，其盈亏见下方虚拟口径）；其余三表盈亏取表内原值。回报率 = 总盈亏 ÷ 总投入。")
    lines.append("")
    return lines


def conf_block():
    lines = ["## 二、信心指数分层（让球 / 胜平负）", ""]
    buckets = ["5", "6", "7", "8", "9+"]

    def bk(v):
        return "9+" if (v.isdigit() and int(v) >= 9) else (v if v in buckets else "")

    acc = defaultdict(lambda: {
        "hcp": Counter(), "hcp_pl": 0.0,
        "wdl": Counter(), "wdl_pl": 0.0, "n": 0})
    for r in main_rows:
        b = bk(r["信心"])
        if not b:
            continue
        a = acc[b]
        a["n"] += 1
        if r["hcp_settled"]:
            a["hcp"][r["hcp_hit"]] += 1
            a["hcp_pl"] += r["hcp_pl"] or 0
        if r["wdl_hit"] is not None:
            a["wdl"][r["wdl_hit"]] += 1
            a["wdl_pl"] += r["wdl_pl"] or 0

    lines.append("### 让球（实投口径）")
    lines.append("")
    lines.append("| 信心 | 场次 | 命中 | 未中 | 命中率 | 总盈亏 | 回报率 |")
    lines.append("|---|---|---|---|---|---|---|")
    for b in buckets:
        if b not in acc:
            continue
        a = acc[b]
        inv = STAKE * (a["hcp"][True] + a["hcp"][False])
        roi = a["hcp_pl"] / inv if inv else None
        lines.append(f"| {b} 分 | {a['n']} | {a['hcp'][True]} | {a['hcp'][False]} | {pct(rate(a['hcp']))} | {money(a['hcp_pl'])} | {pct(roi)} |")
    unk = sum(1 for r in main_rows if bk(r["信心"]) == "")
    if unk:
        lines.append(f"| 未标注 | {unk} | — | — | — | — | — |")
    lines.append("")

    lines.append("### 胜平负（虚拟口径：等额 100 按赔率结算，非实投）")
    lines.append("")
    lines.append("| 信心 | 场次 | 命中 | 未中 | 命中率 | 虚拟盈亏 | 虚拟回报率 |")
    lines.append("|---|---|---|---|---|---|---|")
    for b in buckets:
        if b not in acc:
            continue
        a = acc[b]
        inv = STAKE * (a["wdl"][True] + a["wdl"][False])
        roi = a["wdl_pl"] / inv if inv else None
        lines.append(f"| {b} 分 | {a['n']} | {a['wdl'][True]} | {a['wdl'][False]} | {pct(rate(a['wdl']))} | {money(a['wdl_pl'])} | {pct(roi)} |")
    lines.append("")
    lines.append("> 阅读提示：让球为真实模拟盈亏；胜平负命中率为真实判定，但盈亏为「假设每场均单注 100」的虚拟结算，仅用于横向对比，不代表实际下注收益。")
    lines.append("")
    return lines


def league_block():
    lines = ["## 三、联赛红黑榜（让球实投口径）", ""]
    acc = defaultdict(lambda: {"hit": Counter(), "pl": 0.0, "n": 0})
    for r in main_rows:
        if not r["hcp_settled"]:
            continue
        a = acc[r["联赛"]]
        a["n"] += 1
        a["hit"][r["hcp_hit"]] += 1
        a["pl"] += r["hcp_pl"] or 0
    stat = sorted(
        ((lg, v) for lg, v in acc.items()),
        key=lambda kv: (kv[1]["pl"], rate(kv[1]["hit"]) or 0), reverse=True)
    lines.append(f"共 {len(stat)} 个联赛有已结算让球场次。")
    lines.append("")

    def row(lg, v):
        inv = STAKE * (v["hit"][True] + v["hit"][False])
        return (f"| {lg} | {v['n']} | {v['hit'][True]} | {pct(rate(v['hit']))} "
                f"| {money(v['pl'])} | {pct(v['pl'] / inv if inv else None)} |")

    lines.append("最赚钱 3 个联赛：")
    lines.append("")
    lines.append("| 联赛 | 场次 | 命中 | 命中率 | 总盈亏 | 回报率 |")
    lines.append("|---|---|---|---|---|---|")
    lines.extend(row(lg, v) for lg, v in stat[:3])
    lines.append("")
    lines.append("最坑人 3 个联赛：")
    lines.append("")
    lines.append("| 联赛 | 场次 | 命中 | 命中率 | 总盈亏 | 回报率 |")
    lines.append("|---|---|---|---|---|---|")
    lines.extend(row(lg, v) for lg, v in stat[-3:])
    lines.append("")
    small = [f"{lg}({v['n']}场)" for lg, v in stat[:3] + stat[-3:] if v["n"] < 3]
    if small:
        lines.append(f"> 注意：{'、'.join(small)} 样本过小，结论仅供参考。")
        lines.append("")
    return lines


def play_block():
    lines = ["## 四、玩法红黑榜", ""]
    lines.append("| 玩法 | 口径 | 场次 | 已结算 | 命中 | 命中率 | 总盈亏 | 回报率 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    stat = []
    for label, rows, pl_key in [
        ("让球", main_rows, "hcp_pl"),
        ("胜平负", main_rows, "wdl_pl"),
        ("总进球", play_rows["总进球"], "pl"),
        ("半全场", play_rows["半全场"], "pl"),
        ("比分", play_rows["比分"], "pl"),
    ]:
        hit_key = "hcp_hit" if label == "让球" else ("wdl_hit" if label == "胜平负" else "hit")
        settled = [r for r in rows if r[hit_key] is not None and (label in ("让球", "胜平负") or r["settled"])]
        hits = Counter(r[hit_key] for r in settled)
        pl = sum(r[pl_key] or 0 for r in settled)
        inv = STAKE * len(settled)
        stat.append((label, len(rows), len(settled), hits[True], rate(hits), pl, pl / inv if inv else None))
        mark = "虚拟" if label == "胜平负" else "实投"
        lines.append(f"| {label} | {mark} | {len(rows)} | {len(settled)} | {hits[True]} | {pct(rate(hits))} | {money(pl)} | {pct(pl / inv if inv else None)} |")
    lines.append("")
    real = [s for s in stat if s[0] != "胜平负"]
    best_hr = max(real, key=lambda s: s[4] or 0)
    worst_pl = min(real, key=lambda s: s[5])
    lines.append(f"- 实投玩法中**命中率最高**：{best_hr[0]}（{pct(best_hr[4])}）；")
    lines.append(f"- 实投玩法中**盈亏最差**：{worst_pl[0]}（{money(worst_pl[5])}）。")
    lines.append("")
    lines.append("> 胜平负为虚拟口径，未纳入「实投红黑榜」结论，仅供对比。")
    lines.append("")
    return lines


# 错题高频词：主题词 → 关键词组（子串匹配，一场命中一次）
TOPIC_KEYWORDS = [
    ("伤病", ["伤病", "受伤", "伤停", "缺阵", "伤疑"]),
    ("红牌", ["红牌", "染红", "罚下", "十人应战", "少打一人"]),
    ("停赛", ["停赛", "禁赛"]),
    ("轮换轮休", ["轮换", "轮休", "替补阵容"]),
    ("门将失误", ["门将失误", "守门员失误", "黄油手", "扑球脱手"]),
    ("裁判判罚", ["误判", "漏判", "错判", "判罚争议", "点球争议", "VAR"]),
    ("盘口异动", ["盘口", "水位", "赔率异动"]),
    ("天气场地", ["天气", "雨战", "雪战", "大风", "场地", "草皮"]),
    ("数据缺失", ["数据缺失", "无数据", "资料缺失", "信息不足"]),
    ("模型高估", ["高估"]),
    ("模型低估", ["低估"]),
    ("剧本判断错", ["剧本"]),
    ("体能/状态", ["体能", "疲惫", "状态低迷", "专注", "赛程密集"]),
    ("效率/终结", ["效率", "把握机会", "终结", "射正", "浪费机会", "转化率"]),
    ("运气", ["运气", "折射", "门线", "门柱"]),
    ("开局/绝杀", ["闪击", "开局", "绝杀", "补时", "压哨"]),
]


def keyword_block():
    lines = ["## 五、错题高频词（归因语料统计）", ""]
    deep_cells, old_cells = [], []
    for r in main_rows:
        a = r["归因"]
        if not a:
            continue
        if a.startswith("【深度归因】"):
            deep_cells.append(a)
        else:
            old_cells.append(a)
    lines.append(f"语料：模拟30.csv 错题标签(归因)列 —— 深度归因 {len(deep_cells)} 场、旧归因 {len(old_cells)} 场"
                 f"（同一场可命中多个主题词；仅统计让球/胜平负所涉场次）。")
    lines.append("")
    lines.append("| 主题词 | 深度归因命中 | 旧归因命中 | 合计 |")
    lines.append("|---|---|---|---|")
    stat = []
    for topic, kws in TOPIC_KEYWORDS:
        d = sum(1 for a in deep_cells if any(k in a for k in kws))
        o = sum(1 for a in old_cells if any(k in a for k in kws))
        stat.append((topic, d, o))
    for topic, d, o in sorted(stat, key=lambda x: x[1] + x[2], reverse=True):
        lines.append(f"| {topic} | {d} | {o} | {d + o} |")
    lines.append("")
    lines.append("> 阅读提示：本表统计「归因文本中出现该主题词的错题场次」，非互斥（一场可含多个主题词）；"
                 "「高估/低估/剧本」为模型判断类词汇，其余为真实意外因素。")
    lines.append("")
    return lines


def main():
    global main_rows, play_rows
    main_rows = load_main()
    play_rows = {name: load_play(name, fname) for name, fname in PLAY_CSVS.items()}

    report = ["# 30 天模拟盘数据汇总报告", ""]
    dates = sorted({r["日期"] for r in main_rows})
    report.append(f"- 数据范围：{dates[0]} ~ {dates[-1]}，共 {len(dates)} 个销售日、{len(main_rows)} 场（模拟30 口径）。")
    report.append("- 生成方式：summarize_30days.py 纯 Python 客观统计，无大模型参与。")
    report.append("")
    report.extend(overall_block())
    report.extend(conf_block())
    report.extend(league_block())
    report.extend(play_block())
    report.extend(keyword_block())

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    print(f"已生成: {OUT_PATH}")
    # 终端同步输出关键数字
    for line in report:
        if line.startswith("|") or line.startswith(("#", "- ")):
            print(line)


if __name__ == "__main__":
    main()
