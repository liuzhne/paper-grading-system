#!/usr/bin/env bash
# 在实例上用一次性容器跑运维命令（迁移、切换到本机数据库）。迁移与切换工作流经 SSH 调用它。
#
#   ops.sh <镜像标签> <命令...>      标准输入：KEY=VALUE 行（scripts/production_env.py --kind ops）
#
# 连接串从标准输入读进一个只有本用户可读的临时文件，用完即删：不出现在命令行、进程列表与日志里。
# 容器接入 compose 网络 pgs_default，连得到 db；切换前数据还在 Supabase 时也能出网。
set -euo pipefail

cd "$(dirname "$0")"

tag="${1:-}"
if [[ ! "$tag" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || [[ $# -lt 2 ]]; then
	echo "用法：ops.sh <镜像标签> <命令...>（连接串从标准输入读）" >&2
	exit 2
fi
shift
if ! docker image inspect "paper-grading-system:$tag" >/dev/null 2>&1; then
	echo "本机没有镜像 paper-grading-system:$tag" >&2
	exit 1
fi

env_file="$(mktemp)"
trap 'rm -f "$env_file"' EXIT
chmod 0600 "$env_file"
cat >"$env_file"

docker run --rm --network pgs_default --env-file "$env_file" "paper-grading-system:$tag" "$@"
