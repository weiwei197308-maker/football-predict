#!/usr/bin/env python3
"""推送今日竞彩预测 + 昨日复盘摘要到飞书群（自定义机器人 Webhook）"""

import csv
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")


def find_file(prefix, target_date_str, ext="csv"):
    """找指定日期的文件，找不到则回退到最新一份"""
    exact = os.path.join(OUTPUT_DIR, f"{prefix}{target_date_str}.{ext}")
    if os.path.exists(exact):
        return exact
    if not os.path.isdir(OUTPUT_DIR):
        return None
    files = sorted(
        f for f in os.listdir(OUTPUT_DIR)
        if f.startswith(prefix) and f.endswith(f".{ext}")
    )
    return os.path.join(OUTPUT_DIR, files[-1]) if files else None


def load_today_predictions(today_str):
    """读取今日分析 CSV，组装每场预测（摘要版）"""
    path = find_file("竞彩分析_", today_str, "csv")
    if not path:
        return [], 0
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    blocks = []
    for r in rows:
        num = r.get("编号", "")
        league = r.get("联赛", "")
        matchup = r.get("对阵", "")
        kickoff = r.get("开赛", "")
        kickoff_short = kickoff[5:16].replace("T", " ") if kickoff else ""
        conf = r.get("信心指数", "?")
        spf = r.get("胜平负推荐", "")
        rq = r.get("让球推荐", "")
        ttg = r.get("总进球推荐", "")
        hafu = r.get("半全场推荐", "")
        crs = r.get("比分推荐", "")

        text = (
            f"**{num} {league}**　{matchup}\n"
            f"⏰ {kickoff_short}　信心 {conf}/10\n"
            f"胜平负：{spf}\n"
            f"让球：{rq}\n"
            f"总进球：{ttg}\n"
            f"半全场：{hafu}\n"
            f"比分：{crs}"
        )
        blocks.append(text)
    return blocks, len(rows)


def load_odds_map(today_str):
    """读取赛程 CSV，按编号索引赔率数据"""
    path = find_file("竞彩足球_", today_str, "csv")
    if not path:
        return {}
    with open(path, encoding="utf-8-sig") as f:
        return {r.get("编号", ""): r for r in csv.DictReader(f)}


def build_detail_blocks(today_str):
    """组装每场详细预测块（赔率 + 五大玩法推荐与理由 + 综合分析 + 近况）"""
    path = find_file("竞彩分析_", today_str, "csv")
    if not path:
        return []
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    odds_map = load_odds_map(today_str)

    blocks = []
    for r in rows:
        num = r.get("编号", "")
        league = r.get("联赛", "")
        matchup = r.get("对阵", "")
        kickoff = r.get("开赛", "")
        kickoff_short = kickoff[5:16].replace("T", " ") if kickoff else ""
        conf = r.get("信心指数", "?")
        o = odds_map.get(num, {})

        gl = o.get("让球数", "")
        odds_line = (
            f"胜平负 {o.get('胜平负-主胜','?')}/{o.get('胜平负-平','?')}/{o.get('胜平负-客胜','?')}"
            f"　|　让球{gl} {o.get('让球-主胜','?')}/{o.get('让球-平','?')}/{o.get('让球-客胜','?')}"
        )

        plays = [
            ("🏆 胜平负", r.get("胜平负推荐", ""), r.get("胜平负理由", "")),
            ("⚽ 让球", r.get("让球推荐", ""), r.get("让球理由", "")),
            ("🎯 总进球", r.get("总进球推荐", ""), r.get("总进球理由", "")),
            ("🔄 半全场", r.get("半全场推荐", ""), r.get("半全场理由", "")),
            ("🔢 比分", r.get("比分推荐", ""), r.get("比分理由", "")),
        ]
        parts = [
            f"**{num} {league}**　{matchup}",
            f"⏰ {kickoff_short}　信心 **{conf}/10**",
            f"💰 {odds_line}",
        ]
        for name, rec, reason in plays:
            if rec:
                parts.append(f"{name}：**{rec}**")
            if reason:
                parts.append(f"　{reason}")

        summary = r.get("综合分析", "").strip()
        if summary:
            parts.append(f"📝 {summary}")

        # 历史数据只保留首行近况汇总（逐场战绩列表过长）
        hist_first = r.get("历史数据", "").strip().splitlines()
        if hist_first and hist_first[0]:
            parts.append(f"📊 {hist_first[0]}")

        blocks.append("\n".join(parts))
    return blocks


