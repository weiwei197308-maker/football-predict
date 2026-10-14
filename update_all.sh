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

echo "==================== 全部完成 ===================="
echo "本次运行日志：$LOG_FILE"
