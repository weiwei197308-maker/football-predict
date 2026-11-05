#!/usr/bin/env python3
"""让球胜平负独立分析影子实验（A/B/C/D + 市场组）—— 默认开启，绝不影响正式预测。

临时关闭方式：export DISABLE_RQ_SHADOW=1

分组定义：
  A 组（纯盲判）：请求体仅由白名单字段构建——编号/联赛/主客队/排名/开赛/历史数据/
      体彩让球数（让球数是比赛条件，不是市场信息）。模型输出净胜球五档概率，
      程序用与正式生产 v2 完全相同的 map_net_goal_probs_to_handicap +
      select_handicap_picks 确定性映射出主推/次选，模型不直接给让球结论。
  B 组（市场复核）：输入 = A 组原始结果 + 独立构造的市场信息 + 锁定剧本。
      同样输出五档概率并经同一程序映射。必须保留 A 组原始结果；变更需给出理由。
  C 组（正式预测）：原样记录现有正式让球推荐/次选/推导版本/概率字段，不改正式逻辑。
  D 组（规则基线）：v1 单点映射——锁定首选比分 + 体彩让球数 →
      derive_handicap_recommendation（与生产 v1 降级口径一致）。
  市场组：让球赔率隐含概率降序，并列按 主胜→平→客胜 裁决（明示规则）。

隔离约束：
  - 本模块所有函数只读 match_data / parsed，绝不修改正式对象；
  - 影子数据独立写入 output/影子实验_让球_<date>.json，不进入正式 CSV / 快照；
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

from lottery_rules import (
    NET_GOAL_BUCKETS,
    HANDICAP_OUTCOMES,
    parse_handicap,
    validate_net_goal_probabilities,
    map_net_goal_probs_to_handicap,
    select_handicap_picks,
    derive_handicap_recommendation,
    _extract_handicap_direction,
    implied_probabilities,
)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# A 组盲判白名单：仅比赛与球队自身信息 + 体彩让球数（比赛条件）。
# 任何赔率、比分赔率、锁定比分、核心剧本输出、正式推荐及其衍生字段一律禁止。
BLIND_INPUT_FIELDS = ("编号", "联赛", "主队", "客队", "主队排名", "客队排名",
                      "开赛日期", "开赛时间", "让球数")

SHADOW_FILE_PREFIX = "影子实验_让球_"
SHADOW_FILE_VERSION = 1

RQ_DIRECTIONS = list(HANDICAP_OUTCOMES)  # ["主胜", "平", "客胜"]

BLIND_SYSTEM_PROMPT = """你是一位专业的足球比赛分析师。本任务是「让球胜平负盲判」：
你将**不会**收到任何赔率、盘口或市场信息，只能依据球队与比赛本身的信息做判断。
体彩让球数会作为比赛条件给出（负数=主队让球，正数=客队让球），它不是市场信息。

请根据给出的比赛信息，独立判断本场净胜球五档互斥结果的概率分布，只返回合法 JSON：
{
  "net_goal_probabilities": {"主胜2+": 0.25, "主胜1": 0.25, "平": 0.25, "客胜1": 0.15, "客胜2+": 0.10},
  "rq_blind_reasoning": "依据球队攻防、实力差、联赛风格、近期状态等足球因素的判断过程",
  "rq_blind_uncertainty": "证据不足之处如实说明",
  "sufficient": true
}

规则（必须遵守）：
- net_goal_probabilities 五项必须齐全、为 0~1 小数、合计恰为 1.0，顺序固定为
  主胜2+、主胜1、平、客胜1、客胜2+（净胜球 = 主队进球 - 客队进球）。
- 概率必须来自你对比赛剧本与实力差的真实信念，有主次之分；严禁五项平均分配或凑数。
- 证据确实不足时 sufficient=false 且 net_goal_probabilities 给 null，不要伪造分布。
- 让球胜平负结论由程序按让球数从你的概率分布确定性推导，你不需要也不允许直接给让球推荐。
- 严禁提及或引用任何赔率、盘口信息——你没有也不应假设这些数据。
- 只输出 JSON，不要输出任何额外文字。"""

REVIEW_SYSTEM_PROMPT = """你是一位专业的足球比赛分析师。本任务是「让球胜平负市场复核」：
此前已在**完全无赔率**条件下给出净胜球五档概率盲判。现在你将看到市场赔率信息与锁定比赛剧本，
请对盲判概率进行交叉验证，只返回合法 JSON：
{
  "net_goal_probabilities": {"主胜2+": 0.25, "主胜1": 0.25, "平": 0.25, "客胜1": 0.15, "客胜2+": 0.10},
  "changed": false,
  "change_reason": ""
}

