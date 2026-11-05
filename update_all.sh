#!/bin/bash
cd "$(dirname "$0")"

# 运行日志落盘留痕：无论调用方如何重定向 stdout/stderr，本次运行的完整输出都留存一份
LOG_DIR="$(pwd)/output/run_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/update_all_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo ""
echo "==================== 模拟盘一键更新 ===================="

if [ -n "$1" ]; then
    DATES=("$1")
else
    DATES=()
    for f in output/竞彩复盘_*.txt; do
        [ -f "$f" ] || continue
        d=$(basename "$f" | sed 's/竞彩复盘_\(.*\)\.txt/\1/')
        DATES+=("$d")
    done
    today=$(date +%Y-%m-%d)
    analysis_today="output/竞彩分析_${today}.csv"
    if [ -f "$analysis_today" ]; then
        already=0
        for existing in "${DATES[@]}"; do
            if [ "$existing" = "$today" ]; then already=1; break; fi
        done
        if [ $already -eq 0 ]; then
            DATES+=("$today")
        fi
    fi
fi

echo "将处理 ${#DATES[@]} 个日期: ${DATES[*]}"
echo ""

for d in "${DATES[@]}"; do
    echo "---------- $d ----------"
    python3 update_sim_tracker.py "$d"
    echo ""
    python3 update_goals_tracker.py "$d"
    echo ""
    python3 update_half_full_tracker.py "$d"
    echo ""
    python3 update_score_tracker.py "$d"
    echo ""
done

# 软失败步骤包装：失败只打印警告，不中断脚本（参照 run_all.sh 深度归因步骤的容错方式）
run_step_soft() {
    local label="$1"; local fail_msg="$2"; shift 2
    echo "▶ $label"
    if "$@"; then
        echo "✅ $label 完成"
    else
        local rc=$?
        # 注意：macOS bash 3.2 在双引号内"$var<多字节字符>"相邻时会吞掉变量及后续前导字节，
        # 故此处用 printf 拼接，保证失败消息字节正确（不要用 echo "$var（..."）
        printf '%s（退出码 %s，可选步骤，不影响上方模拟盘更新结果）\n' "$fail_msg" "$rc"
    fi
    echo ""
}

echo ""
echo "---------- 错题归因总表（第 5 张表，可选步骤） ----------"
run_step_soft "错题归因总表生成" "⚠️ 错题归因总表生成失败，跳过" python3 build_attribution_table.py

echo "==================== 全部完成 ===================="
echo "本次运行日志：$LOG_FILE"
