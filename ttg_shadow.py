#!/usr/bin/env python3
"""总进球独立分析影子实验（A/B/C/C2/D 组）—— 默认开启，绝不影响正式预测。

临时关闭方式：export DISABLE_TTG_SHADOW=1
C2 独立判断组默认开启（每场多花 1 次 LLM 调用），临时关闭：export DISABLE_TTG_C2=1

分组定义：
  A 组（纯盲判）：请求体仅由白名单字段构建——编号/联赛/主客队/排名/开赛/历史数据。
      不含任何赔率、比分赔率、已锁定比分、核心剧本输出或正式推荐。
  B 组（市场复核）：输入 = A 组原始结果 + 独立构造的市场信息 + 锁定剧本。
      必须保留 A 组原始结果；变更需给出理由。B 组结果不得称为纯独立判断。
  C 组（正式预测）：原样记录现有正式总进球推荐，不改正式逻辑。
  C2 组（独立判断对照）：使用独立思考版 prompt 对同一场比赛再跑一遍总进球。
      与 C 组对照，检验弹性候选数 + 市场偏差经验是否能让 AI 跳出赔率复述。
      默认开启，设置 DISABLE_TTG_C2=1 可临时关闭。
  D 组（固定基线）：固定 ["2球","3球","4球"]。

隔离约束：
  - 本模块所有函数只读 match_data / parsed，绝不修改正式对象；
  - 影子数据独立写入 output/影子实验_总进球_<date>.json，不进入正式 CSV / 快照；
  - 影子 API、解析、写入失败只记录到影子记录内部，绝不抛出阻断正式流程；
  - 赛果只能在赛后由 settle_shadow.py 回填，预测时字段绝不包含赛果。
"""
import fcntl
import json
import os
import re
import shutil
import uuid
from datetime import datetime

import requests

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

TTG_LABELS = ["0球", "1球", "2球", "3球", "4球", "5球", "6球", "7+球"]
FIXED_BASELINE_PICKS = ["2球", "3球", "4球"]

# A 组盲判白名单：仅比赛与球队自身信息。任何赔率、比分赔率、锁定比分、
# 核心剧本输出、正式推荐及其衍生字段一律不得进入（白名单之外全部禁止）。
BLIND_INPUT_FIELDS = ("编号", "联赛", "主队", "客队", "主队排名", "客队排名",
                      "开赛日期", "开赛时间")

SHADOW_FILE_PREFIX = "影子实验_总进球_"
SHADOW_FILE_VERSION = 1

BLIND_SYSTEM_PROMPT = """你是一位专业的足球比赛分析师。本任务是「总进球数盲判」：
你将**不会**收到任何赔率、盘口或市场信息，只能依据球队与比赛本身的信息做判断。

请根据给出的比赛信息，独立判断本场两队全场总进球数的可能范围，只返回合法 JSON：
{
  "ttg_blind_picks": ["2球", "3球"],
  "ttg_blind_reasoning": "依据球队攻防、联赛风格、近期状态等足球因素的判断过程",
  "ttg_blind_uncertainty": "证据不足之处如实说明；若信息不足以给出候选，picks 返回空列表",
  "ordered": true
}

规则（必须遵守）：
- ttg_blind_picks 为 0~4 个候选，合法取值为 0球/1球/2球/3球/4球/5球/6球/7+球，不得重复。
- ordered=true 表示列表按你认为的可能性从高到低排序；若无法排序，ordered=false，不得伪造顺序。
- 候选数量由证据决定，不要凑数；证据不足时宁可少给或返回空列表。
- 严禁提及或引用任何赔率、盘口信息——你没有也不应假设这些数据。
- 只输出 JSON，不要输出任何额外文字。"""

