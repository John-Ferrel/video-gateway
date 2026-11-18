#!/usr/bin/env bash
set -euo pipefail
: "${VIDEO_GATEWAY_BASE_URL:?set VIDEO_GATEWAY_BASE_URL}"
: "${VIDEO_GATEWAY_API_KEY:?set VIDEO_GATEWAY_API_KEY}"
: "${FIRST_IMAGE:?set FIRST_IMAGE}"
: "${LAST_IMAGE:?set LAST_IMAGE}"
curl -sS "$VIDEO_GATEWAY_BASE_URL/v1/videos" \
  -H "Authorization: Bearer $VIDEO_GATEWAY_API_KEY" \
  -F model=fasth3-v2 \
  --form-string 'prompt=The character moves continuously from the first frame to the last frame.' \
  -F size=auto -F seconds=5 \
  -F "input_reference=@${FIRST_IMAGE}" \
  -F "last_reference=@${LAST_IMAGE}"
