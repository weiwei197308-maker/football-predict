"""影子实验统计模块 — 冻结常量与口径定义。

所有统计逻辑通过本文件访问常量，不硬编码任何数字、状态、阈值。
修改统计口径必须先改本文件，再跑测试验证。
"""
import re

# ========== 版本 ==========
SCHEMA_VERSION = "1.0"
MODULE_VERSION = "1.0"

# ========== 输出目录 ==========
SHADOW_DIR = "shadow_experiment"
DATA_DIR = f"{SHADOW_DIR}/data"
REPORTS_DIR = f"{SHADOW_DIR}/reports"
RUN_LOGS_DIR = f"{SHADOW_DIR}/run_logs"
MASTER_PATH = f"{DATA_DIR}/shadow_master.json"

# ========== 保护文件（执行后哈希比对） ==========
# glob 模式，与 utils.hash_files 配合
PROTECTED_FILE_PATTERNS = [
    "analyze.py",
    "lottery_rules.py",
    "settle_shadow.py",
    "ttg_shadow.py",  # 影子实验收集模块（影子模块只读其函数，不修改）
    "rq_shadow.py",    # 让球影子实验收集模块
    "hafu_shadow.py",  # 半全场影子实验收集模块
]

# ========== 标签 → 数值 / 区间映射 ==========
TTG_LABELS = ["0球", "1球", "2球", "3球", "4球", "5球", "6球", "7+球"]
FIXED_BASELINE_PICKS = ["2球", "3球", "4球"]  # D 组固定候选，硬编码但在 config 里冻结
TTG_C2_GROUP = "C2独立"  # 总进球独立思考版对照组（仅总进球，默认启用，DISABLE_TTG_C2=1 关闭）


def label_to_number(label):
    """总进球标签 → 数值。7+球 映射为 7.0（开放区间下界）。非法返回 None。"""
    t = str(label or "").strip()
    if not t:
        return None
    if t.startswith("7+") or t.startswith("7＋"):
        return 7.0
    m = re.match(r"^(\d+)", t)
    if m:
        return float(m.group(1))
    return None


def label_to_interval(label):
    """总进球标签 → (lower, upper, is_open) 区间三元组。

    闭区间：6球 → (6, 6, False)
    右开放区间：7+球 → (7, float('inf'), True)
    非法返回 None。
    """
    t = str(label or "").strip()
    if not t:
        return None
    if t.startswith("7+") or t.startswith("7＋"):
        return (7.0, float("inf"), True)
    m = re.match(r"^(\d+)", t)
    if m:
        n = float(m.group(1))
        return (n, n, False)
    return None


def interval_distance(interval_a, interval_b):
    """两个区间的最短距离。

    相交返回 0。不相交返回 gap（a<b 时 b.lower−a.upper，a>b 时相反）。
    interval: (lower, upper, is_open) 或 (lower, upper)
    """
    if interval_a is None or interval_b is None:
        return None
    la, ua = interval_a[0], interval_a[1]
    lb, ub = interval_b[0], interval_b[1]
    if la <= ub and lb <= ua:
        return 0.0  # 两区间相交
    if ua < lb:
        return lb - ua
    return la - ub


def is_open_interval(label):
    """标签是否为开放区间（仅 7+球）。"""
    return str(label or "").strip().startswith("7+") or str(label or "").strip().startswith("7＋")


# ========== 5 类输出分类（优先级从高到低，互斥穷尽） ==========
# 返回的类别名
CAT_RUN_FAILED = "运行失败"
CAT_ILLEGAL = "非法输出"
CAT_UNKNOWN = "未知状态"
CAT_VALID = "有效预测"
CAT_EMPTY = "空输出"

# 运行失败状态集合（ttg_shadow 定义）
FAILED_STATES = {"api_error", "skipped", "not_run"}

# 非法输出状态
ILLEGAL_STATES = {"illegal"}

# 有效候选状态
VALID_STATES = {"ok", "empty"}


