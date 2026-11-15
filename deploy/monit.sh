#!/bin/sh
# Мониторинг Сверки: собирает метрики, шлёт ТЕЛЕГРАМ-ТРЕВОГИ при отклонениях.
# Уроки 15.11.2026: диск забит дампами; воркер завис — 9 часов без эмбеддингов
# (протухший in-progress маркер); проверки зависли в running/queued; контейнеры unhealthy.
# Запуск: cron каждые 15 минут. Пороги — переменные окружения (см. значения по умолчанию).

cd "$(dirname "$0")/.." || exit 1
COMPOSE="docker compose -f deploy/docker-compose.prod.yml"
STATE=/home/sverka/.monit-state
ALERT_EVERY=${ALERT_EVERY:-10800}   # повтор тревоги не чаще, чем раз в 3 ч
RECOVERY=1

PCT_DISK=${PCT_DISK:-90}            # % занятого диска
BACKLOG=${BACKLOG:-5000}            # документов без эмбеддинга
EMBED_STUCK_MIN=${EMBED_STUCK_MIN:-25}  # возраст in-progress маркера эмбед-крона, мин
STUCK_MIN=${STUCK_MIN:-20}          # проверка в running/queued дольше, мин
STUCK_FAIL_MIN=${STUCK_FAIL_MIN:-45}    # ...старше этого — автоматом failed
RAM_MIN_AVAIL=${RAM_MIN_AVAIL:-10}  # % доступной памяти
SWAP_MAX=${SWAP_MAX:-80}            # % занятого swap
LLM_DAY_USD=${LLM_DAY_USD:-5}       # $ за сутки на LLM
RSS_FAIL=${RSS_FAIL:-50}            # упавших источников за проход

tg() { # tg <текст>
    TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2 | tr -d '"'"'"' ')
    PROXY=$(grep '^TELEGRAM_PROXY=' .env | cut -d= -f2 | tr -d '"'"'"' ')
    curl -s -m 15 ${PROXY:+-x "$PROXY"} -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d chat_id=227099630 --data-urlencode "text=$1" >/dev/null
}

