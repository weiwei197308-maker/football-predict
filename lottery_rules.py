"""中国体彩竞彩足球规则引擎（独立、可测试）。

本模块只做纯计算，不依赖网络或大模型。所有"让球映射/总进球/半全场兼容性/一致性检查"
的硬性规则都集中在此，由 Python 程序负责，不交给大模型自由判断。

关键约定：
- 让球数 handicap：负数表示主队让球（如 -1），正数表示客队让球（如 +1）。
- 让球结果 = 主队实际进球 + handicap 后与客队进球比较。
- 胜平负标签统一用中文："主胜" / "平" / "客胜"。
- 半全场代码用 HDA 两字符，如 "HH"、"DH"（H=主胜 D=平 A=客胜）。
"""
import re
from typing import Optional, Tuple, List, Dict


# ============ 基础解析 ============

def parse_handicap(handicap_str) -> Optional[int]:
    """解析体彩让球数，返回 int。无法解析返回 None。

    合法示例："-1", "+1", "-2", "+2", "-3", "+3"。
    不合法示例："", "让1", "abc", None。
    """
    if handicap_str is None:
        return None
    s = str(handicap_str).strip()
    if not s:
        return None
    m = re.match(r"^([+-]?\d+)$", s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def parse_score(score_str) -> Optional[Tuple[int, int]]:
    """解析比分为 (主队进球, 客队进球)。支持 '2:1'、'2-1'、'2 : 1'。"""
    if score_str is None:
        return None
    s = str(score_str).strip()
    if not s:
        return None
    m = re.match(r"^(\d+)\s*[:\-]\s*(\d+)$", s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


HAFU_CN_MAP = {
    "半主胜-全主胜": "HH", "半平-全主胜": "DH", "半客胜-全主胜": "AH",
    "半主胜-全平": "HD", "半平-全平": "DD", "半客胜-全平": "AD",
    "半主胜-全客胜": "HA", "半平-全客胜": "DA", "半客胜-全客胜": "AA",
}


def parse_hafu_code_from_text(value) -> Optional[str]:
    """把单个半全场描述安全转换为 HDA 代码。

    支持 "HH"/"dh"/" AA " 等代码（大小写不敏感），
    以及 9 种中文描述（如 "半平-全客胜" → "DA"，允许内部空格）。
    非法、不完整或非字符串输入一律返回 None。
    """
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", "", value)
    if not text:
        return None
    if len(text) == 2 and all(c in "HDA" for c in text.upper()):
        return text.upper()
    return HAFU_CN_MAP.get(text)


def parse_hafu_codes(hafu_str) -> List[str]:
    """解析半全场推荐，返回 HDA 代码列表。

    兼容 list 输入、"/"或逗号分隔字符串、HDA 代码与中文描述混排；
    非法项跳过，结果保序去重。
    """
    if not hafu_str:
        return []
    if isinstance(hafu_str, (list, tuple)):
        raw_parts = [str(x) for x in hafu_str]
    else:
        raw_parts = re.split(r"[/,，、\s]+", str(hafu_str))
    codes = []
    for part in raw_parts:
        code = parse_hafu_code_from_text(part)
        if code and code not in codes:
            codes.append(code)
    return codes


# 全场结果对应的全部合法半全场候选（用于程序兜底生成）
HAFU_CANDIDATES = {
    "主胜": ["HH", "DH", "AH"],
    "平": ["HD", "DD", "AD"],
    "客胜": ["HA", "DA", "AA"],
}


def hafu_candidates_for_full(spf_cn) -> List[str]:
    """按全场胜平负返回全部合法半全场候选；无法识别返回空列表。"""
    return list(HAFU_CANDIDATES.get(str(spf_cn or "").strip(), []))


def cn_to_code(label: str) -> Optional[str]:
    """中文胜平负标签转单字符代码。主胜→H, 平→D, 客胜→A。"""
    if label is None:
        return None
    s = str(label).strip()
    if s in ("主胜", "H", "h"):
        return "H"
    if s in ("平", "D", "d"):
        return "D"
    if s in ("客胜", "A", "a"):
        return "A"
    return None


def code_to_cn(code: str) -> str:
    """单字符代码转中文。H→主胜, D→平, A→客胜。未知返回原字符串。"""
    return {"H": "主胜", "D": "平", "A": "客胜"}.get(str(code).upper(), str(code))


# ============ 规则计算 ============

def get_result_label(home_goals: int, away_goals: int) -> str:
    """根据比分返回胜平负中文标签。"""
    if home_goals > away_goals:
        return "主胜"
    if home_goals < away_goals:
        return "客胜"
    return "平"


def get_goal_diff(home_goals: int, away_goals: int) -> int:
    """净胜球 = 主队进球 - 客队进球。正数主胜，负数客胜。"""
    return home_goals - away_goals


def compute_handicap_result(home_goals: int, away_goals: int, handicap) -> str:
    """计算让球胜平负结果。

    规则：adjusted_home = home_goals + handicap，再与 away_goals 比较。
    - handicap 为负：主队让球；为正：客队让球。
    - 返回 "主胜" / "平" / "客胜" 或 "无法判定"。

    示例：
        compute_handicap_result(2, 0, -1)  → "主胜"  (2-1=1 > 0)
        compute_handicap_result(1, 0, -1)  → "平"    (1-1=0 == 0)
        compute_handicap_result(1, 1, -1)  → "客胜"  (1-1=0 < 1)
        compute_handicap_result(0, 1, +1)  → "平"    (0+1=1 == 1)
        compute_handicap_result(0, 2, +1)  → "客胜"  (0+1=1 < 2)
    """
    hc = parse_handicap(handicap)
    if hc is None:
        return "无法判定"
    adjusted = home_goals + hc
    if adjusted > away_goals:
        return "主胜"
    if adjusted == away_goals:
        return "平"
    return "客胜"


def compute_total_goals(home_goals: int, away_goals: int) -> int:
    """总进球数 = 主队 + 客队。"""
    return home_goals + away_goals


def total_goals_label(total: int) -> str:
    """总进球数转体彩标签。0-6 原样，7 及以上为 '7+'。"""
    if total >= 7:
        return "7+"
    return str(total)


def parse_total_goals_list(value) -> List[int]:
    """解析总进球推荐（字符串或列表），返回整数进球数列表。

    兼容 "2球 / 3球 / 1球"、"2,3"、以及 list/tuple 输入；
    "7+球" → 7，非法项跳过。结果保序去重。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = [str(x) for x in value]
    else:
        parts = re.split(r"[/,，、\s]+", str(value))
    goals: List[int] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)(?:\+球|球|\+)?$", part)
        if m:
            n = int(m.group(1))
            if n not in goals:
                goals.append(n)
    return goals


def get_hafu_full_result(hafu_code: str) -> Optional[str]:
    """从半全场代码取全场结果代码。'HH' → 'H', 'DA' → 'A'。

    两个字符都必须是合法的 HDA 代码，否则返回 None（如 "XH"）。
    """
    if not hafu_code or len(hafu_code) != 2:
        return None
    code = str(hafu_code).upper()
    if code[0] not in "HDA" or code[1] not in "HDA":
        return None
    return code[1]


def is_hafu_compatible(hafu_code: str, full_result_cn: str) -> bool:
    """检查半全场代码的全场部分是否与全场胜平负一致。"""
    full_code = get_hafu_full_result(hafu_code)
    expected = cn_to_code(full_result_cn)
    if full_code is None or expected is None:
        return False
    return full_code == expected


# ============ 一致性检查 ============

def check_score_vs_spf(score_str: str, spf_cn: str) -> Tuple[bool, str]:
    """检查 1：胜平负与首选比分是否一致。"""
    score = parse_score(score_str)
    if score is None:
        return False, f"首选比分格式异常：{score_str!r}"
    h, a = score
    actual = get_result_label(h, a)
    ok = actual == spf_cn
    return ok, "" if ok else f"首选比分 {score_str} 对应 {actual}，与胜平负推荐 {spf_cn} 不一致"


def check_score_vs_goal_diff(score_str: str, goal_diff_range: str) -> Tuple[str, str]:
    """检查 2：净胜球分布与首选比分是否一致。

    返回 (状态, 消息)，状态为 "通过" / "冲突" / "无法判定"。
    空区间、无法识别的区间描述、比分格式异常一律返回 "无法判定"，
    严禁默认当作检查通过。
    """
    text = str(goal_diff_range or "").strip()
    if not text:
        return "无法判定", "净胜球区间缺失，无法核对"
    score = parse_score(score_str)
    if score is None:
        return "无法判定", f"首选比分格式异常：{score_str!r}，无法核对净胜球"
    gd = get_goal_diff(*score)
    if "主胜2" in text:
        ok, msg = gd >= 2, f"首选比分净胜球 {gd}，不在主胜2+区间"
    elif "主胜1" in text:
        ok, msg = gd == 1, f"首选比分净胜球 {gd}，不在主胜1区间"
    elif "客胜1" in text:
        ok, msg = gd == -1, f"首选比分净胜球 {gd}，不在客胜1区间"
    elif "客胜2" in text:
        ok, msg = gd <= -2, f"首选比分净胜球 {gd}，不在客胜2+区间"
    elif "平" in text:
        ok, msg = gd == 0, f"首选比分净胜球 {gd}，不在平局区间"
    else:
        return "无法判定", f"无法识别的净胜球区间描述：{text}"
    return ("通过", "") if ok else ("冲突", msg)


def check_score_vs_handicap(score_str: str, handicap, handicap_rec_cn: str) -> Tuple[bool, str]:
    """检查 3：体彩让球结论是否符合实际让球数和首选比分。

    handicap_rec_cn 是让球推荐的方向（主胜/平/客胜），不含"让±N"前缀。
    """
    score = parse_score(score_str)
    if score is None:
        return False, f"首选比分格式异常：{score_str!r}"
    hc = parse_handicap(handicap)
    if hc is None:
        # 让球缺失或非法：硬性错误，不能产生正常让球预测
        return False, f"让球数无法解析：{handicap!r}，不应输出正常让球推荐"
    computed = compute_handicap_result(score[0], score[1], hc)
    if computed == "无法判定":
        return False, f"让球数 {handicap!r} 无法判定"
    ok = computed == handicap_rec_cn
    return ok, "" if ok else (
        f"首选比分 {score_str} + 让球 {handicap} → 应为 {computed}，"
        f"与让球推荐 {handicap_rec_cn} 不一致"
    )


def check_hafu_vs_full(hafu_str: str, full_result_cn: str) -> Tuple[bool, str]:
    """检查 4：半全场是否可能导向对应的全场结果。

    所有推荐的半全场代码，其全场部分必须与全场胜平负一致。
    """
    codes = parse_hafu_codes(hafu_str)
    if not codes:
        return False, f"半全场推荐无法解析：{hafu_str!r}"
    bad = [c for c in codes if not is_hafu_compatible(c, full_result_cn)]
    if bad:
        return False, f"半全场 {','.join(bad)} 的全场部分与全场 {full_result_cn} 不一致"
    return True, ""


def check_score_vs_total_goals(score_str: str, total_goals_str: str) -> Tuple[bool, str]:
    """检查 5：总进球是否等于首选比分双方进球之和。"""
    score = parse_score(score_str)
    if score is None:
        return False, f"首选比分格式异常：{score_str!r}"
    computed_total = compute_total_goals(*score)
    # 解析总进球推荐列表（统一走 parse_total_goals_list）
    rec_goals = parse_total_goals_list(total_goals_str)
    if not rec_goals:
        return False, f"总进球推荐无法解析：{total_goals_str!r}"
    if computed_total not in rec_goals:
        # 7+ 的情况
        if computed_total >= 7 and 7 in rec_goals:
            return True, ""
        return False, (
            f"首选比分 {score_str} 总进球 {computed_total}，"
            f"不在总进球推荐 {total_goals_str} 中"
        )
    return True, ""


def run_consistency_checks(prediction: Dict, handicap) -> Tuple[List[str], List[str], List[str]]:
    """对一场比赛的预测结果做一致性检查。

    Args:
        prediction: LLM 返回的解析结果 dict，应包含：
            胜平负推荐, 让球推荐, 总进球推荐, 半全场推荐, 比分推荐,
            （可选）净胜球分布
        handicap: 体彩让球数（字符串或 int），如 "-1"

    Returns:
        (hard_errors, soft_warnings, unjudgeable)
        hard_errors: 必须修正的规则错误列表
        soft_warnings: 软性冲突提示列表
        unjudgeable: 数据缺失或无法判定的项（不得当作检查通过）
    """
    hard_errors = []
    soft_warnings = []
    unjudgeable = []

    if not isinstance(prediction, dict):
        return ["预测结果不是 JSON 对象，无法校验"], [], []

    spf = str(prediction.get("胜平负推荐", "") or "").strip()
    rq_rec = str(prediction.get("让球推荐", "") or "").strip()
    ttg_val = prediction.get("总进球推荐", "")
    hafu_val = prediction.get("半全场推荐", "")
    crs_rec = prediction.get("比分推荐", "")
    goal_diff = str(prediction.get("净胜球分布", "") or "").strip()

    # 列表字段统一转成 " / " 分隔字符串，供后续解析
    ttg_rec = " / ".join(str(x) for x in ttg_val) if isinstance(ttg_val, list) else str(ttg_val or "")
    hafu_rec = " / ".join(str(x) for x in hafu_val) if isinstance(hafu_val, list) else str(hafu_val or "")

    # 统一提取比分列表（兼容 list 和 "1:0 / 2:0" 字符串）
    if isinstance(crs_rec, list):
        scores = [str(s).strip() for s in crs_rec if str(s).strip()]
    else:
        scores = [s.strip() for s in re.split(r"[/,，、\s]+", str(crs_rec or "")) if s.strip()]
    first_score = scores[0] if scores else ""

    if not first_score:
        unjudgeable.append("首选比分缺失，无法判定交叉校验")
    else:
        # 检查 1：胜平负 vs 首选比分
        if not spf:
            unjudgeable.append("胜平负推荐缺失，无法核对与首选比分一致性")
        else:
            ok, msg = check_score_vs_spf(first_score, spf)
            if not ok:
                hard_errors.append(msg)

        # 检查 2：净胜球分布 vs 首选比分
        if not goal_diff:
            unjudgeable.append("净胜球分布缺失，无法核对净胜球一致性")
        else:
            status, msg = check_score_vs_goal_diff(first_score, goal_diff)
            if status == "冲突":
                hard_errors.append(msg)
            elif status == "无法判定":
                unjudgeable.append(msg)

        # 检查 3：让球 vs 首选比分（硬性）
        hc = parse_handicap(handicap)
        if hc is None:
            if rq_rec and "无法" not in rq_rec:
                hard_errors.append(f"让球数 {handicap!r} 无法解析，不应输出正常让球推荐")
            else:
                unjudgeable.append("让球数缺失，让球推荐无法判定")
        else:
            # 从让球推荐提取方向（如 "让-1主胜" → "主胜"）
            rq_dir = _extract_handicap_direction(rq_rec)
            if rq_dir is None:
                if not rq_rec:
                    unjudgeable.append("让球推荐缺失，无法核对让球一致性")
                elif "无法" in rq_rec:
                    unjudgeable.append(f"让球推荐无法判定：{rq_rec!r}")
                else:
                    hard_errors.append(f"让球推荐格式异常：{rq_rec!r}")
            else:
                ok, msg = check_score_vs_handicap(first_score, hc, rq_dir)
                if not ok and prediction.get("让球推导版本") == HANDICAP_VERSION_V2:
                    # v2 分布口径：让球主推由五类净胜球概率确定性映射而来，可能与首选
                    # 比分的单点结果不同（跨剧本）。不按单点口径判硬错误，降为软性
                    # 提示，要求结合「净胜球概率分布/让球结果概率」字段审阅。
                    point = derive_handicap_recommendation(first_score, hc)
                    soft_warnings.append(
                        f"让球推荐为{rq_dir}（v2 净胜球分布口径），首选比分 {first_score} "
                        f"单点推导为 {point}：差异来自净胜球概率分布，属跨剧本结果，"
                        "需结合净胜球概率字段审阅，不以首选比分单点判错"
                    )
                elif not ok:
                    hard_errors.append(msg)

        # 检查 5：总进球 vs 首选比分（硬性）
        ok, msg = check_score_vs_total_goals(first_score, ttg_rec)
        if not ok:
            hard_errors.append(msg)

    # 检查 4：半全场 vs 全场胜平负（硬性）
    if spf and hafu_rec:
        ok, msg = check_hafu_vs_full(hafu_rec, spf)
        if not ok:
            hard_errors.append(msg)

    # 检查 6：备选比分逐个检查（格式硬性；方向/净胜球区间为软性提示）
    for s in scores[1:]:
        pair = parse_score(s)
        if pair is None:
            hard_errors.append(f"备选比分格式异常：{s!r}")
            continue
        alt_spf = get_result_label(*pair)
        if spf and alt_spf != spf:
            soft_warnings.append(
                f"备选比分 {s} 属于替代剧本：方向为{alt_spf}，与首选方向 {spf} 不同"
            )
        elif spf and goal_diff:
            status, _ = check_score_vs_goal_diff(s, goal_diff)
            if status == "冲突":
                soft_warnings.append(
                    f"备选比分 {s} 不在净胜球分布 {goal_diff} 区间内（同方向替代剧本，净胜球分布提示）"
                )

    return hard_errors, soft_warnings, unjudgeable


# ============ 核心比赛剧本校验（阶段二） ============

CORE_RESULT_LABELS = {"主胜", "平", "客胜"}
CORE_GOAL_DIFF_INTERVALS = ("主胜2+", "主胜1", "平", "客胜1", "客胜2+")


def normalize_core_result(value):
    """将核心剧本胜平负标签规范为：主胜 / 平 / 客胜；未知值返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    aliases = {
        "主胜": "主胜", "主队胜": "主胜", "H": "主胜", "h": "主胜",
        "平": "平", "平局": "平", "和": "平", "D": "平", "d": "平",
        "客胜": "客胜", "客队胜": "客胜", "A": "客胜", "a": "客胜",
    }
    return aliases.get(text)


def normalize_core_goal_interval(value):
    """规范化净胜球区间；仅接受受控别名，未知描述返回 None。"""
    if value is None:
        return None
    text = re.sub(r"\s+", "", str(value)).replace("球及以上", "+").replace("球以上", "+")
    aliases = {
        "主胜2+": "主胜2+", "主胜2": "主胜2+", "主胜2球": "主胜2+",
        "主胜2球+": "主胜2+", "主胜2+球": "主胜2+", "主胜两球+": "主胜2+",
        "主胜1": "主胜1", "主胜1球": "主胜1", "主胜一球": "主胜1",
        "平": "平", "平局": "平", "平0": "平", "0球": "平",
        "客胜1": "客胜1", "客胜1球": "客胜1", "客胜一球": "客胜1",
        "客胜2+": "客胜2+", "客胜2": "客胜2+", "客胜2球": "客胜2+",
        "客胜2球+": "客胜2+", "客胜2+球": "客胜2+", "客胜两球+": "客胜2+",
    }
    return aliases.get(text)


def goal_diff_to_core_interval(goal_diff):
    """将主队净胜球整数映射到项目使用的五档区间。"""
    if goal_diff >= 2:
        return "主胜2+"
    if goal_diff == 1:
        return "主胜1"
    if goal_diff == 0:
        return "平"
    if goal_diff == -1:
        return "客胜1"
    return "客胜2+"


def validate_core_script(script, expected_home="", expected_away=""):
    """校验核心剧本 JSON。纯函数：不修改输入；未知区间绝不默认通过。

    返回 dict：ok/errors/warnings/unjudgeable/normalized/chain_ok/alternative_scripts。
    备选比分若属于另一种结果或净胜球区间，会被明确归为替代剧本；这不自动否定主剧本。
    """
    import copy

    errors = []
    warnings = []
    unjudgeable = []
    alternative_scripts = []
    if not isinstance(script, dict):
        return {
            "ok": False, "errors": ["核心剧本返回值不是 JSON 对象"], "warnings": [],
            "unjudgeable": [], "normalized": None, "chain_ok": False,
            "alternative_scripts": [],
        }

    original = copy.deepcopy(script)
    required = ("home_team", "away_team", "full_time_result", "net_goal_interval", "primary_score", "alternative_scores", "match_script", "net_goal_reasoning", "uncertainties", "confidence")
    for field in required:
        if field not in original or original.get(field) in (None, ""):
            errors.append(f"缺少必需字段：{field}")

    result = normalize_core_result(original.get("full_time_result"))
    if original.get("full_time_result") not in (None, "") and result is None:
        errors.append(f"full_time_result 枚举非法：{original.get('full_time_result')!r}；允许值为主胜/平局/客胜")

    interval = normalize_core_goal_interval(original.get("net_goal_interval"))
    if original.get("net_goal_interval") not in (None, "") and interval is None:
        errors.append(f"net_goal_interval 区间未知：{original.get('net_goal_interval')!r}")
        unjudgeable.append("无法判断核心净胜球区间")

    home = str(original.get("home_team") or "").strip()
    away = str(original.get("away_team") or "").strip()
    if expected_home and home and home != expected_home:
        errors.append(f"主队名称不一致：剧本为 {home!r}，本场为 {expected_home!r}")
    if expected_away and away and away != expected_away:
        errors.append(f"客队名称不一致：剧本为 {away!r}，本场为 {expected_away!r}")
    if expected_home and not home:
        warnings.append("核心剧本未返回 home_team，无法核对主队名称")
    if expected_away and not away:
        warnings.append("核心剧本未返回 away_team，无法核对客队名称")

    primary_raw = original.get("primary_score")
    primary_parsed = parse_score(primary_raw)
    primary_score = ""
    actual_result = None
    actual_interval = None
    if primary_parsed is None:
        if primary_raw not in (None, ""):
            errors.append(f"primary_score 比分格式错误：{primary_raw!r}")
        unjudgeable.append("无法解析主推比分")
    else:
        h, a = primary_parsed
        primary_score = f"{h}:{a}"
        actual_result = get_result_label(h, a)
        actual_interval = goal_diff_to_core_interval(h - a)
        if result is not None and actual_result != result:
            errors.append(f"核心链冲突：比分 {primary_score} 对应 {actual_result}，但 full_time_result 为 {result}")
        if interval is not None and actual_interval != interval:
            errors.append(f"核心链冲突：比分 {primary_score} 对应净胜球区间 {actual_interval}，但 net_goal_interval 声明为 {interval}")

    alternatives_raw = original.get("alternative_scores", [])
    if alternatives_raw is None:
        alternatives_raw = []
    if not isinstance(alternatives_raw, list):
        errors.append("alternative_scores 必须是数组")
        alternatives_raw = []
    alternatives = []
    seen = set()
    primary_tuple = primary_parsed
    for index, raw_score in enumerate(alternatives_raw, start=1):
        parsed_score = parse_score(raw_score)
        if parsed_score is None:
            errors.append(f"第 {index} 个备选比分格式非法：{raw_score!r}")
            continue
        score = f"{parsed_score[0]}:{parsed_score[1]}"
        if score == primary_score:
            errors.append(f"备选比分 {score} 与主推比分重复")
            continue
        if score in seen:
            errors.append(f"备选比分重复：{score}")
            continue
        seen.add(score)
        alternatives.append(score)
        alt_result = get_result_label(*parsed_score)
        alt_interval = goal_diff_to_core_interval(parsed_score[0] - parsed_score[1])
        reasons = []
        if result is not None and alt_result != result:
            reasons.append(f"结果方向为{alt_result}，不同于核心方向{result}")
        if interval is not None and alt_interval != interval:
            reasons.append(f"净胜球区间为{alt_interval}，不同于核心区间{interval}")
        if reasons:
            note = f"备选比分 {score} 属于替代剧本：" + "；".join(reasons)
            warnings.append(note)
            alternative_scripts.append({"score": score, "result": alt_result, "interval": alt_interval, "reason": reasons})

    confidence = str(original.get("confidence") or "").strip()
    if confidence and confidence not in {"低", "中", "高"}:
        errors.append(f"confidence 非法：{confidence!r}；允许值为低/中/高")

    # 可选：净胜球五类概率分布。缺失不阻断（走 v1 单点降级）；非法不伪造，
    # 给 warning 并置 None，由后处理按 v1 首选比分单点口径推导并标注局限。
    probs_check = validate_net_goal_probabilities(original.get("net_goal_probabilities"))
    if original.get("net_goal_probabilities") is not None and not probs_check["ok"]:
        warnings.append(
            f"net_goal_probabilities 无效（{probs_check['reason']}）；"
            "让球推导按首选比分单点降级，不以备选比分计数或平均分冒充概率"
        )
    elif probs_check["ok"] and probs_check["normalized"]:
        warnings.append("net_goal_probabilities 合计偏离 100%（1% 容差内），已按比例归一化")
    for _w in probs_check["warnings"]:
        warnings.append(f"net_goal_probabilities：{_w}")

    match_script = str(original.get("match_script") or "").strip()
    net_reasoning = str(original.get("net_goal_reasoning") or "").strip()
    uncertainties = original.get("uncertainties", [])
    if not isinstance(uncertainties, list):
        errors.append("uncertainties 必须是数组")
        uncertainties = []
    uncertainties = [str(x).strip() for x in uncertainties if str(x).strip()]

    normalized = {
        "home_team": home or expected_home,
        "away_team": away or expected_away,
        "full_time_result": result,
        "net_goal_interval": interval,
        "primary_score": primary_score,
        "alternative_scores": alternatives,
        "match_script": match_script,
        "net_goal_reasoning": net_reasoning,
        "uncertainties": uncertainties,
        "confidence": confidence if confidence in {"低", "中", "高"} else "",
        "net_goal_probabilities": probs_check["probabilities"],
    }
    chain_ok = bool(
        result is not None and interval is not None and primary_parsed is not None
        and actual_result == result and actual_interval == interval
    )
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "unjudgeable": unjudgeable,
        "normalized": normalized,
        "chain_ok": chain_ok,
        "alternative_scripts": alternative_scripts,
        "original": original,
    }


def _extract_handicap_direction(rq_rec: str) -> Optional[str]:
    """从让球推荐字符串提取方向。

    例：'让-1主胜' → '主胜', '让+1客胜' → '客胜', '让-1平' → '平'。
    若只有方向（如 '主胜'）也可直接返回。
    """
    if not rq_rec:
        return None
    s = rq_rec.strip()
    if "主胜" in s:
        return "主胜"
    if "客胜" in s:
        return "客胜"
    if "平" in s:
        return "平"
    return None


def derive_handicap_recommendation(score_str: str, handicap) -> str:
    """根据首选比分和体彩让球数，推导出正确的让球推荐字符串。

    返回格式：'让-1主胜' / '让+1客胜' / '让-1平'。
    若让球无法解析，返回 '让球无法判定'。
    """
    score = parse_score(score_str)
    hc = parse_handicap(handicap)
    if score is None or hc is None:
        return "让球无法判定"
    result = compute_handicap_result(score[0], score[1], hc)
    sign = "+" if hc >= 0 else ""
    return f"让{sign}{hc}{result}"


# ============ 赔率隐含概率与让球临界线（A 思路：赔率辅助，不替代独立判断） ============

def _to_float(value) -> Optional[float]:
    """尽力把赔率转成正浮点数；无法解析或非正数返回 None。"""
    if value is None:
        return None
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def implied_probabilities(odds) -> Dict[str, float]:
    """由十进制赔率计算去水（归一化）后的隐含概率。

    - 输入为 {选项: 赔率} 的 dict；非法/缺失项跳过，非 dict 或全无效返回 {}。
    - 归一化后各项之和为 1（剔除庄家水位），仅用于市场倾向排序。
    """
    if not isinstance(odds, dict):
        return {}
    raw = {}
    for key, val in odds.items():
        num = _to_float(val)
        if num is not None:
            raw[str(key)] = 1.0 / num
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}


