import requests
import csv
import json
import os
import sys
import re
import copy
from datetime import date

from football_history import build_history_for_match
from lottery_rules import (
    parse_handicap,
    parse_score,
    parse_hafu_codes,
    parse_hafu_code_from_text,
    hafu_candidates_for_full,
    is_hafu_compatible,
    normalize_result_label,
    run_consistency_checks,
    derive_handicap_recommendation,
    validate_core_script,
    format_core_validation_report,
    analyze_handicap_line,
    goal_diff_to_core_interval,
    map_net_goal_probs_to_handicap,
    select_handicap_picks,
    format_handicap_pick,
    format_net_goal_probabilities,
    format_handicap_probabilities,
    validate_net_goal_probabilities,
    MARKET_STATE_INDETERMINATE,
    HANDICAP_VERSION_V1,
    HANDICAP_VERSION_V2,
)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

API_KEY_FALLBACK = ""  # API Key 仅从环境变量读取，不在源码中保存

# 密钥格式：sk- 开头，后接至少 16 位英文字母/数字；网络调用前 fail-fast 校验
_API_KEY_RE = re.compile(r"^sk-[A-Za-z0-9]{16,}$")


def validate_api_key(api_key):
    """校验 API Key 是否存在且格式合法。返回 (ok, 错误提示)；绝不回显完整密钥。"""
    key = str(api_key or "").strip()
    if not key:
        return False, (
            "未设置 DEEPSEEK_API_KEY 环境变量。请先在终端执行 "
            "export DEEPSEEK_API_KEY=<真实密钥> 后重试；不要把密钥写入代码或聊天窗口。"
        )
    if not _API_KEY_RE.match(key):
        return False, (
            "DEEPSEEK_API_KEY 格式非法（应以 sk- 开头，仅含英文字母/数字，"
            f"sk- 后至少 16 位）。当前值前 3 个字符为 {key[:3]!r}，长度 {len(key)}，"
            "疑似占位符或被中文注释污染，已在调用 API 前停止。"
        )
    return True, ""


def ensure_api_key_for_request(api_key):
    """HTTP 请求前的最后防线：密钥非法时立即抛 ValueError，绝不进入网络层。

    背景：HTTP 头按 latin-1 编码，Authorization 头里的密钥若含中文/非 ASCII
    字符（如占位符"你的真实密钥"），requests 会在发送阶段抛 UnicodeEncodeError。
    validate_api_key 的 ASCII 白名单正则可在调用前拦截该错误。
    """
    ok, err = validate_api_key(api_key)
    if not ok:
        raise ValueError(err)


ERROR_CATEGORIES = [
    ("高估进攻", ["高估", "进攻"]),
    ("低估防守韧性", ["低估", "防守"]),
    ("高估主场优势", ["高估", "主场"]),
    ("低估反击能力", ["低估", "反击"]),
    ("低估客场韧性", ["低估", "客场"]),
    ("高估球星/个人能力", ["高估", "哈兰德"]),
    ("忽略战意/态度", ["忽略", "战意"]),
    ("高估小联赛主队", ["高估", "统治力"]),
]

PRECAUTION_RULES = """
⚠️ 防错规则（请严格遵守）：
- 同类错误出现 1-2 次：只当作小概率随机事件，不要轻易改变基础判断逻辑，最多信心指数降低 0.5
- 同类错误出现 3 次及以上：重点规避此类型错误，分析时主动检查相关维度
- 注意同一条错误记录可能同时命中多个类别，请分别对每个类别应用以上规则"""


def load_lessons():
    lessons_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lessons.txt")
    if not os.path.exists(lessons_path):
        return None
    try:
        with open(lessons_path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if not lines:
            return None

        recent = lines[-50:]

        groups = {}
        for name, keywords in ERROR_CATEGORIES:
            for line in recent:
                if all(kw in line for kw in keywords):
                    groups.setdefault(name, []).append(line)

        if not groups:
            return None

        sorted_groups = sorted(groups.items(), key=lambda x: -len(x[1]))

        parts = ["⚠️ 以下是你过去积累的复盘教训，请参考："]
        for name, entries in sorted_groups:
            count = len(entries)
            if count >= 3:
                tag = "高频！重点规避"
            elif count >= 1:
                tag = "小概率"
            else:
                continue
            parts.append(f"\n【次数:{count}】{name}类错误（{tag}）")
            for e in entries[:3]:
                short = e.replace("[2026-09-27]", "").strip()
                if len(short) > 100:
                    short = short[:100] + "..."
                parts.append(f"  - {short}")

        parts.append(PRECAUTION_RULES)
        return "\n".join(parts)
    except Exception:
        return None


LESSONS_TEXT = load_lessons()


def find_latest_csv(output_dir="output"):
    if not os.path.isdir(output_dir):
        print(f"找不到数据目录：{output_dir}")
        return None

    files = [f for f in os.listdir(output_dir)
             if f.startswith("竞彩足球_") and f.endswith(".csv")
             and "_raw" not in f]

    if not files:
        print(f"{output_dir} 里没有找到 CSV 文件。请先运行 get_matches.py")
        return None

    files.sort(reverse=True)
    return os.path.join(output_dir, files[0])


def load_matches_from_csv(csv_path):
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"已加载 {len(rows)} 场比赛：{csv_path}")
    return rows


def build_match_prompt_data(row, history=None):
    match_num = row.get("编号", "")
    league = row.get("联赛", "")
    home = row.get("主队", "")
    home_rank = row.get("主队排名", "")
    away = row.get("客队", "")
    away_rank = row.get("客队排名", "")
    date_ = row.get("开赛日期", "")
    time_ = row.get("开赛时间", "")

    spf = {
        "主胜": row.get("胜平负-主胜", ""),
        "平": row.get("胜平负-平", ""),
        "客胜": row.get("胜平负-客胜", ""),
    }

    rq_goal = row.get("让球数", "")
    rq = {
        "让球数": rq_goal,
        "主胜": row.get("让球-主胜", ""),
        "平": row.get("让球-平", ""),
        "客胜": row.get("让球-客胜", ""),
    }

    ttg_keys = ["0球", "1球", "2球", "3球", "4球", "5球", "6球", "7+球"]
    ttg = {}
    for k in ttg_keys:
        v = row.get(f"总进球-{k}", "")
        if v:
            ttg[k] = v

    hafu_keys = [
        "半主胜-全主胜", "半主胜-全平", "半主胜-全客胜",
        "半平-全主胜", "半平-全平", "半平-全客胜",
        "半客胜-全主胜", "半客胜-全平", "半客胜-全客胜",
    ]
    hafu = {}
    for k in hafu_keys:
        v = row.get(f"半全场-{k}", "")
        if v:
            hafu[k] = v

    crs = {}
    for col in row.keys():
        if col.startswith("比分-"):
            score = col.replace("比分-", "")
            v = row[col]
            if v:
                crs[score] = v

    data = {
        "编号": match_num,
        "联赛": league,
        "主队": home,
        "客队": away,
        "对阵": f"{home}{home_rank} vs {away}{away_rank}",
        "开赛": f"{date_} {time_}",
        "胜平负赔率": spf,
        "让球赔率": rq,
        "总进球赔率": ttg,
        "半全场赔率": hafu,
        "比分赔率": crs,
    }

    if history and history.get("available"):
        hist_info = {}
        if history.get("home_recent_text"):
            hist_info[f"{home} 近期战绩"] = history["home_recent_text"]
        if history.get("away_recent_text"):
            hist_info[f"{away} 近期战绩"] = history["away_recent_text"]
        if history.get("h2h_text"):
            hist_info["历史交锋"] = history["h2h_text"]
        if hist_info:
            data["历史数据"] = hist_info

    return data


