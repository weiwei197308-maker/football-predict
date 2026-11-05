#!/usr/bin/env python3
"""半全场独立分析影子实验（A/B/C/D + 市场组）—— 默认开启，绝不影响正式预测。

临时关闭方式：export DISABLE_HAFU_SHADOW=1

分组定义：
  A 组（纯盲判）：请求体仅由白名单字段构建——编号/联赛/主客队/排名/开赛/历史数据。
      不含任何赔率、比分赔率、已锁定比分、核心剧本输出或正式推荐。
      模型从 9 种合法组合中有序给出 0~4 个候选。
  B 组（市场复核）：输入 = A 组原始结果 + 独立构造的市场信息 + 锁定剧本。
      必须保留 A 组原始结果；变更需给出理由。B 组结果不得称为纯独立判断。
  C 组（正式预测）：三层原样记录——模型原始输出（_原始快照）、
      规则修正来源标记（半全场来源）、最终推荐（parsed），不改正式逻辑。
  D 组（固定基线）：固定 ["HH", "DD", "AA"]。
  市场组：半全场赔率升序前三，并列按 HH→AA 规范顺序裁决（明示规则）。

隔离约束：
  - 本模块所有函数只读 match_data / parsed，绝不修改正式对象；
  - 影子数据独立写入 output/影子实验_半全场_<date>.json，不进入正式 CSV / 快照；
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

# 9 种合法半全场组合，规范顺序与正式 CSV 列顺序一致（用于市场组并列裁决与分布统计）
HAFU_LABELS = ["HH", "HD", "HA", "DH", "DD", "DA", "AH", "AD", "AA"]
FIXED_BASELINE_PICKS = ["HH", "DD", "AA"]

# 正式半全场赔率键 → HDA 代码
HAFU_ODDS_KEY_TO_CODE = {
    "半主胜-全主胜": "HH", "半主胜-全平": "HD", "半主胜-全客胜": "HA",
    "半平-全主胜": "DH", "半平-全平": "DD", "半平-全客胜": "DA",
    "半客胜-全主胜": "AH", "半客胜-全平": "AD", "半客胜-全客胜": "AA",
}

# A 组盲判白名单：仅比赛与球队自身信息。任何赔率、比分赔率、锁定比分、
# 核心剧本输出、正式推荐及其衍生字段一律不得进入（白名单之外全部禁止）。
BLIND_INPUT_FIELDS = ("编号", "联赛", "主队", "客队", "主队排名", "客队排名",
                      "开赛日期", "开赛时间")

SHADOW_FILE_PREFIX = "影子实验_半全场_"
SHADOW_FILE_VERSION = 1

BLIND_SYSTEM_PROMPT = """你是一位专业的足球比赛分析师。本任务是「半全场盲判」：
你将**不会**收到任何赔率、盘口或市场信息，只能依据球队与比赛本身的信息做判断。

请根据给出的比赛信息，独立判断本场半场+全场结果组合，只返回合法 JSON：
{
  "hafu_blind_picks": ["HH", "DH"],
  "hafu_blind_reasoning": "依据球队风格、上下半场节奏、近期状态等足球因素的判断过程",
  "hafu_blind_uncertainty": "证据不足之处如实说明；若信息不足以给出候选，picks 返回空列表",
  "ordered": true
}

规则（必须遵守）：
- 合法取值仅 9 种：HH HD HA DH DD DA AH AD AA
  （H=主胜 D=平 A=客胜；第一位=半场结果，第二位=全场结果）。
- hafu_blind_picks 为 0~4 个候选，不得重复。
- ordered=true 表示列表按你认为的可能性从高到低排序；若无法排序，ordered=false，不得伪造顺序。
- 候选数量由证据决定，不要凑数；证据不足时宁可少给或返回空列表。
- 严禁提及或引用任何赔率、盘口信息——你没有也不应假设这些数据。
- 只输出 JSON，不要输出任何额外文字。"""

REVIEW_SYSTEM_PROMPT = """你是一位专业的足球比赛分析师。本任务是「半全场市场复核」：
此前已在**完全无赔率**条件下给出盲判结果。现在你将看到市场赔率信息与锁定比赛剧本，
请对盲判结果进行交叉验证，只返回合法 JSON：
{
  "hafu_review_picks": ["HH", "DH", "DD"],
  "changed": false,
  "change_reason": ""
}

