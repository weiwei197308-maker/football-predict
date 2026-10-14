"""统一复盘数据加载器。

所有 tracker 不再各自解析 竞彩复盘_*.txt，而是统一从 每日记录_YYYY-MM-DD.json 读取。
数据源由 review.py 写入，是唯一的"命中判定"来源。

返回结构：
{
  num: {
    "全场": "2-2",          # 全场比分，如 "0-3"
    "半场": "0-2",          # 半场比分，可能为 "-" 表示缺失
    "胜平负": "平",         # 实际胜平负（主胜/平/客胜）
    "让球": "主胜",         # 让球数修正后实际结果
    "总进球": "4球",        # 如 "4球"，未开奖时为 ""
    "半全场": "AD",         # 2字符 HDA 组合，半场缺失可能为 "?D"
    "比分": "2:2",          # 全场比分用冒号格式
    "hit_spf": True/False,  # 胜平负命中
    "hit_rq": True/False,   # 让球命中
    "hit_ttg": True/False,  # 总进球命中
    "hit_hafu": True/False, # 半全场命中
    "hit_crs": True/False,  # 比分命中
    "归因": "...",          # 错误归因文本，命中无误时为 None
  },
  ...
}
"""
import json
import os


def load_review_data(date_str, output_dir="output"):
    """读取 review.py 写出的 每日记录_*.json，返回按编号索引的字典。"""
    path = os.path.join(output_dir, f"每日记录_{date_str}.json")
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [警告] 读取 {path} 失败: {e}")
        return {}

    result = {}
    for item in data.get("场次", []):
        num = item.get("编号", "").strip()
        if not num:
            continue

        pred = item.get("预判", {}) or {}
        actual = item.get("赛果", {}) or {}
        hit = item.get("命中", {}) or {}

        result[num] = {
            "全场": actual.get("全场", ""),
            "半场": actual.get("半场", ""),
            "胜平负": actual.get("胜平负", ""),
            "让球": actual.get("让球", ""),
            "总进球": actual.get("总进球", ""),
            "半全场": actual.get("半全场", ""),
            "比分": actual.get("比分", ""),
            "hit_spf": bool(hit.get("胜平负", False)),
            "hit_rq": bool(hit.get("让球", False)),
            "hit_ttg": bool(hit.get("总进球", False)),
            "hit_hafu": bool(hit.get("半全场", False)),
            "hit_crs": bool(hit.get("比分", False)),
            "归因": item.get("错误归因"),
        }
    return result
