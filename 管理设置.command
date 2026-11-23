#!/bin/bash
APP="$HOME/Library/Application Support/MediaAutoArchive/media_archive.py"
if [ ! -f "$APP" ]; then
  echo "尚未安装媒体自动归档，请先运行 安装.command。"
  read -r -p "按回车退出..." _
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 已不可用，请检查安装。"
  read -r -p "按回车退出..." _
  exit 1
fi
while true; do
  echo
  echo "==============================="
  echo "      媒体自动归档 · MVP"
  echo "==============================="
  echo "1) 查看保存位置与最近记录"
  echo "2) 选择新的保存位置"
  echo "3) 手动扫描已连接 SD 卡 / U 盘"
  echo "4) 将一个 U 盘名称加入自动扫描白名单"
  echo "5) 从白名单中移除 U 盘"
  echo "6) 查看最近导入日志"
  echo "7) 关闭插卡自动导入（不删除数据）"
  echo "0) 退出"
  read -r -p "请选择：" option
  case "$option" in
    1) python3 "$APP" status ;;
    2) python3 "$APP" configure ;;
    3) python3 "$APP" scan ;;
    4) read -r -p "输入 U 盘在 Finder 中显示的磁盘名：" n; python3 "$APP" allow-source "$n" ;;
    5) read -r -p "输入要移除的磁盘名：" n; python3 "$APP" deny-source "$n" ;;
    6) tail -n 60 "$HOME/Library/Application Support/MediaAutoArchive/activity.log" ;;
    7) python3 "$(dirname "$0")/installer.py" --uninstall; break ;;
    0) break ;;
    *) echo "无效选项，请重试。" ;;
  esac
done