规则（必须遵守）：
- 赔率只能作为参考证据，不能自动成为最终结论；不得无理由推翻盲判。
- changed=false 时 net_goal_probabilities 必须与盲判完全一致；changed=true 时 change_reason
  必须非空，说明是哪个具体证据促使调整。
- net_goal_probabilities 五项齐全、0~1 小数、合计恰为 1.0，顺序固定。
- 严禁五项平均分配或凑数；复核结果仍是有主次的真实信念分布。
- 只输出 JSON，不要输出任何额外文字。"""


def shadow_enabled():
    """影子实验开关：默认开启（不影响正式预测，任何失败仅打印不阻断）。

    如需临时关闭：export DISABLE_RQ_SHADOW=1
    """
    return os.environ.get("DISABLE_RQ_SHADOW", "") != "1"


def canon_rq_direction(token):
    """规范化让球方向标签；非法返回 None。接受 主胜/平/客胜 或 让-1主胜 形式。"""
    t = str(token or "").strip()
    if not t:
        return None
    if t in RQ_DIRECTIONS:
        return t
    return _extract_handicap_direction(t)


def build_blind_input(row, history=None):
    """仅从白名单字段构建 A 组盲判输入（新建对象，不复制 match_data）。"""
    data = {k: str(row.get(k, "") or "") for k in BLIND_INPUT_FIELDS}
    home_rank = data["主队排名"]
    away_rank = data["客队排名"]
    data["对阵"] = f"{data['主队']}{home_rank} vs {data['客队']}{away_rank}"
    data["开赛"] = f"{data['开赛日期']} {data['开赛时间']}".strip()
    # 让球数规范化为整数字符串；无法解析时保留原文并在记录中标注（不伪造）
    hc = parse_handicap(data.get("让球数"))
    data["体彩让球数"] = str(hc) if hc is not None else str(data.get("让球数") or "")
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
    rq = md.get("让球赔率") or {}
    return {
        "胜平负赔率": md.get("胜平负赔率") or {},
        "让球赔率": rq,
        "总进球赔率": md.get("总进球赔率") or {},
        "半全场赔率": md.get("半全场赔率") or {},
        "比分赔率": md.get("比分赔率") or {},
        "让球市场倾向": [d for d, _ in rq_market_rank(match_data)],
    }


def rq_market_rank(match_data):
    """让球赔率隐含概率降序 [(方向, 概率), ...]；并列按 主胜→平→客胜 裁决（明示规则）。"""
    rq = (match_data or {}).get("让球赔率") or {}
    odds = {k: rq.get(k) for k in RQ_DIRECTIONS}
    probs = implied_probabilities(odds)
    order = {d: i for i, d in enumerate(RQ_DIRECTIONS)}
    return sorted(probs.items(), key=lambda kv: (-kv[1], order.get(kv[0], 99)))


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


def call_deepseek_rq_blind(blind_input, api_key):
    """A 组盲判调用。请求体仅含白名单输入。失败返回 __API_ERROR__ 前缀文本。"""
    user_content = (
        "请在无赔率条件下独立判断本场净胜球五档概率分布：\n\n"
        + json.dumps(blind_input, ensure_ascii=False, indent=2)
    )
    try:
        return _post_json(BLIND_SYSTEM_PROMPT, user_content, api_key, 600)
    except requests.RequestException as e:
        return f"__API_ERROR__: {e}"
    except (KeyError, IndexError, TypeError) as e:
        return f"__PARSE_ERROR__: {e}"


def call_deepseek_rq_review(blind_result, market_info, locked_script, api_key):
    """B 组市场复核调用。blind_result 为 A 组原始结果（只读，不回传修改）。"""
    user_content = (
        "【盲判结果（无赔率条件下给出，请尊重其独立性）】\n"
        + json.dumps({
            "盲判净胜球概率": blind_result.get("净胜球概率"),
            "盲判主推": blind_result.get("主推", ""),
            "盲判次选": blind_result.get("次选", ""),
            "盲判理由": blind_result.get("理由", ""),
            "盲判不确定说明": blind_result.get("不确定说明", ""),
        }, ensure_ascii=False, indent=2)
        + "\n\n【市场信息（仅供交叉验证，不得自动成为结论）】\n"
        + json.dumps(market_info, ensure_ascii=False, indent=2)
        + "\n\n【已锁定核心剧本（仅供节奏参考）】\n"
        + json.dumps(locked_script or {}, ensure_ascii=False, indent=2)
        + "\n\n请复核盲判净胜球概率分布，返回规定 JSON。"
    )
    try:
        return _post_json(REVIEW_SYSTEM_PROMPT, user_content, api_key, 600)
    except requests.RequestException as e:
        return f"__API_ERROR__: {e}"
    except (KeyError, IndexError, TypeError) as e:
        return f"__PARSE_ERROR__: {e}"


def _empty_group_result(text):
    return {"状态": "illegal", "净胜球概率": None, "概率校验": {},
            "让球映射": {}, "主推": "", "次选": "", "建议双选": False,
            "候选": [], "理由": "", "不确定说明": "", "有序": True,
            "错误": "", "原始文本前缀": str(text or "")[:200]}


def _probs_and_map(probs_raw, handicap, rq_odds=None):
    """五档概率 → 校验 → 映射 → 主推/次选。

    返回 (状态, 结果dict)。状态 ∈ ok/empty/illegal。全程不修改 probs_raw。
    """
    out = {"净胜球概率": None, "概率校验": {}, "让球映射": {},
           "主推": "", "次选": "", "建议双选": False, "候选": [], "错误": ""}
    hc = parse_handicap(handicap)
    if hc is None:
        out["错误"] = f"体彩让球数 {handicap!r} 无法解析，让球映射无法计算"
        return "illegal", out
    if probs_raw is None:
        return "empty", out  # 模型明示证据不足：不给概率，不伪造
    check = validate_net_goal_probabilities(probs_raw)
    out["概率校验"] = {k: check.get(k) for k in
                     ("ok", "normalized", "warnings", "reason")}
    if not check.get("ok"):
        out["错误"] = f"净胜球概率校验未通过：{check.get('reason', '未知原因')}"
        return "illegal", out
    probs = check["probabilities"]
    mapping = map_net_goal_probs_to_handicap(probs, hc)
    decision = select_handicap_picks(mapping, handicap_odds=rq_odds)
    primary = decision.get("primary") or ""
    secondary = decision.get("secondary") or ""
    picks = [d for d in (primary, secondary) if d in RQ_DIRECTIONS]
    out.update(
        净胜球概率={b: probs[b] for b in NET_GOAL_BUCKETS},
        让球映射={
            "handicap": mapping.get("handicap"),
            "exact": mapping.get("exact"),
            "probabilities": mapping.get("probabilities"),
            "bounds": mapping.get("bounds"),
            "straddles": mapping.get("straddles"),
        },
        主推=primary,
        次选=secondary,
        建议双选=bool(decision.get("double")),
        候选=picks,
    )
    return ("ok" if picks else "empty"), out


def validate_rq_blind(text, handicap):
    """校验 A 组盲判输出。返回结构化字典，状态 ∈ ok/empty/illegal/api_error。"""
    result = _empty_group_result(text)
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
    result["理由"] = str(obj.get("rq_blind_reasoning", "") or "")
    result["不确定说明"] = str(obj.get("rq_blind_uncertainty", "") or "")
    sufficient = obj.get("sufficient", True)
    probs_raw = obj.get("net_goal_probabilities")
    if sufficient is False or probs_raw is None:
        result["状态"] = "empty"
        if sufficient is False and not result["不确定说明"]:
            result["不确定说明"] = "模型声明证据不足（sufficient=false）"
        return result
    state, mapped = _probs_and_map(probs_raw, handicap, rq_odds=None)
    result.update(mapped)
    result["状态"] = state
    return result


def validate_rq_review(text, blind_picks, handicap, rq_odds=None):
    """校验 B 组复核输出。blind_picks 用于计算校验后变更，不被修改。"""
    result = _empty_group_result(text)
    result.update(模型声明变更=False, 校验后变更=False, 变更理由="", 理由缺失=False)
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
    probs_raw = obj.get("net_goal_probabilities")
    if probs_raw is None:
        result["错误"] = "复核未给出净胜球概率（B 组不允许弃选后覆盖盲判）"
        return result
    state, mapped = _probs_and_map(probs_raw, handicap, rq_odds=rq_odds)
    result.update(mapped)
    if state != "ok":
        result["状态"] = state
        return result
    claimed = bool(obj.get("changed", False))
    reason = str(obj.get("change_reason", "") or "").strip()
    actual_changed = list(result["候选"]) != list(blind_picks or [])
    if actual_changed and not claimed:
        reason = ("[校验] 模型声明未变更但候选实际不同，按已变更记录。"
                  + (f"模型理由：{reason}" if reason else ""))
    result.update(模型声明变更=claimed, 校验后变更=actual_changed,
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


def _build_c_official(parsed):
    """C 组：原样记录正式让球推荐及概率字段（只读，不改正式逻辑）。"""
    p = parsed or {}
    rec = p.get("让球推荐", "") or ""
    sec = p.get("让球次选", "") or ""
    primary_dir = canon_rq_direction(rec)
    secondary_dir = canon_rq_direction(sec)
    picks = [d for d in (primary_dir, secondary_dir) if d]
    return {
        "状态": "ok" if picks else "empty",
        "候选": picks,
        "有序": True,
        "让球推荐": str(rec),
        "让球次选": str(sec),
        "让球推导版本": str(p.get("让球推导版本", "") or ""),
        "概率来源": str(p.get("概率来源", "") or ""),
        "净胜球概率分布": p.get("净胜球概率分布"),  # dict 或 None，原样记录
        "让球结果概率": p.get("让球结果概率"),      # dict 或 None，原样记录
    }


def _build_d_baseline(parsed, handicap):
    """D 组：v1 单点映射——锁定首选比分 + 体彩让球数 → 方向。无法判定给 empty。"""
    locked = ((parsed or {}).get("_原始快照") or {}).get("锁定核心剧本") or {}
    primary_score = locked.get("primary_score", "")
    derived = derive_handicap_recommendation(primary_score, handicap)
    direction = canon_rq_direction(derived)
    return {
        "状态": "ok" if direction else "empty",
        "候选": [direction] if direction else [],
        "有序": True,
        "锁定首选比分": str(primary_score or ""),
        "让球推荐": derived,
        "错误": "" if direction else "锁定首选比分或让球数缺失/非法，v1 单点无法推导",
    }


def collect_shadow_record(row, history, match_data, parsed, api_key, target_date):
    """收集单场比赛的 A/B/C/D/市场 影子记录。任何阶段失败只记录不抛出。"""
    handicap = (match_data or {}).get("让球赔率", {}).get("让球数") \
        or (row or {}).get("让球数", "")
    record = {
        "对齐键": make_align_key(target_date, row, match_data),
        "实验运行ID": str(uuid.uuid4()),
        "写入时间": datetime.now().isoformat(timespec="seconds"),
        "体彩让球数": str(parse_handicap(handicap))
        if parse_handicap(handicap) is not None else str(handicap or ""),
        "A盲判": _empty_group_result(""),
        "B复核": dict(_empty_group_result(""), 模型声明变更=False,
                      校验后变更=False, 变更理由="", 理由缺失=False),
        "C正式": {"状态": "not_run", "候选": [], "有序": True},
        "D规则": {"状态": "not_run", "候选": [], "有序": True},
        "市场倾向": {"候选": [d for d, _ in rq_market_rank(match_data)],
                      "赔率": {k: ((match_data or {}).get("让球赔率") or {}).get(k)
                                 for k in RQ_DIRECTIONS}},
        "实际赛果": None,
        "影子错误": "",
    }
    record["A盲判"]["状态"] = "not_run"
    record["B复核"]["状态"] = "not_run"
    # A 组：盲判（请求体仅白名单字段）
    try:
        blind_raw = call_deepseek_rq_blind(build_blind_input(row, history), api_key)
        record["A盲判"] = validate_rq_blind(blind_raw, handicap)
    except Exception as e:  # 影子任何异常都不得阻断正式流程
        record["A盲判"].update(状态="api_error", 错误=f"盲判调用异常：{e}")
    # B 组：市场复核（A 组失败时无复核对象，跳过）
    if record["A盲判"]["状态"] in ("ok", "empty"):
        try:
            locked = ((parsed or {}).get("_原始快照") or {}).get("锁定核心剧本") or {}
            rq_odds = {k: ((match_data or {}).get("让球赔率") or {}).get(k)
                       for k in RQ_DIRECTIONS}
            review_raw = call_deepseek_rq_review(
                record["A盲判"], build_market_info(match_data), locked, api_key)
            record["B复核"] = validate_rq_review(
                review_raw, record["A盲判"]["候选"], handicap, rq_odds=rq_odds)
        except Exception as e:
            record["B复核"].update(状态="api_error", 错误=f"复核调用异常：{e}")
    else:
        record["B复核"].update(状态="skipped", 错误="A 组失败，无复核对象")
    # C 组：正式预测原样记录
    try:
        record["C正式"] = _build_c_official(parsed)
    except Exception as e:
        record["C正式"] = {"状态": "illegal", "候选": [], "有序": True,
                           "错误": f"C 组记录异常：{e}"}
    # D 组：v1 规则基线
    try:
        record["D规则"] = _build_d_baseline(parsed, handicap)
    except Exception as e:
        record["D规则"] = {"状态": "illegal", "候选": [], "有序": True,
                           "错误": f"D 组构建异常：{e}"}
    return record


# ---------------------------------------------------------------- 文件写入

def shadow_file_path(target_date, output_dir="output"):
    return os.path.join(output_dir, f"{SHADOW_FILE_PREFIX}{target_date}.json")


def _atomic_write_json(path, obj):
    """临时文件（同目录）→ 回读校验 → os.replace。失败清理临时文件并抛出。"""
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
    for group in ("A盲判", "B复核", "C正式", "D规则"):
        if group not in record:
            return f"缺少 {group} 字段"
    return ""


def write_shadow_file(records, target_date, output_dir="output", collected_count=None):
    """合并写入影子文件。返回统计字典；任何失败都不抛出（影子不阻断正式）。

    计数口径与 ttg_shadow.write_shadow_file 完全一致：
      本次输入数 / 成功收集数 / 非法剔除数 / 新增数 / 重复跳过数 / 冲突数 / 合并后总记录数
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
                # 保留首次记录，冲突进入独立列表，绝不静默覆盖
                conflicts.append({"时间": datetime.now().isoformat(timespec="seconds"),
                                  "对齐键": rec["对齐键"], "已保留": by_key[fp_key],
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


# ---------------------------------------------------------------- 评估口径（模块内轻量版）

def _group_entry(record, group):
    g = record.get(group) or {}
    return {
        "候选": list(g.get("候选", []) or []),
        "有序": bool(g.get("有序", True)),
        "状态": g.get("状态", "ok"),
    }


def group_metrics(entries):
    """单组方向候选统计（轻量版，与 shadow_experiment 统计模块口径一致）。

    entries: [{候选, 有序, 状态, 实际}, ...]，仅含已结算且可关联场次。
    """
    n = len(entries)
    valid, ordered = [], []
    n_empty = n_illegal = 0
    for e in entries:
        if e["状态"] in ("api_error", "illegal", "skipped", "not_run"):
            n_illegal += 1
            continue
        if not e["候选"]:
            n_empty += 1
            continue
        valid.append(e)
        if e["有序"]:
            ordered.append(e)

    def _pct(a, b):
        return round(100.0 * a / b, 2) if b else None

    first_hit = sum(1 for e in ordered if e["候选"][0] == e["实际"])
    cover_hit = sum(1 for e in valid if e["实际"] in e["候选"])
    dist = {d: sum(1 for e in valid if e["候选"] and e["候选"][0] == d)
            for d in RQ_DIRECTIONS}
    return {
        "已结算场次数": n,
        "有效输出场次": len(valid),
        "可用结果覆盖率": _pct(len(valid), n),
        "首选命中率": _pct(first_hit, len(ordered)),
        "主推次选覆盖率": _pct(cover_hit, len(valid)),
        "首选方向分布": dist,
        "空输出数": n_empty,
        "非法或失败输出数": n_illegal,
    }


def evaluate_records(records):
    """对已回填赛果的影子记录计算四组 + 市场组指标（轻量版）。"""
    settled = [r for r in records if (r.get("实际赛果") or {}).get("让球")]
    out = {}
    for group in ("A盲判", "B复核", "C正式", "D规则", "市场倾向"):
        entries = []
        for r in settled:
            e = _group_entry(r, group)
            e["实际"] = r["实际赛果"]["让球"]
            entries.append(e)
        out[group] = group_metrics(entries)
    out["样本量"] = len(settled)
    return out
