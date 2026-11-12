"""API-Football 联赛白名单（zucai_all 全量样本环境专用）。

priority 越小优先级越高；当日请求预算不足时按优先级截断。
prefix 用于生成比赛编号（前缀 + 两位序号，如 PL01）。
api_id 为 API-Football (v3.football.api-sports.io) 联赛 ID，首次 dry-run 时需对照实际返回核实。
"""

LEAGUES = [
    # 优先级 1：五大联赛 + 欧冠/欧联
    {"api_id": 39,  "name": "英超",   "full_name": "英格兰超级联赛",   "prefix": "PL",   "priority": 1},
    {"api_id": 140, "name": "西甲",   "full_name": "西班牙甲级联赛",   "prefix": "LL",   "priority": 1},
    {"api_id": 135, "name": "意甲",   "full_name": "意大利甲级联赛",   "prefix": "SA",   "priority": 1},
    {"api_id": 78,  "name": "德甲",   "full_name": "德国甲级联赛",     "prefix": "BL",   "priority": 1},
    {"api_id": 61,  "name": "法甲",   "full_name": "法国甲级联赛",     "prefix": "L1",   "priority": 1},
    {"api_id": 2,   "name": "欧冠",   "full_name": "欧洲冠军联赛",     "prefix": "UCL",  "priority": 1},
    {"api_id": 3,   "name": "欧联",   "full_name": "欧罗巴联赛",       "prefix": "UEL",  "priority": 1},
    # 优先级 2：次级联赛 + 欧协联 + 荷甲/葡超
    {"api_id": 40,  "name": "英冠",   "full_name": "英格兰冠军联赛",   "prefix": "CH",   "priority": 2},
    {"api_id": 141, "name": "西乙",   "full_name": "西班牙乙级联赛",   "prefix": "LL2",  "priority": 2},
    {"api_id": 136, "name": "意乙",   "full_name": "意大利乙级联赛",   "prefix": "SA2",  "priority": 2},
    {"api_id": 79,  "name": "德乙",   "full_name": "德国乙级联赛",     "prefix": "BL2",  "priority": 2},
    {"api_id": 62,  "name": "法乙",   "full_name": "法国乙级联赛",     "prefix": "L2",   "priority": 2},
    {"api_id": 848, "name": "欧协联", "full_name": "欧洲协会联赛",     "prefix": "UECL", "priority": 2},
    {"api_id": 88,  "name": "荷甲",   "full_name": "荷兰甲级联赛",     "prefix": "ERE",  "priority": 2},
    {"api_id": 94,  "name": "葡超",   "full_name": "葡萄牙超级联赛",   "prefix": "POR",  "priority": 2},
    # 优先级 3：其他欧洲主流联赛
    {"api_id": 144, "name": "比甲",   "full_name": "比利时甲级联赛",   "prefix": "BEL",  "priority": 3},
    {"api_id": 179, "name": "苏超",   "full_name": "苏格兰超级联赛",   "prefix": "SCO",  "priority": 3},
    {"api_id": 203, "name": "土超",   "full_name": "土耳其超级联赛",   "prefix": "TUR",  "priority": 3},
    # 优先级 4：亚洲联赛（赛季为自然年）
    {"api_id": 169, "name": "中超",   "full_name": "中国足球超级联赛", "prefix": "CSL",  "priority": 4},
    {"api_id": 98,  "name": "日职",   "full_name": "日本J1联赛",       "prefix": "J1",   "priority": 4},
    {"api_id": 292, "name": "韩K",    "full_name": "韩国K联赛",        "prefix": "K1",   "priority": 4},
]

BY_API_ID = {l["api_id"]: l for l in LEAGUES}