def rank_outcomes_by_implied_prob(odds) -> List[Tuple[str, float]]:
    """按隐含概率从高到低排序，返回 [(选项, 概率), ...]；无有效赔率返回 []。"""
    probs = implied_probabilities(odds)
    return sorted(probs.items(), key=lambda kv: -kv[1])


def handicap_critical_goal_diff(handicap) -> Optional[int]:
    """让球临界净胜球：使体彩让球结果恰为「平」的主队净胜球。

    由 adjusted = 主队进球 + handicap == 客队进球 推得 净胜球 = -handicap。
    例：让球 -1 → 净胜 1 球为让球平；让球 +1 → 净胜 -1（客胜1）为让球平。
    """
    hc = parse_handicap(handicap)
    if hc is None:
        return None
    return -hc


def interval_net_goal_range(net_goal_interval) -> Optional[Tuple[int, int]]:
    """把核心净胜球区间映射为可枚举的净胜球整数范围 (min, max)。

    "主胜2+"/"客胜2+" 为开区间，用代表上/下界（±8）枚举，足以覆盖让球方向变化。
    无法识别返回 None（严禁默认通过）。
    """
    text = str(net_goal_interval or "").strip()
    if not text:
        return None
    if "主胜2" in text:
        return (2, 8)
    if "主胜1" in text:
        return (1, 1)
    if "客胜1" in text:
        return (-1, -1)
    if "客胜2" in text:
        return (-8, -2)
    if "平" in text:
        return (0, 0)
    return None


