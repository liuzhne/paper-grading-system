#!/usr/bin/env bash
# 在实例上把服务切到某个已载入的镜像版本。部署与回滚都用它。
#
#   deploy.sh <提交 SHA>   切到这个版本（镜像须已 docker load 到本机）
#   deploy.sh --list       列出本机还留着的版本，标出正在运行的那个
#
# CI 先把镜像经 SSH 载入、把 compose.yml、caddy/ 和本脚本同步到 /opt/pgs、写好 .env，再调用它。
# 回滚就是用 --list 里的旧 SHA 再调用一次，不需要重新构建。
set -euo pipefail

cd "$(dirname "$0")"

IMAGE=paper-grading-system
KEEP=3

compose() {
	docker compose --project-name pgs --file compose.yml "$@"
}

# 本机留着的版本，新的在前（不含 current 别名）。
versions() {
	docker image ls "$IMAGE" --format '{{.CreatedAt}}|{{.Tag}}' |
		sort -r |
		cut -d'|' -f2 |
		grep -v -x -e current -e '<none>' || true
}

if [[ "${1:-}" == "--list" ]]; then
	current="$(cat RELEASE 2>/dev/null || true)"
	while read -r tag; do
		[[ -z "$tag" ]] && continue
		if [[ "$tag" == "$current" ]]; then
			echo "$tag  ← 正在运行"
		else
			echo "$tag"
		fi
	done < <(versions)
	exit 0
fi

tag="${1:-}"
if [[ ! "$tag" =~ ^[0-9a-f]{7,40}$ ]]; then
	echo "用法：deploy.sh <提交 SHA> | --list" >&2
	exit 2
fi
if ! docker image inspect "$IMAGE:$tag" >/dev/null 2>&1; then
	echo "本机没有镜像 $IMAGE:$tag。用 deploy.sh --list 查看可用版本。" >&2
	exit 1
fi
for file in .env db.env; do
	if [[ ! -f "$file" ]]; then
		echo "缺少 /opt/pgs/$file：部署任务会在调用本脚本前写好它；手工部署见 RUNBOOK 9.4。" >&2
		exit 1
	fi
done

# 先校验 Caddy 配置，再动服务：配置有错时线上保持原样。
if [[ -n "$(compose ps --quiet caddy 2>/dev/null)" ]]; then
	compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
fi

docker tag "$IMAGE:$tag" "$IMAGE:current"

if [[ -z "$(compose ps --quiet app 2>/dev/null)" ]]; then
	# 第一次部署：全部建起来（caddy 等 app 健康后才启动）。
	compose up --detach --wait --wait-timeout 180
else
	# db 先就绪：下面换 app、worker 都带 --no-deps，不会顺带把它拉起来。配置没变时不重建。
	compose up --detach --wait --wait-timeout 120 db
	# 先换 app：Caddy 在空档期间让新请求等着，用户只看到几秒延迟。
	compose up --detach --no-deps --force-recreate --wait --wait-timeout 180 app
	# 再换 worker：旧 worker 停下前最多等 100 秒让在执行的条目做完。
	compose up --detach --no-deps --force-recreate worker
	# 不指定服务：caddy 没在跑就拉起来，已在跑的一律不重建；--remove-orphans 删掉 compose.yml 里
	# 已经没有的服务留下的容器（例如 2026-10-28 停用的 backup），否则它照旧运行。卷原样保留。
	compose up --detach --no-recreate --remove-orphans
	compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
fi

echo "$tag" >RELEASE

# 只留最近 $KEEP 个版本；正在运行的那个永远保留。
kept=0
while read -r old; do
	[[ -z "$old" ]] && continue
	if [[ "$old" == "$tag" ]]; then
		continue
	fi
	kept=$((kept + 1))
	if ((kept >= KEEP)); then
		docker image rm "$IMAGE:$old" >/dev/null
	fi
done < <(versions)
docker image prune --force >/dev/null

echo "已切到 $tag"
