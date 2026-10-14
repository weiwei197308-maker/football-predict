#!/bin/bash
# 一键跑完整流程：获取赛程 → AI 分析 → 生成报告 → 复盘昨日 → 推送飞书
# 用法：cd /Users/mac/Desktop/zucai && bash run_all.sh

cd /Users/mac/Desktop/zucai
PYTHON=/Users/mac/miniforge3/bin/python

# 显式加载用户 shell 配置：所有 API Key 都约定写在 ~/.zshrc 里
# bash 直接读取 ~/.zshrc 会遇到少量 zsh 专有命令报错，但 export 语句 POSIX 兼容、能正确生效
if [ -f "$HOME/.zshrc" ]; then
    set +e
    source "$HOME/.zshrc" 2>/dev/null
    set -e
fi
# 兜底：如果用户用 bash 且写在 ~/.bashrc 里
if [ -z "$DEEPSEEK_API_KEY" ] && [ -f "$HOME/.bashrc" ]; then
    set +e
    source "$HOME/.bashrc" 2>/dev/null
    set -e
fi

# 运行日志落盘留痕：无论调用方如何重定向 stdout/stderr，本次运行的完整输出都留存一份
LOG_DIR="/Users/mac/Desktop/zucai/output/run_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_all_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

DATE=$(date +%Y-%m-%d)
YESTERDAY=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d yesterday +%Y-%m-%d)

# 环境变量检查：DEEPSEEK_API_KEY 必须设置
if [ -z "$DEEPSEEK_API_KEY" ]; then
    echo "❌ 错误：环境变量 DEEPSEEK_API_KEY 未设置"
    echo ""
    echo "设置方法（二选一）："
    echo "  1. 临时设置："
    echo "     export DEEPSEEK_API_KEY=你的密钥"
    echo "     bash run_all.sh"
    echo ""
    echo "  2. 写入 ~/.zshrc 永久生效："
    echo "     echo 'export DEEPSEEK_API_KEY=你的密钥' >> ~/.zshrc"
    echo "     source ~/.zshrc"
    exit 1
fi

# 智谱 Key 仅用于可选的深度归因步骤：缺失只提醒，不阻断流程
if [ -z "$ZHIPU_API_KEY" ]; then
    echo "⚠️ 提醒：ZHIPU_API_KEY 未设置，本次运行的深度归因步骤将跳过（其余步骤不受影响）"
    echo "   设置方法：echo 'export ZHIPU_API_KEY=你的密钥' >> ~/.zshrc && source ~/.zshrc"
    echo ""
fi

# 深度归因独立群 webhook：缺失只提醒，不阻断流程
if [ -z "$FEISHU_ATTR_WEBHOOK" ]; then
    echo "ℹ️ 提醒：FEISHU_ATTR_WEBHOOK 未设置，深度归因不会推送独立群（其余步骤不受影响）"
    echo ""
fi

# 失败步骤收集：任何一步失败都记录，但不中断后续步骤，结尾统一汇总
FAILED_STEPS=()

run_step() {
    local label="$1"; shift
    echo "▶ $label"
    if "$@"; then
        echo "✅ $label 完成"
    else
        local rc=$?
        echo "❌ $label 失败（退出码 $rc），继续执行后续步骤"
        FAILED_STEPS+=("$label（退出码 $rc）")
    fi
    echo ""
}

# 软失败步骤：失败只警告跳过，不计入 FAILED_STEPS，不影响整体运行结论
# （用于深度归因等依赖外部联网服务的可选增强步骤）
run_step_soft() {
    local label="$1"; shift
    echo "▶ $label"
    if "$@"; then
        echo "✅ $label 完成"
    else
        local rc=$?
        echo "⚠️ 深度归因失败，跳过（退出码 $rc，可选增强步骤，不影响后续推送）"
    fi
    echo ""
}

echo "=============================================="
echo " 竞彩足球每日分析  -  $DATE"
echo "=============================================="
echo ""

run_step "[1/6] 获取今日赛程..." $PYTHON get_matches.py
run_step "[2/6] AI 五大玩法分析（可能要等2-3分钟）..." $PYTHON analyze.py
run_step "[3/6] 生成格式化报告..." $PYTHON generate_report.py
run_step "[4/6] 复盘昨日比赛（$YESTERDAY）..." $PYTHON review.py "$YESTERDAY"
run_step_soft "[5/6] 昨日错题深度归因（联网搜索，可选，$YESTERDAY）..." $PYTHON enrich_attribution.py "$YESTERDAY"
run_step "[6/6] 推送到飞书群..." $PYTHON push_feishu.py

echo "=============================================="
if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
    echo " 全部跑完！去飞书群看结果吧 🎉"
else
    echo " 流程结束，但有 ${#FAILED_STEPS[@]} 个步骤失败："
    for s in "${FAILED_STEPS[@]}"; do echo "   ❌ $s"; done
    echo " 请查看上方对应步骤的报错日志后单独重跑该步骤。"
fi
echo " 今日报告：output/竞彩报告_${DATE}.txt"
echo " 昨日复盘：output/竞彩复盘_${YESTERDAY}.txt"
echo " 昨日深度归因（若第5步成功）：output/深度归因_${YESTERDAY}.txt"
echo " 本次运行日志：$LOG_FILE"
echo "=============================================="
