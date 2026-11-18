#!/usr/bin/env bash
# Idempotent, no-sudo installation as a systemd user service.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit it before exposing the service."
fi

mkdir -p data
chmod 700 data

mkdir -p "$HOME/.config/systemd/user"
sed "s#/opt/video-gateway#$ROOT#g" deploy/video-gateway.service \
  > "$HOME/.config/systemd/user/video-gateway.service"
systemctl --user daemon-reload
systemctl --user enable --now video-gateway
systemctl --user --no-pager status video-gateway || true
echo "Logs: journalctl --user -u video-gateway -f"
