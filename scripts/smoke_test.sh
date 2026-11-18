#!/usr/bin/env bash
# End-to-end smoke test against a running gateway.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="${BASE:-http://127.0.0.1:8787}"
KEY="${VG_API_KEY:-$(cat data/api_key 2>/dev/null || true)}"
if [ -z "$KEY" ]; then
  echo "Set VG_API_KEY or create data/api_key" >&2
  exit 1
fi

echo "== health =="
curl -fsS "$BASE/healthz" | python3 -m json.tool

echo "== models =="
curl -fsS "$BASE/v1/models" -H "Authorization: Bearer $KEY" | python3 -m json.tool | head -30

echo "== create =="
JOB=$(curl -fsS -X POST "$BASE/v1/videos" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"fasth3-v2","prompt":"A red panda in a bamboo forest, cinematic light.","size":"832x480","seed":7}')
echo "$JOB" | python3 -m json.tool | head -20
ID=$(echo "$JOB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

echo "== poll $ID =="
for _ in $(seq 1 90); do
  STATE=$(curl -fsS "$BASE/v1/videos/$ID" -H "Authorization: Bearer $KEY")
  STATUS=$(echo "$STATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["internal_status"])')
  echo "$STATUS"
  case "$STATUS" in
    completed|failed|cancelled) break ;;
  esac
  sleep 2
done
echo "$STATE" | python3 -m json.tool | head -60

if [ "$(echo "$STATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')" = "completed" ]; then
  URL=$(echo "$STATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["content_url"])')
  case "$URL" in
    http://*|https://*) MEDIA="$URL" ;;
    *) MEDIA="$BASE$URL" ;;
  esac
  curl -fsS "$MEDIA" -o /tmp/video-gateway-smoke.mp4
  ls -lh /tmp/video-gateway-smoke.mp4
  echo "smoke OK"
else
  echo "smoke FAILED" >&2
  exit 1
fi