def interval_handicap_directions(net_goal_interval, handicap) -> List[str]:
    """在给定净胜球区间与让球数下，让球胜平负所有可能出现的方向（保序去重）。"""
    rng = interval_net_goal_range(net_goal_interval)
    hc = parse_handicap(handicap)
    if rng is None or hc is None:
        return []
    lo, hi = rng
    dirs: List[str] = []
    for net in range(lo, hi + 1):
        adjusted = net + hc
        label = "主胜" if adjusted > 0 else ("平" if adjusted == 0 else "客胜")
        if label not in dirs:
            dirs.append(label)
    return dirs


def analyze_handicap_line(primary_score, net_goal_interval, handicap, spf="",
                          handicap_odds=None, spf_odds=None) -> Dict:
    """A 思路「让球临界线」分析（纯函数，不依赖网络）。

    以「首选比分 + 净胜球区间 + 体彩让球数」为核心推导让球胜平负推荐，并给出：
    - derived                程序推导的让球推荐字符串（如 "让-1平"）
    - critical_goal_diff     让球临界净胜球（净该球数即让球平）
    - interval_directions    净胜球区间在让球后可能出现的方向集合
    - interval_consistency   首选比分方向是否落在区间允许方向内（通过/提示/无法判定）
    - spf_vs_handicap        胜平负方向与让球方向是否一致（一致/分离/无法判定）
    - walk_line_risk         区间横跨临界净胜球 → 让球平走盘风险
    - market                 赔率隐含概率排序（辅助市场倾向）
    - unjudgeable / notes    无法判定项与说明（供报告展示）

    赔率只用于识别市场倾向与临界线风险，不替代独立判断，也不直接改写让球结论。
    """
    out = {
        "derived": "让球无法判定",
        "critical_goal_diff": None,
        "interval_directions": [],
        "interval_consistency": "无法判定",
        "spf_vs_handicap": "无法判定",
        "walk_line_risk": False,
        "market": [],
        "unjudgeable": [],
        "notes": [],
    }
    hc = parse_handicap(handicap)
    score = parse_score(primary_score)
    if hc is None:
        out["unjudgeable"].append("让球数缺失或非法，让球临界线无法判定")
    if score is None:
        out["unjudgeable"].append("首选比分缺失或非法，让球推导无法判定")

    if hc is not None:
        out["critical_goal_diff"] = handicap_critical_goal_diff(hc)
        out["derived"] = derive_handicap_recommendation(primary_score, hc) if score is not None else "让球无法判定"

    # 净胜球区间 → 让球后可能方向
    directions = interval_handicap_directions(net_goal_interval, hc) if hc is not None else []
    out["interval_directions"] = directions
    rng = interval_net_goal_range(net_goal_interval)
    crit = out["critical_goal_diff"]

    if rng is None:
        out["unjudgeable"].append(f"净胜球区间 {net_goal_interval!r} 无法识别，无法核对让球临界线")
    elif crit is not None:
        lo, hi = rng
        if lo < hi and lo <= crit <= hi:
            out["walk_line_risk"] = True
            out["notes"].append(
                f"净胜球区间 {net_goal_interval} 横跨让球临界净胜球 {crit}，"
                f"存在走盘（让球平）风险，需在让球理由中说明临界条件。"
            )
        elif lo == hi == crit:
            out["notes"].append(
                f"净胜球区间 {net_goal_interval} 恰落在让球临界净胜球 {crit}，让球平为最可能结果。"
            )
        elif not (lo <= crit <= hi):
            out["notes"].append(
                f"让球临界净胜球 {crit} 不在净胜球区间 {net_goal_interval} 内，"
                f"让球结果不易走盘，方向相对明确。"
            )

    # 首选比分推出的让球方向是否与区间允许方向一致
    derived_dir = _extract_handicap_direction(out["derived"])
    if directions and derived_dir:
        if derived_dir in directions:
            out["interval_consistency"] = "通过"
        else:
            out["interval_consistency"] = "提示"
            out["notes"].append(
                f"首选比分推出的让球方向 {derived_dir} 不在净胜球区间 {net_goal_interval} "
                f"允许的让球方向 {directions} 内，请复核净胜球区间与首选比分。"
            )

    # 胜平负方向 vs 让球方向：允许分离，但必须说明为让球临界导致的合理情况
    spf_norm = normalize_core_result(spf) or ""
    if spf_norm and derived_dir and out["derived"] != "让球无法判定":
        if cn_to_code(spf_norm) == cn_to_code(derived_dir):
            out["spf_vs_handicap"] = "一致"
        else:
            out["spf_vs_handicap"] = "分离"
            out["notes"].append(
                f"胜平负为 {spf_norm}，但让球后结果为 {derived_dir}"
                f"（让球临界净胜球 {crit}，体彩让球 {handicap}），属让球临界导致的合理分离，"
                "不得仅凭胜平负方向直接推断让球方向。"
            )
    elif not spf_norm:
        out["unjudgeable"].append("胜平负缺失，无法核对胜平负与让球方向的一致性")

    # 赔率：市场倾向（辅助，不替代独立判断）
    ranked = rank_outcomes_by_implied_prob(handicap_odds)
    if ranked:
        out["market"] = ranked
        out["notes"].append(
            "让球赔率隐含概率排序（市场倾向，仅辅助）："
            + "、".join(f"{k}({v * 100:.1f}%)" for k, v in ranked)
        )
    spf_ranked = rank_outcomes_by_implied_prob(spf_odds)
    if spf_ranked:
        out["notes"].append(
            "胜平负赔率隐含概率排序（市场倾向，仅辅助）："
            + "、".join(f"{k}({v * 100:.1f}%)" for k, v in spf_ranked)
        )
    return out