SYSTEM_PROMPT = """你是一位专业的竞彩足球分析师，遵循「中国体育彩票足球分析 A 思路」。

核心原则（必须遵守）：
1. 预测胜平负 ≠ 预测让球胜平负。让球结果由让球数和比分决定，不是主胜的简单延伸。
2. 分析核心是判断「净胜球分布」，而非仅仅判断谁更强。
3. 必须先建立「比赛剧本」，再由同一剧本推导五种玩法。五种玩法必须互相校验，不得互相矛盾。
4. 不得为了迎合赔率或预设结论而强行修改比赛判断。
5. 不得把赛后结果倒灌到赛前预测中。
6. 数据不足时明确标注不确定性，不得编造球队排名、近期战绩、伤停、赔率变化或概率数据。

五大玩法规则：
1. 胜平负 (SPF)：全场主胜 / 平 / 客胜
2. 让球胜平负 (HHAD)：体彩设定让球数（负数=主队让球，正数=客队让球）。
   让球后主队进球 = 主队实际进球 + 让球数，再与客队进球比较。
   例如让球 -1，比分 2:0 → 2+(-1)=1 > 0 → 让胜；比分 1:0 → 0=0 → 让平；比分 1:1 → 0<1 → 让负。
   让球临界净胜球 = -让球数：净胜该球数时让球恰为平。分析必须先判断净胜球区间是否横跨该临界线。
3. 总进球数 (TTG)：两队全场进球总数，0-6 及 7+，推荐 3 个
4. 半全场 (HAFU)：半场+全场结果组合，H=主胜 D=平 A=客胜，如 HH=半主胜全主胜
5. 比分 (CRS)：全场具体比分，推荐 3 个

分析顺序（严格按此顺序思考）：
步骤1【基本面与赔率】：结合赔率识别市场倾向、净胜球临界线与风险，但赔率只作辅助证据，
  不得替代独立判断，也不得机械地把低赔等同于稳胆、或仅因赔率高就排除某结果。
步骤2【比赛剧本】：判断比赛整体倾向（主胜/平/客胜）、可能的比赛剧本、净胜球分布
  （主胜2+ / 主胜1 / 平 / 客胜1 / 客胜2+）、最可能净胜球区间、不确定性因素、支持证据。
步骤3【推导胜平负】：由剧本得出胜平负首选。
步骤4【推导让球胜平负】：先读取体彩实际让球数，算出「让球临界净胜球」（= 让球数取反：净胜该球数时让球恰为平），
  判断净胜球区间是否横跨临界线；再据首选比分推导让球胜/平/负。
  不允许仅根据主胜/客胜直接生成让球结论；胜平负方向与让球方向不一致（如主胜剧本让球为平/负）属合理情况，
  必须在让球理由中说明其临界条件。
步骤5【推导半全场】：先判断上半场走势，再结合全场剧本。半全场的全场部分必须与胜平负一致。
步骤6【推导总进球】：根据比赛节奏和候选比分，总进球必须与首选比分相容。
步骤7【推导比分】：候选比分必须支持胜平负方向、符合净胜球分布、与总进球和让球相容；
  不同净胜球区间的备选比分必须标注为替代剧本。

请严格以 JSON 格式返回，不要输出任何额外文字。格式如下：
{
  "核心剧本": "对比赛整体走势的判断，100字以内",
  "胜平负推荐": "主胜" 或 "平" 或 "客胜",
  "胜平负理由": "引用赔率和历史数据的分析",
  "净胜球分布": "主胜2+ / 主胜1 / 平 / 客胜1 / 客胜2+（选最可能的一个区间）",
  "让球推荐": "让-1主胜" 或 "让+1客胜"（必须包含体彩实际让球数）,
  "让球理由": "由首选比分和让球数推导的过程",
  "总进球推荐": ["2球", "3球", "4球"],
  "总进球理由": "与候选比分交叉验证",
  "半全场推荐": ["HH", "DH", "AH"],
  "半全场理由": "上半场走势 + 全场剧本",
  "比分推荐": ["2:0", "2:1", "1:0"],
  "比分理由": "支持胜平负、净胜球、总进球、让球",
  "信心指数": 1-10 的整数,
  "数据缺失说明": "如有缺失数据及其影响，否则填空字符串",
  "综合分析": "结合历史数据+赔率的整体判断，150字以内"
}

注意：
- 让球推荐必须包含体彩实际让球数（如 "让-1主胜"）。让球数从输入数据的「让球赔率.让球数」字段读取。
- 比分推荐的第一个是首选比分，后续是备选。所有比分必须与胜平负、净胜球、总进球、让球相容。
- 如果有历史数据，请在各条理由中引用。"""


