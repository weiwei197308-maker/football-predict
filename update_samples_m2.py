"""update_samples_m2.py — 补跑后更新 data/samples.csv 里某日期的 M1/M2 字段。

用法：bash run_py.sh update_samples_m2.py --date 2026-11-21 [--arm M1|M2|both]
依赖：output/{竞彩分析,独立分析}_{date}.csv 存在；data/samples.csv 对应日期行已存在。
不修改任何其它文件，只把 samples.csv 里对应行的 m{1,2}_*、m{1,2}_*_first、m{1,2}_*_any 写回。
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settle_all as sa  # noqa

PLAYS = ("spf", "rq", "ttg", "hafu", "crs")


def load_results(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compute_arm_fields(target_date, arm, output_dir="output", data_dir="data"):
    """复用 settle_all 基础函数，生成目标日期某臂的 sample-like 行。"""
    sched = sa.load_schedule(os.path.join(output_dir, f"竞彩足球_{target_date}.csv"))
    results = load_results(os.path.join(output_dir, f"赛果_{target_date}.json"))
    fname = "竞彩分析" if arm == "M1" else "独立分析"
    arm_path = os.path.join(output_dir, f"{fname}_{target_date}.csv")
    arm_rows = sa.load_arm(arm_path)

    out = {}  # match_id → fields dict
    for row in arm_rows:
        num = str(row.get("编号", "") or "").strip()
        sc = sched.get(num)
        if not sc:
            continue
        match_id = sc.get("matchId", "")
        res = sa.find_result(results, sc)
        if not res:
            continue
        act = sa.actuals(res, sc.get("让球数", ""))
        hits, _cands, raws = sa.settle_row(row, act)
        prefix = "m1" if arm == "M1" else "m2"
        fields = {}
        for play in PLAYS:
            fields[f"{prefix}_{play}"] = raws.get(play, "")
            first, any_hit = hits.get(play, (None, None))
            fields[f"{prefix}_{play}_first"] = "" if first is None else int(first)
            fields[f"{prefix}_{play}_any"] = "" if any_hit is None else int(any_hit)
        out[match_id] = fields
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--output-dir", default="output")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--arm", choices=("M1", "M2", "both"), default="both")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    samples_path = os.path.join(args.data_dir, "samples.csv")
    if not os.path.exists(samples_path):
        print(f"❌ {samples_path} 不存在")
        sys.exit(1)

    to_run = ["M1", "M2"] if args.arm == "both" else [args.arm]
    all_new = {}
    for arm in to_run:
        new = compute_arm_fields(args.date, arm, args.output_dir, args.data_dir)
        all_new.update(new)
        print(f"\n{arm}：从 CSV 算出 {len(new)} 场字段")

    with open(samples_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    matched = 0
    patched = 0
    already = 0
    for row in rows:
        if row.get("date") != args.date:
            continue
        matched += 1
        mid = row.get("match_id", "")
        if mid not in all_new:
            continue
        new = all_new[mid]
        was_empty_any = any(
            str(row.get(k, "")).strip() == ""
            for k in new if k.startswith(("m1_", "m2_"))
        )
        for k, v in new.items():
            row[k] = v
        if was_empty_any:
            patched += 1
        else:
            already += 1

    print(f"\nsamples.csv 里 date={args.date} 共 {matched} 行：")
    print(f"  更新 {patched} 行，已存在值未改 {already} 行，未匹配 {matched - patched - already} 行")

    if args.dry_run:
        print("（--dry-run 模式，未写回）")
        return

    with open(samples_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n✅ 已写回 {samples_path}")


if __name__ == "__main__":
    main()
