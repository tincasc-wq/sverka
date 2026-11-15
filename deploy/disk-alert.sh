#!/bin/sh
# Тревога в Telegram при заполнении диска (порог 10% свободного) (15.11.2026: 100% — бэкап .part забил
# диск, db/redis ушли в unhealthy, проверки падали; теперь порог и оповещение заранее).
PCT=$(df / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
[ "$PCT" -lt 90 ] && exit 0
TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' ~/sverka/.env | cut -d= -f2 | tr -d '"'"'"' ')
PROXY=$(grep '^TELEGRAM_PROXY=' ~/sverka/.env | cut -d= -f2 | tr -d '"'"'"' ')
FREE=$(df -h / | awk 'NR==2 {print $4}')
curl -s -m 15 ${PROXY:+-x "$PROXY"} -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d chat_id=227099630 \
  --data-urlencode "text=⚠️ sverka: диск заполнен на ${PCT}% (порог 10% свободного). Свободно: ${FREE}. Проверь бэкапы/логи/WAL."