CORE_SCRIPT_PROMPT = """你是一位专业的竞彩足球分析师，遵循「中国体育彩票足球分析 A 思路」。
本阶段只判断核心比赛剧本，不输出任何体彩玩法推荐。

核心原则：预测胜平负不等于预测让球结果；分析重点是最可能的净胜球分布；必须区分主推剧本与替代剧本；赔率和历史数据只能作为证据，不得编造缺失信息。

净胜球区间只能使用以下规范值之一：主胜2+、主胜1、平、客胜1、客胜2+。
full_time_result 只能使用：主胜、平局、客胜。
confidence 只能使用：低、中、高。
比分格式必须为“主队进球:客队进球”。主推比分必须同时符合 full_time_result 和 net_goal_interval。
备选比分可以代表不同剧本，但必须如实返回；程序会将不一致的备选标记为替代剧本，不要把它们伪装成同一净胜球剧本。

只返回合法 JSON，不要 Markdown，不要额外解释。结构如下：
{
  "home_team": "主队名称",
  "away_team": "客队名称",
  "full_time_result": "主胜/平局/客胜",
  "net_goal_interval": "主胜2+/主胜1/平/客胜1/客胜2+",
  "primary_score": "2:1",
  "alternative_scores": ["1:0", "2:0"],
  "match_script": "核心比赛走势，100字以内",
  "net_goal_reasoning": "为何选择该净胜球区间",
  "uncertainties": ["可能改变比赛剧本的因素"],
  "confidence": "低/中/高",
  "net_goal_probabilities": {"主胜2+": 0.0, "主胜1": 0.0, "平": 0.0, "客胜1": 0.0, "客胜2+": 0.0}
}

关于 net_goal_probabilities（净胜球五类互斥结果的概率判断）：
- 该字段可选；如提供，五项必须是 0~1 的小数且合计为 1.0，顺序为 主胜2+、主胜1、平、客胜1、客胜2+。
- 概率必须来自你对比赛剧本、实力差、赔率与历史证据的综合判断，是有主次的真实信念分布；严禁把备选比分平均分配，或用备选比分个数冒充概率。
- 让球胜平负将由程序按本场实际体彩让球数，从这五项概率确定性推导（临界净胜球为让球平），你不需要也不允许在玩法层自行编造让球概率。
- 若确实无法给出有依据的完整分布，请省略该字段或给 null；程序会明确标记概率缺失并按首选比分单点口径降级，绝不替你伪造概率。
"""

GROUNDED_PLAY_INSTRUCTION = """【已通过 Python 校验并锁定的核心剧本】
{locked_script}

后续五种玩法必须围绕这份核心剧本生成，不得另行改变全场胜平负、净胜球区间或主推比分。让球推荐由程序按实际体彩让球数与净胜球概率分布（缺失时按锁定首选比分）做确定性规则推导，你给出的让球推荐会被程序校验或覆盖；你无需也不得在玩法输出中编造任何概率数字。总进球必须包含锁定比分的进球总数；半全场全场部分必须与锁定胜平负一致。若发现不确定因素，请在理由中说明，不得静默更改核心剧本。"""


def call_deepseek(match_data, api_key, extra_user_instruction=""):
    ensure_api_key_for_request(api_key)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    final_system = SYSTEM_PROMPT
    if LESSONS_TEXT:
        final_system = SYSTEM_PROMPT + "\n\n" + LESSONS_TEXT

    user_content = f"""请分析以下这场竞彩足球比赛，给出五个维度的推荐：

{json.dumps(match_data, ensure_ascii=False, indent=2)}"""
    if extra_user_instruction:
        user_content += "\n\n" + extra_user_instruction

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": final_system},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.7,
        "max_tokens": 1500,
    }

    try:
        resp = requests.post(DEEPSEEK_API_URL, headers=headers,
                             json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return content.strip()
    except requests.RequestException as e:
        return f"__API_ERROR__: {e}"
    except (KeyError, IndexError) as e:
        return f"__PARSE_ERROR__: {e}"


def extract_json(text):
    if text.startswith("__"):
        return None, text

    text = text.strip()

    code_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if code_match:
        text = code_match.group(1)

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        text = text[brace_start:brace_end + 1]

    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败：{e}\n原文片段：{text[:200]}"



def call_deepseek_core_script(match_data, api_key, feedback=""):
    """调用 LLM 只生成核心比赛剧本；feedback 用于有限重生成。"""
    ensure_api_key_for_request(api_key)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    user_content = (
        "请根据以下赛前数据生成核心比赛剧本 JSON，不要输出体彩玩法。\n\n"
        + json.dumps(match_data, ensure_ascii=False, indent=2)
    )
    if feedback:
        user_content += "\n\n上一次输出未通过 Python 校验，请根据以下错误清单修正，并返回完整 JSON：\n" + feedback
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": CORE_SCRIPT_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.4,
        "max_tokens": 900,
    }
    try:
        resp = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except requests.RequestException as e:
        return f"__API_ERROR__: {e}"
    except (KeyError, IndexError, TypeError) as e:
        return f"__PARSE_ERROR__: {e}"


def build_core_feedback_text(validation, parse_error=""):
    """构造具体、可执行的核心剧本校验反馈：必须修正 / 待确认 / 建议检查。"""
    lines = []
    if parse_error:
        lines.append(f"必须修正：输出解析错误：{parse_error}")
    if isinstance(validation, dict):
        for e in validation.get("errors", []):
            lines.append(f"必须修正：{e}")
        for u in validation.get("unjudgeable", []):
            lines.append(f"待确认：{u}")
        for w in validation.get("warnings", []):
            lines.append(f"建议检查：{w}")
    if not lines:
        lines.append("必须修正：未通过校验，请重新检查必需字段及核心链一致性。")
    return "\n".join(lines)


class CoreScriptResult(dict):
    """阶段一结果 dict：支持键访问，同时兼容 (normalized, report, error) 三元组解包。"""

    def __iter__(self):
        return iter((self.get("normalized"), self.get("report"), self.get("error")))


def analyze_core_script(match_data, api_key, home="", away="", max_retries=1, return_details=False):
    """阶段一：LLM 输出 → JSON 解析 → Python 校验 → 最多一次错误反馈重生成。

    返回 CoreScriptResult（dict），含 ok/normalized/validation/validation_attempts/
    raw_attempts/llm_calls/parse_errors/report/error；按三元组解包时等价于
    (规范化核心剧本或 None, 校验报告, 错误文本或 None)。
    校验失败时不允许进入正式玩法分析。
    """
    feedback = ""
    last_validation = None
    last_parse_error = ""
    raw_attempts = []
    validation_attempts = []
    parse_errors = []
    attempts = min(2, max(1, int(max_retries) + 1))  # 最多一次重生成

    for attempt in range(attempts):
        raw = call_deepseek_core_script(match_data, api_key, feedback=feedback)
        raw_text = raw if isinstance(raw, str) else repr(raw)
        raw_attempts.append(raw_text)
        parsed, parse_error = extract_json(raw)
        last_parse_error = parse_error or ""
        if parse_error:
            parse_errors.append(parse_error)
            validation = {
                "ok": False,
                "errors": [f"JSON 获取/解析失败：{parse_error}"],
                "warnings": [],
                "unjudgeable": ["无法解析核心剧本 JSON"],
                "normalized": None,
                "chain_ok": False,
                "alternative_scripts": [],
            }
        else:
            validation = validate_core_script(parsed, expected_home=home, expected_away=away)
        last_validation = validation
        # 深拷贝快照，避免后续处理意外改写历史校验结果。
        validation_attempts.append(json.loads(json.dumps(validation, ensure_ascii=False, default=str)))
        if validation.get("ok"):
            report = format_core_validation_report(validation, attempt + 1)
            return CoreScriptResult({
                "ok": True,
                "normalized": validation["normalized"],
                "validation": validation,
                "validation_attempts": validation_attempts,
                "raw_attempts": raw_attempts,
                "llm_calls": attempt + 1,
                "parse_errors": parse_errors,
                "report": report,
                "error": None,
            })
        if attempt + 1 < attempts:
            feedback = build_core_feedback_text(validation, parse_error)

    report = format_core_validation_report(last_validation or {}, len(raw_attempts))
    err = "核心剧本校验未通过，已阻止第二阶段玩法分析"
    if last_parse_error:
        err += f"；最后一次解析错误：{last_parse_error[:160]}"
    return CoreScriptResult({
        "ok": False,
        "normalized": None,
        "validation": last_validation or {},
        "validation_attempts": validation_attempts,
        "raw_attempts": raw_attempts,
        "llm_calls": len(raw_attempts),
        "parse_errors": parse_errors,
        "report": report,
        "error": err,
    })


