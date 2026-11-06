#!/bin/bash
# 单步运行统一入口：先加载 ~/.zshrc 中的环境变量，再执行 Python。
# 背景：API Key 只从环境变量读取；非交互 shell（IDE 终端、bash -c、定时任务）
#       不会自动加载 ~/.zshrc，直接 `python3 xxx.py` 会取不到 Key。
# 用法：bash run_py.sh get_matches_api.py --date 2026-11-05
# Key 只存在于 ~/.zshrc，本脚本不读取、不打印、不落盘任何密钥值。

cd "$(dirname "$0")" || exit 1

if [ -f "$HOME/.zshrc" ]; then
    set +e
    . "$HOME/.zshrc" 2>/dev/null
    set -e
fi

PYTHON=/Users/mac/miniforge3/bin/python
[ -x "$PYTHON" ] || PYTHON=python3

exec "$PYTHON" "$@"
