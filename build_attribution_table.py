#!/usr/bin/env python3
"""错题归因总表生成器（独立脚本，项目第 5 张表）。

遍历 output/每日记录_*.json 的所有场次 × 5 玩法，命中严格为 False（v is False）
才产一行；命中 key 缺失或为 None 不产行（与 tracker 的 bool(get) 口径不同，见对账说明）。

归因来源：
  - 该场存在有效深度归因 → "深度"：摘要与根因分类**仅**取【失败根因】段
    （【事实依据】【与旧归因的差异】不参与分类——差异段里的"高估/低估"是元描述）；
  - 否则（无深度文件 / 调用失败占位文本 / 缺【失败根因】段）→ 安全降级为 "旧"，
    取每日记录"错误归因"原文。

输出 错题归因总表.csv（tracker 类产物，允许覆盖重写；覆盖前打印行数变化）。
注意：同场多玩法行共享同一场级归因，统计频次须按场次去重。
"""
import csv
import glob
import json
import os
import re

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
TARGET_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "错题归因总表.csv")

PLAYBOOKS = ["胜平负", "让球", "总进球", "半全场", "比分"]
CSV_HEADER = ["日期", "编号", "联赛", "对阵", "玩法", "AI预测", "实际赛果", "归因来源", "根因分类", "归因摘要"]
SUMMARY_MAX = 60

DEEP_FAIL_PREFIX = "(深度归因调用失败"
UNCLASSIFIED = "未分类"

# 关键词 → 根因分类（按枚举固定顺序；多类命中全部保留，按本表顺序用 | 连接）。
# 宁漏勿错：不配裸词"裁判"/"门将"；"水位"有天气语境歧义，已知保留。
ROOT_CAUSE_RULES = [
    ("伤病", ["伤", "伤病", "受伤", "伤停", "伤退", "缺阵", "双缺"]),
    ("红牌", ["红牌", "染红", "罚下", "少打一人", "十人应战"]),
    ("轮换轮休", ["轮换", "轮休", "替补阵容"]),
    ("门将失误", ["门将失误", "黄油手", "守门员失误", "扑球脱手"]),
    ("裁判误判", ["误判", "漏判", "错判", "裁判误判", "裁判漏判", "判罚争议", "点球争议", "VAR"]),
    ("盘口异动", ["盘口", "赔率异动", "水位"]),
    ("天气场地", ["天气", "场地", "雨战", "雪战", "大风", "草皮"]),
    ("数据缺失", ["数据缺失", "无数据", "资料缺失", "信息不足"]),
    ("模型高估", ["高估"]),
    ("模型低估", ["低估"]),
    ("剧本判断错", ["剧本"]),
]

_DATE_RE_RECORD = re.compile(r"每日记录_(\d{4}-\d{2}-\d{2})\.json$")
_DATE_RE_DEEP = re.compile(r"深度归因_(\d{4}-\d{2}-\d{2})\.json$")