def format_core_audit_details(details):
    """格式化阶段一审计信息，便于写入现有 CSV 的一致性检查字段。"""
    if not isinstance(details, dict):
        return ""
    audit = {
        "llm_calls": details.get("llm_calls", 0),
        "normalized_script": details.get("normalized"),
        "final_validation": details.get("validation", {}),
        "validation_attempts": details.get("validation_attempts", []),
        "parse_errors": details.get("parse_errors", []),
        "raw_attempts": details.get("raw_attempts", []),
    }
    return "【核心剧本审计详情】\n" + json.dumps(audit, ensure_ascii=False, indent=2, default=str)


def apply_locked_core_script(parsed, core):
    """将已校验核心剧本作为唯一事实依据，覆盖玩法层核心字段。"""
    if not isinstance(parsed, dict) or not isinstance(core, dict):
        return parsed
    parsed["核心剧本"] = core.get("match_script", "")
    parsed["胜平负推荐"] = core.get("full_time_result", "")
    parsed["净胜球分布"] = core.get("net_goal_interval", "")
    primary = core.get("primary_score", "")
    # 只有同方向、同净胜球区间的备选才进入正式比分推荐列表。
    compatible_alternatives = []
    for score in core.get("alternative_scores", []):
        from lottery_rules import parse_score, get_result_label, goal_diff_to_core_interval
        pair = parse_score(score)
        if pair and get_result_label(*pair) == core.get("full_time_result") and goal_diff_to_core_interval(pair[0] - pair[1]) == core.get("net_goal_interval"):
            compatible_alternatives.append(score)
    parsed["比分推荐"] = [x for x in [primary] + compatible_alternatives if x]
    # 阶段一校验后的净胜球概率分布（可能为 None）：内部键，仅供 post_process 做
    # v2 让球推导；下划线前缀不会进入快照「修正后预测」与 CSV 原始字段。
    parsed["_net_goal_probabilities"] = core.get("net_goal_probabilities")
    reasoning = core.get("net_goal_reasoning", "")
    old_score_reason = str(parsed.get("比分理由", "") or "").strip()
    parsed["比分理由"] = f"[核心剧本锁定] {reasoning}" + (f"；玩法层补充：{old_score_reason}" if old_score_reason else "")
    uncertainties = core.get("uncertainties", [])
    if uncertainties:
        old_missing = str(parsed.get("数据缺失说明", "") or "").strip()
        uncertainty_text = "核心剧本不确定因素：" + "；".join(uncertainties)
        parsed["数据缺失说明"] = f"{old_missing}；{uncertainty_text}" if old_missing else uncertainty_text
    return parsed


def analyze_match_two_stage(match_data, api_key, handicap_line, home="", away="", max_retries=1):
    """两阶段编排：先锁定并校验核心剧本，成功后才调用玩法层。"""
    core_details = analyze_core_script(
        match_data, api_key, home=home, away=away, max_retries=max_retries,
        return_details=True,
    )
    core = core_details.get("normalized")
    core_report = core_details.get("report", "")
    core_error = core_details.get("error")
    audit_report = format_core_audit_details(core_details)
    if core_error or not core_details.get("ok") or not isinstance(core, dict):
        blocked_report = "\n\n".join(x for x in [core_report, audit_report,
            "状态：核心剧本校验未通过，已阻止第二阶段正式玩法分析；本场不产生胜平负等任何玩法预测。"] if x)
        return None, blocked_report, core_error or "核心剧本校验未通过，已阻止第二阶段玩法分析"

    locked = {
        "home_team": core.get("home_team", home),
        "away_team": core.get("away_team", away),
        "full_time_result": core.get("full_time_result"),
        "net_goal_interval": core.get("net_goal_interval"),
        "primary_score": core.get("primary_score"),
        "alternative_scores": core.get("alternative_scores", []),
        "match_script": core.get("match_script", ""),
        "net_goal_reasoning": core.get("net_goal_reasoning", ""),
        "uncertainties": core.get("uncertainties", []),
        "confidence": core.get("confidence", ""),
        "net_goal_probabilities": core.get("net_goal_probabilities"),
    }
    instruction = GROUNDED_PLAY_INSTRUCTION.format(
        locked_script=json.dumps(locked, ensure_ascii=False, indent=2)
    )
    raw = call_deepseek(match_data, api_key, extra_user_instruction=instruction)
    parsed, err = extract_json(raw)
    if err:
        return None, core_report, f"第二阶段玩法层 JSON 获取/解析失败：{err}"
    if not isinstance(parsed, dict):
        return None, core_report, "第二阶段玩法层返回值不是 JSON 对象"

    # 在任何覆盖/修正之前，保留 LLM 原始玩法输出快照（不写入 CSV，用于复盘对照）
    raw_play_snapshot = json.loads(json.dumps(parsed, ensure_ascii=False, default=str))
    parsed = apply_locked_core_script(parsed, core)
    parsed, play_report = post_process_prediction(parsed, handicap_line, match_data)
    parsed["_原始快照"] = {
        "原始玩法输出": raw_play_snapshot,
        "锁定核心剧本": json.loads(json.dumps(locked, ensure_ascii=False, default=str)),
    }
    combined_report = core_report + "\n\n" + audit_report + "\n\n【五种玩法校验】\n" + play_report
    return parsed, combined_report, None


