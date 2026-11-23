#!/bin/bash
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 Python 3，请先安装 Python 3.9 或更高版本。"
  read -r -p "按回车退出..." _
  exit 1
fi
python3 "$(dirname "$0")/installer.py" "$@"
echo
read -r -p "按回车关闭窗口..." _