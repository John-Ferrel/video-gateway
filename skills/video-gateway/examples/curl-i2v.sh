#!/usr/bin/env bash
set -euo pipefail
: "${VIDEO_GATEWAY_BASE_URL:?set VIDEO_GATEWAY_BASE_URL}"
: "${VIDEO_GATEWAY_API_KEY:?set VIDEO_GATEWAY_API_KEY}"
: "${IMAGE:?set IMAGE to a local JPG/PNG/WEBP}"
curl -sS "$VIDEO_GATEWAY_BASE_URL/v1/videos" \
  -H "Authorization: Bearer $VIDEO_GATEWAY_API_KEY" \
  -F model=fasth3-v2 \
  --form-string 'prompt=The character turns toward the camera and smiles; rain and city ambience.' \
  -F size=auto -F seconds=10 \
  -F "input_reference=@${IMAGE}"
