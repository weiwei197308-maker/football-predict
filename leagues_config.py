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
