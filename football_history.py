import requests
import os
from datetime import datetime
from typing import Optional, List, Dict

FOOTBALL_API_URL = "https://v3.football.api-sports.io"


def _require_api_key() -> str:
    """从环境变量读取 API Key；缺失时抛出明确错误，避免静默失败。"""
    key = os.environ.get("FOOTBALL_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "未设置 FOOTBALL_API_KEY 环境变量：请先执行 "
            "export FOOTBALL_API_KEY=<你的密钥> 后重试；不要把密钥写入代码或仓库。"
        )
    return key


TEAM_MAP = {
    "中国": None,
    "新西兰": None,
    "江原FC": 2746,
    "仁川联": 2763,
    "格拉夫夏普": 199,
    "登博思": 421,
    "丹麦": None,
    "威尔士": None,
    "塞尔维亚": None,
    "荷兰": None,
    "奥地利": None,
    "科索沃": None,
    "德国": 25,
    "希腊": 1117,
    "挪威": None,
    "葡萄牙": 27,
    "哥伦布机员": 1613,
    "迈阿密国际": 9568,
}


def get_team_id(cn_name: str) -> Optional[int]:
    if cn_name in TEAM_MAP:
        return TEAM_MAP[cn_name]

    api_key = _require_api_key()
    headers = {"x-apisports-key": api_key}

    candidates = [cn_name]
    if "FC" not in cn_name.upper():
        candidates.append(cn_name + " FC")

    for q in candidates:
        try:
            resp = requests.get(
                f"{FOOTBALL_API_URL}/teams",
                params={"search": q},
                headers=headers,
                timeout=10,
            )
            results = resp.json().get("response", [])
            if results:
                tid = results[0]["team"]["id"]
                TEAM_MAP[cn_name] = tid
                return tid
        except Exception:
            pass

    TEAM_MAP[cn_name] = None
    return None


def get_recent_matches(team_id: int, limit: int = 5) -> List[Dict]:
    api_key = _require_api_key()
    headers = {"x-apisports-key": api_key}

    matches = []
    for season in [2024, 2023, 2022]:
        try:
            resp = requests.get(
                f"{FOOTBALL_API_URL}/fixtures",
                params={"team": team_id, "season": season},
                headers=headers,
                timeout=15,
            )
            d = resp.json()
            for m in d.get("response", []):
                status = m.get("fixture", {}).get("status", {}).get("short", "")
                if status in ("FT", "AET", "PST"):
                    teams = m.get("teams", {})
                    goals = m.get("goals", {})
                    home = teams.get("home", {})
                    away = teams.get("away", {})
                    is_home = home.get("id") == team_id

                    hg = goals.get("home")
                    ag = goals.get("away")
                    if hg is None or ag is None:
                        continue

                    if is_home:
                        our_goals, opp_goals = hg, ag
                        opp_name = away.get("name", "")
                    else:
                        our_goals, opp_goals = ag, hg
                        opp_name = home.get("name", "")

                    if our_goals > opp_goals:
                        result = "W"
                    elif our_goals < opp_goals:
                        result = "L"
                    else:
                        result = "D"

                    matches.append({
                        "date": m["fixture"]["date"][:10],
                        "opponent": opp_name,
                        "home_away": "H" if is_home else "A",
                        "score": f"{our_goals}-{opp_goals}",
                        "result": result,
                    })
        except Exception as e:
            pass

        if len(matches) >= limit:
            break

    matches.sort(key=lambda x: x["date"], reverse=True)
    return matches[:limit]


def get_head_to_head(hid: int, aid: int, limit: int = 5) -> List[Dict]:
    api_key = _require_api_key()
    headers = {"x-apisports-key": api_key}

    try:
        resp = requests.get(
            f"{FOOTBALL_API_URL}/fixtures/headtohead",
            params={"h2h": f"{hid}-{aid}"},
            headers=headers,
            timeout=15,
        )
        d = resp.json()
    except Exception:
        return []

    results = []
    for m in d.get("response", []):
        teams = m.get("teams", {})
        goals = m.get("goals", {})
        hg = goals.get("home")
        ag = goals.get("away")
        if hg is None or ag is None:
            continue
        results.append({
            "date": m["fixture"]["date"][:10],
            "home": teams.get("home", {}).get("name", ""),
            "away": teams.get("away", {}).get("name", ""),
            "score": f"{hg}-{ag}",
        })

    results.sort(key=lambda x: x["date"], reverse=True)
    return results[:limit]


def summarize_recent(matches: List[Dict]) -> str:
    if not matches:
        return ""

    results = [m["result"] for m in matches]
    w = results.count("W")
    d = results.count("D")
    l = results.count("L")
    total_goals_for = sum(int(m["score"].split("-")[0]) for m in matches)
    total_goals_against = sum(int(m["score"].split("-")[1]) for m in matches)

    summary = f"近{len(matches)}场：{w}胜{d}平{l}负，"
    summary += f"进{total_goals_for}球/失{total_goals_against}球\n"

    for m in matches:
        ra = "主场" if m["home_away"] == "H" else "客场"
        summary += f"  {m['date']} {ra} vs {m['opponent']} {m['score']} ({m['result']})\n"

    return summary.strip()


def summarize_h2h(matches: List[Dict]) -> str:
    if not matches:
        return ""

    summary = f"历史交锋（近{len(matches)}次）：\n"
    for m in matches:
        summary += f"  {m['date']} {m['home']} {m['score']} {m['away']}\n"

    return summary.strip()


def build_history_for_match(home_cn: str, away_cn: str) -> Dict:
    home_id = get_team_id(home_cn)
    away_id = get_team_id(away_cn)

    result = {
        "home_team": home_cn,
        "away_team": away_cn,
        "home_id": home_id,
        "away_id": away_id,
        "home_recent": [],
        "away_recent": [],
        "h2h": [],
        "home_recent_text": "",
        "away_recent_text": "",
        "h2h_text": "",
        "available": False,
    }

    if home_id:
        result["home_recent"] = get_recent_matches(home_id)
        result["home_recent_text"] = summarize_recent(result["home_recent"])

    if away_id:
        result["away_recent"] = get_recent_matches(away_id)
        result["away_recent_text"] = summarize_recent(result["away_recent"])

    if home_id and away_id:
        result["h2h"] = get_head_to_head(home_id, away_id)
        result["h2h_text"] = summarize_h2h(result["h2h"])

    result["available"] = bool(home_id or away_id)

    return result
