"""模拟盘结算与记录状态的统一实现（纯函数，无网络/文件依赖）。

背景（30 天数据审计确认的问题）：
1. 旧 tracker 固定按 100/3 分摊投注，不论实际推荐几个选项 —— 选项数为 2 时
   少投 33.3 元却仍按 -100 计亏，命中时返奖也少算。
2. 空预测（API 无输出/解析失败/保存缺失）被当作"预测未中"记 -100 并进入错题归因。
3. 选项数与赔率数不一致时被静默截断（如 10-10 周六003 的 HP 非法编码）。

业务口径（取自 summarize_30days.py 文档字符串与 simulate 表既有约定）：
- 模拟投注额 100 元 = 每场该玩法总预算，均分到实际解析出的合法选项数。
- 赔率为体彩固定奖金（含本金）：命中净盈亏 = 命中选项赔率 × 单选项投注 - 总投入；
  全部未中净盈亏 = -总投入。
- 无预测输出：不进命中率分母、不在模型盈亏账记 -100；资金账若计执行损失须单列。

状态枚举（写入"记录状态"列，命中/未中/特殊状态可对账）：
"""
from lottery_rules import parse_hafu_picks_strict

# ---- 记录状态枚举 ----
STATUS_HIT = "命中"
STATUS_MISS = "未中"
STATUS_NO_PREDICTION = "无预测输出"      # 有赛果但预测为空（API/解析/保存链路问题）
STATUS_UNSETTLED = "未结算"              # 有预测但无赛果（待赛/延期/赛果未同步）
STATUS_NO_DATA = "数据缺失"              # 预测与赛果均缺失
STATUS_ODDS_MISMATCH = "赔率数量异常"    # 合法选项数 ≠ 赔率数
STATUS_ODDS_MISSING = "赔率缺失"         # 命中选项无赔率，无法结算
STATUS_JUDGEMENT_MISSING = "判定缺失"    # 有预测但判定信息缺失（如历史 JSON 命中字段为空）
STATUS_INVALID_CODE = "含非法编码"       # 原始推荐含无法规范化的 token（如 HP）

SETTLED_STATUSES = (STATUS_HIT, STATUS_MISS)  # 仅这两类进入命中率分母与模型盈亏账


def classify_play_record(pred_raw, has_result):
    """按预测文本与赛果有无给出基础状态（命中与否需另行判定）。

    pred_raw: 原始推荐字符串（空串/None 表示无预测输出）。
    has_result: 该场是否已有赛果。
    """
    has_pred = bool(str(pred_raw or "").strip())
    if has_pred and has_result:
        return STATUS_MISS  # 占位：调用方命中判定后应改为"命中"
    if has_pred and not has_result:
        return STATUS_UNSETTLED
    if not has_pred and has_result:
        return STATUS_NO_PREDICTION
    return STATUS_NO_DATA


def settle_multi_pick(stake_total, picks, odds_list, hit_index):
    """多选玩法结算。返回 (状态, 净盈亏或 None)。

    stake_total: 每场总预算（100）。
    picks: 合法选项列表（已规范化）。
    odds_list: 与 picks 逐项对应的赔率字符串列表。
    hit_index: 命中选项在 picks 中的下标；全部未中传 -1。

    规则：
    - 无选项 → STATUS_NO_PREDICTION（调用方通常已由 classify 拦截，双保险）。
    - len(picks) != len(odds_list) → STATUS_ODDS_MISMATCH，不结算。
    - 命中但该项赔率缺失/非数字 → STATUS_ODDS_MISSING，不结算。
    - 命中 → 净盈亏 = float(odds) × stake_total/len(picks) - stake_total。
    - 未中 → 净盈亏 = -stake_total。
    """
    n = len(picks)
    if n == 0:
        return STATUS_NO_PREDICTION, None
    if len(odds_list) != n:
        return STATUS_ODDS_MISMATCH, None
    per_option = stake_total / n
    if hit_index < 0:
        return STATUS_MISS, -float(stake_total)
    try:
        odds_val = float(str(odds_list[hit_index]).strip())
    except (ValueError, TypeError):
        return STATUS_ODDS_MISSING, None
    return STATUS_HIT, round(odds_val * per_option - stake_total, 2)


def parse_hafu_for_settle(pred_raw):
    """半全场专用：严格解析 + 非法编码标注。返回 (codes, invalids, extra_status)。

    extra_status 为 STATUS_INVALID_CODE 表示原始推荐含非法 token；
    命中判定与结算仅在 codes 上进行，非法 token 永不参与映射。
    """
    codes, invalids = parse_hafu_picks_strict(pred_raw)
    return codes, invalids, (STATUS_INVALID_CODE if invalids else None)


def summarize_records(records):
    """汇总逐场记录，保证分组与总体守恒。

    records: [{"联赛": str, "状态": str, "盈亏": float|None}, ...]
    返回 {
      "by_status": {状态: 场次},
      "by_league": {联赛: {状态: 场次, "盈亏": float}},
      "hit_rate": 命中/(命中+未中)，分母为 0 时为 None,
      "total_pl": 已结算净盈亏合计（仅命中/未中两类）,
      "settled_count": 已结算场次数,
    }
    """
    by_status = {}
    by_league = {}
    for r in records:
        st = r["状态"]
        by_status[st] = by_status.get(st, 0) + 1
        lg = r.get("联赛", "")
        bucket = by_league.setdefault(lg, {})
        bucket[st] = bucket.get(st, 0) + 1
        if st in SETTLED_STATUSES and r.get("盈亏") is not None:
            bucket["盈亏"] = bucket.get("盈亏", 0.0) + r["盈亏"]

    hits = by_status.get(STATUS_HIT, 0)
    misses = by_status.get(STATUS_MISS, 0)
    denom = hits + misses
    total_pl = sum(
        v.get("盈亏", 0.0) for v in by_league.values()
    )
    return {
        "by_status": by_status,
        "by_league": by_league,
        "hit_rate": (hits / denom) if denom else None,
        "total_pl": round(total_pl, 2),
        "settled_count": denom,
    }
