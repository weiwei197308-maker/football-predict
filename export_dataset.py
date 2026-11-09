"""export_dataset.py — 把按日拆分的 JSONL/CSV 汇总成 AI 可直接分析的两张宽表。

输入：
  data/odds/{date}.jsonl   每场一行完整赔率（赛程脚本产出，已按当日赛程裁剪）
  data/samples.csv         结算后的样本总表（一场一行：推荐/赛果/命中）
输出（默认 data/analysis/）：
  dataset.csv              一场一行：基础信息 + 五玩法赔率 + 两臂推荐 + 命中
  odds_crs.csv             一场一行：28 档比分赔率（单独成表，避免主表过宽）
  README.md                字段字典与口径说明（每次导出时重写）

用法：
  python export_dataset.py [--data-dir data] [--out-dir data/analysis]
                           [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""

import argparse
import csv
import glob
import json
import os
from datetime import datetime

import flashscore_odds as fo

HAFU_COLUMNS = (
    ("半主胜-全主胜", "hafu_hh"),
    ("半主胜-全平", "hafu_hd"),
    ("半主胜-全客胜", "hafu_ha"),
    ("半平-全主胜", "hafu_dh"),
    ("半平-全平", "hafu_dd"),
    ("半平-全客胜", "hafu_da"),
    ("半客胜-全主胜", "hafu_ah"),
    ("半客胜-全平", "hafu_ad"),
    ("半客胜-全客胜", "hafu_aa"),
)
ODDS_COLUMNS = (
    [("spf", "主胜", "spf_home"), ("spf", "平", "spf_draw"), ("spf", "客胜", "spf_away")]
    + [("rq", "主胜", "rq_home"), ("rq", "平", "rq_draw"), ("rq", "客胜", "rq_away")]
    + [("ttg", name, f"ttg_{name[:-1].replace('+', 'plus')}") for name in fo.TTG_NAMES]
    + [("hafu", cn, out) for cn, out in HAFU_COLUMNS]
)
SAMPLE_REC_COLUMNS = [f"m1_{p}" for p in ("spf", "rq", "ttg", "hafu", "crs")] + [
    f"m2_{p}" for p in ("spf", "rq", "ttg", "hafu", "crs")]
SAMPLE_HIT_COLUMNS = [f"m1_{p}_{k}" for p in ("spf", "rq", "ttg", "hafu", "crs")
                      for k in ("first", "any")] + [
    f"m2_{p}_{k}" for p in ("spf", "rq", "ttg", "hafu", "crs")
    for k in ("first", "any")]

DATASET_COLUMNS = (
    ["date", "match_id", "league", "league_display", "home", "away",
     "kickoff_cst", "status", "ft_home", "ft_away", "ht_home", "ht_away",
     "settled", "rq_line", "odds_source", "history_source"]
    + [out for _, _, out in ODDS_COLUMNS]
    + ["batch"] + SAMPLE_REC_COLUMNS + SAMPLE_HIT_COLUMNS
)
CRS_COLUMNS = ["date", "match_id", "league", "home", "away"] + [
    f"crs_{sc.replace(':', '_')}" for sc in fo.CRS_SCORES]


def _load_samples(data_dir):
    """data/samples.csv → {(date, match_id): row}；文件缺失返回空。"""
    path = os.path.join(data_dir, "samples.csv")
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, "r", encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            key = (row.get("date", ""), row.get("match_id", ""))
            if key[1]:
                out[key] = row
    return out


def _load_records(data_dir, start=None, end=None):
    """读出 data/odds/*.jsonl 的全部记录，附带由文件名得到的日期。"""
    folder = os.path.join(data_dir, "odds")
    records = []
    for path in sorted(glob.glob(os.path.join(folder, "*.jsonl"))):
        d = os.path.splitext(os.path.basename(path))[0]
        if start and d < start:
            continue
        if end and d > end:
            continue
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("match_id"):
                    records.append((d, obj))
    records.sort(key=lambda item: (item[0], item[1].get("kickoff_cst", ""),
                                   item[1]["match_id"]))
    return records


def _score(sample, rec, key):
    """赛果优先取 samples.csv，其次 JSONL；null 归一为空串。"""
    value = sample.get(key) if sample else None
    if value in (None, ""):
        value = rec.get(key)
    return "" if value is None else value


def _settled(ft_home, ft_away):
    return 1 if str(ft_home).strip() and str(ft_away).strip() else 0


def build_rows(data_dir="data", start=None, end=None):
    """返回 (dataset_rows, crs_rows, dates)。"""
    samples = _load_samples(data_dir)
    dataset, crs_rows, dates = [], [], set()
    for d, rec in _load_records(data_dir, start, end):
        sample = samples.get((d, rec["match_id"]), {})
        mapped = rec.get("odds_mapped") or {}
        flags = rec.get("odds_flags") or {}
        ft_home, ft_away = _score(sample, rec, "ft_home"), _score(sample, rec, "ft_away")
        row = {
            "date": d,
            "match_id": rec["match_id"],
            "league": rec.get("league_name") or rec.get("league_display", ""),
            "league_display": rec.get("league_display", ""),
            "home": rec.get("home", ""),
            "away": rec.get("away", ""),
            "kickoff_cst": str(rec.get("kickoff_cst", "")).replace("T", " ")[:16],
            "status": rec.get("status", ""),
            "ft_home": ft_home,
            "ft_away": ft_away,
            "ht_home": _score(sample, rec, "ht_home"),
            "ht_away": _score(sample, rec, "ht_away"),
            "settled": _settled(ft_home, ft_away),
            "rq_line": (mapped.get("rq") or {}).get("让球数", ""),
            "odds_source": flags.get("odds_source", ""),
            "history_source": rec.get("history_source", ""),
            "batch": sample.get("batch", ""),
        }
        for market, key, out in ODDS_COLUMNS:
            row[out] = (mapped.get(market) or {}).get(key, "")
        for col in SAMPLE_REC_COLUMNS + SAMPLE_HIT_COLUMNS:
            row[col] = sample.get(col, "")
        dataset.append(row)

        crs = mapped.get("crs") or {}
        crs_row = {k: row[k] for k in ("date", "match_id", "league", "home", "away")}
        for sc in fo.CRS_SCORES:
            crs_row[f"crs_{sc.replace(':', '_')}"] = crs.get(sc, "")
        crs_rows.append(crs_row)
        dates.add(d)
    return dataset, crs_rows, dates


README_TEMPLATE = """# 数据分析表字段说明

由 `export_dataset.py` 自动生成，**每次导出会重写本文件**。
数据来源：`data/odds/{date}.jsonl`（赛程/赔率）+ `data/samples.csv`（结算/命中）。

## 文件

| 文件 | 粒度 | 用途 |
|---|---|---|
| `dataset.csv` | 一场一行 | 主分析表：基础信息 + 五玩法赔率 + M1/M2 推荐 + 命中 |
| `odds_crs.csv` | 一场一行 | 28 档比分赔率，按 `match_id` 与主表关联 |

## 主表字段

### 基础
- `date`：比赛所在日（Asia/Shanghai），也是 JSONL 文件名日期
- `match_id`：Flashscore 比赛 ID；同日同 ID 唯一
- `league` / `league_display`：中文联赛名 / Flashscore 全称
- `home` / `away`：主/客队（Flashscore 英文名）
- `kickoff_cst`：开赛时间（北京时间，`YYYY-MM-DD HH:MM`）
- `status`：赛程状态（导出时为 `scheduled`）
- `ft_home` / `ft_away` / `ht_home` / `ht_away`：全场/半场比分；未结算为空
- `settled`：1=已有全场比分，0=未结算
- `rq_line`：让球线，`+1` / `-1` / `0`（整数盘）
- `odds_source`：赔率来源，固定 `apify:flashscore`
- `history_source`：历史数据来源（`goal` / `none`）

### 赔率（十进制小数赔率）
- `spf_home` / `spf_draw` / `spf_away`：胜平负
- `rq_home` / `rq_draw` / `rq_away`：让球胜平负（`rq_line` 已应用到让球数上）
- `ttg_0` … `ttg_6` / `ttg_7plus`：总进球 0–7+
- `hafu_hh/hd/ha/dh/dd/da/ah/ad/aa`：半全场，编码为「半场结果+全场结果」，
  `h`=主胜、`d`=平、`a`=客胜，例如 `hafu_hd` = 半主胜-全平

### 样本（来自 `data/samples.csv`，未结算时为空）
- `batch`：A=API-Football 存档，B=Apify
- `m1_spf` / `m1_rq` / `m1_ttg` / `m1_hafu` / `m1_crs`：M1（带赔率）推荐原文
- `m2_*`：M2（无赔率）推荐原文
- `m1_{玩法}_{first|any}` / `m2_{玩法}_{first|any}`：是否命中，`1/0`；
  `first`=只看首推，`any`=任一候选；空=无有效预测或不可判定

## 比分表字段

- `date` / `match_id` / `league` / `home` / `away`：与主表一致
- `crs_0_0` … `crs_5_2`：28 档比分赔率，列为 `主队进球_客队进球`；
  空 = 数据源无该档报价（不是解析失败）

## 口径提醒

- 总进球由 O/U 阶梯去水归一推导；四分之一盘近似会略微平滑尾部。
- 让球只取整数盘；「让球-平」由比分净胜球分布推导，非博彩公司直报。
- 每日只含白名单联赛、最多 60 场；未结算场次的赛果与命中字段为空。
- `ht_*` 若 Flashscore 只提供下半场进球，已用「全场 − 下半场」反推。

## 导出摘要

- 日期范围：__START__ ~ __END__
- 比赛行数：__MATCHES__
- 已结算：__SETTLED__
- 日期数：__DAYS__
"""


def write_outputs(dataset, crs_rows, dates, out_dir="data/analysis", start=None, end=None):
    os.makedirs(out_dir, exist_ok=True)
    dataset_path = os.path.join(out_dir, "dataset.csv")
    crs_path = os.path.join(out_dir, "odds_crs.csv")
    readme_path = os.path.join(out_dir, "README.md")
    with open(dataset_path, "w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=DATASET_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(dataset)
    with open(crs_path, "w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=CRS_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(crs_rows)
    days = sorted(dates)
    settled = sum(1 for r in dataset if r["settled"])
    text = (README_TEMPLATE
            .replace("__START__", start or (days[0] if days else "-"))
            .replace("__END__", end or (days[-1] if days else "-"))
            .replace("__MATCHES__", str(len(dataset)))
            .replace("__SETTLED__", str(settled))
            .replace("__DAYS__", str(len(days))))
    text += f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    with open(readme_path, "w", encoding="utf-8") as fp:
        fp.write(text)
    return dataset_path, crs_path, readme_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", default=os.path.join("data", "analysis"))
    ap.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD（含）")
    ap.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（含）")
    args = ap.parse_args()

    dataset, crs_rows, dates = build_rows(args.data_dir, args.start, args.end)
    if not dataset:
        raise SystemExit(f"错误：{args.data_dir}/odds 下没有可导出的 JSONL 记录")
    paths = write_outputs(dataset, crs_rows, dates, args.out_dir, args.start, args.end)
    settled = sum(1 for r in dataset if r["settled"])
    print(f"比赛 {len(dataset)} 行，已结算 {settled} 行，日期 {len(dates)} 个")
    for p in paths:
        print(f"  {p}")


if __name__ == "__main__":
    main()
