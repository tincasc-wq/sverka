#!/bin/sh
# Шлюз сборщика: внутренний HTTP-прокси :3128 → VLESS-Reality → зарубежный сервер (Ирландия).
# Через него идут только источники с via_gateway и зарубежные LLM (COLLECTOR_PROXY, LLM_PROXY).
# Параметры — из ~/sverka/.env на сервере (GATEWAY_*), в git их нет.
set -eu
: "${GATEWAY_HOST:?}" "${GATEWAY_UUID:?}" "${GATEWAY_PBK:?}" "${GATEWAY_SID:?}"
SNI="${GATEWAY_SNI:-www.bmw.de}"
cat > /tmp/config.json <<EOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [{"listen": "0.0.0.0", "port": 3128, "protocol": "http", "settings": {}}],
  "outbounds": [{
    "protocol": "vless",
    "settings": {"vnext": [{"address": "$GATEWAY_HOST", "port": 443,
      "users": [{"id": "$GATEWAY_UUID", "flow": "xtls-rprx-vision", "encryption": "none"}]}]},
    "streamSettings": {"network": "tcp", "security": "reality",
      "realitySettings": {"serverName": "$SNI", "fingerprint": "chrome",
        "publicKey": "$GATEWAY_PBK", "shortId": "$GATEWAY_SID"}}
  }]
}
EOF
exec xray run -c /tmp/config.json
