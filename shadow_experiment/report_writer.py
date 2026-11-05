"""影子实验统计模块 — 报告输出层。

6 类输出：
  O1 当日新增比赛明细 CSV
  O2 当日结算情况 JSON
  O3 累计统计 CSV + MD
  O4 A→B 复核价值 CSV
  O5 数据质量 JSON
  O6 执行日志 JSON（由 run_manifest 写）
"""
import csv
import json
import os
from datetime import datetime

from . import config
from .utils import save_json, write_csv, sha256_bytes


def write_daily_detail_csv(result, date_str):
    """O1: 逐场明细 CSV。"""
    ab = result["ab_pair"]
    detail_rows = []
    for d in ab["per_runkey_details"]:
        detail_rows.append({
            "RunKey": d["runkey"],
            "编号": d["编号"],
            "实际总进球": d["实际"],
            "AB分类": d.get("AB分类", ""),
            "AB分类名": d.get("AB分类名", ""),
            "首选区间距离改善": d.get("首选区间距离改善", ""),
            "首选精确误差改善": d.get("首选精确误差改善", ""),
            "集合距离改善": d.get("集合最短区间距离改善", ""),
            "方向判定": d.get("方向判定", ""),
            "A覆盖": d.get("A覆盖", ""),
            "B覆盖": d.get("B覆盖", ""),
            "覆盖变化": d.get("覆盖变化", ""),
        })

    path = f"{config.REPORTS_DIR}/daily_detail_{date_str}.csv"
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    if detail_rows:
        header = list(detail_rows[0].keys())
        write_csv(path, detail_rows, header=header)
    else:
        write_csv(path, [], header=["RunKey", "编号"])
    return path


def write_daily_settlement_json(result, date_str):
    """O2: 当日结算情况 JSON。"""
    settled = result["settled_records"]
    pending = result["pending_records"]

    # 从 per_runkey_details 拿到已结算清单
    settled_list = []
    for d in result["ab_pair"]["per_runkey_details"]:
        settled_list.append({
            "编号": d["编号"],
            "实际": d["实际"],
            "AB分类": d.get("AB分类", ""),
        })

    obj = {
        "执行日期": date_str,
        "已结算场次数": settled,
        "未结算场次数": pending,
        "RunKey 冲突数": len(result["runkey_conflicts"]),
        "MatchKey 冲突数": len(result["matchkey_conflicts"]),
        "已结算清单": settled_list,
        "冲突详情": {
            "RunKey": result["runkey_conflicts"],
            "MatchKey": result["matchkey_conflicts"],
        },
    }
    path = f"{config.REPORTS_DIR}/daily_settlement_{date_str}.json"
    save_json(path, obj)
    return path


def write_shadow_summary_csv(result, date_str):
    """O3a: 四组指标 + 极端桶汇总 CSV。"""
    rows = []

    # 四组指标
    for g in ("A盲判", "B复核", "C正式", "D固定", "市场前三"):
        m = result["group_metrics"].get(g, {})
        rows.append({"类别": "四组指标", "子项": g, **m})

    # 极端桶
    for bid, b in result["extreme_buckets"].items():
        row = {"类别": "极端桶", "子项": bid, "桶名": b.get("桶名", ""),
               "场次数": b.get("场次数", 0)}
        for g in ("A盲判", "B复核", "C正式", "D固定", "市场前三"):
            row[f"{g}_覆盖率"] = b.get(f"{g}_覆盖率", "")
        rows.append(row)

    # D × 市场四格表
    dm = result["d_market_table"]
    rows.append({"类别": "D×市场四格表", "子项": "", **dm})

    path = f"{config.REPORTS_DIR}/shadow_summary.csv"
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    if rows:
        # header 按所有 row 的 key 并集取，避免不同类别 row 字段被裁掉
        header = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    header.append(k)
        write_csv(path, rows, header=header)
    return path


