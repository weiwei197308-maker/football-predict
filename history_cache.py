"""本地历史数据缓存查询：替代 football_history 的在线 API 拉取。

历史数据由 output/history_cache_{date}.json 预先落盘提供
（key = matchId 字符串，value 含 home_recent_text / away_recent_text /
h2h_text / available 等字段），本模块只做本地读取，不发起任何网络请求，
不消耗 FOOTBALL_API_KEY 额度。
"""
import csv
import json
import os
import re


def load_history_lookup(csv_path):
    """从赛程 CSV 与同日期历史缓存构建查询函数。

    - 读取 CSV，建立 (主队, 客队) -> matchId 映射；
    - 从 CSV 文件名提取日期（YYYY-MM-DD），读取同目录 history_cache_{date}.json；
    - 返回 fn(home, away) -> dict：命中返回缓存条目；
      未命中或缓存不存在/损坏时返回 {"available": False}。
    """
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    pair_to_id = {}
    for row in rows:
        home = row.get("主队", "")
        away = row.get("客队", "")
        match_id = str(row.get("matchId", "") or "").strip()
        if home and away and match_id:
            pair_to_id[(home, away)] = match_id

    cache = {}
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(csv_path))
    if m:
        cache_path = os.path.join(
            os.path.dirname(csv_path), f"history_cache_{m.group(1)}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    cache = raw
            except (OSError, ValueError):
                cache = {}

    def lookup(home, away):
        match_id = pair_to_id.get((home, away))
        if not match_id:
            return {"available": False}
        entry = cache.get(match_id)
        if isinstance(entry, dict):
            return entry
        return {"available": False}

    return lookup
