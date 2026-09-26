#!/bin/bash
# 一键安装 Codex-Lead-DeepSeek（macOS，兼容系统自带 bash 3.2）。
# 从脚本自身目录解析项目根，路径含空格安全；失败返回非零，仅在 TTY 等待。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"

SKIP_RUNTIME=0
for argument in "$@"; do
  case "$argument" in
    --skip-runtime) SKIP_RUNTIME=1 ;;
  esac
done

fail() {
  printf '%s\n' "安装未完成：$1" >&2
  if [ -t 0 ]; then
    printf '%s' "按回车键关闭此窗口…" >&2
    read -r _drain || true
  fi
  exit 1
}

if [ "$SKIP_RUNTIME" -eq 0 ]; then
  if [ -x "$ROOT/bin/bootstrap" ]; then
    printf '%s\n' "[1/3] 准备便携 Python/Node 运行时…"
    "$ROOT/bin/bootstrap" || fail "bootstrap 未能完成（退出码 $?）"
  else
    printf '%s\n' "[1/3] 未找到 bin/bootstrap，跳过便携运行时准备"
  fi
else
  printf '%s\n' "[1/3] 已按 --skip-runtime 跳过便携运行时准备"
fi

PYTHON="$ROOT/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 2>/dev/null || true)"
fi
[ -n "$PYTHON" ] || fail "找不到 Python 3；请先安装 Python 3.9+"

# 只影响本次调用，不修改任何 shell 配置。
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

printf '%s\n' "[2/3] 运行安装向导…"
"$PYTHON" -B -m leadseek.onboard --source "$ROOT" "$@"
STATUS=$?
[ "$STATUS" -eq 0 ] || fail "向导返回非零（退出码 $STATUS）"

printf '%s\n' "[3/3] 完成。可再次双击本文件重新配置；已有凭据不会被自动覆盖。"
exit 0