# ============ 净胜球五类概率分布 → 让球胜平负概率（v2：分布口径，v1 单点函数保持不变） ============

HANDICAP_VERSION_V1 = "v1_首选比分单点"
HANDICAP_VERSION_V2 = "v2_净胜球分布"

# 五类互斥净胜球结果（桶），顺序即正式输出顺序
NET_GOAL_BUCKETS = ("主胜2+", "主胜1", "平", "客胜1", "客胜2+")

# 概率合计容差（分数口径）：|合计-1| <= 1% 视为合法并按比例归一化；超出即判非法 → 降级，绝不伪造
NET_GOAL_PROB_SUM_TOL = 0.01

# 主推/次选决策阈值（确定性规则，不含任何让平出现比例）
DOUBLE_PICK_GAP = 0.08             # 精确分布下，前二概率差小于该值 → 建议双选
DOUBLE_PICK_TOP_MIN = 0.40         # 最高概率低于该值 → 不输出单一确定性主推
MODEL_MARKET_CONFLICT_MIN = 0.40   # 模型与市场主推各自概率均不低于该值且方向不同 → 标记分歧（不翻转）

# 市场交叉验证状态：与旧 market_conflict 布尔字段并存，区分“无分歧”与“证据不足”。
MARKET_STATE_CONFLICT = "存在分歧"
MARKET_STATE_NO_CONFLICT = "无分歧"
MARKET_STATE_INDETERMINATE = "无法判断"