REVIEW_SYSTEM_PROMPT = """你是一位专业的足球比赛分析师。本任务是「总进球市场复核」：
此前已在**完全无赔率**条件下给出盲判结果。现在你将看到市场赔率信息与锁定比赛剧本，
请对盲判结果进行交叉验证，只返回合法 JSON：
{
  "ttg_review_picks": ["2球", "3球", "4球"],
  "changed": false,
  "change_reason": ""
}

规则（必须遵守）：
- 赔率只能作为参考证据，不能自动成为最终结论；不得无理由推翻盲判。
- changed=false 时 ttg_review_picks 必须与盲判完全一致；changed=true 时 change_reason
  必须非空，说明是哪个具体证据促使调整（如市场明显倾向某区间、盲判遗漏的节奏因素）。
- ttg_review_picks 为 1~4 个候选，按可能性从高到低排序，合法取值 0球~7+球。
- 只输出 JSON，不要输出任何额外文字。"""

INDEPENDENT_SYSTEM_PROMPT = """你是一位专业的足球比赛分析师。本任务是「总进球独立判断」：
你将同时看到比赛信息、市场赔率、核心剧本锁定的首选比分。
关键约束：**不要复述市场赔率 Top3**——以下是对你这个模型的历史统计关键观察（442场）：

⚠️ 市场赔率结构性偏差：
- 市场在 75% 的场次里把 3球 定得比 2球 赔率更低（意味着市场更看好 3球）。
- 但实际赛果里，2球(22.0%) 和 3球(23.9%) 概率几乎相等，1球(13.3%) 和 4球(15.2%) 占比也不低。
- 你之前的模型 70% 的首选都押 3球，本质是复述市场赔率最低项——命中率 22.6%（等于市场隐含概率），没有独立价值。
- 多候选分注是 ROI 头号杀手：3选（最低赔 + 次低赔 + 第三低赔）ROI -30%，1选1 ROI +110%。

请基于比赛节奏、攻防强度、锁定剧本和首选比分独立判断，只返回合法 JSON：
{
  "ttg_independent_picks": ["3球", "2球"],
  "ttg_independent_reasoning": "独立判断的理由，必须说明：为什么这么选？有没有拒绝哪些市场赔率提示？",
  "ordered": true
}

规则（必须遵守）：
- 候选数 1-3 个（不是硬凑 3 个）。首选**必须等于锁定首选比分的进球总数**（这是规则引擎硬约束）。
- 后续候选仅当你有充分理由判断真实进球分布可能偏离首选比分时才追加——不要为了凑数加长尾（赔率高、命中概率低的选项）。
- 不要因为 3球 赔率最低就优先选它——市场在这个球数上有系统性偏差。
- 候选按可能性从高到低排序，合法取值 0球/1球/2球/3球/4球/5球/6球/7+球，不得重复。
- 只输出 JSON，不要输出任何额外文字。"""


def shadow_enabled():
    """影子实验开关：默认开启（不影响正式预测，任何失败仅打印不阻断）。

    如需临时关闭：export DISABLE_TTG_SHADOW=1
    """
    return os.environ.get("DISABLE_TTG_SHADOW", "") != "1"


def enable_ttg_c2():
    """C2 独立判断组开关：默认开启（每场多花 1 次 LLM 调用）。

    如需临时关闭：export DISABLE_TTG_C2=1
    """
    return os.environ.get("DISABLE_TTG_C2", "") != "1"


def canon_ttg_label(token):
    """规范化总进球标签；非法返回 None。7 及以上统一映射为 7+球。"""
    t = str(token or "").strip()
    if not t:
        return None
    if t.startswith("7+") or t.startswith("7＋"):
        return "7+球"
    m = re.match(r"^(\d+)", t)
    if m:
        n = int(m.group(1))
        return "7+球" if n >= 7 else f"{n}球"
    return None