def classify_output(group_dict, canon_fn=None):
    """对一个组（A盲判/B复核/C正式/D固定）的输出分类。

    group_dict: {"状态": str, "候选": list, ...}  —— 必须是 dict，否则回退到 {}
    canon_fn: 候选合法性校验函数（None 时跳过深度校验）

    返回: (类别名, 理由字符串)
    """
    # 类型归一化兜底（上游若传入 str/None/list 一律回退到空 dict）
    if not isinstance(group_dict, dict):
        group_dict = {}

    state = str(group_dict.get("状态", "") or "")
    picks = list(group_dict.get("候选", []) or [])

    if state in FAILED_STATES:
        return (CAT_RUN_FAILED, f"状态={state}")

    if state in ILLEGAL_STATES:
        return (CAT_ILLEGAL, f"状态={state}")

    if state not in VALID_STATES:
        return (CAT_UNKNOWN, f"未知状态={state}")

    # state ∈ VALID_STATES，检查候选
    if not picks:
        return (CAT_EMPTY, "候选为空")

    # 候选合法性深度校验
    if canon_fn is not None:
        legal_picks = []
        for p in picks:
            num = label_to_number(p)
            if num is None:
                return (CAT_EMPTY, f"候选含非法标签: {p}")
            legal_picks.append(p)
        if not legal_picks:
            return (CAT_EMPTY, "候选全部非法")

    return (CAT_VALID, f"候选数={len(picks)}")


# ========== 6 个极端进球桶 ==========
EXTREME_BUCKETS = [
    ("B01", "0-1球", {"0球", "1球"}),
    ("B2", "2球", {"2球"}),
    ("B34", "3-4球", {"3球", "4球"}),
    ("B5", "5球", {"5球"}),
    ("B6", "6球", {"6球"}),
    ("B7P", "7+球（开放）", {"7+球"}),
]


def bucket_for_actual(actual_label):
    """实际赛果标签 → 桶 ID。"""
    for bid, bname, labels in EXTREME_BUCKETS:
        if actual_label in labels:
            return bid
    return None


# ========== 阶段观察节点 ==========
PHASE_THRESHOLDS = [
    (0, 30, "数据积累中，无统计结论"),
    (30, 100, "初步观察阶段，指标为描述性统计"),
    (100, 300, "中期观察阶段，跨阶段对比反映趋势"),
    (300, float("inf"), "充分观察阶段，仍不构成统计显著性结论"),
]


def phase_for_n(n_settled):
    """已结算场次 → (阶段编号, 阶段描述)。"""
    if n_settled < 30:
        return (1, "数据积累中，无统计结论")
    if n_settled < 100:
        return (2, "初步观察阶段，指标为描述性统计")
    if n_settled < 300:
        return (3, "中期观察阶段，跨阶段对比反映趋势")
    return (4, "充分观察阶段，仍不构成统计显著性结论")


# ========== 300 场观察节点（预注册口径） ==========
NODE_THRESHOLD = 300
NODE_ALPHA = 0.05
NODE_N_PLAYS = 3
NODE_ALPHA_CORRECTED = NODE_ALPHA / NODE_N_PLAYS
NODE_OBSERVE_PLAYS = ("总进球", "让球", "半全场")


# ========== 配对群体准入条件 ==========
# P1 首选误差配对
P1_REQUIREMENTS = {
    "A": {"类别": CAT_VALID, "有序": True, "候选非空": True},
    "B": {"类别": CAT_VALID, "有序": True, "候选非空": True},
}
# P2 集合距离配对
P2_REQUIREMENTS = {
    "A": {"类别": CAT_VALID, "候选非空": True},
    "B": {"类别": CAT_VALID, "候选非空": True},
}
# P3 覆盖变化配对
P3_REQUIREMENTS = {
    "A": {"类别": CAT_VALID, "候选非空": True},
    "B": {"类别": CAT_VALID, "候选非空": True},
}


# ========== A→B 分类决策树 ==========
def classify_ab_change(a_picks, b_picks):
    """A/B 候选集合 → AB-0/1/2/3 分类。

    互斥穷尽 4 类：
      AB-0: 首选不变 AND 集合不变
      AB-1: 首选变化 AND 集合不变（仅调顺序）
      AB-2: 首选不变 AND 集合变化（集合微调）
      AB-3: 首选变化 AND 集合变化（实质性变更）
    """
    a_list = list(a_picks or [])
    b_list = list(b_picks or [])
    p1 = (a_list[:1] != b_list[:1])
    s1 = (set(a_list) != set(b_list))

    if not p1 and not s1:
        return "AB-0", "无变更"
    if p1 and not s1:
        return "AB-1", "仅调顺序"
    if not p1 and s1:
        return "AB-2", "集合微调"
    return "AB-3", "实质性变更"


