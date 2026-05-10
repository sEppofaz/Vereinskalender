#!/bin/bash
source /etc/pka/secrets.env

DATE=$(date '+%Y-%m-%d %H:%M')

# Service-Status
RENAME=$(systemctl is-active rename-webhook 2>/dev/null)
NGINX=$(systemctl is-active nginx 2>/dev/null)

# Reboot
REBOOT=""
[ -f /var/run/reboot-required ] && REBOOT="⚠️ Reboot erforderlich!"

# Disk Space
DISK=$(df -h / | awk 'NR==2 {print $3 "/" $2 " (" $5 " voll)"}')

# RAM
MEM=$(free -h | awk '/^Mem:/{print $3 "/" $2}')

# Offene Ports
PORTS=$(ss -tulnp | grep LISTEN | awk '{print $5}' | sort -u | tr '\n' ' ')

# Fehlgeschlagene SSH-Logins (letzte 7 Tage)
SSH_FAILS=$(journalctl _SYSTEMD_UNIT=ssh.service --since "7 days ago" 2>/dev/null | grep -c "Failed password" || true)
SSH_FAILS=$(echo "$SSH_FAILS" | head -1 | tr -d '[:space:]')
SSH_FAILS=${SSH_FAILS:-0}

# Ausstehende Updates
UPDATES_COUNT=$(apt list --upgradable 2>/dev/null | grep -c "/" || true)
UPDATES_COUNT=$(echo "$UPDATES_COUNT" | head -1 | tr -d '[:space:]')
UPDATES_COUNT=${UPDATES_COUNT:-0}
SECURITY_COUNT=$(apt list --upgradable 2>/dev/null | grep -c "security" || true)
SECURITY_COUNT=$(echo "$SECURITY_COUNT" | head -1 | tr -d '[:space:]')
SECURITY_COUNT=${SECURITY_COUNT:-0}

# Credential-Leak in Logs (HTTP-Access + Python-Variablenzuweisungen ausgeschlossen)
LEAK=$(journalctl -u rename-webhook --since "7 days ago" 2>/dev/null \
  | grep -vE '"(GET|POST|PUT|DELETE|PATCH|HEAD) /' \
  | grep -vE '\w+(token|password|secret)\w*\s*=' \
  | grep -ciE "(token|password|secret|api[._]key|bearer)" || true)
LEAK=$(echo "$LEAK" | head -1 | tr -d '[:space:]')
LEAK=${LEAK:-0}

# SSL-Zertifikat-Ablauf (vereinskalender.online)
CERT_EXPIRY=$(echo | timeout 5 openssl s_client -connect vereinskalender.online:443 \
  -servername vereinskalender.online 2>/dev/null \
  | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "$CERT_EXPIRY" ]; then
  CERT_DAYS=$(( ( $(date -d "$CERT_EXPIRY" +%s) - $(date +%s) ) / 86400 ))
else
  CERT_DAYS=-1
fi

# Fail2ban
F2B=$(systemctl is-active fail2ban 2>/dev/null)
F2B_BANS=$(fail2ban-client status sshd 2>/dev/null | grep "Currently banned" | awk '{print $NF}' || echo "0")
F2B_BANS=${F2B_BANS:-0}

# Rate-Limit-Hits (429er in nginx-Log, letzte 7 Tage)
RATE_HITS=$(grep " 429 " /var/log/nginx/access.log 2>/dev/null | wc -l || echo "0")
RATE_HITS=${RATE_HITS:-0}

# pip audit
PIP_VULN=$(/opt/rename-webhook/bin/pip-audit --format=text 2>/dev/null | grep -c "vulnerability" || true)
PIP_VULN=$(echo "$PIP_VULN" | head -1 | tr -d '[:space:]')
PIP_VULN=${PIP_VULN:-0}

# Status-Symbole
[ "$RENAME" = "active" ]  && RENAME_MSG="✅ rename-webhook: aktiv"  || RENAME_MSG="❌ rename-webhook: $RENAME"
[ "$NGINX" = "active" ]   && NGINX_MSG="✅ nginx: aktiv"             || NGINX_MSG="❌ nginx: $NGINX"
[ "$UPDATES_COUNT" -gt 0 ] && UPDATES_MSG="⚠️ Offene Updates: $UPDATES_COUNT (Security: $SECURITY_COUNT)" || UPDATES_MSG="✅ Alle Updates installiert"
[ "$SSH_FAILS" -gt 1000 ] && SSH_MSG="⚠️ SSH Fehlversuche: $SSH_FAILS" || SSH_MSG="✅ SSH Fehlversuche: $SSH_FAILS"
[ "$LEAK" -gt 0 ]         && LEAK_MSG="⚠️ Credential-Leak in Logs: $LEAK Treffer" || LEAK_MSG="✅ Logs: kein Credential-Leak"
[ "$PIP_VULN" -gt 0 ]     && PIP_MSG="⚠️ pip audit: $PIP_VULN Schwachstellen" || PIP_MSG="✅ pip audit: keine Schwachstellen"
[ "$F2B" = "active" ]     && F2B_MSG="✅ Fail2ban: aktiv ($F2B_BANS gebannte IPs)" || F2B_MSG="❌ Fail2ban: $F2B"
[ "$RATE_HITS" -gt 100 ]  && RATE_MSG="⚠️ Rate-Limit-Hits (429): $RATE_HITS" || RATE_MSG="✅ Rate-Limit-Hits: $RATE_HITS"

if [ "$CERT_DAYS" -lt 0 ]; then
  CERT_MSG="⚠️ SSL-Zertifikat: nicht prüfbar"
elif [ "$CERT_DAYS" -lt 14 ]; then
  CERT_MSG="🚨 SSL-Zertifikat laeuft in ${CERT_DAYS}d ab!"
elif [ "$CERT_DAYS" -lt 30 ]; then
  CERT_MSG="⚠️ SSL-Zertifikat: noch ${CERT_DAYS}d gueltig"
else
  CERT_MSG="✅ SSL-Zertifikat: noch ${CERT_DAYS}d gueltig"
fi

[ -n "$REBOOT" ] && REBOOT_LINE=$(printf "\n\n%s" "$REBOOT") || REBOOT_LINE=""

MSG=$(printf "🔒 Security-Check %s\n\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n\n📦 Disk: %s | RAM: %s\n🌐 Ports: %s%s" \
  "$DATE" \
  "$RENAME_MSG" "$NGINX_MSG" \
  "$UPDATES_MSG" \
  "$SSH_MSG" "$F2B_MSG" \
  "$LEAK_MSG" "$PIP_MSG" \
  "$CERT_MSG" "$RATE_MSG" \
  "$DISK" "$MEM" "$PORTS" "$REBOOT_LINE")

curl -s "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${MSG}" > /dev/null