def build_blind_input(row, history=None):
    """仅从白名单字段构建 A 组盲判输入（新建对象，不复制 match_data）。"""
    data = {k: str(row.get(k, "") or "") for k in BLIND_INPUT_FIELDS}
    home_rank = data["主队排名"]
    away_rank = data["客队排名"]
    data["对阵"] = f"{data['主队']}{home_rank} vs {data['客队']}{away_rank}"
    data["开赛"] = f"{data['开赛日期']} {data['开赛时间']}".strip()
    if history and history.get("available"):
        hist_info = {}
        if history.get("home_recent_text"):
            hist_info[f"{data['主队']} 近期战绩"] = history["home_recent_text"]
        if history.get("away_recent_text"):
            hist_info[f"{data['客队']} 近期战绩"] = history["away_recent_text"]
        if history.get("h2h_text"):
            hist_info["历史交锋"] = history["h2h_text"]
        if hist_info:
            data["历史数据"] = hist_info
    return data


def build_market_info(match_data):
    """B 组市场信息：从 match_data 独立构造（只读），明确列出允许的市场字段。"""
    md = match_data or {}
    return {
        "胜平负赔率": md.get("胜平负赔率") or {},
        "让球赔率": md.get("让球赔率") or {},
        "总进球赔率": md.get("总进球赔率") or {},
        "半全场赔率": md.get("半全场赔率") or {},
        "比分赔率": md.get("比分赔率") or {},
        "总进球赔率最低三项": market_top3(md),
    }


def market_top3(match_data):
    """总进球赔率升序前三；并列时按 0球→7+球 规范顺序取靠前档（明示裁决规则）。"""
    odds = (match_data or {}).get("总进球赔率") or {}
    items = []
    for idx, label in enumerate(TTG_LABELS):
        raw = odds.get(label)
        if raw in (None, ""):
            continue
        try:
            items.append((float(raw), idx, label))
        except (TypeError, ValueError):
            continue
    items.sort()
    return [label for _, _, label in items[:3]]


def _extract_json(text):
    """提取首个 JSON 对象；失败返回 (None, 错误描述)。"""
    if not isinstance(text, str) or not text.strip():
        return None, "空响应"
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
    if m:
        t = m.group(1)
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        t = t[s:e + 1]
    try:
        return json.loads(t), None
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc}"