# ========== 方向判断 ==========
def compute_ideal_direction(a_pick_interval, actual_interval):
    """A 首选区间 vs 实际区间 → 理想方向 (+1/-1/0)。

    +1: 实际整体在 A 首选上方，应向上调
    -1: 实际整体在 A 首选下方，应向下调
     0: A 首选已覆盖实际（方向不可判定）
    """
    if a_pick_interval is None or actual_interval is None:
        return None
    la, ua = a_pick_interval[0], a_pick_interval[1]
    lb, ub = actual_interval[0], actual_interval[1]

    # 相交 → A 已覆盖实际
    if la <= ub and lb <= ua:
        return 0
    if ua < lb:
        return +1  # 实际整体在 A 上方
    return -1  # 实际整体在 A 下方


def compute_b_move_direction(a_pick_num, b_pick_num):
    """A 首选数值 vs B 首选数值 → B 移动方向 (+1/-1/0)。"""
    if a_pick_num is None or b_pick_num is None:
        return None
    diff = b_pick_num - a_pick_num
    if diff > 0:
        return +1
    if diff < 0:
        return -1
    return 0


# ========== 市场前三特殊处理 ==========
# 市场前三不是影子实验组，但按同一五分类框架统计
MARKET_GROUP_CONFIG = {
    "名称": "市场前三",
    "候选来源": "赔率升序前三，并列按 0球→7+球 规范顺序裁决",
    "有序": True,  # 永远有序（赔率升序）
    "状态": "ok",   # 永远 ok（赔率缺失时 market_top3 自动跳过）
}


# ========== 多玩法注册表（让球/半全场扩展；总进球既有常量与行为不变） ==========
PLAY_TTG = "总进球"
PLAY_RQ = "让球"
PLAY_HAFU = "半全场"
PLAY_ORDER = [PLAY_TTG, PLAY_RQ, PLAY_HAFU]

# 让球：方向为 主胜/平/客胜（候选已剥离让球前缀，赛果同为方向）
RQ_LABELS = ["主胜", "平", "客胜"]

# 半全场：9 种合法 HDA 组合，规范顺序与正式 CSV 列顺序一致
HAFU_LABELS = ["HH", "HD", "HA", "DH", "DD", "DA", "AH", "AD", "AA"]
HAFU_BASELINE_PICKS = ["HH", "DD", "AA"]  # 半全场 D 组固定候选

PLAY_CONFIG = {
    PLAY_TTG: {
        "文件前缀": "影子实验_总进球_", "实际键": "总进球", "类型": "区间",
        "组": ("A盲判", "B复核", "C正式", "D固定"), "市场键": "市场前三",
        "标签表": TTG_LABELS, "报告后缀": "",
    },
    PLAY_RQ: {
        "文件前缀": "影子实验_让球_", "实际键": "让球", "类型": "方向",
        "组": ("A盲判", "B复核", "C正式", "D规则"), "市场键": "市场倾向",
        "标签表": RQ_LABELS, "报告后缀": "_rq",
    },
    PLAY_HAFU: {
        "文件前缀": "影子实验_半全场_", "实际键": "半全场", "类型": "组合",
        "组": ("A盲判", "B复核", "C正式", "D固定"), "市场键": "市场前三",
        "标签表": HAFU_LABELS, "报告后缀": "_hafu",
    },
}


def classify_output_general(group_dict, canon_fn):
    """对任一玩法某组的输出做 5 类分类（与 classify_output 同优先级、互斥穷尽）。

    与 classify_output 的唯一差异：候选合法性深度校验使用传入的 canon_fn
    （classify_output 硬编码 label_to_number，仅适用于总进球）。
    返回: (类别名, 理由字符串)
    """
    if not isinstance(group_dict, dict):
        group_dict = {}

    state = str(group_dict.get("状态", "") or "")
    picks = list(group_dict.get("候选", []) or [])

    if state in FAILED_STATES:
        return (CAT_RUN_FAILED, f"状态={state}")
    if state in ILLEGAL_STATES:
        return (CAT_ILLEGAL, f"状态={state}")
    if state not in VALID_STATES:
        return (CAT_UNKNOWN, f"未知状态={state}")
    if not picks:
        return (CAT_EMPTY, "候选为空")

    legal_picks = []
    for p in picks:
        if canon_fn is not None and canon_fn(p) is None:
            return (CAT_EMPTY, f"候选含非法标签: {p}")
        legal_picks.append(p)
    if not legal_picks:
        return (CAT_EMPTY, "候选全部非法")

    return (CAT_VALID, f"候选数={len(picks)}")