def post_process_prediction(parsed, handicap_line, match_data=None):
    """对 LLM 预测做确定性修正，并按修正后的结果重新检查出报告。

    修正不覆盖原始预测：函数在入口深拷贝，传入 dict 保持原样；
    报告使用 "状态: …" 四态（通过 / 已修正，复查通过 / 仍有冲突 / 无法判定）。
    总进球补入项追加在列表末尾并明确说明仅为一致性修正，不制造优先假象。

    Args:
        parsed: LLM 返回的解析 dict
        handicap_line: 体彩让球数字符串，如 "-1"

    Returns:
        (修正后的 parsed dict 副本, 一致性报告字符串)
    """
    if not isinstance(parsed, dict):
        return parsed, "解析结果非 dict，状态: 无法判定；需人工检查模型输出格式"

    result = copy.deepcopy(parsed)
    fixes = []
    notes = []
    extra_unjudgeable = []

    # 统一提取比分列表（兼容 list 和 "1:0 / 2:0" 字符串）
    crs_val = result.get("比分推荐", [])
    if isinstance(crs_val, list):
        scores = [str(s).strip() for s in crs_val if str(s).strip()]
    else:
        scores = [s.strip() for s in re.split(r"[/,，、\s]+", str(crs_val or "")) if s.strip()]
    first_score = scores[0] if scores else ""
    hc = parse_handicap(handicap_line)

    # A 思路：让球临界线分析（净胜球区间 + 体彩让球数 + 首选比分；赔率仅作辅助市场倾向）
    rq_odds = match_data.get("让球赔率") if isinstance(match_data, dict) else None
    spf_odds = match_data.get("胜平负赔率") if isinstance(match_data, dict) else None
    handicap_analysis = analyze_handicap_line(
        first_score,
        str(result.get("净胜球分布", "") or ""),
        handicap_line,
        spf=str(result.get("胜平负推荐", "") or ""),
        handicap_odds=({k: rq_odds.get(k) for k in ("主胜", "平", "客胜")}
                       if isinstance(rq_odds, dict) else None),
        spf_odds=spf_odds if isinstance(spf_odds, dict) else None,
    )

    # 1. 让球（两个口径用「让球推导版本」字段隔离，历史复盘口径不受影响）：
    #    v2：存在经校验的五类净胜球概率 → 确定性映射让球三概率 → 主推/次选/风险；
    #    v1：概率缺失或非法 → 首选比分单点推导（旧行为逐字保留），并明确标注局限。
    current = str(result.get("让球推荐", "") or "").strip()
    net_probs = result.get("_net_goal_probabilities")
    rq_odds_clean = ({k: rq_odds.get(k) for k in ("主胜", "平", "客胜")}
                     if isinstance(rq_odds, dict) else None)

    # R2：纵深防御。在 V2 分支入口再次完整校验五桶概率；只有取得规范化 dict 才能映射。
    # 原始概率缺失仍走普通 V1 降级；非空但校验失败时记录原因并明确标记为校验失败。
    v2_recheck_failed = ""
    if net_probs is not None:
        recheck = validate_net_goal_probabilities(net_probs)
        if (recheck.get("ok") and isinstance(recheck.get("probabilities"), dict)
                and recheck["probabilities"]):
            net_probs = recheck["probabilities"]
        else:
            v2_recheck_failed = str(recheck.get("reason") or "净胜球概率二次校验未通过")
            net_probs = None

    if hc is None:
        result["让球推荐"] = "让球无法判定"
        if current:
            fixes.append(f"让球数 {handicap_line!r} 无法解析，让球推荐 {current!r} → 让球无法判定")
        old_reason = str(result.get("让球理由", "") or "").strip()
        if current or old_reason:
            # 推荐被置为无法判定时，任何旧让球理由都不得原样残留（防自相矛盾）
            result["让球理由"] = (
                f"[程序校验修正] 让球数 {handicap_line!r} 无法解析，让球推荐已置为让球无法判定，"
                "不做任何方向推断。"
                + (f"原 AI 让球理由：{old_reason}" if old_reason else "")
            )
        extra_unjudgeable.append("让球数缺失，让球推荐无法判定")
        result["让球推导版本"] = HANDICAP_VERSION_V1
        result["让球次选"] = ""
        result["净胜球概率分布"] = None
        result["让球结果概率"] = None
        result["概率来源"] = f"缺失：让球数 {handicap_line!r} 无法解析，净胜球概率无法映射为让球概率"
    elif isinstance(net_probs, dict):
        mapping = map_net_goal_probs_to_handicap(net_probs, hc)
        decision = select_handicap_picks(mapping, handicap_odds=rq_odds_clean)
        derived = format_handicap_pick(hc, decision["primary"])
        secondary_pick = (format_handicap_pick(hc, decision["secondary"])
                          if decision.get("secondary") else "")
        result["让球推荐"] = derived
        result["让球次选"] = secondary_pick if secondary_pick != derived else ""
        result["让球推导版本"] = HANDICAP_VERSION_V2
        result["概率来源"] = "模型核心剧本 net_goal_probabilities（经程序五项校验与归一化）"
        result["净胜球概率分布"] = dict(net_probs)
        result["让球结果概率"] = {
            "exact": decision["exact"],
            "probabilities": decision["probabilities"],
            "bounds": decision["bounds"],
            "straddles": decision["straddles"],
            "market_conflict": decision["market_conflict"],
            "market_conflict_state": decision.get(
                "market_conflict_state", MARKET_STATE_INDETERMINATE),
            "double": decision["double"],
        }
        if current and current != derived:
            fixes.append(f"让球推荐 {current or '空'} → {derived}（v2 净胜球分布推导）")

        reason_lines = [
            f"[程序规则推导·v2净胜球分布] 实际体彩让球 {handicap_line}；主推 {derived}"
            + (f"，次选 {result['让球次选']}" if result["让球次选"] else "") + "。",
            "净胜球概率（模型核心剧本，经校验归一）："
            + format_net_goal_probabilities(net_probs) + "。",
        ]
        reason_lines.extend(n if n.endswith("。") else n + "。" for n in decision["notes"])
        # 跨净胜球区间的备选比分：仅作替代情景说明并标注其让球结果，绝不参与概率累加
        interval_text = str(result.get("净胜球分布", "") or "")
        for sc in scores[1:]:
            pair = parse_score(sc)
            if pair is None:
                continue
            alt_interval = goal_diff_to_core_interval(pair[0] - pair[1])
            if interval_text and alt_interval != interval_text:
                reason_lines.append(
                    f"替代剧本：备选比分 {sc}（{alt_interval}）在让球 {handicap_line} 下情景结果为 "
                    f"{derive_handicap_recommendation(sc, hc)}；无概率权重，不参与概率累加。"
                )
        reason_lines.extend(u if u.endswith("。") else u + "。" for u in decision["uncertainties"])
        result["让球理由"] = "\n".join(reason_lines)
        notes.extend(decision["uncertainties"])
    elif first_score:
        derived = derive_handicap_recommendation(first_score, hc)
        if derived != "让球无法判定" and current != derived:
            critical_txt = ""
            if handicap_analysis.get("critical_goal_diff") is not None:
                critical_txt = (
                    f"让球临界净胜球={handicap_analysis['critical_goal_diff']}"
                    f"（净胜球区间 {result.get('净胜球分布', '')}）；"
                )
            result["让球推荐"] = derived
            result["让球理由"] = (
                f"[程序校验修正] 首选比分 {first_score} + 让球 {handicap_line} → {derived}。"
                f"原 AI 推荐：{current or '空'}。{critical_txt}"
                "让球结论由首选比分与体彩让球数按程序规则推导，非模型自由生成。"
            )
            fixes.append(f"让球推荐 {current or '空'} → {derived}")
        # v1 降级元数据：新增字段，不改变任何既有推荐/理由/报告文本
        result["让球推导版本"] = HANDICAP_VERSION_V1
        result["让球次选"] = ""
        result["净胜球概率分布"] = None
        result["让球结果概率"] = None
        if v2_recheck_failed:
            result["概率来源"] = (
                f"二次校验失败：{v2_recheck_failed}；已降级为首选比分单点推导（v1），"
                "不反映净胜球分布，备选比分仅作替代剧本说明、不参与概率累加"
            )
            notes.append(
                f"净胜球概率二次校验失败（{v2_recheck_failed}），让球已降级 v1 单点推导；"
                "失败原因同时写入「概率来源」字段")
        else:
            result["概率来源"] = (
                "缺失：模型核心剧本未提供有效净胜球概率分布；让球按首选比分单点推导（v1），"
                "不反映净胜球分布，备选比分仅作替代剧本说明、不参与概率累加"
            )
    else:
        extra_unjudgeable.append("首选比分缺失，让球推荐无法核对")
        result["让球推导版本"] = HANDICAP_VERSION_V1
        result["让球次选"] = ""
        result["净胜球概率分布"] = None
        result["让球结果概率"] = None
        result["概率来源"] = (
            f"二次校验失败：{v2_recheck_failed}；首选比分缺失，让球无法核对"
            if v2_recheck_failed else "缺失：首选比分与净胜球概率均无，让球无法核对"
        )
        if v2_recheck_failed:
            notes.append(
                f"净胜球概率二次校验失败（{v2_recheck_failed}），且首选比分缺失，"
                "让球无法核对；失败原因同时写入「概率来源」字段")

    # 2. 总进球：规范化 + 去重；首选比分对应值缺失时补在末尾（不制造优先假象）
    if first_score:
        sc = parse_score(first_score)
        if sc is not None:
            total = sc[0] + sc[1]
            ttg_val = result.get("总进球推荐", [])
            if isinstance(ttg_val, list):
                tokens = [str(t).strip() for t in ttg_val if str(t).strip()]
            else:
                tokens = [t.strip() for t in re.split(r"[/,，、\s]+", str(ttg_val or "")) if t.strip()]
            canonical = []
            for t in tokens:
                m = re.match(r"^(\d+)", t)
                if m:
                    label = "7+球" if int(m.group(1)) >= 7 else f"{m.group(1)}球"
                else:
                    label = t
                if label not in canonical:
                    canonical.append(label)
            correct_label = "7+球" if total >= 7 else f"{total}球"
            if correct_label not in canonical:
                canonical.append(correct_label)
                old_reason = str(result.get("总进球理由", "") or "").strip()
                result["总进球理由"] = (
                    f"[程序校验修正] 首选比分 {first_score} 对应总进球 {total}，"
                    f"为满足一致性补入 {correct_label}；该补入仅为规则一致性修正，"
                    "不代表该选项概率更高。"
                    + (f"原理由：{old_reason}" if old_reason else "")
                )
                fixes.append(f"总进球推荐补入 {correct_label}")
            result["总进球推荐"] = canonical

    # 3. 半全场：中文转码 → 过滤不兼容 → 全不兼容/缺失时规则兜底
    spf_raw = str(result.get("胜平负推荐", "") or "").strip()
    spf = normalize_result_label(spf_raw) or spf_raw
    hafu_val = result.get("半全场推荐", [])
    if isinstance(hafu_val, list):
        raw_tokens = [str(t).strip() for t in hafu_val if str(t).strip()]
    else:
        raw_tokens = [t.strip() for t in re.split(r"[/,，、\s]+", str(hafu_val or "")) if t.strip()]
    codes = parse_hafu_codes(raw_tokens)
    had_chinese = any(
        not (len(t) == 2 and all(c in "HDA" for c in t.upper())) and parse_hafu_code_from_text(t)
        for t in raw_tokens
    )
    if had_chinese:
        notes.append("半全场推荐含中文描述，已中文转码为 HDA 代码")
    if codes and spf in ("主胜", "平", "客胜"):
        compatible = [c for c in codes if is_hafu_compatible(c, spf)]
        removed = [c for c in codes if c not in compatible]
        if not compatible:
            fallback = hafu_candidates_for_full(spf)
            result["半全场推荐"] = fallback
            result["半全场理由"] = (
                f"[程序校验修正] 原半全场推荐与全场 {spf} 全部不兼容，"
                f"已按规则兜底生成 {' / '.join(fallback)}（程序兜底生成，待模型确认）。"
            )
            fixes.append(f"半全场全部不兼容，程序兜底生成 {fallback}（待模型确认）")
        elif removed:
            result["半全场推荐"] = compatible
            old_reason = str(result.get("半全场理由", "") or "").strip()
            result["半全场理由"] = (
                f"[程序校验修正] 移除与全场 {spf} 不兼容的半全场 {removed}。"
                + (f"原理由：{old_reason}" if old_reason else "")
            )
            fixes.append(f"半全场移除不兼容项 {removed}")
        else:
            result["半全场推荐"] = codes
    elif codes:
        # 半全场可解析但胜平负缺失：保留转码结果，兼容性无法核对
        result["半全场推荐"] = codes
        extra_unjudgeable.append(f"胜平负推荐缺失（{spf_raw!r}），半全场兼容性无法判定")
    elif raw_tokens:
        result["半全场推荐"] = []
        result["半全场理由"] = (
            "[程序校验修正] 半全场推荐无法解析且胜平负缺失，无法判定，已清空；需模型重新生成。"
        )
        extra_unjudgeable.append("半全场推荐无法解析且胜平负缺失，无法判定")
    elif spf in ("主胜", "平", "客胜"):
        fallback = hafu_candidates_for_full(spf)
        if fallback:
            result["半全场推荐"] = fallback
            result["半全场理由"] = (
                f"[程序校验修正] 半全场推荐缺失，按全场 {spf} 规则兜底生成 "
                f"{' / '.join(fallback)}（程序兜底生成，待模型确认）。"
            )
            fixes.append(f"半全场缺失，程序兜底生成 {fallback}")

    # A 思路：并入让球临界线分析说明与无法判定项（不改变已由程序推导的让球结论）
    handicap_notes = list(handicap_analysis.get("notes", []))
    extra_unjudgeable.extend(handicap_analysis.get("unjudgeable", []))

    # ---- 修正后重新检查：报告必须来自修正后的状态 ----
    recheck_hard, recheck_soft, recheck_unjudgeable = run_consistency_checks(result, handicap_line)
    all_unjudgeable = list(dict.fromkeys(extra_unjudgeable + recheck_unjudgeable))

    if recheck_hard:
        status = "状态: 仍有冲突"
    elif all_unjudgeable:
        status = "状态: 无法判定"
    elif fixes:
        status = "状态: 已修正，复查通过"
    else:
        status = "状态: 通过"

    report_parts = [status]
    if fixes:
        report_parts.append("修正项：")
        for f in fixes:
            report_parts.append(f"  - {f}")
    if notes:
        for n in notes:
            report_parts.append(f"  - {n}")
    report_parts.append("复查结果：")
    for e in recheck_hard:
        report_parts.append(f"  硬性错误：{e}")
    for w in recheck_soft:
        report_parts.append(f"  软性冲突：{w}")
    for u in all_unjudgeable:
        report_parts.append(f"  待确认：{u}")
    if not (recheck_hard or recheck_soft or all_unjudgeable):
        report_parts.append("  一致性检查通过，无硬性错误、软性冲突和待确认项。")
    if handicap_notes:
        report_parts.append("让球临界线分析（A 思路）：")
        for n in handicap_notes:
            report_parts.append(f"  - {n}")
    if recheck_hard or all_unjudgeable:
        report_parts.append("待处理（需模型重新生成）：")
        for item in list(recheck_hard) + all_unjudgeable:
            report_parts.append(f"  - 需模型重新生成：{item}")

    return result, "\n".join(report_parts)