def load_yesterday_review(today_str):
    """读取昨日复盘报告，返回 (复盘日期, 命中率统计, 逐场复盘块列表)"""
    d = date.fromisoformat(today_str) - timedelta(days=1)
    yesterday_str = d.strftime("%Y-%m-%d")
    path = find_file("竞彩复盘_", yesterday_str, "txt")
    if not path:
        return "", "", []
    with open(path, encoding="utf-8") as f:
        text = f.read()

    # 提取复盘日期
    dm = re.search(r"复盘报告\s*-\s*(\d{4}-\d{2}-\d{2})", text)
    review_date = dm.group(1) if dm else yesterday_str

    # 提取「整体命中率统计」到「逐场复盘详情」之间的汇总
    stats = ""
    m = re.search(r"整体命中率统计(.*?)逐场复盘详情", text, re.S)
    if m:
        lines = [ln.strip() for ln in m.group(1).splitlines()
                 if ln.strip()
                 and not set(ln.strip()) <= {"-"}  # 去掉纯分隔线
                 and not ln.strip().startswith("📋")]
        stats = "\n".join(lines)

    # 解析每场复盘（以 🏟️ 切分）
    detail_blocks = []
    for chunk in re.split(r"🏟️", text)[1:]:
        block = parse_match_detail(chunk)
        if block:
            detail_blocks.append(block)

    return review_date, stats, detail_blocks


def parse_match_detail(chunk):
    """把单个 🏟️ 复盘块转成紧凑的 lark_md 文本"""
    lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
    if not lines:
        return ""

    # 第1行：编号 + 联赛
    head = lines[0]
    # 第2行：对阵 (置信 x/10)
    matchup = lines[1] if len(lines) > 1 else ""

    score_line = ""
    handicap_line = ""
    result_lines = []
    attribution = ""

    for ln in lines[2:]:
        if "半场" in ln and "全场" in ln:
            score_line = ln
        elif "让球数" in ln:
            hm = re.search(r"让球数：\s*(\S+)", ln)
            handicap_line = f"让球 {hm.group(1)}" if hm else ""
        elif re.match(r"[✅❌]", ln):
            # 五种玩法命中行，去掉书名号压缩长度
            compact = ln.replace("「", "").replace("」", "")
            result_lines.append(compact)
        elif "AI 归因" in ln:
            attribution = re.sub(r"^.*?AI 归因[：:]\s*", "💡 ", ln)

    parts = [f"**🏟️ {head}**", matchup]
    meta = "　".join(p for p in (score_line, handicap_line) if p)
    if meta:
        parts.append(meta)
    parts.extend(result_lines)
    if attribution:
        parts.append(attribution)
    return "\n".join(parts)


# 日报卡元素数保护：飞书对单卡元素数无公开硬上限，实测 108 个元素可正常推送（2026-10-18），
# 上限设 120 留约 10% 余量；超出时复盘明细整体折叠为一条 note（全显示或全不显示，避免截断成半截）。
# 字节层面的 28000 保护线在 main() 中另行兜底。
MAX_CARD_ELEMENTS = 120


def build_card(today_str, pred_blocks, match_count, review_date, review_stats, review_details):
    md = today_str[5:]
    title = f"⚽ 竞彩 AI 日报 | {md} | {match_count}场"
    if review_stats or review_details:
        title += " + 昨日复盘"

    elements = [
        {"tag": "div", "text": {"tag": "lark_md",
         "content": f"**📅 {today_str}（{match_count} 场开售）**"}},
        {"tag": "hr"},
    ]

    # 预测块为日报主体，始终全部保留
    for block in pred_blocks:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": block}})
        elements.append({"tag": "hr"})

    if review_stats or review_details:
        if review_stats:
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                             "content": f"**📊 昨日复盘命中率（{review_date}）**\n{review_stats}"}})
            elements.append({"tag": "hr"})
        # 复盘明细整体判断能否放下（每场占 div+hr 两个元素），放不下则全部折叠
        if review_details and len(elements) + 2 * len(review_details) > MAX_CARD_ELEMENTS:
            print(f"⚠️ 日报卡元素数将达 {len(elements) + 2 * len(review_details)}，"
                  f"超过保护上限 {MAX_CARD_ELEMENTS}，复盘明细已折叠（共 {len(review_details)} 场）")
            elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                             "content": f"逐场复盘共 {len(review_details)} 场，卡片容量所限已省略，"
                                        f"详见本地文件 output/竞彩复盘_{review_date}.txt"}]})
        else:
            for detail in review_details:
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": detail}})
                elements.append({"tag": "hr"})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": "📊 昨日复盘：暂无（可能比赛尚未开奖）"}})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "green",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements,
        },
    }


def send(webhook, payload, label="消息"):
    try:
        resp = requests.post(webhook, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"❌ {label}请求失败：{e}")
        return False
    except ValueError:
        print(f"❌ {label}返回非 JSON：{resp.text[:200]}")
        return False

    code = data.get("code", data.get("StatusCode"))
    if code == 0:
        print(f"✅ {label}推送成功")
        return True
    print(f"❌ {label}返回错误：{json.dumps(data, ensure_ascii=False)[:300]}")
    return False


