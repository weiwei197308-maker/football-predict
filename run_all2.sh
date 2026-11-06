#!/bin/bash
# zucai_all 全量样本环境一键流程：API 赛程+赔率+历史 → M1 带赔率分析 → M2 无赔率独立分析 → 回填昨日赛果 → 配对结算
# 不生成报告、不推飞书（与主环境实验约定一致，仅落盘产物）
# 用法：cd /Users/mac/Desktop/zucai_all && bash run_all2.sh

cd /Users/mac/Desktop/zucai_all
PYTHON=/Users/mac/miniforge3/bin/python

# 所有 API Key 约定写在 ~/.zshrc（FOOTBALL_API_KEY 与 DEEPSEEK_API_KEY 都必须存在）
if [ -f "$HOME/.zshrc" ]; then
    set +e
    source "$HOME/.zshrc" 2>/dev/null
    set -e
fi

LOG_DIR="/Users/mac/Desktop/zucai_all/output/run_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_all2_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

DATE=$(date +%Y-%m-%d)
YESTERDAY=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d yesterday +%Y-%m-%d)

for V in DEEPSEEK_API_KEY FOOTBALL_API_KEY; do
    if [ -z "${!V}" ]; then
        echo "❌ 错误：环境变量 $V 未设置（请写入 ~/.zshrc 后 source）"
        exit 1
    fi
done

# h2h 走 goal-api（默认 compare）时 GOAL_API_KEY 必填：提前在启动阶段拦截，
# 避免跑到第 1 步 get_matches_api.py 才 fail-fast
H2H_SRC="${H2H_SOURCE:-compare}"
if [ "$H2H_SRC" != "api" ] && [ -z "$GOAL_API_KEY" ]; then
    echo "❌ 错误：环境变量 GOAL_API_KEY 未设置（H2H_SOURCE=$H2H_SRC 需要 goal-api；请写入 ~/.zshrc 后 source）"
    exit 1
fi

FAILED_STEPS=()
run_step() {
    local label="$1"; shift
    echo "▶ $label"
    if "$@"; then
        echo "✅ $label 完成"
    else
        local rc=$?
        # macOS bash 3.2 多字节字符紧邻变量展开会吞字节，用 printf 拼接（同主环境 run_all.sh 的处理）
        printf '❌ %s 失败（退出码 %s），继续执行后续步骤\n' "$label" "$rc"
        FAILED_STEPS+=("$(printf '%s（退出码 %s）' "$label" "$rc")")
    fi
    echo ""
}

echo "=============================================="
echo " 全量样本实验（M1 带赔率 vs M2 无赔率） - $DATE"
echo "=============================================="
echo ""

run_step "[1/5] 获取今日赛程+赔率+历史（API-Football）..." $PYTHON get_matches_api.py
run_step "[2/5] M1 带赔率分析（analyze.py 原样，wrapper 注入历史缓存）..." $PYTHON run_analyze_m1.py
run_step "[3/5] M2 无赔率独立分析（可能要等几分钟）..." $PYTHON analyze_independent.py
run_step "[4/5] 回填昨日赛果 $YESTERDAY ..." $PYTHON get_results_api.py --date "$YESTERDAY"
run_step "[5/5] 昨日配对结算 $YESTERDAY ..." $PYTHON settle_all.py --date "$YESTERDAY"

echo "=============================================="
if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
    echo " 全部跑完！"
else
    echo " 流程结束，但有 ${#FAILED_STEPS[@]} 个步骤失败："
    for s in "${FAILED_STEPS[@]}"; do echo "   ❌ $s"; done
fi
echo " 今日赛程：output/竞彩足球_${DATE}.csv"
echo " M1 产物：output/竞彩分析_${DATE}.csv"
echo " M2 产物：output/独立分析_${DATE}.csv"
echo " 昨日对照：output/对照报告_${YESTERDAY}.md"
echo " 本次运行日志：$LOG_FILE"
echo "=============================================="
