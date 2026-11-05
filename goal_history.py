"""goal-api 历史数据适配层（h2h + 双方近况）。

只负责把 goal-api 的响应转换成 history_cache 既有契约：
available / home_recent_text / away_recent_text / h2h_text。
不读写 CSV，不参与赔率/赛果链路；失败一律抛出 GoalApiError，由调用方决定回退。

认证：GOAL_API_KEY 只从环境变量读取，Authorization: Bearer <key>；
缺失或含空白字符时 fail-fast，任何异常信息都不会包含 key 本身。

时区：goal-api 自 2026-08-20 起全部时间为 UTC，文本展示统一转 Asia/Shanghai。
"""

import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

from football_history import summarize_h2h, summarize_recent

GOAL_BASE = os.environ.get("GOAL_API_BASE", "https://api.goal-api.com/v1").rstrip("/")
CST = timezone(timedelta(hours=8))
HTTP_TIMEOUT = 20
INDEX_PAGE_LIMIT = 500

_goal_req_count = 0


class GoalApiError(RuntimeError):
    """goal-api 调用/解析失败；kind 用于区分鉴权、配额、超时、结构异常。"""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


def goal_request_count():
    """本进程内已发出的 goal-api 请求数（不消耗 API-Football 配额）。"""
    return _goal_req_count


def get_goal_api_key():
    key = os.environ.get("GOAL_API_KEY", "").strip()
    if not key:
        raise GoalApiError(
            "未设置 GOAL_API_KEY 环境变量：请先 export GOAL_API_KEY=<你的密钥> 后重试；"
            "密钥只放在环境变量，不要写入代码或仓库。", kind="config")
    if len(key) < 10 or re.search(r"\s", key):
        raise GoalApiError(
            "GOAL_API_KEY 格式异常（为空、过短或含空白字符），请检查环境变量。",
            kind="config")
    return key


def goal_get(path, params=None, timeout=HTTP_TIMEOUT):
    """GET goal-api，返回 data 字段；失败抛 GoalApiError，不记录 key。"""
    global _goal_req_count
    url = f"{GOAL_BASE}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {get_goal_api_key()}",
               "Accept": "application/json"}
    try:
        resp = requests.get(url, params=params or {}, headers=headers, timeout=timeout)
    except requests.Timeout as e:
        raise GoalApiError("goal-api 请求超时", kind="timeout") from e
    except requests.RequestException as e:
        raise GoalApiError(f"goal-api 网络错误: {type(e).__name__}", kind="network") from e
    _goal_req_count += 1

    if resp.status_code in (401, 403):
        raise GoalApiError("goal-api 鉴权失败（401/403），请检查 GOAL_API_KEY", kind="auth")
    if resp.status_code == 429:
        raise GoalApiError("goal-api 超配额/限流（429）", kind="quota")
    if resp.status_code != 200:
        raise GoalApiError(f"goal-api HTTP {resp.status_code}", kind="http")

    try:
        payload = resp.json()
    except ValueError as e:
        raise GoalApiError("goal-api 返回非 JSON", kind="parse") from e
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise GoalApiError("goal-api 返回 success=false 或结构异常", kind="parse")
    if "data" not in payload:
        raise GoalApiError("goal-api 响应缺少 data 字段", kind="parse")
    return payload


# ---------------- fixture 索引与 team id 解析 ----------------

def _entry_from_fixture(item):
    """goal-api Fixture -> {api_id, home_team_id, away_team_id, names}。"""
    if not isinstance(item, dict):
        return None
    api_id = str(item.get("apiId") or "").strip()
    home = item.get("homeTeam") if isinstance(item.get("homeTeam"), dict) else {}
    away = item.get("awayTeam") if isinstance(item.get("awayTeam"), dict) else {}
    home_id = str(home.get("id") or item.get("homeTeamId") or "").strip()
    away_id = str(away.get("id") or item.get("awayTeamId") or "").strip()
    if not api_id or not home_id or not away_id:
        return None
    return {
        "api_id": api_id,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_team_api_id": str(home.get("apiId") or "").strip(),
        "away_team_api_id": str(away.get("apiId") or "").strip(),
        "home_name": str(home.get("name") or item.get("homeTeamName") or "").strip(),
        "away_name": str(away.get("name") or item.get("awayTeamName") or "").strip(),
    }


def build_fixture_index(fixtures):
    """把 fixture 列表转成 apiId -> team id 映射（纯函数，便于测试）。"""
    index = {}
    for item in fixtures or []:
        entry = _entry_from_fixture(item)
        if entry:
            index[entry["api_id"]] = entry
    return index


def fetch_fixture_index(target_dates):
    """按 UTC 日期批量拉 goal-api fixtures，减少每场额外请求。"""
    index = {}
    for d in sorted({str(x) for x in target_dates if x}):
        offset = 0
        while True:
            payload = goal_get(f"fixtures/date/{d}",
                               {"limit": INDEX_PAGE_LIMIT, "offset": offset})
            data = payload.get("data")
            if not isinstance(data, list):
                raise GoalApiError("goal-api fixtures 返回 data 非列表", kind="parse")
            index.update(build_fixture_index(data))
            pagination = payload.get("pagination") or {}
            if not pagination.get("hasMore"):
                break
            offset += len(data) or INDEX_PAGE_LIMIT
            if offset > 5000:
                break
    return index