def load_json(path):
    """读取 JSON；失败打印警告并返回 None（跳过该文件，不中断整体）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [警告] 读取 {path} 失败: {e}（跳过该文件）")
        return None


def load_deep_attributions(output_dir=OUTPUT_DIR):
    """读所有 深度归因_<date>.json → {日期: {编号: 深度归因原文}}。

    仅认无后缀的标准文件名（带 --suffix 另存的变体不进总表）；
    调用失败占位文本不算有效深度归因。
    """
    deep = {}
    for path in sorted(glob.glob(os.path.join(output_dir, "深度归因_*.json"))):
        m = _DATE_RE_DEEP.search(os.path.basename(path))
        if not m:
            continue
        data = load_json(path)
        if data is None:
            continue
        per_match = {}
        for item in data.get("场次", []):
            num = str(item.get("编号") or "").strip()
            text = item.get("深度归因")
            if not num or not isinstance(text, str):
                continue
            text = text.strip()
            if not text or text.startswith(DEEP_FAIL_PREFIX):
                continue
            per_match[num] = text
        deep[m.group(1)] = per_match
    return deep


def extract_root_cause_section(deep_text):
    """从深度归因全文中提取【失败根因】段（到下一个【…】小节标题或文末为止）。

    找不到该段时返回 ""（调用方据此安全降级为旧来源）。
    """
    if not isinstance(deep_text, str):
        return ""
    m = re.search(r"【失败根因】(.*?)(?=【|$)", deep_text, re.S)
    return m.group(1).strip() if m else ""


def fold(text):
    """折叠所有空白（含换行）为单空格，保持 CSV 单元格单行。"""
    return " ".join(str(text).split())


def truncate(text, limit=SUMMARY_MAX):
    """折叠为单行后压到 ≤limit 字，超出截断并加 "…"。"""
    text = fold(text)
    return text if len(text) <= limit else text[:limit] + "…"


def classify(text):
    """关键词规则分类：多类命中按 ROOT_CAUSE_RULES 枚举顺序用 | 连接；都不中 → 未分类。"""
    if not text:
        return UNCLASSIFIED
    cats = [cat for cat, kws in ROOT_CAUSE_RULES if any(kw in text for kw in kws)]
    return "|".join(cats) if cats else UNCLASSIFIED


def build_rows(output_dir=OUTPUT_DIR):
    """遍历所有每日记录，产出错题行列表（一场一玩法一行）。"""
    deep_by_date = load_deep_attributions(output_dir)
    rows = []
    for path in sorted(glob.glob(os.path.join(output_dir, "每日记录_*.json"))):
        m = _DATE_RE_RECORD.search(os.path.basename(path))
        if not m:
            continue
        date_str = m.group(1)
        data = load_json(path)
        if data is None:
            continue
        deep_map = deep_by_date.get(date_str, {})

        for item in data.get("场次", []):
            num = str(item.get("编号") or "").strip()
            if not num:
                continue
            hits = item.get("命中") or {}
            pred = item.get("预判") or {}
            actual = item.get("赛果") or {}

            # 严格 v is False 才算错题；key 缺失 / None 不产行
            wrong_playbooks = [p for p in PLAYBOOKS if hits.get(p) is False]
            if not wrong_playbooks:
                continue

            old_text = item.get("错误归因") or ""
            deep_text = deep_map.get(num)
            section = extract_root_cause_section(deep_text) if deep_text else ""
            if section:
                source, basis = "深度", section
            else:
                # 无深度归因 / 调用失败占位 / 缺【失败根因】段 → 安全降级为旧来源
                source, basis = "旧", old_text

            cause = classify(basis)
            summary = truncate(basis) if basis.strip() else ""

            for pb in wrong_playbooks:
                rows.append([
                    date_str, num,
                    str(item.get("联赛", "")), str(item.get("对阵", "")), pb,
                    str(pred.get(pb, "")), str(actual.get(pb, "")),
                    source, cause, summary,
                ])
    return rows


def save_rows(rows, path=TARGET_CSV):
    """覆盖重写总表（tracker 类产物，允许覆盖），覆盖前打印行数变化。"""
    old_count = None
    if os.path.exists(path):
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                old_count = sum(1 for _ in csv.DictReader(f))
        except OSError:
            old_count = None
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)
    if old_count is not None:
        print(f"覆盖重写 {os.path.basename(path)}: {old_count} → {len(rows)} 行（变化 {len(rows) - old_count:+d}）")
    else:
        print(f"新建 {os.path.basename(path)}: {len(rows)} 行")


def main():
    print("=== 错题归因总表生成 ===")
    rows = build_rows(OUTPUT_DIR)
    save_rows(rows, TARGET_CSV)

    n_deep = sum(1 for r in rows if r[7] == "深度")
    n_old = len(rows) - n_deep
    print(f"来源分布: 深度 {n_deep} 行 / 旧 {n_old} 行")
    from collections import Counter
    # 统计各分类命中行数（多类的每一类都计）
    counter = Counter()
    for r in rows:
        for cat in r[8].split("|"):
            counter[cat] += 1
    top = counter.most_common(5)
    print("根因分类 top5: " + (", ".join(f"{c} {n}" for c, n in top) if top else "（无）"))
    n_un = sum(1 for r in rows if r[8] == UNCLASSIFIED)
    pct = (n_un / len(rows) * 100) if rows else 0.0
    print(f"未分类: {n_un} 行（{pct:.1f}%）")


if __name__ == "__main__":
    main()
