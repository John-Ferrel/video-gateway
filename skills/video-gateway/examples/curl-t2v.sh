#!/usr/bin/env bash
set -euo pipefail
: "${VIDEO_GATEWAY_BASE_URL:?set VIDEO_GATEWAY_BASE_URL}"
: "${VIDEO_GATEWAY_API_KEY:?set VIDEO_GATEWAY_API_KEY}"
curl -sS "$VIDEO_GATEWAY_BASE_URL/v1/videos" \
  -H "Authorization: Bearer $VIDEO_GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"fasth3-v2","prompt":"A red panda walks through a misty bamboo forest, cinematic morning light.","size":"auto","seconds":10,"seed":42}'