def _post_json(system_prompt, user_content, api_key, max_tokens):
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.4,
        "max_tokens": max_tokens,
    }
    resp = requests.post(
        DEEPSEEK_API_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json=payload, timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def call_deepseek_ttg_blind(blind_input, api_key):
    """A 组盲判调用。请求体仅含白名单输入。失败返回 __API_ERROR__ 前缀文本。"""
    user_content = (
        "请在无赔率条件下独立判断本场总进球候选范围：\n\n"
        + json.dumps(blind_input, ensure_ascii=False, indent=2)
    )
    try:
        return _post_json(BLIND_SYSTEM_PROMPT, user_content, api_key, 600)
    except requests.RequestException as e:
        return f"__API_ERROR__: {e}"
    except (KeyError, IndexError, TypeError) as e:
        return f"__PARSE_ERROR__: {e}"


def call_deepseek_ttg_independent(row, history, match_data, parsed, api_key):
    """C2 组独立判断调用。输入 = 比赛信息 + 市场赔率 + 锁定剧本（首选比分）。"""
    blind_input = build_blind_input(row, history)
    market_info = build_market_info(match_data)
    locked = ((parsed or {}).get("_原始快照") or {}).get("锁定核心剧本") or {}
    # 锁定首选比分：总进球独立判断必须先满足"首选 = 首选比分进球数"规则
    primary_score = str((parsed or {}).get("首选比分") or (locked.get("主推比分") if isinstance(locked, dict) else "") or "")
    primary_tg = None
    if primary_score and isinstance(primary_score, str):
        m = re.match(r"(\d+)\s*[-:：]\s*(\d+)", primary_score)
        if m:
            t = int(m.group(1)) + int(m.group(2))
            primary_tg = "7+球" if t >= 7 else f"{t}球"
    user_content = (
        "【比赛信息（白名单字段，无赔率）】\n"
        + json.dumps(blind_input, ensure_ascii=False, indent=2)
        + "\n\n【市场赔率信息（仅供参考，不要复述 Top3）】\n"
        + json.dumps(market_info, ensure_ascii=False, indent=2)
        + "\n\n【锁定核心剧本（首选比分）】\n"
        + json.dumps(locked if isinstance(locked, dict) else {}, ensure_ascii=False, indent=2)
        + (f"\n\n【硬约束】首选必须等于锁定首选比分的进球总数：{primary_tg}" if primary_tg else "")
        + "\n\n请独立判断总进球候选，不要复述市场赔率 Top3。"
    )
    try:
        return _post_json(INDEPENDENT_SYSTEM_PROMPT, user_content, api_key, 600)
    except requests.RequestException as e:
        return f"__API_ERROR__: {e}"
    except (KeyError, IndexError, TypeError) as e:
        return f"__PARSE_ERROR__: {e}"


def validate_ttg_independent(text):
    """校验 C2 组独立判断输出。"""
    result = {"状态": "illegal", "候选": [], "理由": "", "有序": True,
              "错误": "", "原始文本前缀": str(text or "")[:200]}
    if str(text or "").startswith("__API_ERROR__"):
        result.update(状态="api_error", 错误=str(text))
        return result
    if str(text or "").startswith("__PARSE_ERROR__"):
        result.update(错误=str(text))
        return result
    obj, err = _extract_json(text)
    if err:
        result["错误"] = err
        return result
    picks, note, perr = _validate_picks(obj, "ttg_independent_picks", max_n=3)
    if perr:
        result["错误"] = perr
        return result
    result["候选"] = picks
    result["理由"] = str(obj.get("ttg_independent_reasoning", "") or "")
    result["有序"] = bool(obj.get("ordered", True))
    result["状态"] = "empty" if not picks else "ok"
    return result


def call_deepseek_ttg_review(blind_result, market_info, locked_script, api_key):
    """B 组市场复核调用。blind_result 为 A 组原始结果（只读，不回传修改）。"""
    user_content = (
        "【盲判结果（无赔率条件下给出，请尊重其独立性）】\n"
        + json.dumps({
            "盲判候选": blind_result.get("候选", []),
            "盲判理由": blind_result.get("理由", ""),
            "盲判不确定说明": blind_result.get("不确定说明", ""),
        }, ensure_ascii=False, indent=2)
        + "\n\n【市场信息（仅供交叉验证，不得自动成为结论）】\n"
        + json.dumps(market_info, ensure_ascii=False, indent=2)
        + "\n\n【已锁定核心剧本（仅供节奏参考）】\n"
        + json.dumps(locked_script or {}, ensure_ascii=False, indent=2)
        + "\n\n请复核盲判总进球候选，返回规定 JSON。"
    )
    try:
        return _post_json(REVIEW_SYSTEM_PROMPT, user_content, api_key, 600)
    except requests.RequestException as e:
        return f"__API_ERROR__: {e}"
    except (KeyError, IndexError, TypeError) as e:
        return f"__PARSE_ERROR__: {e}"


def _validate_picks(obj, key, max_n=4):
    """从已解析对象提取候选列表。返回 (picks, dedup_note, error)。"""
    raw = obj.get(key)
    if raw is None:
        return None, "", f"缺少字段 {key}"
    if not isinstance(raw, list):
        return None, "", f"{key} 不是列表"
    picks, seen, illegal = [], set(), []
    for token in raw:
        label = canon_ttg_label(token)
        if label is None:
            illegal.append(str(token))
            continue
        if label not in seen:
            seen.add(label)
            picks.append(label)
    if illegal:
        return None, "", f"含非法球数标签 {illegal}"
    if len(picks) > max_n:
        return None, "", f"候选超过 {max_n} 项（{len(picks)} 项）"
    dedup_note = "原始输出含重复候选，已去重" if len(picks) < len(raw) else ""
    return picks, dedup_note, ""


def validate_ttg_blind(text):
    """校验 A 组盲判输出。返回结构化字典，状态 ∈ ok/empty/illegal/api_error。"""
    result = {"状态": "illegal", "候选": [], "理由": "", "不确定说明": "",
              "有序": True, "错误": "", "原始文本前缀": str(text or "")[:200]}
    if str(text or "").startswith("__API_ERROR__"):
        result.update(状态="api_error", 错误=str(text))
        return result
    if str(text or "").startswith("__PARSE_ERROR__"):
        result.update(错误=str(text))
        return result
    obj, err = _extract_json(text)
    if err:
        result["错误"] = err
        return result
    picks, note, perr = _validate_picks(obj, "ttg_blind_picks")
    if perr:
        result["错误"] = perr
        return result
    result["候选"] = picks
    result["理由"] = str(obj.get("ttg_blind_reasoning", "") or "")
    result["不确定说明"] = str(obj.get("ttg_blind_uncertainty", "") or "")
    result["有序"] = bool(obj.get("ordered", True))
    if note:
        result["不确定说明"] = (result["不确定说明"] + "；" + note).strip("；")
    result["状态"] = "empty" if not picks else "ok"
    return result


def validate_ttg_review(text, blind_picks):
    """校验 B 组复核输出。blind_picks 用于计算校验后变更，不被修改。"""
    result = {"状态": "illegal", "候选": [], "模型声明变更": False,
              "校验后变更": False, "变更理由": "", "理由缺失": False,
              "错误": "", "原始文本前缀": str(text or "")[:200]}
    if str(text or "").startswith("__API_ERROR__"):
        result.update(状态="api_error", 错误=str(text))
        return result
    if str(text or "").startswith("__PARSE_ERROR__"):
        result.update(错误=str(text))
        return result
    obj, err = _extract_json(text)
    if err:
        result["错误"] = err
        return result
    picks, note, perr = _validate_picks(obj, "ttg_review_picks")
    if perr:
        result["错误"] = perr
        return result
    if not picks:
        result["错误"] = "复核候选为空（B 组不允许弃选后覆盖盲判）"
        return result
    claimed = bool(obj.get("changed", False))
    reason = str(obj.get("change_reason", "") or "").strip()
    actual_changed = list(picks) != list(blind_picks)
    if actual_changed and not claimed:
        reason = ("[校验] 模型声明未变更但候选实际不同，按已变更记录。"
                  + (f"模型理由：{reason}" if reason else ""))
    result.update(候选=picks, 模型声明变更=claimed, 校验后变更=actual_changed,
                  变更理由=reason, 状态="ok")
    if actual_changed and not str(obj.get("change_reason", "") or "").strip():
        result["理由缺失"] = True
    return result


def make_align_key(target_date, row, match_data):
    """稳定关联键：日期+编号+主队+客队+开赛。赛后回填与复盘对齐均使用该键。"""
    return {
        "日期": target_date,
        "编号": str(row.get("编号", "") or ""),
        "主队": str(row.get("主队", "") or ""),
        "客队": str(row.get("客队", "") or ""),
        "开赛": str((match_data or {}).get("开赛", "") or ""),
    }


def collect_shadow_record(row, history, match_data, parsed, api_key, target_date):
    """收集单场比赛的 A/B/C/C2/D 影子记录。任何阶段失败只记录不抛出。"""
    _c2_default = {"状态": "disabled", "候选": [], "理由": "", "有序": True,
                   "错误": "", "原始文本前缀": ""}
    record = {
        "对齐键": make_align_key(target_date, row, match_data),
        "实验运行ID": str(uuid.uuid4()),
        "写入时间": datetime.now().isoformat(timespec="seconds"),
        "A盲判": {"状态": "not_run", "候选": [], "理由": "", "不确定说明": "",
                  "有序": True, "错误": "", "原始文本前缀": ""},
        "B复核": {"状态": "not_run", "候选": [], "模型声明变更": False,
                  "校验后变更": False, "变更理由": "", "理由缺失": False,
                  "错误": "", "原始文本前缀": ""},
        "C正式": {"状态": "ok", "候选": list((parsed or {}).get("总进球推荐", []) or []),
                  "有序": True},
        "C2独立": dict(_c2_default),
        "D固定": {"状态": "ok", "候选": list(FIXED_BASELINE_PICKS), "有序": True},
        "市场前三": market_top3(match_data),
        "实际赛果": None,
        "影子错误": "",
    }
    # A 组：盲判（请求体仅白名单字段）
    try:
        blind_raw = call_deepseek_ttg_blind(build_blind_input(row, history), api_key)
        record["A盲判"] = validate_ttg_blind(blind_raw)
    except Exception as e:  # 影子任何异常都不得阻断正式流程
        record["A盲判"].update(状态="api_error", 错误=f"盲判调用异常：{e}")
    # B 组：市场复核（A 组失败时无复核对象，跳过）
    if record["A盲判"]["状态"] in ("ok", "empty"):
        try:
            locked = ((parsed or {}).get("_原始快照") or {}).get("锁定核心剧本") or {}
            review_raw = call_deepseek_ttg_review(
                record["A盲判"], build_market_info(match_data), locked, api_key)
            record["B复核"] = validate_ttg_review(review_raw, record["A盲判"]["候选"])
        except Exception as e:
            record["B复核"].update(状态="api_error", 错误=f"复核调用异常：{e}")
    else:
        record["B复核"].update(状态="skipped", 错误="A 组失败，无复核对象")
    # C2 组：独立判断对照（默认开启，DISABLE_TTG_C2=1 时跳过，每场多花 1 次 LLM）
    if enable_ttg_c2():
        try:
            ind_raw = call_deepseek_ttg_independent(row, history, match_data, parsed, api_key)
            record["C2独立"] = validate_ttg_independent(ind_raw)
        except Exception as e:
            record["C2独立"].update(状态="api_error", 错误=f"独立判断调用异常：{e}")
    return record


# ---------------------------------------------------------------- 文件写入

def shadow_file_path(target_date, output_dir="output"):
    return os.path.join(output_dir, f"{SHADOW_FILE_PREFIX}{target_date}.json")


def _atomic_write_json(path, obj):
    """临时文件（同目录）→ 回读校验 → os.replace。失败清理临时文件并抛出。

    注意：os.replace 仅保证目标文件不出现半写状态；临时文件本身的写入
    正确性依赖回读校验兜底，临时文件必须与目标同文件系统（故同目录）。
    """
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp_path, encoding="utf-8") as f:
            json.load(f)  # 回读校验：临时文件必须是可解析的完整 JSON
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _record_content_fp(record):
    """去重指纹：剔除写入时间/实验运行ID/实际赛果后比较预测内容。"""
    stripped = {k: v for k, v in record.items()
                if k not in ("写入时间", "实验运行ID", "实际赛果")}
    return json.dumps(stripped, ensure_ascii=False, sort_keys=True)


