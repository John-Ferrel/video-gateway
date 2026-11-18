#!/usr/bin/env bash
set -euo pipefail
: "${VIDEO_GATEWAY_BASE_URL:?set VIDEO_GATEWAY_BASE_URL}"
: "${VIDEO_GATEWAY_API_KEY:?set VIDEO_GATEWAY_API_KEY}"
: "${REF_A:?set REF_A}"; : "${REF_B:?set REF_B}"
curl -sS "$VIDEO_GATEWAY_BASE_URL/v1/videos" \
  -H "Authorization: Bearer $VIDEO_GATEWAY_API_KEY" \
  -F model=fasth3-omniref \
  --form-string 'prompt=<Picture 1> and <Picture 2> walk together through a neon-lit market in the rain. Keep both characters distinct; city ambience and soft rain.' \
  -F size=auto -F seconds=10 \
  -F "reference_images=@${REF_A}" \
  -F "reference_images=@${REF_B}"
