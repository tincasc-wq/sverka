#!/usr/bin/env bash
# Первичная настройка чистого VPS (Ubuntu 24.04/26.04). Запускается один раз от root:
#   .\deploy\deploy.ps1 setup
# Что делает: обновления, Docker, firewall (22/80/443), пользователь sverka с тем же
# SSH-ключом, вход только по ключу, автообновления безопасности, swap, ежедневный бэкап.
set -euo pipefail

if [[ ! -s /root/.ssh/authorized_keys ]]; then
  echo "На сервере нет SSH-ключа для root — сначала добавьте ключ, иначе после отключения" \
       "входа по паролю на сервер будет не попасть." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get -y install docker.io docker-compose-v2 ufw unattended-upgrades fail2ban
# Docker Hub бывает недоступен из РФ — зеркало Google для популярных образов.
# Не помогло — .\deploy\deploy.ps1 images перенесёт образы с локальной машины.
mkdir -p /etc/docker
[[ -f /etc/docker/daemon.json ]] || echo '{"registry-mirrors": ["https://mirror.gcr.io"]}' > /etc/docker/daemon.json
systemctl enable docker
systemctl restart docker

# Пользователь для приложения: в группе docker, тот же ключ, что у root.
if ! id sverka >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" sverka
fi
usermod -aG docker sverka
install -d -m 700 -o sverka -g sverka /home/sverka/.ssh
install -m 600 -o sverka -g sverka /root/.ssh/authorized_keys /home/sverka/.ssh/authorized_keys
install -d -m 750 -o sverka -g sverka /home/sverka/backups

# Firewall: только SSH и веб.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

# SSH: только по ключу.
cat > /etc/ssh/sshd_config.d/90-sverka.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
systemctl reload ssh || systemctl reload sshd

# Автоматические обновления безопасности.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# Swap 2 ГБ — подстраховка при пиках памяти у воркера.
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

timedatectl set-timezone Europe/Moscow

# Ежедневная резервная копия базы в 03:30 (после ночного пересчёта рейтингов).
cat > /etc/cron.d/sverka-backup <<'EOF'
30 3 * * * sverka /home/sverka/sverka/deploy/backup.sh >> /home/sverka/backups/backup.log 2>&1
EOF

echo "Готово. Дальше: .\\deploy\\deploy.ps1 env, затем .\\deploy\\deploy.ps1 push"