def write_shadow_report_md(result, date_str):
    """O3b: 阶段分析报告 MD。"""
    lines = []
    lines.append(f"# 影子实验总进球统计报告 — {date_str}")
    lines.append("")

    # 阶段 banner
    phase_num = result["phase_num"]
    phase_desc = result["phase_desc"]
    lines.append(f"> **第 {phase_num} 阶段观察**：{phase_desc}")
    lines.append("")
    lines.append(f"累计已结算场次：**{result['settled_records']}**  （阶段 1/2/3 阈值：30/100/300）")
    lines.append("")

    # 质量概览
    qr = result["quality_report"]
    lines.append("## 数据质量概览")
    lines.append(f"- 影子文件加载：{qr['files_loaded']} 个成功，{len(qr['files_errors'])} 个错误")
    lines.append(f"- 总记录数：{qr['total_records']}（已结算 {qr['settled_records']}，未结算 {qr['pending_records']}）")
    lines.append(f"- RunKey 冲突：{qr['runkey_conflict_count']}  MatchKey 冲突：{qr['matchkey_conflict_count']}")
    lines.append(f"- 分母不变式全部成立：{qr['invariant_pass']}")
    lines.append("")

    # 四组指标
    lines.append("## 四组指标汇总")
    lines.append("")
    lines.append("| 组 | N_settled | N_valid | 首选命中率 | 原始覆盖率 | 等宽3覆盖率 | 首选区间距离↓ | 首选精确误差↓ | 候选距离↓ | 候选数 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for g in ("A盲判", "B复核", "C正式", "D固定", "市场前三"):
        m = result["group_metrics"].get(g, {})
        lines.append(
            f"| {g} | {m.get('已结算场次数 N_settled', '')} | {m.get('有效预测场次 N_valid', '')} "
            f"| {m.get('首选命中率_率', '—')}% | {m.get('原始候选集合覆盖率_率', '—')}% "
            f"| {m.get('等宽三档覆盖率_率', '—')}% | {m.get('首选区间距离均值', '—')} "
            f"| {m.get('首选精确误差均值', '—')} | {m.get('候选集合最短区间距离均值', '—')} "
            f"| {m.get('候选平均数量', '—')} |"
        )
    lines.append("")

    # C2 独立判断对照（仅总进球，且仅双方均有效）
    c2c = result.get("c2_comparison") or {}
    if c2c.get("配对数"):
        def _fmt_dist(d):
            return "  ".join(f"{k} {v}%" for k, v in (d or {}).items())

        lines.append("## C2 独立判断对照（C正式 vs C2独立）")
        lines.append("")
        lines.append(
            f"仅统计 C2 已启用且双方均有效的已结算场次：**n={c2c['配对数']}**"
            f"（C2 启用并已结算 {c2c['启用场次数']} 场）")
        lines.append("")
        lines.append("| 指标 | C正式 | C2独立 |")
        lines.append("|---|---|---|")
        lines.append(f"| 首选命中率 | {c2c['C首选命中率']}% | {c2c['C2首选命中率']}% |")
        lines.append(f"| 候选覆盖率 | {c2c['C覆盖率']}% | {c2c['C2覆盖率']}% |")
        lines.append(f"| 候选数均值 | {c2c['C候选数均值']} | {c2c['C2候选数均值']} |")
        lines.append("")
        lines.append(f"- C 首选球数分布：{_fmt_dist(c2c['C首选分布'])}")
        lines.append(f"- C2 首选球数分布：{_fmt_dist(c2c['C2首选分布'])}")
        lines.append(f"- C 首选未进入 C2 候选的场次：{c2c['C首选未进入C2候选的场次']}")
        lines.append("")
    elif c2c:
        lines.append("## C2 独立判断对照（C正式 vs C2独立）")
        lines.append("")
        lines.append(f"- {c2c.get('说明', '暂无可用配对')}（已启用并结算 {c2c.get('启用场次数', 0)} 场）")
        lines.append("")

    # A→B 配对
    ab = result["ab_pair"]
    lines.append("## A→B 复核价值分析")
    lines.append("")
    p1 = ab["p1_stats"]
    p2 = ab["p2_stats"]
    p3 = ab["p3_stats"]

    lines.append(f"### 首选误差配对（P1, n={p1.get('配对数', 0)}）")
    lines.append(f"- 首选区间距离改善均值：{p1.get('首选区间距离改善均值', '—')}（+ 表示 B 更好）")
    lines.append(f"- 首选精确误差改善均值：{p1.get('首选精确误差改善均值', '—')}（仅双侧闭区间）")
    lines.append(f"- 方向理想≠0 的场次：{p1.get('方向理想≠0的场次', 0)}")
    lines.append(f"  - 方向正确移动：{p1.get('方向正确移动', 0)}")
    lines.append(f"  - 方向错误移动：{p1.get('方向错误移动', 0)}")
    lines.append(f"  - 方向不可判定（A首选已覆盖实际）：{p1.get('方向不可判定(A已覆盖)', 0)}")
    lines.append("")

    lines.append(f"### 集合距离配对（P2, n={p2.get('配对数', 0)}）")
    lines.append(f"- 集合最短区间距离改善均值：{p2.get('集合最短区间距离改善均值', '—')}")
    lines.append("")

    lines.append(f"### 覆盖变化配对（P3, n={p3.get('配对数', 0)}）")
    lines.append(f"- A 未覆盖场次：{p3.get('A未覆盖场次数', 0)}")
    lines.append(f"  - B 补全覆盖：{p3.get('B补全覆盖(A未→B已)', 0)} → 补全率 {p3.get('补全率', '—')}%")
    lines.append(f"- A 覆盖场次：{p3.get('A覆盖场次数', 0)}")
    lines.append(f"  - B 丧失覆盖：{p3.get('B丧失覆盖(A已→B未)', 0)} → 丧失率 {p3.get('丧失率', '—')}%")
    lines.append("")

    # 分类
    from collections import Counter
    cls_counts = Counter(cls_id for _, cls_id, _ in ab["classifications"])
    cls_total = sum(cls_counts.values()) or 1
    lines.append("### A→B 分类分布")
    lines.append("| 分类 | 场次 | 占比 | 含义 |")
    lines.append("|---|---|---|---|")
    for cid, cnt in sorted(cls_counts.items()):
        cname = next((name for _, cid2, name in ab["classifications"] if cid2 == cid), "")
        lines.append(f"| {cid} | {cnt} | {round(100.0*cnt/cls_total, 1)}% | {cname} |")
    lines.append("")

    # D × 市场
    dm = result["d_market_table"]
    lines.append("## D 组 vs 市场前三 四格表")
    lines.append(f"- a (D和市场都覆盖) = {dm['a_D和市场都覆盖']}")
    lines.append(f"- b (D覆盖, 市场未覆盖) = {dm['b_D覆盖市场未覆盖']}")
    lines.append(f"- c (D未覆盖, 市场覆盖) = {dm['c_D未覆盖市场覆盖']}")
    lines.append(f"- d (D和市场都未覆盖) = {dm['d_D和市场都未覆盖']}")
    lines.append(f"- D 覆盖率 = {dm['D覆盖率']}%  市场覆盖率 = {dm['市场覆盖率']}%")
    lines.append(f"- D 相对市场覆盖率差 = {dm['D相对市场覆盖率差']}% = (b−c)/N")
    lines.append(f"- ⚠️ {dm['注意']}")
    lines.append("")

    path = f"{config.REPORTS_DIR}/shadow_report_{date_str}.md"
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def write_ab_pair_csv(result, date_str):
    """O4: A→B 逐场配对 CSV。"""
    ab = result["ab_pair"]
    rows = []
    for d in ab["per_runkey_details"]:
        rows.append(d)
    path = f"{config.REPORTS_DIR}/ab_pair_{date_str}.csv"
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    if rows:
        header = list(rows[0].keys())
        write_csv(path, rows, header=header)
    return path


def write_quality_report_json(result, date_str):
    """O5: 数据质量报告 JSON。"""
    path = f"{config.REPORTS_DIR}/quality_report_{date_str}.json"
    save_json(path, result["quality_report"])
    return path


def write_all_reports(result, date_str):
    """一次性生成全部 O1-O5 输出，返回各文件路径。"""
    outputs = {
        "O1_daily_detail": write_daily_detail_csv(result, date_str),
        "O2_daily_settlement": write_daily_settlement_json(result, date_str),
        "O3a_summary_csv": write_shadow_summary_csv(result, date_str),
        "O3b_report_md": write_shadow_report_md(result, date_str),
        "O4_ab_pair": write_ab_pair_csv(result, date_str),
        "O5_quality": write_quality_report_json(result, date_str),
    }
    return outputs


# ========== 多玩法报告（让球/半全场扩展；既有总进球报告函数不变） ==========

def _play_groups(result):
    """按玩法配置返回 (组..., 市场键) 展示顺序。"""
    pcfg = config.PLAY_CONFIG[result["玩法"]]
    return list(pcfg["组"]) + [pcfg["市场键"]]


def write_play_daily_detail_csv(result, date_str):
    """玩法 O1: 逐场明细 CSV。"""
    suffix = config.PLAY_CONFIG[result["玩法"]]["报告后缀"]
    ab = result["ab_pair"]
    rows = []
    for d in ab["per_runkey_details"]:
        rows.append({
            "RunKey": d["runkey"], "编号": d["编号"], "实际": d["实际"],
            "AB分类": d.get("AB分类", ""), "AB分类名": d.get("AB分类名", ""),
            "A首选命中": d.get("A首选命中", ""), "B首选命中": d.get("B首选命中", ""),
            "A覆盖": d.get("A覆盖", ""), "B覆盖": d.get("B覆盖", ""),
            "覆盖变化": d.get("覆盖变化", ""),
        })
    path = f"{config.REPORTS_DIR}/daily_detail{suffix}_{date_str}.csv"
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    if rows:
        write_csv(path, rows, header=list(rows[0].keys()))
    else:
        write_csv(path, [], header=["RunKey", "编号"])
    return path


def write_play_settlement_json(result, date_str):
    """玩法 O2: 当日结算情况 JSON。"""
    suffix = config.PLAY_CONFIG[result["玩法"]]["报告后缀"]
    obj = {
        "玩法": result["玩法"],
        "执行日期": date_str,
        "已结算场次数": result["settled_records"],
        "未结算场次数": result["pending_records"],
        "RunKey 冲突数": len(result["runkey_conflicts"]),
        "MatchKey 冲突数": len(result["matchkey_conflicts"]),
        "已结算清单": [{"编号": d["编号"], "实际": d["实际"],
                        "AB分类": d.get("AB分类", "")}
                       for d in result["ab_pair"]["per_runkey_details"]],
        "冲突详情": {"RunKey": result["runkey_conflicts"],
                     "MatchKey": result["matchkey_conflicts"]},
    }
    path = f"{config.REPORTS_DIR}/daily_settlement{suffix}_{date_str}.json"
    save_json(path, obj)
    return path


def write_play_summary_csv(result, date_str):
    """玩法 O3a: 各组指标汇总 CSV（每次执行整体覆写，口径与总进球一致）。"""
    suffix = config.PLAY_CONFIG[result["玩法"]]["报告后缀"]
    rows = []
    for g in _play_groups(result):
        m = result["group_metrics"].get(g, {})
        flat = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v)
                for k, v in m.items()}
        rows.append({"类别": "各组指标", "子项": g, **flat})
    ab = result["ab_pair"]
    rows.append({"类别": "A→B复核", "子项": "汇总",
                 **{k: (json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v)
                    for k, v in ab.items()
                    if k not in ("classifications", "per_runkey_details")}})
    path = f"{config.REPORTS_DIR}/shadow_summary{suffix}.csv"
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    header, seen = [], set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                header.append(k)
    write_csv(path, rows, header=header)
    return path