def build_detail_cards(today_str, detail_blocks, max_bytes=24000):
    """把每场详细预测块按字节预算拆成多张交互卡片"""
    cards = []
    buf = []
    size = 1000  # 卡片框架开销预留

    def flush():
        nonlocal buf, size
        if buf:
            cards.append(list(buf))
            buf = []
            size = 1000

    for block in detail_blocks:
        block_size = len(block.encode("utf-8")) + 200  # hr 等开销
        if size + block_size > max_bytes and buf:
            flush()
        buf.append(block)
        size += block_size
    flush()

    total = len(cards)
    md = today_str[5:]
    payloads = []
    for i, group in enumerate(cards, 1):
        title = f"📖 今日完整预测 | {md} ({i}/{total})" if total > 1 else f"📖 今日完整预测 | {md}"
        elements = []
        for block in group:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": block}})
            elements.append({"tag": "hr"})
        payloads.append({
            "msg_type": "interactive",
            "card": {
                "header": {
                    "template": "blue",
                    "title": {"tag": "plain_text", "content": title},
                },
                "elements": elements,
            },
        })
    return payloads


# ---------- 深度归因独立群推送（可选增强） ----------

def load_attribution(date_str):
    """读取昨日深度归因 JSON（由 enrich_attribution.py 生成），无则返回 None"""
    path = os.path.join(OUTPUT_DIR, f"深度归因_{date_str}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_attribution_cards(attr_date, attr, review_stats):
    """深度归因卡片：1 张汇总卡（统计+错题清单+免责）+ 明细按字节预算拆成多张卡"""
    md = attr_date[5:]
    entries = attr.get("场次", [])

    # 汇总卡：错题清单逐行放同一个 div，元素数恒定不受场数影响
    lines = []
    for m in entries:
        plays = "、".join(m.get("错误玩法", [])) or "?"
        lines.append(f"**{m.get('编号', '')}** {m.get('对阵', '')}（{m.get('实际比分', '')}）错误：{plays}")
    # 场次极多时截断清单，防止单 div 超字节上限
    MAX_SUMMARY_LINES = 40
    if len(lines) > MAX_SUMMARY_LINES:
        lines = lines[:MAX_SUMMARY_LINES] + [f"…其余 {len(entries) - MAX_SUMMARY_LINES} 场见明细卡"]

    summary_elements = [
        {"tag": "div", "text": {"tag": "lark_md",
         "content": f"**🧠 深度归因（联网搜索）| {attr_date} | 错题 {len(entries)} 场**"}},
        {"tag": "hr"},
    ]
    if review_stats:
        summary_elements.append({"tag": "div", "text": {"tag": "lark_md",
                                 "content": f"**📊 命中率统计**\n{review_stats}"}})
        summary_elements.append({"tag": "hr"})
    summary_elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    summary_elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                             "content": "内容由联网搜索的 LLM 生成，事实依据需人工抽查"
                                        "（原文见 output/深度归因_" + attr_date + ".json），不构成投注建议"}]})
    payloads = [{
        "msg_type": "interactive",
        "card": {
            "header": {"template": "green",
                       "title": {"tag": "plain_text", "content": f"🧠 深度归因汇总 | {md} | {len(entries)}场错题"}},
            "elements": summary_elements,
        },
    }]

    # 明细块：原归因 vs 深度归因对照（战报材料原文太长，仅留 JSON 文件备查）
    blocks = []
    for m in entries:
        parts = [
            f"**{m.get('编号', '')} {m.get('联赛', '')}**　{m.get('对阵', '')}　实际 {m.get('实际比分', '')}",
            f"错误玩法：{'、'.join(m.get('错误玩法', []))}",
            f"【原归因】{m.get('原错误归因') or '（无）'}",
            "【深度归因·联网】",
            m.get("深度归因", ""),
        ]
        blocks.append("\n".join(p for p in parts if p))

    # 按字节预算拆卡（同 build_detail_cards 逻辑）
    cards, buf, size = [], [], 1000
    for block in blocks:
        block_size = len(block.encode("utf-8")) + 200
        if size + block_size > 24000 and buf:
            cards.append(list(buf))
            buf, size = [], 1000
        buf.append(block)
        size += block_size
    if buf:
        cards.append(buf)

    total = len(cards)
    for i, group in enumerate(cards, 1):
        elements = []
        for block in group:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": block}})
            elements.append({"tag": "hr"})
        title = f"🧠 深度归因明细 | {md} ({i}/{total})" if total > 1 else f"🧠 深度归因明细 | {md}"
        payloads.append({
            "msg_type": "interactive",
            "card": {
                "header": {"template": "blue",
                           "title": {"tag": "plain_text", "content": title}},
                "elements": elements,
            },
        })
    return payloads


