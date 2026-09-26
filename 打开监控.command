#!/bin/bash
# Double-click entry point: start the local monitor and open it in the browser.
# Only an interactive TTY waits for a key press, so automation never hangs.
script="$0"
while [ -L "$script" ]; do
  link=$(readlink "$script")
  case "$link" in
    /*) script="$link" ;;
    *) case "$script" in
         */*) script="${script%/*}/$link" ;;
         *) script="$link" ;;
       esac ;;
  esac
done
case "$script" in
  */*) DIR=${script%/*} ;;
  *) DIR=. ;;
esac
DIR="$(cd "$DIR" && pwd)"
"$DIR/bin/monitor" start
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  echo "监控启动失败（退出码 $STATUS）。"
  if [ -t 0 ]; then
    echo "按回车关闭。"
    read -r _
  fi
  exit "$STATUS"
fi
exit 0
