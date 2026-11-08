"""store_samples.py — zucai_all 长期样本累计层。

存储口径（2026-11-08 用户拍板：JSONL + 汇总 CSV）：
  - data/odds/{date}.jsonl        每场一行完整赔率（由 flashscore_odds.write_odds_jsonl 写）
  - data/samples.csv              追加式总表：一场一行，含 M1/M2 推荐、赛果、命中
  - data/batches.md               批次台账：A=API-Football 历史存档，B=Apify 起

CSV 追加规则：以 (date, batch, match_id) 去重，重复跑同一天不会产生重复行。
"""

import csv
import json
import os
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
PLAYS = ("spf", "rq", "ttg", "hafu", "crs")

SAMPLE_COLUMNS = (
    ["date", "batch", "match_id", "league", "home", "away", "kickoff",
     "rq_line", "history_source", "odds_source"]
    + [f"m1_{p}" for p in PLAYS]
    + [f"m2_{p}" for p in PLAYS]
    + ["ft_home", "ft_away", "ht_home", "ht_away"]
    + [f"m1_{p}_{k}" for p in PLAYS for k in ("first", "any")]
    + [f"m2_{p}_{k}" for p in PLAYS for k in ("first", "any")]
    + ["generated_at"]
)

BATCHES_MD_HEADER = """# 批次台账

- **A 批**：API-Football 历史存档（≤2026-11-06）。本环境已彻底隔离，不再发起任何请求。
- **B 批**：Apify Flashscore（2026-11-07 起），赛程/赔率/赛果唯一来源。

| 批次 | 日期 | 场次 | 数据源 | 说明 |
|---|---|---|---|---|
| A | ≤2026-11-06 | - | API-Football | 历史存档，已隔离 |
"""


def _samples_path(data_dir="data"):
    return os.path.join(data_dir, "samples.csv")


def _batches_path(data_dir="data"):
    return os.path.join(data_dir, "batches.md")


def _now():
    return datetime.now(CST).isoformat(timespec="seconds")


def append_samples(target_date, batch, rows, data_dir="data"):
    """追加样本行（按 match_id 去重），返回 (路径, 实际新增行数)。"""
    os.makedirs(data_dir, exist_ok=True)
    path = _samples_path(data_dir)
    seen = set()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fp:
                for old in csv.DictReader(fp):
                    seen.add((old.get("date", ""), old.get("batch", ""),
                              old.get("match_id", "")))
        except (OSError, csv.Error):
            seen = set()

    fresh = []
    for row in rows:
        key = (str(row.get("date", target_date)), str(row.get("batch", batch)),
               str(row.get("match_id", "")))
        if key in seen:
            continue
        seen.add(key)
        payload = {col: row.get(col, "") for col in SAMPLE_COLUMNS}
        payload["date"] = row.get("date", target_date)
        payload["batch"] = batch
        payload["generated_at"] = row.get("generated_at") or _now()
        fresh.append(payload)

    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
    mode = "a" if not write_header else "w"
    with open(path, mode, newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=SAMPLE_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(fresh)
    return path, len(fresh)


def append_batch(target_date, batch, count, note="", data_dir="data"):
    """在 data/batches.md 追加一行（同 batch+date 幂等），返回路径。"""
    os.makedirs(data_dir, exist_ok=True)
    path = _batches_path(data_dir)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(BATCHES_MD_HEADER)
    with open(path, "r", encoding="utf-8") as fp:
        text = fp.read()
    marker = f"| {batch} | {target_date} |"
    if marker in text:
        return path
    line = (f"| {batch} | {target_date} | {count} | "
            f"{'Apify Flashscore' if batch == 'B' else 'API-Football'} | "
            f"{note or '每日样本追加'} |\n")
    if not text.endswith("\n"):
        line = "\n" + line
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(line)
    return path


def count_samples(data_dir="data"):
    """样本总行数（不含表头），供运行日志汇总。"""
    path = _samples_path(data_dir)
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8-sig") as fp:
            return max(0, sum(1 for _ in csv.DictReader(fp)))
    except (OSError, csv.Error):
        return 0