# ----------------------------------------------------------------------------
# 候选联赛（2026-11-07 整理，**尚未接入**：不在 LEAGUES / BY_API_ID 中）
#
# 入选标准：主环境近 37 份「竞彩足球_*.csv」里从未出现过（即竞彩不开售），
# 用来给去重后的 zucai_all 提供增量样本。
#
# 使用前必须完成两件事，否则会跑错联赛或让 h2h 回退 API-Football 白吃配额：
#   1. 核对 api_id：明日配额重置后用 `/leagues?country=xx&season=2026` 逐个确认，
#      确认后把 verified 改成 True，再整条移入 LEAGUES；
#   2. 补齐 goal_league_id：goal-api 联赛 id 与 API-Football 不同源，
#      确认后同时并入 goal_history.GOAL_LEAGUE_IDS，再迁移。
# ----------------------------------------------------------------------------
CANDIDATE_LEAGUES = [
    # 英格兰低级别
    {"api_id": 41,  "name": "英甲",   "full_name": "英格兰甲级联赛",   "prefix": "EL1", "priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 42,  "name": "英乙",   "full_name": "英格兰乙级联赛",   "prefix": "EL2", "priority": 3, "goal_league_id": None, "verified": False},
    # 欧洲二三级联赛
    {"api_id": 80,  "name": "德丙",   "full_name": "德国丙级联赛",     "prefix": "BL3", "priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 63,  "name": "法丙",   "full_name": "法国全国联赛",     "prefix": "L3",  "priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 138, "name": "意丙",   "full_name": "意大利丙级联赛",   "prefix": "SA3", "priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 95,  "name": "葡甲",   "full_name": "葡萄牙甲级联赛",   "prefix": "POR2","priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 145, "name": "比乙",   "full_name": "比利时挑战者联赛", "prefix": "BEL2","priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 180, "name": "苏冠",   "full_name": "苏格兰冠军联赛",   "prefix": "SCO2","priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 207, "name": "瑞士超", "full_name": "瑞士超级联赛",     "prefix": "SUI", "priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 119, "name": "丹超",   "full_name": "丹麦超级联赛",     "prefix": "DEN", "priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 218, "name": "奥甲",   "full_name": "奥地利甲级联赛",   "prefix": "AUT", "priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 197, "name": "希超",   "full_name": "希腊超级联赛",     "prefix": "GRE", "priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 345, "name": "捷甲",   "full_name": "捷克甲级联赛",     "prefix": "CZE", "priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 106, "name": "波甲",   "full_name": "波兰甲级联赛",     "prefix": "POL", "priority": 3, "goal_league_id": None, "verified": False},
    {"api_id": 235, "name": "俄超",   "full_name": "俄罗斯超级联赛",   "prefix": "RUS", "priority": 3, "goal_league_id": None, "verified": False},
    # 欧洲以外
    {"api_id": 128, "name": "阿甲",   "full_name": "阿根廷甲级联赛",   "prefix": "ARG", "priority": 4, "goal_league_id": None, "verified": False},
    {"api_id": 72,  "name": "巴乙",   "full_name": "巴西乙级联赛",     "prefix": "BRA2","priority": 4, "goal_league_id": None, "verified": False},
    {"api_id": 262, "name": "墨超",   "full_name": "墨西哥超级联赛",   "prefix": "MEX", "priority": 4, "goal_league_id": None, "verified": False},
]

# ----------------------------------------------------------------------------
# Apify Flashscore 映射（2026-11-08 起本环境唯一赔率/赛程来源）
#
# 键为 Flashscore 的 competition.display_name，值中的 cfg 直接引用上面的联赛条目
# （保留 name/priority/api_id 等字段，供分析、结算与 goal-api 历史映射复用）。
# 候选联赛若 goal id 未核对，历史会自动回退到 goal-api 全天索引（每 UTC 日期 1 次）。
# ----------------------------------------------------------------------------
FLASHSCORE_NAMES = {
    # 优先级 1
    39:  "ENGLAND: Premier League",
    140: "SPAIN: LaLiga",
    135: "ITALY: Serie A",
    78:  "GERMANY: Bundesliga",
    61:  "FRANCE: Ligue 1",
    2:   "EUROPE: Champions League",
    3:   "EUROPE: Europa League",
    # 优先级 2
    40:  "ENGLAND: Championship",
    141: "SPAIN: LaLiga 2",
    136: "ITALY: Serie B",
    79:  "GERMANY: 2. Bundesliga",
    62:  "FRANCE: Ligue 2",
    848: "EUROPE: Conference League",
    88:  "NETHERLANDS: Eredivisie",
    94:  "PORTUGAL: Liga Portugal",
    # 优先级 3
    144: "BELGIUM: Pro League",
    179: "SCOTLAND: Premiership",
    203: "TURKEY: Super Lig",
    41:  "ENGLAND: League One",
    42:  "ENGLAND: League Two",
    80:  "GERMANY: 3. Liga",
    63:  "FRANCE: National",
    138: "ITALY: Serie C",
    95:  "PORTUGAL: Liga Portugal 2",
    145: "BELGIUM: Challenger Pro League",
    180: "SCOTLAND: Championship",
    207: "SWITZERLAND: Super League",
    119: "DENMARK: Superliga",
    218: "AUSTRIA: Bundesliga",
    197: "GREECE: Super League",
    345: "CZECH REPUBLIC: Chance Liga",
    106: "POLAND: Ekstraklasa",
    235: "RUSSIA: Premier League",
    # 优先级 4
    169: "CHINA: Super League",
    98:  "JAPAN: J1 League",
    292: "SOUTH KOREA: K League 1",
    128: "ARGENTINA: Liga Profesional",
    72:  "BRAZIL: Serie B",
    262: "MEXICO: Liga MX",
}

_BY_API_ID_ALL = {l["api_id"]: l for l in LEAGUES}
_BY_API_ID_ALL.update({l["api_id"]: l for l in CANDIDATE_LEAGUES})
BY_FLASHSCORE = {
    display: {**_BY_API_ID_ALL[api_id], "flashscore": display}
    for api_id, display in FLASHSCORE_NAMES.items()
    if api_id in _BY_API_ID_ALL
}
# Flashscore 把部分联赛拆成子组/别名：这些 display_name 归入同一条 cfg，
# 否则 Actor 的模糊匹配会把它们当成「未收录联赛」白白消耗抓取上限。
FLASHSCORE_ALIASES = {
    "ITALY: Serie C - Group A": 138,
    "ITALY: Serie C - Group B": 138,
    "ITALY: Serie C - Group C": 138,
}
for _alias, _api_id in FLASHSCORE_ALIASES.items():
    if _api_id in _BY_API_ID_ALL:
        BY_FLASHSCORE[_alias] = {**_BY_API_ID_ALL[_api_id], "flashscore": _alias}
# 国际比赛日：欧国联 A–D 组（Flashscore 按组拆成四个 display_name，体彩有售）。
# 纳入理由（2026-11-12 用户确认）：A 思路按赛事类型检索资料，国家联赛场次少，
# 混入不会造成系统性偏移；league 记「欧国联A/B/C/D」，便于事后单独剔除。
NATIONS_LEAGUE_DISPLAYS = {
    "EUROPE: UEFA Nations League - League A": "欧国联A",
    "EUROPE: UEFA Nations League - League B": "欧国联B",
    "EUROPE: UEFA Nations League - League C": "欧国联C",
    "EUROPE: UEFA Nations League - League D": "欧国联D",
}
for _display, _name in NATIONS_LEAGUE_DISPLAYS.items():
    BY_FLASHSCORE[_display] = {
        "api_id": None, "name": _name,
        "full_name": f"欧洲国家联赛 {_name[-1]} 级",
        "prefix": "UNL", "priority": 5, "flashscore": _display,
    }
FLASHSCORE_COMPETITIONS = list(FLASHSCORE_NAMES.values()) + list(NATIONS_LEAGUE_DISPLAYS)