last_alert() { [ -f "$STATE" ] && grep "^$1 " "$STATE" | tail -1 | awk '{print $2}'; }
mark() { grep -v "^$1 " "$STATE" 2>/dev/null > "$STATE.tmp"; echo "$1 $2 $3" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
now=$(date +%s)

report() { # report <ключ> <сообщение-тревога> [сообщение-восстановление]
    key=$1; prev=$(awk -v k="$1" '$1==k {print $3}' "$STATE" 2>/dev/null); ts=$(last_alert "$key")
    if [ -n "$2" ]; then
        if [ "$prev" != alert ] || [ $((now - ${ts:-0})) -gt "$ALERT_EVERY" ]; then
            tg "🚨 $2"; mark "$key" "$now" alert
        fi
    elif [ "$prev" = alert ] && [ -n "$3" ];then
        tg "✅ $3"; mark "$key" "$now" ok
    fi
}

# --- 1. Диск ---
used=$(df / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
if [ "$used" -ge "$PCT_DISK" ]; then
    report disk "sverka: диск занят на ${used}% (порог ${PCT_DISK}%). Свободно: $(df -h / | awk 'NR==2{print $4}'). Чистить: бэкапы (>2 дней), docker builder prune."
else
    report disk "" "sverka: диск в норме — занято ${used}%."
fi

# --- 2. Контейнеры ---
bad=$(docker ps -a --filter 'name=sverka' --format '{{.Names}}: {{.Status}}' | grep -E 'unhealthy|Exited|Restarting|Dead')
if [ -n "$bad" ]; then
    report containers "sverka: проблемные контейнеры: $(echo "$bad" | tr '\n' '; ')"
else
    report containers "" "sverka: все контейнеры в норме."
fi

# --- 3. Очередь эмбеддингов и залипание крона ---
row=$($COMPOSE exec -T db psql -U sverka -d sverka -t -A -c \
    "select count(*) filter (where embedding is null), count(*) from documents")
pend=${row%%|*}
if [ "${pend:-0}" -ge "$BACKLOG" ]; then
    report embed_backlog "sverka: эмбеддинг-очередь ${pend} документов (порог ${BACKLOG}). Приток выше обработки: либо догон лент, либо впору притормаживать сбор."
else
    report embed_backlog "" "sverka: эмбеддинг-очередь в норме (${pend})."
fi
# In-progress маркер крона старше N минут — крон завис (инцидент 15.11).
key=$($COMPOSE exec -T redis redis-cli keys 'arq:in-progress:cron:embed_documents:*' 2>/dev/null | head -1)
if [ -n "$key" ]; then
    ms=$(echo "$key" | sed 's/.*://')
    age=$(( ( $(date +%s%3N) - ms ) / 60000 ))
    if [ "$age" -ge "$EMBED_STUCK_MIN" ]; then
        report embed_stuck "sverka: эмбед-крон не завершался ${age} мин (лимит ${EMBED_STUCK_MIN}) — похоже на залипание воркера, как 15.11. Проверь воркер: docker restart sverka-worker-1."
    else
        report embed_stuck "" "sverka: эмбед-крон работает (маркер ${age} мин)."
    fi
else
    report embed_stuck "" "sverka: эмбед-крон свободен (маркера нет)."
fi

# --- 4. Зависшие проверки; старше STUCK_FAIL_MIN — автоматом failed ---
stuck=$($COMPOSE exec -T db psql -U sverka -d sverka -t -A -c \
    "select count(*) from checks where status in ('running','queued') and created_at < now() - interval '${STUCK_MIN} minutes'")
if [ "${stuck:-0}" -gt 0 ]; then
    report stuck_checks "sverka: ${stuck} проверок висят в running/queued дольше ${STUCK_MIN} мин (вероятно, падение воркера посреди работы)."
else
    report stuck_checks "" "sverka: зависших проверок нет."
fi
$COMPOSE exec -T db psql -U sverka -d sverka -c \
    "update checks set status='failed', finished_at=now() where status in ('running','queued') and created_at < now() - interval '${STUCK_FAIL_MIN} minutes'" >/dev/null 2>&1

# --- 5. Память и swap ---
avail=$(free -m | awk '/^Mem:/ {printf "%d", $7 * 100 / $2}')
swap_used=$(free -m | awk '/^Swap:/ {if ($2>0) printf "%d", $3 * 100 / $2; else print 0}')
if [ "${avail:-100}" -lt "$RAM_MIN_AVAIL" ] || [ "${swap_used:-0}" -gt "$SWAP_MAX" ]; then
    report ram "sverka: память тесно: доступно ${avail}%, swap занят ${swap_used}%. Воркер (torch) может упасть по OOM."
else
    report ram "" "sverka: память в норме (доступно ${avail}%, swap ${swap_used}%)."
fi

# --- 6. Сайт жив ---
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://ang-rist.ru/health || echo 000)
if [ "$code" != 200 ]; then
    report site "sverka: /health отвечает ${code} (ждали 200) — сайт лежит или Caddy недоступен."
else
    report site "" "sverka: сайт жив (200)."
fi

# --- 7. Расход на LLM за сутки ---
spent=$($COMPOSE exec -T db psql -U sverka -d sverka -t -A -c \
    "select round(coalesce(sum(cost_usd), 0)::numeric, 2) from llm_calls where created_at > now() - interval '24 hours'")
if awk "BEGIN { exit !($spent >= $LLM_DAY_USD) }"; then
    report llm "sverka: за сутки на LLM потрачено \$${spent} (порог \$${LLM_DAY_USD}) — проверки пойдут без LLM-анализа, пока не сбросится дневной лимит."
else
    report llm "" "sverka: расход на LLM в норме (\$${spent}/сутки)."
fi

# --- 8. Сборщик: упавшие источники в последних проходах RSS (из лога воркера) ---
failed=$(docker logs sverka-worker-1 --tail 400 2>&1 | grep 'cron:collect_rss' | grep -o 'failed_sources=[0-9]*' | tail -1 | cut -d= -f2)
if [ "${failed:-0}" -ge "$RSS_FAIL" ]; then
    report rss "sverka: в последних проходах RSS падает ${failed} источников (порог ${RSS_FAIL}) — возможен массовый сбой сети/блокировок."
else
    report rss "" "sverka: RSS-источники в норме (падений: ${failed:-0})."
fi
