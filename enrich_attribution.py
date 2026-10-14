#!/usr/bin/env python3
"""深度归因脚本（独立工具，不修改 review.py 主流程）。

读取 output/每日记录_<date>.json，挑出存在预测错误（任一玩法 命中=False）的比赛，
调用带联网搜索能力的大模型（智谱 GLM / Kimi）搜索真实赛后战报，
生成结合真实意外因素（红牌、伤病、轮换、天气、判罚、运气等）的深度归因。

输出（均为新文件，不覆盖任何现有产物；目标已存在时拒绝写入，退出码 2）：
  output/深度归因_<date>.json   结构化对照（原归因 vs 深度归因）
  output/深度归因_<date>.txt    人类可读对照报告

用法：
  python3 enrich_attribution.py 2026-09-27                    # 默认智谱
  python3 enrich_attribution.py 2026-09-27 --provider kimi    # 用 Kimi
  python3 enrich_attribution.py 2026-09-27 --dry-run          # 只挑错题，不调 API
  python3 enrich_attribution.py 2026-09-27 --suffix _test     # 另存，不覆盖
  python3 enrich_attribution.py 2026-09-27 --model glm-4-flash

环境变量（二选一，与 --provider 对应）：
  ZHIPU_API_KEY    智谱开放平台密钥（https://open.bigmodel.cn）
  MOONSHOT_API_KEY Kimi 开放平台密钥（https://platform.moonshot.cn，KIMI_API_KEY 亦可）

诚实性约束：
  深度归因为联网搜索增强的 LLM 生成内容，仅作复盘参考，不构成事实保证与投注建议；
  prompt 明确要求"搜不到确凿战报时如实说明，不得编造"，输出文件头部带免责声明。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

import requests

ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
KIMI_URL = "https://api.moonshot.cn/v1/chat/completions"

PROVIDERS = {
    "zhipu": {
        "url": ZHIPU_URL,
        "key_env": "ZHIPU_API_KEY",
        "alt_key_envs": ["GLM_API_KEY"],
        "default_model": "glm-4.6",
        # 智谱：web_search 工具，服务端自动联网
        "tools": [{"type": "web_search", "web_search": {"enable": True}}],
        # GLM-4.5+/4.6 默认深度思考，思考+搜索会耗尽 max_tokens 导致正文为空（finish_reason=length），
        # 本任务只需搜索+摘要，显式关闭；并加大输出预算。
        "extra": {"thinking": {"type": "disabled"}},
    },
    "kimi": {
        "url": KIMI_URL,
        "key_env": "MOONSHOT_API_KEY",
        "alt_key_envs": ["KIMI_API_KEY"],
        "default_model": "kimi-latest",
        # Kimi：$web_search 内置函数，服务端自动执行，无需客户端处理 tool_calls
        "tools": [{"type": "builtin_function", "function": {"name": "$web_search"}}],
    },
}

PLAYBOOK_CN = ["胜平负", "让球", "总进球", "半全场", "比分"]

# 第一步：裸查询（不含指令），实测长指令会导致模型放弃构造搜索词、
# 受"日期在未来"的内部困惑干扰而直接答"未搜到"；裸查询能稳定触发联网搜索取回真实战报。
SEARCH_QUERY_TEMPLATE = "{home} {score} {away} {league} 2026年 赛后战报"

ATTRIBUTION_TEMPLATE = """你是竞彩足球复盘专家。以下是这场比赛的预测失败情况，以及通过联网搜索取回的赛后战报材料。

比赛：{num} {league}，{home} vs {away}（2026年{match_date}前后开球）
实际比分：{score}（半场 {half}）
AI 信心指数：{conf}/10

预测与实际的偏差：
{error_summary}