def fetch_single_fixture(match_id):
    """按 API-Football fixture id 直查 goal-api fixture（apiId 可作为 fixture id）。"""
    payload = goal_get(f"fixtures/{match_id}")
    data = payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    return _entry_from_fixture(data) if isinstance(data, dict) else None


def resolve_team_ids(match_id, index=None):
    """返回 (home_goal_id, away_goal_id)；无法解析时返回 (None, None)。

    fixture 索引未命中时按 API-Football fixture id 直查 goal-api；
    鉴权/配额/网络异常向上抛出，交由调用方统一回退。
    """
    entry = (index or {}).get(str(match_id))
    if not entry:
        entry = fetch_single_fixture(match_id)
    if not entry:
        return None, None
    return entry.get("home_team_id"), entry.get("away_team_id")


# ---------------- h2h 响应解析 ----------------

def _cst_datetime(match):
    """RawMatch 的 match_date/match_time 为 UTC，转 Asia/Shanghai 展示。"""
    d = str(match.get("match_date") or "")[:10]
    t = str(match.get("match_time") or "00:00")[:5]
    try:
        dt = datetime.fromisoformat(f"{d}T{t}:00").replace(tzinfo=timezone.utc)
        local = dt.astimezone(CST)
        return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
    except ValueError:
        return d, t


def _scores(match):
    hs = str(match.get("match_hometeam_score") or "").strip()
    as_ = str(match.get("match_awayteam_score") or "").strip()
    if not (hs.isdigit() and as_.isdigit()):
        return None
    return int(hs), int(as_)


def _direct_record(match):
    sc = _scores(match)
    if sc is None:
        return None
    date, _ = _cst_datetime(match)
    return {"date": date,
            "home": str(match.get("match_hometeam_name") or "").strip(),
            "away": str(match.get("match_awayteam_name") or "").strip(),
            "score": f"{sc[0]}-{sc[1]}"}


def _norm_name(name):
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.casefold())


def _recent_record(match, team_name):
    """team1Recent/team2Recent 不带稳定 team key，用同源规范化队名判主客。"""
    sc = _scores(match)
    if sc is None:
        return None
    ours = _norm_name(team_name)
    if not ours:
        return None
    if _norm_name(match.get("match_hometeam_name")) == ours:
        is_home = True
    elif _norm_name(match.get("match_awayteam_name")) == ours:
        is_home = False
    else:
        return None
    hg, ag = sc
    our, opp = (hg, ag) if is_home else (ag, hg)
    result = "W" if our > opp else ("L" if our < opp else "D")
    date, _ = _cst_datetime(match)
    return {"date": date,
            "opponent": str(match.get("match_awayteam_name" if is_home else "match_hometeam_name")
                            or "").strip(),
            "home_away": "H" if is_home else "A",
            "score": f"{our}-{opp}",
            "result": result}


def parse_h2h_payload(payload, home_name=None, away_name=None):
    """goal-api /h2h 响应 -> history_cache 契约（键名不变，文本内容可不同）。

    goal-api 的 RawMatch 只带数字 team id，和 /h2h 使用的 cuid 不同源；
    team1Recent/team2Recent 的主客归属用响应内 team1Name/team2Name 判定。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise GoalApiError("goal-api h2h 响应 data 结构异常", kind="parse")
    if not any(k in data for k in ("directMatches", "team1Recent", "team2Recent")):
        raise GoalApiError("goal-api h2h 响应缺少交锋/近况字段", kind="parse")
    home_name = home_name or str(data.get("team1Name") or "").strip()
    away_name = away_name or str(data.get("team2Name") or "").strip()

    direct = [r for r in (data.get("directMatches") or []) if isinstance(r, dict)]
    recent1 = [r for r in (data.get("team1Recent") or []) if isinstance(r, dict)]
    recent2 = [r for r in (data.get("team2Recent") or []) if isinstance(r, dict)]

    h2h = [x for x in (_direct_record(m) for m in direct) if x]
    h2h.sort(key=lambda x: x["date"], reverse=True)
    h2h = h2h[:5]

    def _recent(rows, team_name):
        out = []
        for m in rows:
            r = _recent_record(m, team_name)
            if r:
                out.append(r)
        out.sort(key=lambda x: x["date"], reverse=True)
        return out[:5]

    home_recent = _recent(recent1, home_name)
    away_recent = _recent(recent2, away_name)

    return {
        "home_recent": home_recent,
        "away_recent": away_recent,
        "h2h": h2h,
        "home_recent_text": summarize_recent(home_recent),
        "away_recent_text": summarize_recent(away_recent),
        "h2h_text": summarize_h2h(h2h),
        "available": bool(home_recent or away_recent or h2h),
    }


def fetch_history_goal(match_id, home_api_id, away_api_id, index=None,
                       home_name=None, away_name=None):
    """一次 /h2h 请求同时取回双方近况 + 直接交锋。

    home_api_id/away_api_id 仅用于团队解析/日志，不参与 h2h 归属判定。
    """
    home_goal_id, away_goal_id = resolve_team_ids(match_id, index)
    if not home_goal_id or not away_goal_id:
        raise GoalApiError("goal-api 无法解析双方 team id", kind="mapping")
    payload = goal_get(f"h2h/{home_goal_id}/{away_goal_id}")
    return parse_h2h_payload(payload, home_name, away_name)
