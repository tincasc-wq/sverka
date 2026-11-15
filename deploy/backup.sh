#!/usr/bin/env bash
# Резервная копия базы: pg_dump в ~/backups, хранится 14 дней. Запускается cron'ом в 03:30.
# Восстановление: .\deploy\deploy.ps1 restore <файл> (или pg_restore вручную).
# Копии лежат на том же сервере — от сбоя диска не спасают; внешнее хранилище — позже.
set -euo pipefail

cd /home/sverka/sverka
target="/home/sverka/backups/sverka-$(date +%F).dump"
docker compose -f deploy/docker-compose.prod.yml exec -T db \
  sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$target.part"
mv "$target.part" "$target"
find /home/sverka/backups -name 'sverka-*.dump' -mtime +7 -delete
echo "$(date '+%F %T') ok $(du -h "$target" | cut -f1) $target"