规则（必须遵守）：
- 赔率只能作为参考证据，不能自动成为最终结论；不得无理由推翻盲判。
- changed=false 时 hafu_review_picks 必须与盲判完全一致；changed=true 时 change_reason
  必须非空，说明是哪个具体证据促使调整。
- hafu_review_picks 为 1~4 个候选，按可能性从高到低排序，合法取值为 9 种 HDA 组合。
- 只输出 JSON，不要输出任何额外文字。"""


def shadow_enabled():
    """影子实验开关：默认开启（不影响正式预测，任何失败仅打印不阻断）。

    如需临时关闭：export DISABLE_HAFU_SHADOW=1
    """
    return os.environ.get("DISABLE_HAFU_SHADOW", "") != "1"


def canon_hafu_code(token):
    """规范化半全场 HDA 代码；非法返回 None。仅接受 9 种合法组合。"""
    t = str(token or "").strip().upper()
    return t if t in HAFU_LABELS else None


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
        "半全场赔率最低三项": market_top3(md),
    }


def market_top3(match_data):
    """半全场赔率升序前三；并列时按 HH→AA 规范顺序取靠前项（明示裁决规则）。"""
    odds = (match_data or {}).get("半全场赔率") or {}
    items = []
    for idx, code in enumerate(HAFU_LABELS):
        raw = None
        for key, c in HAFU_ODDS_KEY_TO_CODE.items():
            if c == code:
                raw = odds.get(key)
                break
        if raw in (None, ""):
            continue
        try:
            items.append((float(raw), idx, code))
        except (TypeError, ValueError):
            continue
    items.sort()
    return [code for _, _, code in items[:3]]


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


def call_deepseek_hafu_blind(blind_input, api_key):
    """A 组盲判调用。请求体仅含白名单输入。失败返回 __API_ERROR__ 前缀文本。"""
    user_content = (
        "请在无赔率条件下独立判断本场半全场候选组合：\n\n"
        + json.dumps(blind_input, ensure_ascii=False, indent=2)
    )
    try:
        return _post_json(BLIND_SYSTEM_PROMPT, user_content, api_key, 600)
    except requests.RequestException as e:
        return f"__API_ERROR__: {e}"
    except (KeyError, IndexError, TypeError) as e:
        return f"__PARSE_ERROR__: {e}"


def call_deepseek_hafu_review(blind_result, market_info, locked_script, api_key):
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
        + "\n\n请复核盲判半全场候选，返回规定 JSON。"
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
        code = canon_hafu_code(token)
        if code is None:
            illegal.append(str(token))
            continue
        if code not in seen:
            seen.add(code)
            picks.append(code)
    if illegal:
        return None, "", f"含非法半全场编码 {illegal}"
    if len(picks) > max_n:
        return None, "", f"候选超过 {max_n} 项（{len(picks)} 项）"
    dedup_note = "原始输出含重复候选，已去重" if len(picks) < len(raw) else ""
    return picks, dedup_note, ""


def validate_hafu_blind(text):
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
    picks, note, perr = _validate_picks(obj, "hafu_blind_picks")
    if perr:
        result["错误"] = perr
        return result
    result["候选"] = picks
    result["理由"] = str(obj.get("hafu_blind_reasoning", "") or "")
    result["不确定说明"] = str(obj.get("hafu_blind_uncertainty", "") or "")
    result["有序"] = bool(obj.get("ordered", True))
    if note:
        result["不确定说明"] = (result["不确定说明"] + "；" + note).strip("；")
    result["状态"] = "empty" if not picks else "ok"
    return result


def validate_hafu_review(text, blind_picks):
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
    picks, note, perr = _validate_picks(obj, "hafu_review_picks")
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


def _canon_list(value):
    """任意形式的半全场候选（list 或分隔字符串）→ 合法代码列表（去重保序）。"""
    if isinstance(value, list):
        tokens = [str(t).strip() for t in value if str(t).strip()]
    else:
        tokens = [t.strip() for t in re.split(r"[/,，、\s]+", str(value or ""))
                  if t.strip()]
    out, seen = [], set()
    for t in tokens:
        c = canon_hafu_code(t)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _build_c_official(parsed):
    """C 组三层记录：模型原始输出 → 规则修正来源 → 最终推荐（只读，不改正式逻辑）。"""
    p = parsed or {}
    snap = (p.get("_原始快照") or {}).get("原始玩法输出") or {}
    raw_picks = snap.get("半全场推荐")
    raw_reason = snap.get("半全场理由", "")
    final_picks = _canon_list(p.get("半全场推荐", []))
    return {
        "状态": "ok" if final_picks else "empty",
        "候选": final_picks,
        "有序": True,
        "原始候选": _canon_list(raw_picks),
        "原始推荐原文": raw_picks if isinstance(raw_picks, (str, list)) else str(raw_picks or ""),
        "原始理由": str(raw_reason or ""),
        "半全场来源": str(p.get("半全场来源", "") or ""),
        "最终理由": str(p.get("半全场理由", "") or ""),
    }


def collect_shadow_record(row, history, match_data, parsed, api_key, target_date):
    """收集单场比赛的 A/B/C/D/市场 影子记录。任何阶段失败只记录不抛出。"""
    record = {
        "对齐键": make_align_key(target_date, row, match_data),
        "实验运行ID": str(uuid.uuid4()),
        "写入时间": datetime.now().isoformat(timespec="seconds"),
        "A盲判": {"状态": "not_run", "候选": [], "理由": "", "不确定说明": "",
                  "有序": True, "错误": "", "原始文本前缀": ""},
        "B复核": {"状态": "not_run", "候选": [], "模型声明变更": False,
                  "校验后变更": False, "变更理由": "", "理由缺失": False,
                  "错误": "", "原始文本前缀": ""},
        "C正式": {"状态": "not_run", "候选": [], "有序": True},
        "D固定": {"状态": "ok", "候选": list(FIXED_BASELINE_PICKS), "有序": True},
        "市场前三": market_top3(match_data),
        "实际赛果": None,
        "影子错误": "",
    }
    # A 组：盲判（请求体仅白名单字段）
    try:
        blind_raw = call_deepseek_hafu_blind(build_blind_input(row, history), api_key)
        record["A盲判"] = validate_hafu_blind(blind_raw)
    except Exception as e:  # 影子任何异常都不得阻断正式流程
        record["A盲判"].update(状态="api_error", 错误=f"盲判调用异常：{e}")
    # B 组：市场复核（A 组失败时无复核对象，跳过）
    if record["A盲判"]["状态"] in ("ok", "empty"):
        try:
            locked = ((parsed or {}).get("_原始快照") or {}).get("锁定核心剧本") or {}
            review_raw = call_deepseek_hafu_review(
                record["A盲判"], build_market_info(match_data), locked, api_key)
            record["B复核"] = validate_hafu_review(review_raw, record["A盲判"]["候选"])
        except Exception as e:
            record["B复核"].update(状态="api_error", 错误=f"复核调用异常：{e}")
    else:
        record["B复核"].update(状态="skipped", 错误="A 组失败，无复核对象")
    # C 组：三层原样记录
    try:
        record["C正式"] = _build_c_official(parsed)
    except Exception as e:
        record["C正式"] = {"状态": "illegal", "候选": [], "有序": True,
                           "错误": f"C 组记录异常：{e}"}
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
    for group in ("A盲判", "B复核", "C正式", "D固定"):
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
    """单组组合候选统计（轻量版，与 shadow_experiment 统计模块口径一致）。

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
    eq3_hit = sum(1 for e in ordered if e["实际"] in e["候选"][:3])
    cover_hit = sum(1 for e in valid if e["实际"] in e["候选"])
    dist = {c: sum(1 for e in valid if c in e["候选"]) for c in HAFU_LABELS}
    return {
        "已结算场次数": n,
        "有效输出场次": len(valid),
        "可用结果覆盖率": _pct(len(valid), n),
        "首选命中率": _pct(first_hit, len(ordered)),
        "等宽3覆盖率": _pct(eq3_hit, len(ordered)),
        "原始候选集合覆盖率": _pct(cover_hit, len(valid)),
        "九组合候选分布": dist,
        "空输出数": n_empty,
        "非法或失败输出数": n_illegal,
    }


def evaluate_records(records):
    """对已回填赛果的影子记录计算四组指标（轻量版）。"""
    settled = [r for r in records if (r.get("实际赛果") or {}).get("半全场")]
    out = {}
    for group in ("A盲判", "B复核", "C正式", "D固定"):
        entries = []
        for r in settled:
            e = _group_entry(r, group)
            e["实际"] = r["实际赛果"]["半全场"]
            entries.append(e)
        out[group] = group_metrics(entries)
    out["样本量"] = len(settled)
    return out
