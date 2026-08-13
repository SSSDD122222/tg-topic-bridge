#!/usr/bin/env bash
# TG Topic Bridge 一键部署脚本（Ubuntu/Debian）
set -euo pipefail

cd "$(dirname "$0")/.."

TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
if [ "$TOTAL_RAM_MB" -lt 1024 ] && ! swapon --show | grep -q .; then
  echo "==> 检测到内存小于 1GB，正在创建 1GB swap..."
  fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if [ ! -f .env ]; then
  echo "错误：缺少 .env 文件。请先执行: cp .env.example .env 并填好配置。"
  exit 1
fi

if grep -q "^BOT_TOKEN=$" .env || ! grep -q "BOT_TOKEN=." .env; then
  echo "错误：.env 里的 BOT_TOKEN 为空，请先填写。"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "==> 未检测到 Docker，开始安装..."
  curl -fsSL https://get.docker.com | sh
fi

systemctl enable --now docker || true

echo "==> 构建并启动机器人..."
docker compose up -d --build

echo ""
echo "部署完成！"
echo "查看日志：docker compose logs -f"
echo "停止：docker compose down"