赛后战报材料（来自联网搜索）：
"""
ATTRIBUTION_TEMPLATE_TAIL = """
要求（严格遵守）：
1. 【事实依据】必须**只来自上述材料**，不得编造；材料为空或明确无战报时，如实写"未搜到可靠战报"。
2. 【失败根因】1-3 条，每条说明一个真实/高度可能的原因及它主要影响了哪个玩法（{playbook}）；材料中找不到事实支撑时，标注"（推测）"。
3. 【与旧归因的差异】1 句话：相比单纯"高估/低估"式结果归因，本次多发现了什么。
4. 总字数 200-400 字，直接输出三个小节，不要重复问题。"""


def fail(msg, code=1):
    print(f"❌ {msg}")
    sys.exit(code)


def validate_key(api_key):
    """密钥缺失或非 ASCII（HTTP 头 latin-1 限制）时在请求前拦截，不回显完整密钥。"""
    if not api_key:
        return False, "密钥未设置"
    if not api_key.isascii():
        return False, "密钥含非 ASCII 字符（HTTP 头将编码失败）"
    if len(api_key) < 8:
        return False, "密钥长度异常"
    return True, ""


def resolve_key(provider):
    cfg = PROVIDERS[provider]
    val = os.environ.get(cfg["key_env"], "").strip()
    if not val:
        for alt in cfg.get("alt_key_envs", []):
            val = os.environ.get(alt, "").strip()
            if val:
                break
    if not val:
        return None
    return val


def collect_failed_matches(record):
    """挑出命中 dict 中存在 False 的场次；返回 [(match, wrong_dict)]。"""
    failed = []
    for m in record.get("场次", []):
        hits = m.get("命中") or {}
        wrong = {k: v for k, v in hits.items() if v is False}
        if wrong:
            failed.append((m, wrong))
    return failed


def build_error_summary(wrong):
    lines = []
    for dim in PLAYBOOK_CN:
        if dim in wrong:
            lines.append(f"  - {dim}：预测错误")
    return "\n".join(lines)


def split_teams(duizhen):
    parts = str(duizhen or "").split(" vs ")
    home = parts[0].strip() if parts else ""
    away = parts[-1].strip() if len(parts) > 1 else ""
    return home, away


def build_search_query(match, date_str):
    result = match.get("赛果") or {}
    home, away = split_teams(match.get("对阵"))
    return SEARCH_QUERY_TEMPLATE.format(
        home=home, away=away,
        score=result.get("全场", ""),
        league=match.get("联赛", ""),
    )


def build_attribution_prompt(match, wrong, date_str, report_material):
    result = match.get("赛果") or {}
    home, away = split_teams(match.get("对阵"))
    return (
        ATTRIBUTION_TEMPLATE.format(
            num=match.get("编号", ""),
            league=match.get("联赛", ""),
            home=home, away=away,
            match_date=date_str,
            score=result.get("全场", "?"),
            half=result.get("半场", "?"),
            conf=match.get("信心指数", "?"),
            error_summary=build_error_summary(wrong),
        )
        + (report_material or "（空——未取回任何战报材料）")
        + ATTRIBUTION_TEMPLATE_TAIL.format(playbook="/".join(PLAYBOOK_CN))
    )


def _post_chat(provider, api_key, payload):
    cfg = PROVIDERS[provider]
    resp = requests.post(
        cfg["url"],
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        # 极端情况：模型只回 tool_calls 没给正文
        raise RuntimeError(f"响应无正文（finish_reason={data['choices'][0].get('finish_reason')}）")
    # 宽松提取联网引用（不同版本字段名不一，取不到则留空）
    refs = []
    for k in ("search_result", "search_results", "web_search", "citations"):
        v = msg.get(k) or data.get(k)
        if v:
            refs.append(v)
    return content, refs


def call_web_llm(provider, model, api_key, prompt):
    """第一步：带联网工具的裸查询，取回赛后战报材料。"""
    cfg = PROVIDERS[provider]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": cfg["tools"],
        "temperature": 0.3,
        "max_tokens": 4000,
    }
    payload.update(cfg.get("extra") or {})
    return _post_chat(provider, api_key, payload)


def call_plain_llm(provider, model, api_key, prompt):
    """第二步：无联网工具，仅依据已取回的战报材料生成结构化归因。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2000,
    }
    payload.update(PROVIDERS[provider].get("extra") or {})
    return _post_chat(provider, api_key, payload)


def check_suffix(suffix):
    if suffix and not re.fullmatch(r"[A-Za-z0-9_\-]+", suffix):
        fail(f"suffix 仅允许字母/数字/下划线/连字符：{suffix!r}", 1)
    return suffix


def out_paths(date_str, suffix):
    base = f"深度归因_{date_str}{suffix}"
    return os.path.join("output", base + ".json"), os.path.join("output", base + ".txt")


def refuse_if_exists(paths):
    for p in paths:
        if os.path.exists(p):
            fail(f"目标文件已存在，拒绝覆盖：{p}（需另存时加 --suffix，如 --suffix _v2）", 2)