def print_analysis(match_data, result):
    print()
    print("=" * 70)
    print(f"  {match_data['编号']}  {match_data['联赛']}")
    print(f"  {match_data['对阵']}")
    print(f"  开赛：{match_data['开赛']}")
    print("=" * 70)

    if result is None or (isinstance(result, str) and result.startswith("__")):
        print(f"  [分析失败] {result}")
        return

    script = result.get("核心剧本", "")
    if script:
        print(f"  📋 核心剧本：{script}")
    gd = result.get("净胜球分布", "")
    if gd:
        print(f"  📏 净胜球分布：{gd}")

    print(f"  胜平负：  {result.get('胜平负推荐', '?')}    理由：{result.get('胜平负理由', '')}")
    print(f"  让球：    {result.get('让球推荐', '?')}    理由：{result.get('让球理由', '')}")
    if result.get("让球推导版本") == HANDICAP_VERSION_V2:
        hp_text = format_handicap_probabilities(result.get("让球结果概率"))
        v2_extra = f"  次选：{result.get('让球次选') or '无'}"
        if hp_text:
            v2_extra += f"    让球概率：{hp_text}"
        print(v2_extra)
        bp_text = format_net_goal_probabilities(result.get("净胜球概率分布"))
        if bp_text:
            print(f"  净胜球概率：{bp_text}")

    ttg = result.get("总进球推荐", [])
    ttg_rec = " / ".join(ttg) if isinstance(ttg, list) else str(ttg)
    print(f"  总进球：  {ttg_rec}    理由：{result.get('总进球理由', '')}")

    hafu = result.get("半全场推荐", [])
    hafu_rec = " / ".join(hafu) if isinstance(hafu, list) else str(hafu)
    hafu_full = hafu_rec.replace("H", "主胜").replace("D", "平").replace("A", "客胜")
    print(f"  半全场：  {hafu_rec} ({hafu_full})    理由：{result.get('半全场理由', '')}")

    crs = result.get("比分推荐", [])
    crs_rec = " / ".join(crs) if isinstance(crs, list) else str(crs)
    print(f"  比分：    {crs_rec}    理由：{result.get('比分理由', '')}")

    print(f"  信心指数：{result.get('信心指数', '?')}/10")
    missing = result.get("数据缺失说明", "")
    if missing:
        print(f"  ⚠️ 数据缺失：{missing}")
    print(f"  综合分析：{result.get('综合分析', '')}")
    check_report = result.get("一致性检查", "")
    if check_report:
        print(f"  🔍 一致性检查：{check_report}")
    print("-" * 70)


