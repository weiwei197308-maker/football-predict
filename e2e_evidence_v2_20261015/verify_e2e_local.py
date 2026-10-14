# -*- coding: utf-8 -*-
"""E2E 证据提取：证明五桶概率来自真实 LLM 响应，并复算 v2 推导链路。只读。"""
import csv
import json
import re
import sys

sys.path.insert(0, "/Users/mac/Desktop/zucai")
from lottery_rules import (  # noqa: E402
    validate_net_goal_probabilities, map_net_goal_probs_to_handicap,
    select_handicap_picks, format_handicap_pick,
)

BASE = "/Users/mac/Desktop/zucai/e2e_evidence_v2_20261015"
snap = json.load(open(f"{BASE}/原始预测快照_2026-10-14.json", encoding="utf-8"))
print(f"快照场次：{len(snap['场次'])}\n")

for e in snap["场次"]:
    mid = e["编号"]
    hc_line = str(e.get("体彩让球数", "")).strip()
    locked = e.get("锁定核心剧本") or {}
    fixed = e.get("修正后预测") or {}
    print("=" * 78)
    print(f"{mid} {e.get('对阵','')}  体彩让球数={hc_line}")
    print(f"  首选比分={locked.get('primary_score')}  区间={locked.get('net_goal_interval')}")

    # --- 1. 原始 LLM 响应中是否真的有五桶概率（从审计 JSON 的 raw_attempts 提取） ---
    audit_raw = (e.get("一致性检查") or "")
    m = re.search(r"【核心剧本审计详情】\s*(\{.*\})", audit_raw, re.DOTALL)
    raw_texts = []
    if m:
        try:
            audit = json.loads(m.group(1))
            raw_texts = audit.get("raw_attempts", []) or []
        except json.JSONDecodeError:
            pass
    hits = [t for t in raw_texts if "net_goal_probabilities" in t]
    print(f"  [原始响应] raw_attempts={len(raw_texts)} 次，含 net_goal_probabilities 的次数={len(hits)}")
    for t in hits[:1]:
        mm = re.search(r"net_goal_probabilities\"\s*:\s*(\{[^}]*\})", t)
        print(f"    原文片段: {mm.group(0)[:200] if mm else '(未捕获到结构)'}")

    # --- 2. 锁定剧本中的概率 → 程序校验 ---
    probs = locked.get("net_goal_probabilities")
    print(f"  [锁定剧本] net_goal_probabilities = {probs}")
    if probs is None:
        print("  [校验] 输入为 None → 程序按设计降级 v1")
        print(f"  [结果] 版本={fixed.get('让球推导版本')}  概率来源={fixed.get('概率来源','')}")
        print(f"  [结果] 让球推荐={fixed.get('让球推荐')}  次选={fixed.get('让球次选','')}")
        continue
    v = validate_net_goal_probabilities(probs)
    print(f"  [校验] ok={v['ok']}  归一化={v.get('probabilities')}  警告={v.get('warnings') or '无'}")
    if not v["ok"]:
        print(f"  [校验失败原因] {v['reason']}")
        print(f"  [结果] 版本={fixed.get('让球推导版本')}  概率来源={fixed.get('概率来源','')}")
        continue

    # --- 3. 复算映射与选号，与修正后预测比对 ---
    from lottery_rules import parse_handicap
    hc = parse_handicap(hc_line)
    mapping = map_net_goal_probs_to_handicap(v["probabilities"], hc)
    dec = select_handicap_picks(mapping, None)
    print(f"  [映射] 让球数 h={hc}  exact={mapping['exact']}  "
          f"胜={mapping['probabilities'].get('主胜'):.4f} 平={mapping['probabilities'].get('平'):.4f} "
          f"负={mapping['probabilities'].get('客胜'):.4f}  合计={sum(mapping['probabilities'].values()):.4f}")
    if not mapping["exact"]:
        print(f"  [映射] 区间 bounds={mapping['bounds']}  straddles={mapping['straddles']}")
    print(f"  [复算] 主推={format_handicap_pick(hc, dec['primary'])}  "
          f"次选={format_handicap_pick(hc, dec['secondary']) if dec['double'] else '-'}")
    print(f"  [程序实际] 版本={fixed.get('让球推导版本')}  让球推荐={fixed.get('让球推荐')}  "
          f"次选={fixed.get('让球次选','')}  概率来源={fixed.get('概率来源','')}")
    payload = fixed.get("让球结果概率") or {}
    print(f"  [程序实际] 让球结果概率={payload.get('probabilities')}  "
          f"double={payload.get('double')}  market_conflict={payload.get('market_conflict')}")
    reason = str(fixed.get("让球理由", ""))
    print(f"  [让球理由前180字] {reason[:180]}")
    unc = fixed.get("数据缺失说明", "")
    if unc:
        print(f"  [不确定性说明前160字] {str(unc)[:160]}")

# --- 4. CSV 落盘检查 ---
print("\n" + "=" * 78)
with open(f"{BASE}/竞彩分析_2026-10-14.csv", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    fields = reader.fieldnames
    rows = list(reader)
print(f"CSV 列数={len(fields)}  尾部5列={fields[-5:]}")
for r in rows:
    print(f"  {r['编号']} 版本={r['让球推导版本']} 推荐={r['让球推荐']} 次选={r['让球次选'] or '-'} "
          f"净胜球分布={r['净胜球概率分布'][:60]}")
    print(f"      让球结果概率={r['让球结果概率'][:80]}")
    print(f"      概率来源={r['概率来源'][:80]}")
