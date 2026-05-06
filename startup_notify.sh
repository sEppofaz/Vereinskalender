#!/bin/bash
source /etc/pka/secrets.env
sleep 10
curl -s "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=✅ Hetzner Server wieder online ($(date '+%Y-%m-%d %H:%M'))" > /dev/null
