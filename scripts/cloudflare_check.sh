#!/usr/bin/env bash
# Verify the public Cloudflare Tunnel + Access path end to end.
#
# Auth options:
#   A) Access service token:
#        CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... \
#        VG_API_KEY=... BASE_URL=https://video.example.com scripts/cloudflare_check.sh
#   B) A browser/`cloudflared access login` token on this host:
#        BASE_URL=https://video.example.com VG_API_KEY=... \
#        USE_CLOUDFLARED_ACCESS=1 scripts/cloudflare_check.sh
#
# The script uses the API key for API routes. Media URLs are HMAC-token URLs;
# with Access enabled they also need the Access credentials, supplied here.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_URL="${BASE_URL:-}"
if [ -z "$BASE_URL" ]; then
  echo "BASE_URL is required (for example https://video.example.com)" >&2
  exit 1
fi
VG_API_KEY="${VG_API_KEY:-$(cat data/api_key 2>/dev/null || true)}"
USE_CLOUDFLARED_ACCESS="${USE_CLOUDFLARED_ACCESS:-0}"
OUT="${OUT:-/tmp/video-gateway-cloudflare.mp4}"

if [ -z "$VG_API_KEY" ]; then
  echo "VG_API_KEY is required" >&2
  exit 1
fi

access_headers=()
if [ -n "${CF_ACCESS_CLIENT_ID:-}" ] && [ -n "${CF_ACCESS_CLIENT_SECRET:-}" ]; then
  access_headers=(-H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID"
                  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET")
fi

request() {
  if [ "$USE_CLOUDFLARED_ACCESS" = "1" ]; then
    cloudflared access curl "$1" "${@:2}"
  else
    curl -fsS "$1" "${@:2}"
  fi
}

echo "== health =="
request "$BASE_URL/healthz" "${access_headers[@]}" | python3 -m json.tool

echo "== models (API key) =="
request "$BASE_URL/v1/models" "${access_headers[@]}" \
  -H "Authorization: Bearer $VG_API_KEY" | python3 -c \
  'import sys,json; d=json.load(sys.stdin); print([m["id"] for m in d["data"]])'

echo "== upload + create =="
JOB=$(request "$BASE_URL/v1/videos" "${access_headers[@]}" \
  -H "Authorization: Bearer $VG_API_KEY" \
  -F model=fasth3-v2 \
  --form-string 'prompt=The character turns toward the camera and smiles; rain and city noise.' \
  -F size=480x832 \
  -F input_reference=@tests/fixtures/anime_reference.png)
echo "$JOB" | python3 -m json.tool | head -20
ID=$(echo "$JOB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

echo "== poll $ID =="
for _ in $(seq 1 180); do
  STATE=$(request "$BASE_URL/v1/videos/$ID" "${access_headers[@]}" \
    -H "Authorization: Bearer $VG_API_KEY")
  STATUS=$(echo "$STATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["internal_status"])')
  echo "$STATUS"
  case "$STATUS" in completed|failed|cancelled) break ;; esac
  sleep 2
done
echo "$STATE" | python3 -m json.tool | head -50

if [ "$(echo "$STATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')" != "completed" ]; then
  echo "job did not complete" >&2
  exit 1
fi

CONTENT=$(echo "$STATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["content_url"])')
THUMB=$(echo "$STATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["thumbnail_url"])')
DL=$(echo "$STATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["download_url"])')

echo "== thumbnail through edge =="
request "$THUMB" "${access_headers[@]}" -o /tmp/video-gateway-thumb.jpg
file /tmp/video-gateway-thumb.jpg

echo "== range request through edge =="
request "$CONTENT" "${access_headers[@]}" -H 'Range: bytes=0-1023' -o /tmp/video-gateway-range.bin
SIZE=$(stat -c %s /tmp/video-gateway-range.bin)
[ "$SIZE" = "1024" ] || { echo "expected 1024 range bytes, got $SIZE" >&2; exit 1; }

echo "== download through edge =="
request "$DL" "${access_headers[@]}" -o "$OUT"
ls -lh "$OUT"
file "$OUT"
echo "cloudflare compatibility OK"
