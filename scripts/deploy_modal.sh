#!/usr/bin/env bash
# Deploy the Modal worker and prepare weights. Requires Modal credentials.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
if [ -z "${MODAL_ENVIRONMENT:-}" ]; then
  unset MODAL_ENVIRONMENT
fi

MODAL_BIN="${MODAL_BIN:-}"
if [ -z "$MODAL_BIN" ] && [ -x .venv/bin/modal ]; then
  MODAL_BIN=".venv/bin/modal"
fi
if [ -z "$MODAL_BIN" ]; then
  MODAL_BIN="$(command -v modal || true)"
fi
if [ -z "$MODAL_BIN" ] || [ ! -x "$MODAL_BIN" ]; then
  echo "modal CLI not found; run: .venv/bin/pip install 'modal==1.6.4'" >&2
  exit 1
fi
if ! "$MODAL_BIN" token info >/dev/null 2>&1; then
  echo "No Modal credentials. Run: $MODAL_BIN token set --token-id ... --token-secret ..." >&2
  exit 1
fi

echo "== Prepare model weights (CPU only, no GPU) =="
"$MODAL_BIN" run worker/modal_app.py --action prepare

echo "== Deploy GPU function =="
"$MODAL_BIN" deploy worker/modal_app.py

echo "== Status =="
"$MODAL_BIN" run worker/modal_app.py --action status

echo
echo "Next: set VIDEO_BACKEND=modal in .env and restart the gateway."
