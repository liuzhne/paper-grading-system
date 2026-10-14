#!/usr/bin/env bash
# 新建或重建 Lightsail 实例后运行一次（Ubuntu 24.04 LTS，需要 root）：
#
#   sudo bash bootstrap.sh --deploy-key "ssh-ed25519 AAAA... pgs-deploy"
#
# 做的事：装 Docker 与 compose、设置 Docker 日志轮转、开 2 GB swap、建部署用户
# deploy（只用密钥登录，属于 docker 组）、建 /opt/pgs、只装安全更新且不自动重启、
# 关闭 SSH 密码登录。可以重复运行。
#
# 实例上没有需要备份的数据：数据库与文件在 Supabase，密钥在 GitHub production 环境里，
# 证书可以重新签发。实例坏了就新建一台，再跑一遍本脚本和一次部署。
set -euo pipefail

deploy_key=""
while (($#)); do
	case "$1" in
	--deploy-key)
		deploy_key="${2:-}"
		shift 2
		;;
	*)
		echo "未知参数：$1" >&2
		exit 2
		;;
	esac
done

if [[ "$(id -u)" -ne 0 ]]; then
	echo "需要 root：sudo bash bootstrap.sh --deploy-key \"...\"" >&2
	exit 1
fi
if [[ ! "$deploy_key" =~ ^ssh-(ed25519|rsa)\  ]]; then
	echo "缺少 --deploy-key：传入 CI 部署用的 SSH 公钥（ssh-ed25519 开头）。" >&2
	exit 2
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 unattended-upgrades

# Docker 日志按大小轮转，避免写满磁盘；compose.yml 里也写了同样的设置。
install -d -m 0755 /etc/docker
cat >/etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" }
}
JSON
systemctl enable --now docker
systemctl restart docker

# 2 GB 内存的实例开 2 GB swap 兜底：解析大文件时的峰值不至于触发 OOM。
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
	if [[ ! -f /swapfile ]]; then
		fallocate -l 2G /swapfile
		chmod 600 /swapfile
		mkswap /swapfile >/dev/null
	fi
	swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
echo 'vm.swappiness=10' >/etc/sysctl.d/60-pgs-swap.conf
sysctl --quiet --load /etc/sysctl.d/60-pgs-swap.conf

# 部署用户：CI 用它的密钥上传镜像、运行 deploy.sh。docker 组等同 root 权限，
# 所以这把密钥只放在 GitHub production 环境里。
if ! id deploy >/dev/null 2>&1; then
	useradd --create-home --shell /bin/bash deploy
fi
usermod -aG docker deploy
install -d -m 0700 -o deploy -g deploy /home/deploy/.ssh
touch /home/deploy/.ssh/authorized_keys
grep -qxF "$deploy_key" /home/deploy/.ssh/authorized_keys || echo "$deploy_key" >>/home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 0600 /home/deploy/.ssh/authorized_keys

install -d -m 0750 -o deploy -g deploy /opt/pgs

# 只自动装安全更新，不自动重启：重启会让服务中断，改为每月挑低峰手动重启（RUNBOOK）。
cat >/etc/apt/apt.conf.d/52pgs-unattended-upgrades <<'APT'
Unattended-Upgrade::Automatic-Reboot "false";
APT
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'APT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT
systemctl enable --now unattended-upgrades

# 只允许密钥登录。
cat >/etc/ssh/sshd_config.d/60-pgs.conf <<'SSH'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
SSH
sshd -t
systemctl reload ssh

echo "完成。下一步：在 GitHub production 环境配好密钥与变量（RUNBOOK 9.4），然后部署。"