def push_attribution_group(review_stats):
    """推送昨日深度归因到独立群。可选增强：任何失败只打印提示，绝不抛错影响主流程。"""
    yesterday_str = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    webhook = os.environ.get("FEISHU_ATTR_WEBHOOK", "").strip()
    if not webhook:
        print("ℹ️ 未设置 FEISHU_ATTR_WEBHOOK，深度归因不推送独立群（配置后自动启用）")
        return
    attr = load_attribution(yesterday_str)
    if not attr:
        print(f"ℹ️ 无 output/深度归因_{yesterday_str}.json，跳过独立群推送")
        return
    if not attr.get("场次"):
        print(f"ℹ️ 深度归因 {yesterday_str} 无错题场次，跳过独立群推送")
        return
    marker = os.path.join(OUTPUT_DIR, f"深度归因_{yesterday_str}.pushed")
    if os.path.exists(marker):
        print(f"ℹ️ 深度归因 {yesterday_str} 已推送过独立群（标记文件存在），跳过")
        return

    payloads = build_attribution_cards(yesterday_str, attr, review_stats)
    print(f"准备推送深度归因到独立群：{len(payloads)} 张卡片（Webhook: ...{webhook[-12:]}）")
    for i, dp in enumerate(payloads, 1):
        if not send(webhook, dp, label=f"深度归因 {i}/{len(payloads)}"):
            print("⚠️ 深度归因推送中断（可选增强步骤，不影响主流程）；未写已推送标记，重跑会重发剩余卡片")
            return
    with open(marker, "w", encoding="utf-8") as f:
        f.write(f"pushed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    print(f"✅ 深度归因独立群推送完成（{len(payloads)} 张卡），已写标记 {marker}")


def main():
    # 主群 webhook 只从环境变量读取，不再内置地址；缺失即报错退出
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        print("❌ 错误：环境变量 FEISHU_WEBHOOK 未设置，无法推送飞书群")
        print("")
        print("设置方法（二选一）：")
        print("  1. 临时设置：")
        print("     export FEISHU_WEBHOOK=你的机器人Webhook地址")
        print("     python3 push_feishu.py")
        print("")
        print("  2. 写入 ~/.zshrc 永久生效：")
        print("     echo 'export FEISHU_WEBHOOK=你的机器人Webhook地址' >> ~/.zshrc")
        print("     source ~/.zshrc")
        sys.exit(1)

    today_str = date.today().strftime("%Y-%m-%d")

    pred_blocks, match_count = load_today_predictions(today_str)
    review_date, review_stats, review_details = load_yesterday_review(today_str)

    if not pred_blocks and not review_stats and not review_details:
        print("❌ output/ 里找不到今日分析和昨日复盘数据，无法推送")
        return

    payload = build_card(today_str, pred_blocks, match_count,
                         review_date, review_stats, review_details)

    # 飞书卡片内容上限保护（约 28KB）
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw.encode("utf-8")) > 28000:
        payload = build_card(today_str, pred_blocks, match_count,
                             review_date, review_stats, [])
        payload["card"]["elements"].append(
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": f"逐场复盘共{len(review_details)}场，内容过长已省略，详见本地报告文件"}]})
        raw = json.dumps(payload, ensure_ascii=False)

    print(f"准备推送飞书：今日 {match_count} 场预测"
          + (f" + {review_date} 复盘 {len(review_details)} 场" if review_details else ""))
    print(f"卡片大小：{len(raw.encode('utf-8'))} 字节")
    print(f"Webhook: ...{webhook[-12:]}")  # 只打印尾部，不泄露完整地址

    if not send(webhook, payload, label="日报卡片"):
        return

    # 详细预测：每场含赔率/推荐理由/综合分析，按卡片上限自动拆成多张
    detail_blocks = build_detail_blocks(today_str)
    if detail_blocks:
        detail_payloads = build_detail_cards(today_str, detail_blocks)
        total = len(detail_payloads)
        for i, dp in enumerate(detail_payloads, 1):
            size_kb = len(json.dumps(dp, ensure_ascii=False).encode("utf-8")) // 1024
            print(f"详细预测卡片 {i}/{total}（约 {size_kb}KB）")
            if not send(webhook, dp, label=f"详细预测 {i}/{total}"):
                break

    # 深度归因独立群推送（可选增强，任何异常不影响以上主流程）
    try:
        push_attribution_group(review_stats)
    except Exception as e:
        print(f"⚠️ 深度归因独立群推送异常（不影响主流程）：{e}")


if __name__ == "__main__":
    main()