CSV_FIELDNAMES = [
    "编号", "联赛", "对阵", "开赛",
    "胜平负推荐", "胜平负理由",
    "让球推荐", "让球理由",
    "总进球推荐", "总进球理由",
    "半全场推荐", "半全场理由",
    "比分推荐", "比分理由",
    "信心指数", "综合分析", "历史数据",
    "核心剧本", "净胜球分布", "体彩让球数",
    "数据缺失说明", "一致性检查",
    # v2 让球推导新增字段（尾部追加，前 22 列含义与顺序保持不变）
    "让球次选", "净胜球概率分布", "让球结果概率", "概率来源", "让球推导版本",
]


def build_failed_result_row(match_num, league, match_data, history, handicap_line, reason, check_report):
    """失败/阻止行保留既有 CSV 全部字段，但绝不伪造玩法预测。"""
    row = {field: "" for field in CSV_FIELDNAMES}
    row.update({
        "编号": match_num,
        "联赛": league,
        "对阵": match_data.get("对阵", ""),
        "开赛": match_data.get("开赛", ""),
        "综合分析": f"核心剧本校验未通过/分析失败：{str(reason)[:240]}",
        "历史数据": "有" if (history and history.get("available")) else "无",
        "体彩让球数": handicap_line,
        "一致性检查": check_report,
    })
    return row


def _validate_result_row_fields(row, fieldnames, row_index=0):
    """在写入 CSV 前校验单行字段与 CSV_FIELDNAMES 是否一致。

    返回 None 表示一致；返回字符串为诊断信息（缺失/多余/类型异常）。
    不静默忽略字段——所有异常必须显式诊断。
    """
    if not isinstance(row, dict):
        return f"第 {row_index} 行：不是 dict，类型={type(row).__name__}"
    missing = [f for f in fieldnames if f not in row]
    extra = [k for k in row.keys() if k not in fieldnames]
    problems = []
    if missing:
        problems.append(f"缺失字段 {missing}")
    if extra:
        problems.append(f"多余字段 {extra}")
    return "; ".join(problems) if problems else None


