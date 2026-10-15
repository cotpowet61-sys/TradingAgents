#!/usr/bin/env bash
# ============================================================================
# TradeVision — боевой деплой на чистый Hetzner Ubuntu 24.04 (запуск от root).
#
#   DOMAIN=tv.example.com bash portal/deploy/deploy.sh
#
# Что делает:
#   1) системные пакеты, ufw (только 22/80/443), fail2ban, unattended-upgrades;
#   2) отдельный системный пользователь без sudo;
#   3) код из стабильной ветки (BRANCH, по умолчанию main) в /srv/tradevision;
#   4) venv + зависимости ИМЕННО portal/requirements.txt;
#   5) portal/.env: генерирует PORTAL_AUTH_SECRET/ADMIN_TOKEN, остальное заполняете вы;
#   6) systemd (tradevision + tg-бот) с hardening;
#   7) Caddy (HTTPS автоматом) с лимитами;
#   8) ежедневный бэкап данных И секретов (.env) с ротацией 14 дней.
#
# Идемпотентен: повторный запуск обновляет код и не перезаписывает .env.
# ============================================================================
set -euo pipefail

APP=/srv/tradevision
USER_NAME=tradevision
BRANCH=${BRANCH:-main}
DOMAIN=${DOMAIN:-}
REPO=${REPO:-https://github.com/cotpowet61-sys/TradingAgents.git}
BACKUP_DIR=/var/backups/tradevision

[ "$(id -u)" = "0" ] || { echo "Запускать от root (sudo -i)."; exit 1; }

echo "== 1. системные пакеты =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv python3-pip git ufw fail2ban \
    unattended-upgrades ca-certificates curl > /dev/null

echo "== 2. пользователь $USER_NAME (без sudo, без shell-логина брокером) =="
id -u $USER_NAME >/dev/null 2>&1 || useradd -m -s /bin/bash $USER_NAME
usermod -aG $USER_NAME $USER_NAME 2>/dev/null || true

echo "== 3. код из ветки $BRANCH =="
if [ ! -d "$APP/.git" ]; then
  git clone --branch "$BRANCH" "$REPO" "$APP"
fi
cd "$APP"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

echo "== 4. venv + зависимости портала =="
[ -d "$APP/venv" ] || python3.11 -m venv "$APP/venv"
"$APP/venv/bin/pip" -q install --upgrade pip
"$APP/venv/bin/pip" -q install -r "$APP/portal/requirements.txt"

echo "== 5. portal/.env (создаётся один раз) =="
mkdir -p "$APP/portal/data"
chmod 700 "$APP/portal/data"
if [ ! -f "$APP/portal/.env" ]; then
  SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
  ADMIN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
  cat > "$APP/portal/.env" <<ENV
# --- режим продажи ---
SAAS_MODE=true
BRAND_NAME=TradeVision
PRICE_MONTH_USD=100
SITE_URL=https://${DOMAIN:-your-domain.com}
# --- приём оплаты (только ПУБЛИЧНЫЕ адреса! приватных ключей здесь быть НЕ ДОЛЖНО) ---
WALLET_TRC20=
WALLET_BEP20=
BSCSCAN_API_KEY=
TRC20_WATCH_ONLY_ACK=false
# --- телеграм ---
TELEGRAM_URL=
TG_BOT_TOKEN=
ADMIN_TG_ID=
# --- админ API (партнёры/выплаты) ---
ADMIN_TOKEN=$ADMIN
# --- секрет сессий и ключ шифрования ключей клиентов ---
# НЕ ТЕРЯТЬ: без него ключи пользователей не расшифруются. Бэкапится автоматически.
PORTAL_AUTH_SECRET=$SECRET
# --- безопасность ---
TRUST_PROXY_HEADERS=true
FRAME_ANCESTORS='self'
ALLOWED_ORIGINS=
REQUIRE_REAL_DATA=true
# ритм сканера: адаптивный (активный рынок — чаще) + ротация таймфреймов
SCAN_CADENCE_SECONDS=30,60,90
TFS_PER_TICK=1
# фоновые процессы (можно вынести в отдельные unit'ы)
LIVE_SCANNER=true
BILLING_WATCHER=true
# --- AI по умолчанию (ваши ключи; Community-AI для юзеров без своего ключа) ---
AI_BASE_URL=https://api.justcode.lol/v1
AI_API_KEY=
FREE_AI_BASE_URL=https://openrouter.ai/api/v1
FREE_AI_KEY=
FREE_AI_MODEL=meta-llama/llama-3.3-70b-instruct:free
# --- read-only ключи ВЛАДЕЛЬЦА (для «Моих сделок» у владельца) ---
BYBIT_API_KEY=
BYBIT_API_SECRET=
# --- владельцы: вечный доступ без оплаты ---
OWNER_EMAILS=
ENV
  chmod 600 "$APP/portal/.env"
  echo "   !!! создан $APP/portal/.env — заполните кошельки/ключи и перезапустите сервис"
fi
# права и на каталог, и на файлы: брокеру нужен только этот каталог
chown -R "$USER_NAME:$USER_NAME" "$APP/portal/data" "$APP/portal/.env"
chmod 600 "$APP/portal/.env"

echo "== 6. systemd =="
cp "$APP/portal/deploy/tradevision.service" /etc/systemd/system/
cp "$APP/portal/deploy/tv-tgbot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable tradevision tv-tgbot >/dev/null
systemctl restart tradevision tv-tgbot

echo "== 7. фаервол + fail2ban =="
ufw allow OpenSSH >/dev/null
ufw allow 80,443/tcp >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban >/dev/null 2>&1 || true

echo "== 8. Caddy (HTTPS) =="
if [ -n "$DOMAIN" ]; then
  sed "s/your-domain.com/$DOMAIN/g" "$APP/portal/deploy/Caddyfile.sample" > /etc/caddy/Caddyfile
else
  echo "   !!! DOMAIN не задан — оставляю прежний Caddyfile (HTTPS не поднимется)"
  [ -f /etc/caddy/Caddyfile ] || cp "$APP/portal/deploy/Caddyfile.sample" /etc/caddy/Caddyfile
fi
systemctl enable caddy >/dev/null 2>&1 || true
systemctl reload caddy 2>/dev/null || systemctl restart caddy || true

echo "== 9. бэкап данных и секретов (ежедневно 03:00, ротация 14 дней) =="
mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
cat > /etc/cron.d/tradevision-backup <<CRON
0 3 * * * root tar -czf $BACKUP_DIR/tv-\$(date +\%F).tar.gz -C $APP portal/data portal/.env 2>/dev/null && find $BACKUP_DIR -maxdepth 1 -name 'tv-*.tar.gz' -mtime +14 -delete
CRON

echo "== 10. проверка =="
sleep 2
systemctl --no-pager --lines=5 status tradevision || true
curl -fsS "http://127.0.0.1:3005/api/health" | head -c 300 || echo "(портал ещё поднимается — проверьте journalctl -u tradevision -f)"
echo
echo "✅ Готово. Осталось заполнить $APP/portal/.env (кошельки, ключи) и:"
echo "   systemctl restart tradevision tv-tgbot"
echo "   Логи: journalctl -u tradevision -f   |   оплаты: journalctl -u tradevision | grep billing"
echo "   Юридический минимум: заполните portal/static/legal/*.html (маркеры [[...]]) — без этого продавать нельзя."
