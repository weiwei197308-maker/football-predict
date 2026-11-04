#!/usr/bin/env python3
"""影子实验赛果回填（赛后独立运行，只读预测记录，不回改预测字段）。

用法：
  python3 settle_shadow.py 2026-10-29

行为：
  - 依次结算三个玩法的影子文件：总进球 / 让球 / 半全场；
  - 读取各自 output/影子实验_<玩法>_<date>.json 与 output/每日记录_<date>.json；
  - 按 对齐键.编号 关联赛果，仅写入白名单字段「实际赛果」；
  - 已有赛果的记录跳过（不重复回填）；找不到赛果的记录保持 null 并计数；
  - 某玩法影子文件不存在时跳过该玩法（不影响其它玩法）；
    每日记录缺失时全部中止（不回填、不伪造）；
  - 写入前备份原文件；回填后校验除「实际赛果 / 最近回填时间」外所有字段
    与原文件完全一致，任何差异都会中止写入并保留原文件。
"""
import json
import os
import re
import shutil
import sys
from datetime import datetime

import ttg_shadow

try:
    import rq_shadow
except Exception:
    rq_shadow = None
try:
    import hafu_shadow
except Exception:
    hafu_shadow = None


def fail(msg, code=1):
    print(f"❌ {msg}")
    sys.exit(code)


def parse_total_goals(score_text):
    """从 '2-1' / '2:1' 形式的全场比分解析总进球标签；失败返回 None。"""
    m = re.match(r"^\s*(\d+)\s*[-:：]\s*(\d+)\s*$", str(score_text or ""))
    if not m:
        return None
    total = int(m.group(1)) + int(m.group(2))
    return "7+球" if total >= 7 else f"{total}球"


def _extract_ttg(score):
    """总进球赛果提取：全场比分 → 总进球标签。"""
    label = parse_total_goals((score or {}).get("全场"))
    if not label:
        return None
    return {"全场": (score or {}).get("全场", ""), "总进球": label}


def _extract_rq(score):
    """让球赛果提取：赛果.让球 必须是 主胜/平/客胜（让球修正后方向）。"""
    direction = str((score or {}).get("让球", "") or "").strip()
    if direction not in ("主胜", "平", "客胜"):
        return None
    return {"全场": (score or {}).get("全场", ""), "让球": direction}


def _extract_hafu(score):
    """半全场赛果提取：赛果.半全场 必须是 9 种合法 HDA 组合之一。"""
    raw = (score or {}).get("半全场")
    code = hafu_shadow.canon_hafu_code(raw) if hafu_shadow is not None else None
    if not code:
        return None
    return {"半场": (score or {}).get("半场", ""),
            "全场": (score or {}).get("全场", ""), "半全场": code}


def _plays():
    """玩法注册表：名称 → (影子文件路径函数, 赛果提取函数)。"""
    plays = [("总进球", ttg_shadow.shadow_file_path, _extract_ttg)]
    if rq_shadow is not None:
        plays.append(("让球", rq_shadow.shadow_file_path, _extract_rq))
    if hafu_shadow is not None:
        plays.append(("半全场", hafu_shadow.shadow_file_path, _extract_hafu))
    return plays


def settle_one_play(play_name, shadow_path, target_date, score_map_by_key, writer):
    """结算单个玩法影子文件。返回 (ok, 汇总行)。"""
    if not os.path.exists(shadow_path):
        return True, f"[{play_name}] 影子文件不存在，已跳过（{shadow_path}）"

    with open(shadow_path, encoding="utf-8") as f:
        shadow = json.load(f)

    matches = shadow.get("场次", [])
    n_fill = n_skip = n_miss = 0
    now = datetime.now().isoformat(timespec="seconds")
    for rec in matches:
        if rec.get("实际赛果"):
            n_skip += 1
            continue
        num = (rec.get("对齐键") or {}).get("编号", "")
        if num in score_map_by_key:
            rec["实际赛果"] = dict(score_map_by_key[num], 回填时间=now,
                                   回填来源=f"每日记录_{target_date}.json")
            n_fill += 1
        else:
            n_miss += 1

    # 回填前后一致性校验：除白名单字段外，所有内容必须完全一致
    def _strip(obj):
        data = json.loads(json.dumps(obj, ensure_ascii=False))
        data.pop("最近回填时间", None)
        for rec in data.get("场次", []):
            rec.pop("实际赛果", None)
        return data

    with open(shadow_path, encoding="utf-8") as f:
        original = json.load(f)
    new_shadow = dict(shadow)
    new_shadow["最近回填时间"] = now
    if json.dumps(_strip(original), ensure_ascii=False, sort_keys=True) != \
            json.dumps(_strip(new_shadow), ensure_ascii=False, sort_keys=True):
        return False, f"[{play_name}] 回填校验失败：除实际赛果外有字段被改动，已中止写入（原文件未动）"

    backup = f"{shadow_path}.backup.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        shutil.copy2(shadow_path, backup)
    except OSError as e:
        return False, f"[{play_name}] 回填前备份失败，已中止写入：{e}"

    try:
        writer(shadow_path, new_shadow)
    except Exception as e:
        return False, f"[{play_name}] 回填写入失败（原文件与备份均保留：{backup}）：{e}"

    return True, (f"[{play_name}] 回填完成：新增赛果 {n_fill}，已有跳过 {n_skip}，"
                  f"未找到赛果 {n_miss}；备份：{backup}")


def main():
    if len(sys.argv) != 2:
        fail("用法：python3 settle_shadow.py <日期，如 2026-10-29>")
    target_date = sys.argv[1]
    record_path = os.path.join("output", f"每日记录_{target_date}.json")

    plays = _plays()
    existing = [(name, path_fn(target_date), extract)
                for name, path_fn, extract in plays]
    if not any(os.path.exists(p) for _, p, _ in existing):
        fail("三个玩法的影子文件均不存在，无可回填内容")
    if not os.path.exists(record_path):
        fail(f"找不到每日记录：{record_path}（赛果缺失时不回填、不伪造）")

    with open(record_path, encoding="utf-8") as f:
        daily = json.load(f)
    daily_matches = daily.get("场次", [])

    had_failure = False
    for play_name, shadow_path, extract in existing:
        score_map = {}
        for m in daily_matches:
            result = extract(m.get("赛果") or {})
            if m.get("编号") and result:
                score_map[m["编号"]] = result
        ok, line = settle_one_play(play_name, shadow_path, target_date,
                                   score_map, ttg_shadow._atomic_write_json)
        print(line)
        if not ok:
            had_failure = True

    if had_failure:
        sys.exit(1)


if __name__ == "__main__":
    main()
