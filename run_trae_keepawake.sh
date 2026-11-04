#!/bin/zsh
# run_trae_keepawake.sh — macOS Ventura
# 启动 Trae（open -W 等待其退出），期间用 caffeinate -i 阻止系统闲置休眠；Trae 退出后自动结束保活。

set -u

APP="${1:-}"

# 未指定路径时，依次探测常见安装位置
if [[ -z "$APP" ]]; then
  for candidate in "/Applications/Trae.app" "/Applications/Trae CN.app" "$HOME/Applications/Trae.app"; do
    if [[ -d "$candidate" ]]; then
      APP="$candidate"
      break
    fi
  done
fi

# 校验 Trae 路径
if [[ -z "$APP" || ! -d "$APP" ]]; then
  printf '%s\n' "错误：找不到 Trae.app（已检查 /Applications 和 ~/Applications）。" >&2
  printf '%s\n' "用法：$0 [/path/to/Trae.app]" >&2
  exit 1
fi

if [[ ! -f "$APP/Contents/Info.plist" ]]; then
  printf '%s\n' "错误：$APP 不是有效的 macOS 应用包（缺少 Contents/Info.plist）。" >&2
  exit 1
fi

# 退出/中断时确保保活进程被清理
CAFFEINATE_PID=""
cleanup() {
  if [[ -n "$CAFFEINATE_PID" ]] && kill -0 "$CAFFEINATE_PID" 2>/dev/null; then
    kill "$CAFFEINATE_PID" 2>/dev/null
    wait "$CAFFEINATE_PID" 2>/dev/null
  fi
  CAFFEINATE_PID=""
}
trap cleanup EXIT INT TERM HUP

printf '%s\n' "说明：保活需保持笔记本开盖，合盖会休眠并使保活失效；屏幕可以正常熄屏，不影响保活。"
printf '%s\n' "启动保活：caffeinate -i"
caffeinate -i &
CAFFEINATE_PID=$!

printf '%s\n' "启动 Trae：$APP"
open -W -a "$APP"

printf '%s\n' "Trae 已退出，关闭保活。"