HANDICAP_OUTCOMES = ("主胜", "平", "客胜")


def _canonical_bucket_key(key) -> Optional[str]:
    """把模型可能使用的桶名称映射为规范桶名；无法识别返回 None。"""
    s = str(key or "")
    low = s.lower()
    has_home = ("主" in s) or ("home" in low) or low.strip().startswith("h")
    has_away = ("客" in s) or ("away" in low) or low.strip().startswith("a")
    has_draw = ("平" in s) or ("draw" in low)
    if has_home and "2" in s:
        return "主胜2+"
    if has_home and "1" in s:
        return "主胜1"
    if has_away and "1" in s:
        return "客胜1"
    if has_away and "2" in s:
        return "客胜2+"
    if has_draw and not has_home and not has_away:
        return "平"
    return None


def validate_net_goal_probabilities(raw) -> Dict:
    """校验模型给出的五类净胜球概率。纯函数，不修改输入。

    合法标准：
      - 必须为 dict，且能映射出全部 5 个互斥桶（允许中英文别名）；
      - 每项为有限数值且 >= 0；
      - 自动识别百分数口径（任一项 > 1 即按 /100 换算，>100 判非法）；
      - 合计与 1 的偏差不超过 NET_GOAL_PROB_SUM_TOL，随后按比例归一化（消除浮点误差）。

    返回：
      ok            是否合法可用
      probabilities 归一化后的 {桶: 概率}，五项合计恰为 1；非法/缺失时为 None
      normalized    是否做过比例归一化（含百分数换算后的合计修正）
      warnings      非阻断性说明
      reason        缺失/非法原因（降级标记直接使用，调用方不得另造概率）
    """
    out = {"ok": False, "probabilities": None, "normalized": False,
           "warnings": [], "reason": ""}
    if raw is None or (isinstance(raw, dict) and not raw):
        out["reason"] = "模型未提供净胜球概率分布"
        return out
    if not isinstance(raw, dict):
        out["reason"] = "净胜球概率分布不是 JSON 对象"
        return out

    buckets: Dict[str, float] = {}
    for key, val in raw.items():
        canon = _canonical_bucket_key(key)
        if canon is None:
            out["reason"] = f"无法识别的净胜球概率字段：{key!r}"
            return out
        if canon in buckets:
            out["reason"] = f"净胜球概率桶重复：{canon}"
            return out
        try:
            num = float(val)
        except (TypeError, ValueError):
            out["reason"] = f"净胜球概率 {canon} 不是数值：{val!r}"
            return out
        if num != num or num in (float("inf"), float("-inf")):
            out["reason"] = f"净胜球概率 {canon} 非法（NaN/Inf）"
            return out
        if num < 0:
            out["reason"] = f"净胜球概率 {canon} 为负数：{num}"
            return out
        buckets[canon] = num

    missing = [b for b in NET_GOAL_BUCKETS if b not in buckets]
    if missing:
        out["reason"] = "净胜球概率缺少桶：" + "、".join(missing)
        return out

    if max(buckets.values()) > 1.0:
        if max(buckets.values()) > 100.0:
            out["reason"] = "净胜球概率存在大于 100 的项"
            return out
        buckets = {k: v / 100.0 for k, v in buckets.items()}
        out["warnings"].append("模型以百分数口径给出净胜球概率，已换算为分数")

    total = sum(buckets[b] for b in NET_GOAL_BUCKETS)
    if total <= 0:
        out["reason"] = "净胜球概率合计为 0"
        return out
    if abs(total - 1.0) > NET_GOAL_PROB_SUM_TOL + 1e-9:
        out["reason"] = (f"净胜球概率合计 {total * 100:.1f}% 超出 100%±"
                         f"{NET_GOAL_PROB_SUM_TOL * 100:.0f}% 容差，拒绝使用")
        return out

    normalized = {b: min(1.0, max(0.0, buckets[b] / total)) for b in NET_GOAL_BUCKETS}
    # 归一化后强制尾项吸收浮点误差，确保五项合计恰为 1
    drift = 1.0 - sum(normalized.values())
    normalized[NET_GOAL_BUCKETS[0]] += drift
    out.update(ok=True, probabilities=normalized,
               normalized=(abs(total - 1.0) > 1e-9))
    return out


