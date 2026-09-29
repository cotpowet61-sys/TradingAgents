#!/usr/bin/env bash
# ============================================================================
# TradeVision — боевой деплой на чистый Hetzner Ubuntu 24.04 (запуск от root).
#   bash deploy.sh
# После: впишите ключи/кошельки в /srv/tradevision/portal/.env и restart.
# ============================================================================
set -euo pipefail

APP=/srv/tradevision
USER_NAME=tradevision

echo "== 1. системные пакеты =="
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv python3-pip git ufw fail2ban unattended-upgrades caddy > /dev/null

echo "== 2. пользователь $USER_NAME =="
id -u $USER_NAME >/dev/null 2>&1 || useradd -m -s /bin/bash $USER_NAME

echo "== 3. код =="
if [ ! -d "$APP/.git" ]; then
  git clone https://github.com/cotpowet61-sys/TradingAgents.git "$APP"
fi
cd "$APP"
git fetch origin
git checkout arena/01a09056-tradingagents
git reset --hard origin/arena/01a09056-tradingagents

echo "== 4. venv + зависимости =="
[ -d "$APP/venv" ] || python3.11 -m venv "$APP/venv"
"$APP/venv/bin/pip" -q install --upgrade pip
"$APP/venv/bin/pip" -q install -r "$APP/portal/requirements.txt"

echo "== 5. .env (один раз — создаём шаблон, ключи дописываете вы) =="
if [ ! -f "$APP/portal/.env" ]; then
  SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
  cat > "$APP/portal/.env" <<ENV
# --- режим продажи ---
SAAS_MODE=true
BRAND_NAME=TradeVision
PRICE_MONTH_USD=100
SITE_URL=https://your-domain.com
# --- приём оплаты (только ПУБЛИЧНЫЕ адреса! приватных ключей здесь быть НЕ ДОЛЖНО) ---
WALLET_TRC20=
WALLET_BEP20=
BSCSCAN_API_KEY=
# --- телеграм ---
TELEGRAM_URL=
TG_BOT_TOKEN=
ADMIN_TG_ID=
# --- админ API (партнёры) ---
ADMIN_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
# --- секрет сессий (сгенерирован автоматически; не терять — иначе шифртексты ключей юзеров не расшифруются) ---
PORTAL_AUTH_SECRET=$SECRET
# --- AI по умолчанию (ключи ваши, для встроенного Community AI) ---
AI_BASE_URL=https://api.justcode.lol/v1
AI_API_KEY=
# --- read-only ключи ВЛАДЕЛЬЦА для общего сканера (сигналы строятся из них) ---
BYBIT_API_KEY=
BYBIT_API_SECRET=
ENV
  chmod 600 "$APP/portal/.env"
  echo "   !!! создан $APP/portal/.env — заполните значения"
fi

echo "== 6. systemd =="
cp "$APP/portal/deploy/tradevision.service" /etc/systemd/system/
cp "$APP/portal/deploy/tv-tgbot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tradevision tv-tgbot

echo "== 7. фаервол =="
ufw allow OpenSSH >/dev/null
ufw allow 80,443/tcp >/dev/null
ufw --force enable >/dev/null

echo "== 8. Caddy (HTTPS) =="
sed -i "s/your-domain.com/${DOMAIN:-your-domain.com}/" "$APP/portal/deploy/Caddyfile.sample" || true
cp "$APP/portal/deploy/Caddyfile.sample" /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo "== 9. бэкап данных (cron 03:00) =="
cat > /etc/cron.d/tv-backup <<CRON
0 3 * * * $USER_NAME cp -a $APP/portal/data /var/backups/tv-data-\$(date +\%F) 2>/dev/null && find /var/backups -maxdepth 1 -name "tv-data-*" -mtime +14 -exec rm -rf {} \;
CRON

chown -R $USER_NAME:$USER_NAME "$APP"
echo ""
echo "✅ Готово. Осталось: заполнить $APP/portal/.env, подставить домен в"
echo "   /etc/caddy/Caddyfile, systemctl restart tradevision tv-tgbot caddy"
echo "   Логи: journalctl -u tradevision -f   |   оплаты: journalctl -u tradevision | grep billing"