def save_analysis_csv(all_results, target_date, output_dir="output"):
    """保存 CSV；写入前逐行校验字段一致性；失败返回 None 并打印诊断。"""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"竞彩分析_{target_date}.csv")

    fieldnames = CSV_FIELDNAMES

    row_issues = []
    for i, item in enumerate(all_results):
        issue = _validate_result_row_fields(item, fieldnames, row_index=i + 1)
        if issue:
            row_issues.append(issue)
    if row_issues:
        print(f"CSV 字段校验失败（{len(row_issues)} 行异常）：")
        for iss in row_issues:
            print(f"  {iss}")
        # 仍尝试写入，但以 extrasaction='ignore' 防止崩溃——此时 extra 字段被丢弃，
        # 明确告知用户。缺失字段则 DictWriter 写空字符串，不会崩溃。
        try:
            with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for item in all_results:
                    writer.writerow(item)
            print(f"（CSV 已写入，但多余字段被丢弃，缺失字段补空字符串）：{filepath}")
            return filepath
        except OSError as e:
            print(f"保存 CSV 失败：{e}")
            return None
        except ValueError as e:
            print(f"保存 CSV 时字段值异常：{e}")
            return None

    try:
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for item in all_results:
                writer.writerow(item)
        print(f"\n分析结果已保存：{filepath}")
        return filepath
    except OSError as e:
        print(f"保存 CSV 失败：{e}")
        return None
    except ValueError as e:
        print(f"保存 CSV 时字段值异常：{e}")
        return None


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY") or API_KEY_FALLBACK
    key_ok, key_err = validate_api_key(api_key)
    if not key_ok:
        print(f"错误：{key_err}")
        sys.exit(1)

    csv_path = find_latest_csv()
    if not csv_path:
        sys.exit(1)

    target_date_match = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(csv_path))
    target_date = target_date_match.group(1) if target_date_match else date.today().strftime("%Y-%m-%d")

    rows = load_matches_from_csv(csv_path)

    print()
    print("DeepSeek 竞彩足球分析（两阶段核心剧本锁定 + 玩法校验）")
    print("=" * 50)

    all_results = []
    snapshots = []

    for i, row in enumerate(rows, 1):
        home = row.get("主队", "")
        away = row.get("客队", "")
        match_num = row.get("编号", "")
        league = row.get("联赛", "")

        print(f"\n[{i}/{len(rows)}] 正在查询历史数据 {match_num} {home} vs {away} ...", end=" ", flush=True)
        try:
            history = build_history_for_match(home, away)
            hist_flag = "OK" if history.get("available") else "跳过"
            print(hist_flag)
        except Exception as e:
            print(f"失败: {e}")
            history = None

        match_data = build_match_prompt_data(row, history)
        print(f"  正在 AI 分析 {match_num} {match_data['对阵']} ...", end=" ", flush=True)

        handicap_line = row.get("让球数", "")
        parsed, check_report, err = analyze_match_two_stage(
            match_data, api_key, handicap_line, home=home, away=away, max_retries=1
        )

        if err:
            print("失败")
            print(f"  错误：{err}")
            result_row = build_failed_result_row(
                match_num, league, match_data, history, handicap_line, err, check_report
            )
            all_results.append(result_row)
            continue

        print("完成")
        parsed["一致性检查"] = check_report
        print_analysis(match_data, parsed)
        snap = parsed.get("_原始快照") or {}
        snapshots.append({
            "编号": match_num,
            "对阵": match_data["对阵"],
            "体彩让球数": handicap_line,
            "原始玩法输出": snap.get("原始玩法输出", {}),
            "锁定核心剧本": snap.get("锁定核心剧本", {}),
            "修正后预测": {k: v for k, v in parsed.items() if not k.startswith("_")},
            "一致性检查": check_report,
        })

        history_text = ""
        if history and history.get("available"):
            parts = []
            if history.get("home_recent_text"):
                parts.append(history["home_recent_text"])
            if history.get("away_recent_text"):
                parts.append(history["away_recent_text"])
            if history.get("h2h_text"):
                parts.append(history["h2h_text"])
            history_text = "\n".join(parts)

        # 推荐类字段统一转成 " / " 分隔字符串，兼容下游 CSV 读取
        def _join(val):
            if isinstance(val, list):
                return " / ".join(str(v) for v in val)
            return str(val or "")

        result_row = {
            "编号": match_num,
            "联赛": league,
            "对阵": match_data["对阵"],
            "开赛": match_data["开赛"],
            "胜平负推荐": parsed.get("胜平负推荐", ""),
            "胜平负理由": parsed.get("胜平负理由", ""),
            "让球推荐": parsed.get("让球推荐", ""),
            "让球理由": parsed.get("让球理由", ""),
            "总进球推荐": _join(parsed.get("总进球推荐", [])),
            "总进球理由": parsed.get("总进球理由", ""),
            "半全场推荐": _join(parsed.get("半全场推荐", [])),
            "半全场理由": parsed.get("半全场理由", ""),
            "比分推荐": _join(parsed.get("比分推荐", [])),
            "比分理由": parsed.get("比分理由", ""),
            "信心指数": parsed.get("信心指数", ""),
            "综合分析": parsed.get("综合分析", ""),
            "历史数据": history_text,
            # 新增字段
            "核心剧本": parsed.get("核心剧本", ""),
            "净胜球分布": parsed.get("净胜球分布", ""),
            "体彩让球数": handicap_line,
            "数据缺失说明": parsed.get("数据缺失说明", ""),
            "一致性检查": check_report,
            # v2 让球推导：结构化概率转文本落 CSV（v1 降级时为标注文本/空，不伪造）
            "让球次选": parsed.get("让球次选", ""),
            "净胜球概率分布": format_net_goal_probabilities(parsed.get("净胜球概率分布")),
            "让球结果概率": format_handicap_probabilities(parsed.get("让球结果概率")),
            "概率来源": parsed.get("概率来源", ""),
            "让球推导版本": parsed.get("让球推导版本", ""),
        }
        all_results.append(result_row)

    save_analysis_csv(all_results, target_date)

    if snapshots:
        os.makedirs("output", exist_ok=True)
        snap_path = os.path.join("output", f"原始预测快照_{target_date}.json")
        try:
            with open(snap_path, "w", encoding="utf-8") as f:
                json.dump({"复盘日期": target_date, "场次": snapshots}, f,
                          ensure_ascii=False, indent=2, allow_nan=False)
            print(f"原始预测快照已保存：{snap_path}（{len(snapshots)} 场）")
        except (OSError, ValueError, TypeError) as e:
            print(f"保存快照失败：{e}")
            print(f"原始玩法输出未丢失（{len(snapshots)} 场），可在内存检查后重试")


if __name__ == "__main__":
    main()