def build_txt(report):
    lines = []
    lines.append(f"深度归因对照报告 - {report['复盘日期']}")
    lines.append(f"生成时间：{report['生成时间']}  提供方：{report['provider']}  模型：{report['model']}")
    lines.append(f"错题场数：{report['错题总数']}（深度归因成功 {report['成功数']} / 失败 {report['失败数']}）")
    lines.append("说明：深度归因为联网搜索增强的 LLM 生成内容，仅作复盘参考，不构成事实保证与投注建议。")
    lines.append("=" * 60)
    for m in report["场次"]:
        lines.append(f"\n■ {m['编号']} {m['对阵']}（{m['联赛']}）  实际比分 {m['实际比分']}")
        lines.append(f"  错误玩法：{'、'.join(m['错误玩法'])}")
        lines.append(f"  【原归因】{m['原错误归因'] or '（无）'}")
        material = m.get("战报材料")
        if material:
            lines.append(f"  【战报材料·联网搜索】{material[:600]}{'…' if len(material) > 600 else ''}")
        else:
            lines.append("  【战报材料·联网搜索】（未取回）")
        lines.append(f"  【深度归因】\n{m['深度归因']}")
        if m.get("搜索引用"):
            lines.append(f"  【搜索引用】{json.dumps(m['搜索引用'], ensure_ascii=False)[:800]}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="联网深度归因（独立脚本，不改 review.py）")
    ap.add_argument("date", help="复盘日期，如 2026-09-27")
    ap.add_argument("--provider", choices=list(PROVIDERS), default="zhipu")
    ap.add_argument("--model", default=None, help="覆盖默认模型名")
    ap.add_argument("--suffix", default="", help="输出文件名后缀（另存用，防止覆盖）")
    ap.add_argument("--dry-run", action="store_true", help="只列出错题，不调 API、不写文件")
    args = ap.parse_args()

    date_str = args.date
    suffix = check_suffix(args.suffix)

    record_path = os.path.join("output", f"每日记录_{date_str}.json")
    if not os.path.exists(record_path):
        fail(f"找不到每日记录：{record_path}", 1)

    with open(record_path, encoding="utf-8") as f:
        record = json.load(f)

    failed = collect_failed_matches(record)
    print(f"{'='*60}\n  深度归因 - {date_str}（provider={args.provider}）\n{'='*60}")
    print(f"每日记录共 {len(record.get('场次', []))} 场，其中错题 {len(failed)} 场：")
    for m, wrong in failed:
        print(f"  {m.get('编号')} {m.get('对阵')}  错误玩法：{'、'.join(wrong)}")

    if args.dry_run:
        print("\n[dry-run] 未调用 API、未写文件。")
        return

    if not failed:
        print("\n当日无错题，无需深度归因。")
        return

    api_key = resolve_key(args.provider)
    if api_key is None:
        cfg = PROVIDERS[args.provider]
        fail(
            f"未设置 {cfg['key_env']}"
            + (f"（或 {'/'.join(cfg.get('alt_key_envs', []))}）" if cfg.get("alt_key_envs") else "")
            + f"。请先在终端执行：export {cfg['key_env']}=<你的密钥>（写入 ~/.zshrc 时用追加，不要整体覆盖）；"
              "不要把密钥写入代码或聊天窗口。",
            1,
        )
    key_ok, key_err = validate_key(api_key)
    if not key_ok:
        fail(f"API 密钥校验未通过：{key_err}，终止于任何网络请求之前。", 1)

    model = args.model or PROVIDERS[args.provider]["default_model"]
    out_json, out_txt = out_paths(date_str, suffix)
    refuse_if_exists([out_json, out_txt])

    entries = []
    n_ok = n_fail = 0
    for i, (m, wrong) in enumerate(failed, 1):
        num = m.get("编号", "")
        home_away = m.get("对阵", "")
        query = build_search_query(m, date_str)
        print(f"\n[{i}/{len(failed)}] 🔍 {num} {home_away}")
        print(f"  搜索查询：{query}")
        try:
            material, refs = call_web_llm(args.provider, model, api_key, query)
            print(f"  ✅ 战报材料取回（{len(material)} 字）")
        except Exception as e:
            material, refs = None, []
            print(f"  ⚠️ 战报搜索失败：{e}（将以无材料继续归因）")
        try:
            content, _ = call_plain_llm(
                args.provider, model, api_key,
                build_attribution_prompt(m, wrong, date_str, material),
            )
            n_ok += 1
            print(f"  ✅ 深度归因完成（{len(content)} 字）")
        except Exception as e:
            n_fail += 1
            content = f"(深度归因调用失败：{e})"
            print(f"  ❌ 归因失败：{e}")
        entries.append({
            "编号": num,
            "联赛": m.get("联赛", ""),
            "对阵": home_away,
            "实际比分": (m.get("赛果") or {}).get("全场", ""),
            "错误玩法": list(wrong),
            "搜索查询": query,
            "战报材料": material,
            "原错误归因": m.get("错误归因", ""),
            "深度归因": content,
            "搜索引用": refs,
        })

    report = {
        "复盘日期": date_str,
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "provider": args.provider,
        "model": model,
        "说明": "深度归因为联网搜索增强的 LLM 生成内容，仅作复盘参考，不构成事实保证与投注建议；"
                "本文件为独立脚本新增产物，未修改 review.py 输出的每日记录。",
        "错题总数": len(failed),
        "成功数": n_ok,
        "失败数": n_fail,
        "场次": entries,
    }

    os.makedirs("output", exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(build_txt(report) + "\n")

    print(f"\n{'='*60}")
    print(f"  完成：成功 {n_ok} / 失败 {n_fail}")
    print(f"  输出：{out_json}")
    print(f"        {out_txt}")
    if n_fail:
        print("  ⚠️ 存在失败场次（已写入失败标记，未中断其余场次），详见 JSON。")
        sys.exit(3)


if __name__ == "__main__":
    main()
