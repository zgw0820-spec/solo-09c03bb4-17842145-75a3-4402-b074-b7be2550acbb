#!/bin/sh
# 首次启动时把样例裸仓库种到数据卷，然后启动 Web 服务。
# - 空卷（目标路径不存在）：直接克隆样例仓库；
# - 目标路径存在但是无效/不完整的 git 目录（如 refs/ 缺失）：
#   原样备份到 .broken-<时间戳> 后重新种入，保证服务能从空卷/坏卷启动；
# - 已是有效裸仓库：保持原样，里面的数据一律不动。
set -e
: "${REPO_PATH:=/srv/repo.git}"

if [ ! -d "$REPO_PATH" ] || ! git --git-dir="$REPO_PATH" rev-parse --git-dir >/dev/null 2>&1; then
  if [ -e "$REPO_PATH" ]; then
    backup="${REPO_PATH}.broken-$(date +%Y%m%d%H%M%S)"
    echo "警告：$REPO_PATH 不是有效 git 仓库，原样备份到 $backup 后重新初始化"
    mv "$REPO_PATH" "$backup"
  else
    echo "初始化裸仓库 $REPO_PATH（来自 /samples/repo.git）"
  fi
  git clone --quiet --bare /samples/repo.git "$REPO_PATH"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