def _margin_outcome(net_goal: int, handicap: int) -> str:
    """净胜球 + 实际让球数 → 让球结果。adjusted = 主队进球 + handicap。"""
    adjusted = net_goal + handicap
    if adjusted > 0:
        return "主胜"
    if adjusted == 0:
        return "平"
    return "客胜"


def _bucket_outcome_labels(bucket: str, handicap: int) -> List[str]:
    """桶内全部可能净胜球在给定让球数下产生的让球结果集合（按 主胜/平/客胜 排序）。

    单点桶（主胜1/平/客胜1）只有一个净胜球；尾部桶（主胜2+/客胜2+）用解析法
    覆盖「端点 + 临界净胜球 -handicap + 无穷远极限」三类样本，不依赖固定枚举
    上界，保证 |让球|>=2 时尾部跨档（如让-2：净2→让平、净3+→让胜）也能识别。
    """
    crit = -handicap
    if bucket == "主胜1":
        samples = [1]
    elif bucket == "平":
        samples = [0]
    elif bucket == "客胜1":
        samples = [-1]
    elif bucket == "主胜2+":
        samples = [2, float("inf")]
        if crit >= 2:
            samples.append(crit)
    elif bucket == "客胜2+":
        samples = [-2, float("-inf")]
        if crit <= -2:
            samples.append(crit)
    else:
        return []

    labels: List[str] = []
    for d in samples:
        if d == float("inf"):
            label = "主胜"
        elif d == float("-inf"):
            label = "客胜"
        else:
            label = _margin_outcome(int(d), handicap)
        if label not in labels:
            labels.append(label)
    order = {"主胜": 0, "平": 1, "客胜": 2}
    return sorted(labels, key=lambda x: order[x])


