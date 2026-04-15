#!/bin/bash
# setup_servidor.sh — Ejecutar una sola vez en el servidor Ubuntu 22.04
# Uso: bash setup_servidor.sh

set -e
echo "🚀 Configurando servidor Flipazo..."

# ── 1. Sistema ────────────────────────────────────────────────────
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git curl

# Dependencias de sistema para Playwright/Chromium
apt-get install -y \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2

# ── 2. Usuario dedicado (no correr como root) ─────────────────────
useradd -m -s /bin/bash flipazo || echo "Usuario flipazo ya existe"
mkdir -p /home/flipazo/app

# ── 3. Copiar archivos ────────────────────────────────────────────
# (ejecutar desde el directorio donde está el código)
cp flipazo_main.py  /home/flipazo/app/
cp .env             /home/flipazo/app/
cp requirements.txt /home/flipazo/app/
cp -r analytics     /home/flipazo/app/
cp -r affiliate     /home/flipazo/app/
chown -R flipazo:flipazo /home/flipazo/app

# ── 4. Entorno Python ─────────────────────────────────────────────
su - flipazo -c "
    cd /home/flipazo/app
    python3 -m venv venv
    venv/bin/pip install --upgrade pip
    venv/bin/pip install -r requirements.txt
"

# ── 5. Playwright + Chromium ──────────────────────────────────────
su - flipazo -c "
    /home/flipazo/app/venv/bin/playwright install chromium
    /home/flipazo/app/venv/bin/playwright install-deps chromium
"

# ── 6. Systemd services ───────────────────────────────────────────
cp flipazo.service           /etc/systemd/system/flipazo.service
cp flipazo_analytics.service /etc/systemd/system/flipazo-analytics.service
systemctl daemon-reload
systemctl enable flipazo flipazo-analytics
systemctl start  flipazo flipazo-analytics

echo ""
echo "✅ Instalación completada"
echo ""
echo "Comandos útiles:"
echo "  Pipeline:    journalctl -u flipazo -f"
echo "  Analytics:   journalctl -u flipazo-analytics -f"
echo "  Estado:      systemctl status flipazo flipazo-analytics"
echo "  Reiniciar:   systemctl restart flipazo flipazo-analytics"
echo ""
echo "Endpoints analytics (puerto 8080):"
echo "  Redirect:    http://<IP>:8080/r/{deal_id}?canal=telegram"
echo "  Stats deal:  http://<IP>:8080/stats/{deal_id}"
echo "  Stats global:http://<IP>:8080/stats"
echo "  Health:      http://<IP>:8080/health"
