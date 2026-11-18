# Cloudflare Tunnel + Access verification

Reference deployment check, **2026-11-17**. Hostnames below are placeholders
(`video.example.com`, `your-team.cloudflareaccess.com`); substitute your own.

## What was verified

1. **Tunnel ingress** — the running `cloudflared` configuration routed the
   public hostname to the loopback gateway:

   ```
   hostname: video.example.com
   service:  http://127.0.0.1:8787
   ```

   No unrelated tunnel routes were changed.

2. **Origin reachability with the public Host header**

   ```bash
   curl -H 'Host: video.example.com' -H 'X-Forwarded-Proto: https' \
     http://127.0.0.1:8787/healthz
   # {"status":"ok", ... "backend":"modal" ...}
   ```

3. **Access is active at the edge**

   ```bash
   curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' \
     https://video.example.com/healthz
   # 302 https://your-team.cloudflareaccess.com/cdn-cgi/access/login/...
   ```

   Every path (`/`, `/healthz`, `/v1/models`, `/static/*`) redirected to the
   Access login as expected.

4. **Application compatibility through the origin** — automated tests passed:

   - Bearer API key still works when `CF-Access-Client-Id`,
     `CF-Access-Client-Secret`, `Cf-Connecting-Ip`, `X-Forwarded-Proto` and
     `X-Forwarded-Host` headers are present.
   - Multipart image upload creates a completed job.
   - `Range: bytes=0-1023` returns `206 Partial Content` with a 1024-byte body.
   - `?download=1` returns `Content-Disposition: attachment`.
   - Thumbnail and video HMAC tokens are purpose-scoped (a thumbnail token
     cannot fetch the MP4).
   - `PUBLIC_BASE_URL=https://video.example.com` produces absolute URLs; the SPA
     rewrites them to the current origin for SSH/local sessions.

   See `tests/test_cloudflare.py` and `tests/test_api.py`.

5. **No idle GPU after testing** — `modal container list` was empty. The
   deployment window reported three completed `generate` calls and zero live
   containers. Earlier failed probes are included in the cumulative billing.

## Manual authenticated check

Cloudflare Access requires a human login or a service token. An unauthenticated
edge request correctly returns `302`; this is not an application failure. To run
the authenticated public check:

```bash
# Option A: browser/CLI Access session on the gateway host
cloudflared access login https://video.example.com
BASE_URL=https://video.example.com USE_CLOUDFLARED_ACCESS=1 \
  VG_API_KEY="$(cat data/api_key)" scripts/cloudflare_check.sh

# Option B: Access service token
BASE_URL=https://video.example.com \
  CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... \
  VG_API_KEY="$(cat data/api_key)" scripts/cloudflare_check.sh
```

The script checks health, Bearer API auth, a real multipart job, thumbnail,
Range playback and attachment download through the public edge. It creates one
billable 480p job and does not modify Access or the tunnel.

## Programmatic access policy

- Access is an outer identity layer; the gateway **always** requires
  `Authorization: Bearer $VG_API_KEY`.
- Recommended: Access service-token headers plus the Bearer key.
- Service tokens are the supported path. No unauthenticated Access bypass was
  used in the reference deployment; media URLs also carry HMAC tokens.
- The API key is never placed in a media URL.