def map_net_goal_probs_to_handicap(probabilities: Dict, handicap) -> Dict:
    """五桶互斥净胜球概率 → 让球胜平负概率的确定性映射。

    规则：净胜球 d 在让球 h 下结果为 sign(d + h)。
      - 桶内让球结果唯一 → 整桶概率精确计入该结果；
      - 桶内结果不唯一（仅 |让球|>=2 时的尾部桶可能发生）→ 该桶质量不可拆分：
        不平均、不猜测，给每个候选结果计入 [0, mass] 的开区间，exact=False 并在
        straddles 中显式列出。

    返回：
      handicap       解析后的整数让球数（None 表示让球数非法）
      exact          三概率是否可精确给出
      probabilities  exact=True 时为精确三概率（合计 1）；False 时为各结果下界
      bounds         各结果 [下界, 上界]；exact=True 时上下界相等
      straddles      跨档尾部桶明细 [{bucket, mass, labels}]
      bucket_allocation  逐桶审计明细：{桶: {结果: 精确质量或 None(跨档)}}
    """
    hc = parse_handicap(handicap)
    out = {
        "handicap": hc,
        "exact": False,
        "probabilities": {k: 0.0 for k in HANDICAP_OUTCOMES},
        "bounds": {k: [0.0, 0.0] for k in HANDICAP_OUTCOMES},
        "straddles": [],
        "bucket_allocation": {},
    }
    if hc is None or not isinstance(probabilities, dict):
        return out

    lo = {k: 0.0 for k in HANDICAP_OUTCOMES}
    straddle_mass = {k: 0.0 for k in HANDICAP_OUTCOMES}
    exact = True
    for bucket in NET_GOAL_BUCKETS:
        mass = float(probabilities.get(bucket, 0.0) or 0.0)
        labels = _bucket_outcome_labels(bucket, hc)
        alloc = {k: None for k in HANDICAP_OUTCOMES}
        if len(labels) == 1:
            label = labels[0]
            lo[label] += mass
            alloc[label] = mass
        else:
            exact = False
            out["straddles"].append({"bucket": bucket, "mass": mass, "labels": labels})
            for label in labels:
                straddle_mass[label] += mass
        out["bucket_allocation"][bucket] = alloc

    bounds = {}
    for label in HANDICAP_OUTCOMES:
        lo_v = min(1.0, max(0.0, lo[label]))
        hi_v = min(1.0, lo_v + straddle_mass[label])
        bounds[label] = [lo_v, hi_v]
    out["bounds"] = bounds
    out["exact"] = exact
    if exact:
        total = sum(lo.values())
        out["probabilities"] = {k: (lo[k] / total if total else 0.0) for k in HANDICAP_OUTCOMES}
    else:
        out["probabilities"] = {k: bounds[k][0] for k in HANDICAP_OUTCOMES}
    return out


def format_handicap_pick(handicap, label: str) -> str:
    """让球数 + 方向 → 正式推荐字符串（与 v1 同格式：让-1平 / 让+1主胜）。"""
    hc = parse_handicap(handicap)
    if hc is None or label not in HANDICAP_OUTCOMES:
        return "让球无法判定"
    sign = "+" if hc >= 0 else ""
    return f"让{sign}{hc}{label}"


def select_handicap_picks(mapping: Dict, handicap_odds=None) -> Dict:
    """由让球结果概率（+让球赔率辅助）生成主推/次选与风险说明。纯函数。

    - exact 分布：按概率排序；最高概率不足 DOUBLE_PICK_TOP_MIN 或领先次选不足
      DOUBLE_PICK_GAP → double=True，建议双选，但仍给出确定性主推。
    - 区间分布（尾部跨档）：主推候选取概率下界最高者；仅当其下界严格高于其余
      选项上界才称占优，否则必须标记不确定、建议双选。
    - 赔率只做市场交叉验证：主推方向明显分歧时 market_conflict=True 并要求人工
      复核，绝不据此翻转模型分布结论。无让平固定比例，无任何方向配额。
    """
    notes: List[str] = []
    uncertainties: List[str] = []
    exact = bool(mapping.get("exact"))
    hc = mapping.get("handicap")
    double = False

    if exact:
        probs = mapping.get("probabilities", {})
        ranked = sorted(probs.items(), key=lambda kv: (-kv[1], HANDICAP_OUTCOMES.index(kv[0])))
        primary, p1 = ranked[0]
        secondary, p2 = ranked[1]
        if p1 < DOUBLE_PICK_TOP_MIN or (p1 - p2) < DOUBLE_PICK_GAP:
            double = True
            uncertainties.append(
                f"让球最高概率仅 {p1 * 100:.1f}%（规则：最高需≥{DOUBLE_PICK_TOP_MIN * 100:.0f}% "
                f"且领先次选≥{DOUBLE_PICK_GAP * 100:.0f} 个百分点方可单博），建议双选 {primary}/{secondary}"
            )
        notes.append("让球结果概率："
                     + "、".join(f"{k} {probs.get(k, 0.0) * 100:.1f}%" for k in HANDICAP_OUTCOMES))
    else:
        bounds = mapping.get("bounds", {})
        ranked = sorted(HANDICAP_OUTCOMES, key=lambda k: (-bounds.get(k, [0.0])[0],
                                                          HANDICAP_OUTCOMES.index(k)))
        primary, secondary = ranked[0], ranked[1]
        double = True
        hc_text = f"让+{hc}" if hc >= 0 else f"让{hc}"
        for st in mapping.get("straddles", []):
            uncertainties.append(
                f"尾部净胜球桶 {st['bucket']}（概率 {st['mass'] * 100:.1f}%）在{hc_text}下同时对应"
                + "、".join(st["labels"]) + "，缺少桶内次级分布，无法精确拆分，按概率区间处理"
            )
        notes.append("让球结果概率区间："
                     + "、".join(f"{k} {bounds.get(k, [0.0, 0.0])[0] * 100:.1f}%~"
                                   f"{bounds.get(k, [0.0, 0.0])[1] * 100:.1f}%"
                                 for k in HANDICAP_OUTCOMES))
        others = [x for x in HANDICAP_OUTCOMES if x != primary]
        dominates = all(bounds[primary][0] > bounds[x][1] for x in others)
        if not dominates:
            uncertainties.append(
                "让球概率为区间且主推候选下界未严格高于其余选项上界，结论不确定，"
                f"建议双选 {primary}/{secondary} 并降低仓位"
            )

    market = rank_outcomes_by_implied_prob(handicap_odds)
    market_conflict = False
    market_conflict_state = MARKET_STATE_INDETERMINATE
    if market:
        notes.append("让球赔率隐含概率（市场，仅辅助交叉验证）："
                     + "、".join(f"{k} {v * 100:.1f}%" for k, v in market))
        m_primary, m_p1 = market[0]
        if m_primary == primary:
            market_conflict_state = MARKET_STATE_NO_CONFLICT
        else:
            # 精确分布保持旧判定阈值与告警语义；区间分布必须使用模型主推概率下界，
            # 不使用区间中点或上界，避免把未分配的跨档质量当成确定概率。
            if exact:
                p_primary = mapping.get("probabilities", {}).get(primary, 0.0)
            else:
                p_primary = mapping.get("bounds", {}).get(primary, [0.0, 0.0])[0]

            if m_p1 < MODEL_MARKET_CONFLICT_MIN:
                # 市场自身没有达到“明显倾向”门槛，按既有业务口径不报分歧。
                market_conflict_state = MARKET_STATE_NO_CONFLICT
            elif p_primary >= MODEL_MARKET_CONFLICT_MIN:
                market_conflict = True
                market_conflict_state = MARKET_STATE_CONFLICT
                probability_label = "概率" if exact else "下界"
                uncertainties.append(
                    f"模型分布主推 {primary}（{probability_label} {p_primary * 100:.1f}%）与让球赔率市场倾向 "
                    f"{m_primary}（{m_p1 * 100:.1f}%）明显分歧；保留模型分布结论，需人工复核"
                )
            elif not exact:
                # 区间场景的主推下界不足以确认模型方向强度，不能把“未确认”记作“无分歧”。
                market_conflict_state = MARKET_STATE_INDETERMINATE
                uncertainties.append(
                    f"模型区间主推 {primary} 的概率下界仅 {p_primary * 100:.1f}%，"
                    f"市场倾向 {m_primary}（{m_p1 * 100:.1f}%）；市场分歧状态无法判断，需人工复核"
                )
            else:
                # exact 分支保留旧行为：未同时达到双方阈值时不置冲突、不新增旧告警。
                market_conflict_state = MARKET_STATE_NO_CONFLICT
    else:
        notes.append("让球赔率缺失或无法解析，未做市场概率交叉验证")

    return {
        "primary": primary,
        "secondary": secondary,
        "double": double,
        "exact": exact,
        "probabilities": mapping.get("probabilities"),
        "bounds": mapping.get("bounds"),
        "straddles": mapping.get("straddles", []),
        "market": market,
        "market_conflict": market_conflict,
        "market_conflict_state": market_conflict_state,
        "notes": notes,
        "uncertainties": uncertainties,
    }


