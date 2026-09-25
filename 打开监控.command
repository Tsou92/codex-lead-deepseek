#!/bin/bash
# Double-click entry point: start the local monitor and open it in the browser.
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/bin/monitor" start
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  echo "监控启动失败（退出码 $STATUS）。按回车关闭。"
  read -r _
fi
