"""goal-api 历史数据适配层（h2h + 双方近况）。

只负责把 goal-api 的响应转换成 history_cache 既有契约：
available / home_recent_text / away_recent_text / h2h_text。
不读写 CSV，不参与赔率/赛果链路；失败一律抛出 GoalApiError，由调用方决定回退。

认证：GOAL_API_KEY 只从环境变量读取，Authorization: Bearer <key>；
缺失或含空白字符时 fail-fast，任何异常信息都不会包含 key 本身。

时区：goal-api 自 2026-08-20 起全部时间为 UTC，文本展示统一转 Asia/Shanghai。
"""

import difflib
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

from football_history import summarize_h2h, summarize_recent

GOAL_BASE = os.environ.get("GOAL_API_BASE", "https://api.goal-api.com/v1").rstrip("/")
CST = timezone(timedelta(hours=8))
HTTP_TIMEOUT = 45  # 实测服务端约 0.16MB/s，limit=100 需 30s，20s 必然超时
INDEX_PAGE_LIMIT = 100  # 实测上限 100（文档写 500，但 500 会 400）
KICKOFF_WINDOW_MINUTES = 15  # 开球时间容差窗：两数据源开球时间常有分钟级偏差
ROSTER_PAGE_LIMIT = 100
HTTP_RETRIES = 3  # 仅对 timeout / network 重试（服务端约 12% 概率瞬断）
RETRY_BACKOFF_SECONDS = 2

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
    """GET goal-api，返回 data 字段；失败抛 GoalApiError，不记录 key。

    timeout / network 属瞬断（实测约 12%），自动退避重试；鉴权、配额、HTTP 错误不重试。
    """
    global _goal_req_count
    url = f"{GOAL_BASE}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {get_goal_api_key()}",
               "Accept": "application/json"}
    resp = None
    last_error = None
    for attempt in range(HTTP_RETRIES):
        try:
            resp = requests.get(url, params=params or {}, headers=headers, timeout=timeout)
            break
        except requests.Timeout as e:
            last_error = GoalApiError("goal-api 请求超时", kind="timeout")
            last_error.__cause__ = e
        except requests.RequestException as e:
            last_error = GoalApiError(f"goal-api 网络错误: {type(e).__name__}", kind="network")
            last_error.__cause__ = e
        if attempt < HTTP_RETRIES - 1:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    if resp is None:
        _goal_req_count += HTTP_RETRIES
        raise last_error
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

def _kickoff_key(value):
    """UTC kickoff -> 'YYYY-MM-DDTHH:MM'；容忍 'Z' / '+00:00' 两种写法。"""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return None


def _kickoff_dt(value):
    """解析 UTC 开球时间为 datetime；裸字符串（索引键）按 UTC 处理，失败返回 None。"""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _name_match(a, b):
    """规范化队名包含匹配（goal 与 API-Football 命名常缩写/加后缀）。"""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(shorter) >= 4 and shorter in longer


def _league_match(a, b):
    """联赛名包含匹配，用于同开球时间的候选去歧义。"""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


def _entry_from_fixture(item):
    """goal-api Fixture -> {kickoff, home/away team id, names, league}。

    注意：goal-api 的 apiId 与 API-Football fixture id 不同源（实测 404），
    因此用「UTC 开球时间 + 队名/联赛名」作为映射键。
    """
    if not isinstance(item, dict):
        return None
    home = item.get("homeTeam") if isinstance(item.get("homeTeam"), dict) else {}
    away = item.get("awayTeam") if isinstance(item.get("awayTeam"), dict) else {}
    home_id = str(home.get("id") or item.get("homeTeamId") or "").strip()
    away_id = str(away.get("id") or item.get("awayTeamId") or "").strip()
    kickoff = _kickoff_key(item.get("kickoffUtc") or item.get("matchDate"))
    if not kickoff or not home_id or not away_id:
        return None
    return {
        "kickoff": kickoff,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_name": str(home.get("name") or item.get("homeTeamName") or "").strip(),
        "away_name": str(away.get("name") or item.get("awayTeamName") or "").strip(),
        "league_name": str(item.get("leagueName") or "").strip(),
    }