def format_net_goal_probabilities(probs) -> str:
    """五桶概率 → CSV/理由文本：'主胜2+ 42.0% / 主胜1 28.0% / …'；非法输入返回 ''。

    校验：必须包含全部五桶键；每桶为有限数；0 ≤ 值 ≤ 1；合计在 1% 容差内；
    缺失桶不默认补 0——必须显式提供完整分布才格式化。
    """
    if not isinstance(probs, dict):
        return ""
    if any(b not in probs for b in NET_GOAL_BUCKETS):
        return ""
    parts = []
    total = 0.0
    for b in NET_GOAL_BUCKETS:
        try:
            num = float(probs[b])
        except (TypeError, ValueError):
            return ""
        if not (0 <= num <= 1):
            # NaN/inf 在此处也被拒绝（所有与 NaN 的比较均返回 False）。
            return ""
        total += num
        parts.append(f"{b} {num * 100:.1f}%")
    if abs(total - 1.0) > NET_GOAL_PROB_SUM_TOL:
        return ""
    return " / ".join(parts)


def parse_net_goal_probabilities_text(text) -> Optional[Dict[str, float]]:
    """format_net_goal_probabilities 的逆解析（供复盘从 CSV 还原分布）；失败返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    probs = {}
    for b in NET_GOAL_BUCKETS:
        m = re.search(re.escape(b) + r"\s*(\d+(?:\.\d+)?)\s*%", text)
        if not m:
            return None
        probs[b] = float(m.group(1)) / 100.0
    if abs(sum(probs.values()) - 1.0) > 1e-6:
        return None
    return probs


def format_handicap_probabilities(payload) -> str:
    """让球结果概率结构化负载 → CSV 文本；非法输入返回 ''。

    exact 分支：必须包含 HANDICAP_OUTCOMES 全部键；缺失键不默认补 0；每值有限且
    0 ≤ p ≤ 1；合计在 1% 容差内。
    区间分支：每个结果必须有上下界对；每对满足 0 ≤ lo ≤ hi ≤ 1；缺失结果键不默认补 [0,0]。
    NaN/inf 通过 `not (0 <= x <= 1)` 一并拒绝。
    """
    if not isinstance(payload, dict):
        return ""
    exact = payload.get("exact")
    probs = payload.get("probabilities")
    bounds = payload.get("bounds")
    if exact is True:
        if not isinstance(probs, dict):
            return ""
        if any(k not in probs for k in HANDICAP_OUTCOMES):
            return ""  # 缺失键不默认补 0
        parts = []
        total = 0.0
        for k in HANDICAP_OUTCOMES:
            try:
                num = float(probs[k])
            except (TypeError, ValueError):
                return ""
            if not (0 <= num <= 1):
                return ""
            total += num
            parts.append(f"{k} {num * 100:.1f}%")
        if abs(total - 1.0) > NET_GOAL_PROB_SUM_TOL:
            return ""
        return " / ".join(parts)
    if exact is False:
        if not isinstance(bounds, dict):
            return ""
        if any(k not in bounds for k in HANDICAP_OUTCOMES):
            return ""  # 缺失结果键不默认补 [0,0]
        parts = []
        for k in HANDICAP_OUTCOMES:
            pair = bounds[k]
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                return ""
            try:
                lo, hi = float(pair[0]), float(pair[1])
            except (TypeError, ValueError):
                return ""
            if not (0 <= lo <= hi <= 1):
                # 拒绝 lo>hi、lo<0、hi>1、NaN/inf（任一比较返回 False）
                return ""
            parts.append(f"{k} {lo * 100:.1f}%~{hi * 100:.1f}%")
        return " / ".join(parts)
    return ""


# ============ 历史接口别名（保持旧调用方兼容） ============

GOAL_DIFF_INTERVALS = CORE_GOAL_DIFF_INTERVALS
normalize_result_label = normalize_core_result
normalize_goal_interval = normalize_core_goal_interval
goal_diff_to_interval = goal_diff_to_core_interval
extract_handicap_direction = _extract_handicap_direction


def format_core_validation_report(validation, attempts=1):
    """将核心剧本校验结果格式化为可写入 CSV 的文本。"""
    if not isinstance(validation, dict):
        return (f"【核心剧本校验】核心剧本校验未通过：无有效校验报告；"
                f"阶段一 LLM 调用 {attempts} 次")
    if validation.get("ok"):
        status_line = "核心剧本校验通过，锁定核心剧本"
    else:
        status_line = "核心剧本校验未通过"
    parts = [f"【核心剧本校验】{status_line}", f"阶段一 LLM 调用 {attempts} 次"]
    for err in validation.get("errors", []):
        parts.append(f"错误：{err}")
    for warning in validation.get("warnings", []):
        parts.append(f"提示：{warning}")
    for item in validation.get("unjudgeable", []):
        parts.append(f"待确认：{item}")
    return "\n".join(parts)