def write_play_report_md(result, date_str):
    """玩法 O3b: 阶段分析报告 MD。"""
    play = result["玩法"]
    suffix = config.PLAY_CONFIG[play]["报告后缀"]
    labels = config.PLAY_CONFIG[play]["标签表"]
    lines = []
    lines.append(f"# 影子实验{play}统计报告 — {date_str}")
    lines.append("")
    lines.append(f"> **第 {result['phase_num']} 阶段观察**：{result['phase_desc']}")
    lines.append("")
    lines.append(f"累计已结算场次：**{result['settled_records']}**  （阶段 1/2/3 阈值：30/100/300）")
    lines.append("")

    qr = result["quality_report"]
    lines.append("## 数据质量概览")
    lines.append(f"- 影子文件加载：{qr['files_loaded']} 个成功，{len(qr['files_errors'])} 个错误")
    lines.append(f"- 总记录数：{qr['total_records']}（已结算 {qr['settled_records']}，未结算 {qr['pending_records']}）")
    lines.append(f"- RunKey 冲突：{qr['runkey_conflict_count']}  MatchKey 冲突：{qr['matchkey_conflict_count']}")
    lines.append(f"- 分母不变式全部成立：{qr['invariant_pass']}")
    lines.append("")

    lines.append("## 各组指标汇总")
    lines.append("")
    lines.append("| 组 | N_settled | N_valid | N_order | 首选命中率 | 原始覆盖率 | 等宽3覆盖率 | 候选数 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for g in _play_groups(result):
        m = result["group_metrics"].get(g, {})
        lines.append(
            f"| {g} | {m.get('已结算场次数 N_settled', '')} | {m.get('有效预测场次 N_valid', '')} "
            f"| {m.get('有序预测场次 N_order', '')} | {m.get('首选命中率_率', '—')}% "
            f"| {m.get('原始候选集合覆盖率_率', '—')}% | {m.get('等宽三档覆盖率_率', '—')}% "
            f"| {m.get('候选平均数量', '—')} |"
        )
    lines.append("")

    lines.append("## 首选标签分布（有序有效场次）")
    lines.append("")
    lines.append("| 组 | " + " | ".join(labels) + " |")
    lines.append("|---|" + "---|" * len(labels))
    for g in _play_groups(result):
        m = result["group_metrics"].get(g, {})
        dist = m.get("首选标签分布", {})
        lines.append(f"| {g} | " + " | ".join(str(dist.get(lab, 0)) for lab in labels) + " |")
    lines.append("")

    ab = result["ab_pair"]
    quad = ab.get("首选命中四格", {})
    lines.append(f"## A→B 复核价值（配对数 n={ab.get('配对数', 0)}）")
    lines.append("")
    lines.append(f"- 首选命中四格：A对B对={quad.get('A对B对', 0)}，A对B错={quad.get('A对B错', 0)}，"
                 f"A错B对={quad.get('A错B对', 0)}，A错B错={quad.get('A错B错', 0)}")
    lines.append(f"- 复核首选改善率：{ab.get('复核首选改善率', '—')}%（= (A错B对 − A对B错) / 配对数）")
    lines.append(f"- 覆盖补全（A未→B已）：{ab.get('覆盖补全(A未→B已)', 0)}；"
                 f"覆盖丧失（A已→B未）：{ab.get('覆盖丧失(A已→B未)', 0)}；"
                 f"覆盖不变：{ab.get('覆盖不变', 0)}")
    lines.append("")

    from collections import Counter
    cls_counts = Counter(cls_id for _, cls_id, _ in ab["classifications"])
    cls_total = sum(cls_counts.values()) or 1
    lines.append("### A→B 分类分布")
    lines.append("| 分类 | 场次 | 占比 | 含义 |")
    lines.append("|---|---|---|---|")
    for cid, cnt in sorted(cls_counts.items()):
        cname = next((name for _, cid2, name in ab["classifications"] if cid2 == cid), "")
        lines.append(f"| {cid} | {cnt} | {round(100.0*cnt/cls_total, 1)}% | {cname} |")
    lines.append("")

    path = f"{config.REPORTS_DIR}/shadow_report{suffix}_{date_str}.md"
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def write_play_quality_json(result, date_str):
    """玩法 O5: 数据质量报告 JSON。"""
    suffix = config.PLAY_CONFIG[result["玩法"]]["报告后缀"]
    path = f"{config.REPORTS_DIR}/quality_report{suffix}_{date_str}.json"
    save_json(path, result["quality_report"])
    return path


def write_play_reports(result, date_str):
    """一次性生成指定玩法的全部报告输出，返回各文件路径。"""
    return {
        "O1_daily_detail": write_play_daily_detail_csv(result, date_str),
        "O2_daily_settlement": write_play_settlement_json(result, date_str),
        "O3a_summary_csv": write_play_summary_csv(result, date_str),
        "O3b_report_md": write_play_report_md(result, date_str),
        "O5_quality": write_play_quality_json(result, date_str),
    }