def build_fixture_index(fixtures):
    """fixture 列表 -> {开球分钟: [候选条目]}，按名字/联赛在候选内消歧。"""
    index = {}
    for item in fixtures or []:
        entry = _entry_from_fixture(item)
        if entry:
            index.setdefault(entry["kickoff"], []).append(entry)
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


# ---------------- 联赛映射与球队名单（避免拉全天 353 场索引） ----------------

# API-Football 联赛 id -> goal-api 联赛 cuid（2026-11-06 实测 /leagues?search 逐个核对）
GOAL_LEAGUE_IDS = {
    39:  "cmr77dvkr005nrx06lp7rvp49",   # Premier League / England
    140: "cmr77dvnt006nrx063v3w622e",   # La Liga / Spain
    135: "cmr77dvpd006yrx06zig7907g",   # Serie A / Italy
    78:  "cmr77dvgm0002rx06rt2uqxii",   # Bundesliga / Germany
    61:  "cmr77dvqg007crx06q1kaceyo",   # Ligue 1 / France
    2:   "cmr77dw3900f5rx06j05wgzv4",   # UEFA Champions League
    3:   "cmr77dw3900f6rx06tuqwft2d",   # UEFA Europa League
    40:  "cmr77dvkr005hrx068xaahpuh",   # Championship / England
    141: "cmr77dvnt006orx06io7l06lv",   # Segunda División / Spain
    136: "cmr77dvpd006zrx06dmggkel8",   # Serie B / Italy
    79:  "cmr77dvgm0001rx060h6ivt4p",   # 2. Bundesliga / Germany
    62:  "cmr77dvqg007drx06q6y56j5u",   # Ligue 2 / France
    848: "cmr77dw3900f9rx06laad8onf",   # UEFA Conference League
    88:  "cmr77dvrh007vrx0664phtxs5",   # Eredivisie / Netherlands
    94:  "cmr77dvun00adrx06xz20yfxe",   # Primeira Liga / Portugal
    144: "cmr77dw9g00gvrx06jlglb47m",   # First Division A / Belgium
    179: "cmr77dwe100j5rx064jkxo63c",   # Premiership / Scotland
    203: "cmr77dw0q00eprx06rqew3m48",   # Süper Lig / Turkey
    169: "cmr77dvlv0067rx06potai161",   # Chinese Super League
    98:  "cmr77dx7h00rvrx060kholaxg",   # J1 League / Japan
    292: "cmr77dvsf008lrx06l2d31exu",   # K League 1 / Korea Republic
}


def league_goal_id(api_league_id):
    """API-Football 联赛 id -> goal-api 联赛 cuid；未收录返回 None。"""
    try:
        return GOAL_LEAGUE_IDS.get(int(api_league_id))
    except (TypeError, ValueError):
        return None


def fetch_league_roster(goal_league_id):
    """拉某联赛全部球队 -> [(队名, team_id)]。

    /teams 响应约 11KB（对比 /fixtures/date 全天 183KB），是更稳的 team id 来源；
    联赛名单赛季内稳定，调用方应缓存，避免每天重复拉取。
    """
    teams = []
    offset = 0
    while True:
        payload = goal_get("teams", {"leagueId": goal_league_id,
                                     "limit": ROSTER_PAGE_LIMIT, "offset": offset})
        data = payload.get("data")
        if not isinstance(data, list):
            raise GoalApiError("goal-api teams 返回 data 非列表", kind="parse")
        teams.extend((str(t.get("name") or "").strip(), str(t.get("id") or "").strip())
                     for t in data if isinstance(t, dict) and t.get("id"))
        pagination = payload.get("pagination") or {}
        if not pagination.get("hasMore"):
            break
        offset += len(data) or ROSTER_PAGE_LIMIT
        if offset > 5000:
            break
    return [(n, i) for n, i in teams if n and i]