def _validate_shadow_record(record):
    if not isinstance(record, dict):
        return "记录不是 dict"
    key = record.get("对齐键")
    if not isinstance(key, dict) or not all(
            k in key for k in ("日期", "编号", "主队", "客队", "开赛")):
        return "对齐键缺失或字段不全"
    for group in ("A盲判", "B复核", "C正式", "C2独立", "D固定"):
        if group not in record:
            return f"缺少 {group} 字段"
    return ""


def write_shadow_file(records, target_date, output_dir="output", collected_count=None):
    """合并写入影子文件。返回统计字典；任何失败都不抛出（影子不阻断正式）。

    计数口径（含义互不相同，禁止混用）：
      本次输入数   = 传入 records 总数（含非法记录）
      成功收集数   = 调用方成功构建的影子记录数（默认等于本次输入数）
      非法剔除数   = 未通过写入前校验的记录数
      新增数       = 合并后新增的关联键数
      重复跳过数   = 关联键已存在且内容一致而跳过的记录数
      冲突数       = 关联键已存在但内容不同（保留首次记录，进冲突列表）
      合并后总记录数 = 写入后文件内场次数
    """
    path = shadow_file_path(target_date, output_dir)
    stats = {"文件路径": path, "状态": "未写入", "本次输入数": len(records or []),
             "成功收集数": collected_count if collected_count is not None else len(records or []),
             "非法剔除数": 0, "新增数": 0, "重复跳过数": 0, "冲突数": 0,
             "合并后总记录数": 0, "备份路径": "", "锁文件": path + ".lock"}
    records = records or []

    os.makedirs(output_dir, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = os.open(stats["锁文件"], os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stats["状态"] = "锁获取失败，已跳过写入（正式产物不受影响）"
            return stats

        valid_records = []
        for rec in records:
            err = _validate_shadow_record(rec)
            if err:
                stats["非法剔除数"] += 1
            else:
                valid_records.append(rec)

        existing_matches, existing_conflicts = [], []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old = json.load(f)
                existing_matches = list(old.get("场次", []))
                existing_conflicts = list(old.get("冲突记录", []))
            except (json.JSONDecodeError, ValueError, OSError, AttributeError):
                backup = f"{path}.corrupted.{datetime.now().strftime('%Y%m%d%H%M%S')}"
                try:
                    shutil.copy2(path, backup)
                except OSError as e:
                    stats["状态"] = f"损坏文件备份失败，已中止写入（保留原文件）：{e}"
                    return stats
                stats["备份路径"] = backup

        by_key = {}
        for rec in existing_matches:
            fp_key = json.dumps(rec.get("对齐键", {}), ensure_ascii=False, sort_keys=True)
            by_key.setdefault(fp_key, rec)
        merged = list(existing_matches)
        conflicts = list(existing_conflicts)

        for rec in valid_records:
            fp_key = json.dumps(rec["对齐键"], ensure_ascii=False, sort_keys=True)
            if fp_key not in by_key:
                by_key[fp_key] = rec
                merged.append(rec)
                stats["新增数"] += 1
            elif _record_content_fp(by_key[fp_key]) == _record_content_fp(rec):
                stats["重复跳过数"] += 1
            else:
                # 新记录是旧记录的字段真超集（如新启用了 C2独立）：补字段合并，不算冲突
                old_rec = by_key[fp_key]
                old_keys = set(old_rec.keys()) - {"实验运行ID", "写入时间"}
                new_keys = set(rec.keys()) - {"实验运行ID", "写入时间"}
                if old_keys < new_keys:  # 真超集：new 至少比 old 多一个字段
                    merged_rec = dict(old_rec)
                    for k, v in rec.items():
                        if k not in old_rec:
                            merged_rec[k] = v
                            stats.setdefault("字段补入数", 0)
                            stats["字段补入数"] += 1
                    idx = merged.index(old_rec)
                    merged[idx] = merged_rec
                    by_key[fp_key] = merged_rec
                else:
                    # 真正的冲突：保留首次记录，冲突进入独立列表，绝不静默覆盖
                    conflicts.append({"时间": datetime.now().isoformat(timespec="seconds"),
                                      "对齐键": rec["对齐键"], "已保留": old_rec,
                                      "被拒绝的新记录": rec})
                    stats["冲突数"] += 1

        out = {
            "实验日期": target_date,
            "版本": SHADOW_FILE_VERSION,
            "最近写入时间": datetime.now().isoformat(timespec="seconds"),
            "统计": {k: stats[k] for k in ("本次输入数", "成功收集数", "非法剔除数",
                                           "新增数", "重复跳过数", "冲突数")},
            "场次": merged,
            "冲突记录": conflicts,
        }
        _atomic_write_json(path, out)
        stats["合并后总记录数"] = len(merged)
        stats["状态"] = "写入成功"
        return stats
    except Exception as e:
        stats["状态"] = f"写入失败（已保留原文件，不影响正式产物）：{e}"
        return stats
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)


# ---------------------------------------------------------------- 评估口径

def _group_entry(record, group):
    g = record.get(group) or {}
    return {
        "候选": list(g.get("候选", []) or []),
        "有序": bool(g.get("有序", True)),
        "状态": g.get("状态", "ok"),
    }


def group_metrics(entries):
    """单组候选统计。

    entries: [{候选, 有序, 状态, 实际, 市场前三}, ...]，仅含已结算且可关联场次。
    分母区分：
      N        = 全部已结算可关联场次（可用结果覆盖率分母）
      N_valid  = 状态 ok 且候选非空（条件命中指标分母，含无序）
      N_order  = N_valid 中有序输出（首选命中率/等宽-3 分母）
    窄集合不补齐；宽集合（>3 项）等宽-3 按原始优先级取前三项。
    """
    n = len(entries)
    valid, ordered = [], []
    n_empty = n_illegal = n_unordered = 0
    for e in entries:
        if e["状态"] in ("api_error", "illegal", "skipped", "not_run"):
            n_illegal += 1
            continue
        if not e["候选"]:
            n_empty += 1
            continue
        valid.append(e)
        if not e["有序"]:
            n_unordered += 1
        else:
            ordered.append(e)

    def _hits(es, key_fn):
        return sum(1 for e in es if key_fn(e))

    raw_hit = _hits(valid, lambda e: e["实际"] in e["候选"])
    first_hit = _hits(ordered, lambda e: e["候选"][0] == e["实际"])
    eq3_hit = _hits(ordered, lambda e: e["实际"] in e["候选"][:3])
    narrow = sum(1 for e in valid if len(e["候选"]) < 3)
    wide = sum(1 for e in valid if len(e["候选"]) > 3)
    top3_cmp = [e for e in valid if e.get("市场前三")]
    top3_same = sum(1 for e in top3_cmp
                    if set(e["候选"]) == set(e["市场前三"]))

    def _pct(a, b):
        return round(100.0 * a / b, 2) if b else None

    return {
        "已结算场次数": n,
        "有效输出场次": len(valid),
        "可用结果覆盖率": _pct(len(valid), n),
        "原始候选集合覆盖率": _pct(raw_hit, len(valid)),
        "首选命中率": _pct(first_hit, len(ordered)),
        "等宽3覆盖率": _pct(eq3_hit, len(ordered)),
        "候选数量分布": {str(k): sum(1 for e in valid if len(e["候选"]) == k)
                         for k in range(1, 5)},
        "空输出数": n_empty,
        "非法或失败输出数": n_illegal,
        "无序输出数": n_unordered,
        "窄集合占比": _pct(narrow, len(valid)),
        "宽集合数": wide,
        "与赔率前三重合率_描述性": _pct(top3_same, len(top3_cmp)),
    }


def evaluate_records(records):
    """对已回填赛果的影子记录计算 A/B/C/C2/D 五组指标。"""
    settled = [r for r in records if (r.get("实际赛果") or {}).get("总进球")]
    out = {}
    for group in ("A盲判", "B复核", "C正式", "C2独立", "D固定"):
        entries = []
        for r in settled:
            e = _group_entry(r, group)
            e["实际"] = r["实际赛果"]["总进球"]
            e["市场前三"] = r.get("市场前三") or []
            entries.append(e)
        out[group] = group_metrics(entries)
    out["样本量"] = len(settled)
    return out