def _roster_score(query, candidate):
    """队名匹配分：精确 1.0；包含 0.9（Derby ⊂ Derby County）；相似度兜底。"""
    q, c = _norm_name(query), _norm_name(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    shorter, longer = (q, c) if len(q) <= len(c) else (c, q)
    if len(shorter) >= 4 and shorter in longer:
        return 0.9
    return difflib.SequenceMatcher(None, q, c).ratio()


def resolve_team_ids_from_roster(home_name, away_name, roster, threshold=0.72):
    """用联赛球队名单解析双方 goal team id（不依赖开球时间）。

    goal 队名常缩写（Borussia M'gladbach / Coventry City / Bolton Wanderers），
    包含匹配优先、相似度兜底；主客必须落到不同 team id，否则视为失败。
    """
    if not roster:
        return None, None

    def pick(name):
        best_score, best_id = 0.0, None
        for cand_name, cand_id in roster:
            score = _roster_score(name, cand_name)
            if score > best_score:
                best_score, best_id = score, cand_id
        return best_score, best_id

    hs, hid = pick(home_name)
    as_, aid = pick(away_name)
    if hs < threshold or as_ < threshold or not hid or not aid or hid == aid:
        return None, None
    return hid, aid


def resolve_team_ids(kickoff_utc, home_name, away_name, league_name, index=None):
    """在 goal-api fixture 索引中解析双方 team id。

    三重匹配：
    1. 开球时间落在 ±KICKOFF_WINDOW_MINUTES 容差窗内（两数据源开球时间常有分钟级偏差）；
    2. 主客队名包含匹配，主客双向都试（goal-api 主客顺序偶尔相反），正向优先于互换；
    3. 联赛名包含匹配作为同时候选的加分项。
    平分取时间差更小者。解析不到返回 (None, None)；命中互换时按查询侧主客视角换回 id。
    """
    target = _kickoff_dt(kickoff_utc)
    if target is None:
        return None, None
    scored = []
    for key, entries in (index or {}).items():
        cand_dt = _kickoff_dt(key)
        if cand_dt is None:
            continue
        minutes = abs((cand_dt - target).total_seconds()) / 60
        if minutes > KICKOFF_WINDOW_MINUTES:
            continue
        for entry in entries:
            direct = (_name_match(entry.get("home_name"), home_name)
                      and _name_match(entry.get("away_name"), away_name))
            swapped = (_name_match(entry.get("home_name"), away_name)
                       and _name_match(entry.get("away_name"), home_name))
            if not (direct or swapped):
                continue
            score = 2 if direct else 0
            if _league_match(entry.get("league_name"), league_name):
                score += 1
            scored.append((score, -minutes, direct, entry))
    if not scored:
        return None, None
    # sort 稳定：前三项打平时保留索引内原始顺序，避免比较 dict
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    _, _, direct, best = scored[0]
    if direct:
        return best.get("home_team_id"), best.get("away_team_id")
    return best.get("away_team_id"), best.get("home_team_id")


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


def _dedupe_h2h(records):
    """同一个交锋在 goal 源里可能因队名写法不同重复出现（如 M'gladbach 与
    B. Monchengladbach），按「日期 + 比分」去重，保留排序后的第一条。"""
    seen = set()
    out = []
    for r in records:
        key = (r.get("date"), r.get("score"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


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
    h2h = _dedupe_h2h(h2h)[:5]

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


def fetch_history_goal(kickoff_utc, home_name, away_name, league_name,
                       index=None, roster=None):
    """一次 /h2h 请求同时取回双方近况 + 直接交锋。

    roster（联赛球队名单）是主路径，不依赖开球时间，命中率更高；
    未命中再回退到开球时间 ±容差窗的 fixture 索引。
    """
    home_goal_id, away_goal_id = (None, None)
    if roster:
        home_goal_id, away_goal_id = resolve_team_ids_from_roster(
            home_name, away_name, roster)
    if not home_goal_id or not away_goal_id:
        home_goal_id, away_goal_id = resolve_team_ids(
            kickoff_utc, home_name, away_name, league_name, index)
    if not home_goal_id or not away_goal_id:
        raise GoalApiError("goal-api 无法解析双方 team id", kind="mapping")
    payload = goal_get(f"h2h/{home_goal_id}/{away_goal_id}")
    return parse_h2h_payload(payload, home_name, away_name)
