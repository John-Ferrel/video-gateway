# Operations

## Reference deployment

- Gateway: systemd user unit `video-gateway`, active, `127.0.0.1:8787`.
- Worker: Modal app `video-gateway-worker`, deployed on `RTX-PRO-6000`.
- Models: `video-gateway-models` Volume contains FastH3 V2 QAD (63.3 GB),
  OmniRef NVFP4 QAD (108.6 GB) and the hidden Trim NVFP4 (35.4 GB). The
  original MiniMax H3 FL2VA investigation files (~144 GB) are also present but
  are experimental and not served. The `video-gateway-cache` Volume holds
  FlashInfer JIT/autotune state and exact prompt embeddings.
- Public hostname: configure your own hostname; examples use
  `https://video.example.com` and route it through Cloudflare Access.
- API key: `data/api_key` (0600); never committed. For multiple libraries, set
  `API_KEYS=key1,key2` in `.env`; the first key is admin and each slot has its
  own videos/batches while budget and Usage are shared. Keep slot order stable.
- Billing: when the Modal plan exposes `Workspace.billing.summary()`, the
  dashboard and `/usage` show official metered usage; otherwise the gateway
  labels its conservative ledger as `estimated`.
- Quality-first measurements: V2 768p 10 s I2V 191 s/$0.249/68.5 GiB,
  15 s 255 s/$0.318/71.7 GiB, first+last 5 s 58 s warm/$0.084, OmniRef 10 s
  244 s/$0.307/78.8 GiB, OmniRef 15 s 353 s/$0.426/85.2 GiB. Total official
  metered $8.23 for the whole exercise; the $28 soft budget was not changed.
  See `docs/BENCHMARK.md`.

A browser Cloudflare Access login is interactive, so a scripted edge check
needs either an Access service token or a `cloudflared access login` session.
The check script creates one real 480p job; run it only when you are ready to
spend GPU time. Prompt enhancement is optional. The default example is the
OpenCode Go OpenAI-compatible route:

```bash
H3_PROMPT_ENHANCER_BASE_URL=https://opencode.ai/inference/go/openai/v1
H3_PROMPT_ENHANCER_MODEL=deepseek-v4.1-flash
H3_PROMPT_ENHANCER_API_KEY=<key>
# optional; OpenCode Go requires the `x-opencode-session` header
H3_PROMPT_ENHANCER_OPENCODE_SESSION=ses_...
```

Any other OpenAI-compatible endpoint (including CLIProxyAPI) works with the
same three variables. Without a key/session the UI disables the checkbox and
the API uses the prompt as written.

To finish the public end-to-end check, either:

```bash
# browser/CLI session on the host
cloudflared access login https://video.example.com
BASE_URL=https://video.example.com USE_CLOUDFLARED_ACCESS=1 \
  VG_API_KEY="$(cat data/api_key)" scripts/cloudflare_check.sh
```

or create an Access service token and run:

```bash
BASE_URL=https://video.example.com \
  CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... \
  VG_API_KEY="$(cat data/api_key)" scripts/cloudflare_check.sh
```

Both paths use the gateway's Bearer key as well, so Access is not a replacement
for API authentication.


## Deployment topologies

### A. systemd user service (reference deployment)

No Docker/sudo is required.

```bash
cd /opt/video-gateway
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env: API key, VIDEO_BACKEND, Modal credentials if not using modal token set

mkdir -p ~/.config/systemd/user
cp deploy/video-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now video-gateway
systemctl --user status video-gateway
journalctl --user -u video-gateway -f
```

Run `loginctl enable-linger "$USER"` once if you want the user service to start
at boot without an interactive login. The unit restarts on failure
(`Restart=always`, `RestartSec=3`), runs from the venv, and only has write access
to `data/`.

### B. Docker Compose

```bash
docker compose up -d --build
docker compose logs -f gateway
```

The compose file binds `127.0.0.1:8787`; put TLS in front. Data is a bind mount
at `./data`.

## Public access / TLS

The gateway binds loopback by default. If you already run a named Cloudflare
Tunnel, add a new public hostname to it. Do **not** replace or restart unrelated
tunnel routes.

1. Cloudflare dashboard → **Networks → Tunnels** → the existing tunnel →
   **Public Hostname** → add e.g. `video.example.com` with service
   `http://localhost:8787`.
2. Set `PUBLIC_BASE_URL=https://video.example.com` in `.env` and restart the
   gateway, so API/media URLs are absolute.
3. Test from outside:
   `curl -fsS https://video.example.com/healthz`.

### Cloudflare Access

Protect the UI and media with a Self-hosted Access application:

- **Zero Trust → Access → Applications → Add an application → Self-hosted**
- Domain: `video.example.com`
- Policy: allow your email (one-time PIN / Google / GitHub), or a service token
  for automation.
- Session duration: e.g. 24h.

**Programmatic API.** Access is an outer identity layer; the gateway still
requires `Authorization: Bearer $VG_API_KEY`. Choose one:

- **Recommended:** create an Access **service token** and send its headers with
  API calls:
  ```bash
  curl https://video.example.com/v1/videos \
    -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
    -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
    -H "Authorization: Bearer $VG_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"prompt":"a fox in snow"}'
  ```
- Service tokens are the supported programmatic path. Do not add an
  unauthenticated bypass unless you have explicitly accepted the weaker outer
  security posture; the gateway Bearer key remains required either way.

Browser playback/download works after the Access login cookie is set; the
`<video>` and download URLs use HMAC tokens, not the API key. If API clients
need those URLs, they must include the Access service-token headers as well.

Cloudflare's Free plan allows 100 MB request bodies; the gateway caps uploads at
12 MB. Range requests are passed through and tested (`206 Partial Content`).
Cloudflare does not buffer or alter MP4 bytes; downloads are served with
`Content-Disposition: attachment` when `?download=1` is set.

Use `scripts/cloudflare_check.sh` for an end-to-end public check (health, API
key, multipart upload, Access headers, thumbnail, Range playback and download):

```bash
BASE_URL=https://video.example.com \
CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... \
VG_API_KEY="$(cat data/api_key)" \
  scripts/cloudflare_check.sh
```

If you authenticated a browser/CLI Access session on the host instead, set
`USE_CLOUDFLARED_ACCESS=1`. The script does not modify Access or the tunnel.

For personal access without a public hostname, use an SSH tunnel:
`ssh -L 8787:127.0.0.1:8787 user@203.0.113.10`, then open
<http://127.0.0.1:8787>.

If you change `HOST=0.0.0.0`, ensure the API key is strong and a firewall/TLS
proxy is in front. The gateway does not implement TLS itself.

## Modal setup

```bash
modal token set --token-id ak-... --token-secret as-...
modal profile current
```

Do not put Modal token secrets in `.env` or Git. The CLI stores them in
`~/.modal.toml` (mode 0600). If the gateway runs as the same user, it can use the
CLI profile. Alternatively set `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` in the
service environment, but a root-owned `EnvironmentFile` with 0600 is preferable
to a repo `.env`.

### Prepare weights (CPU-only, no GPU charge)

```bash
modal run worker/modal_app.py --action prepare
modal run worker/modal_app.py --action status
```

This downloads the three FastH3 checkpoints into the `video-gateway-models`
Volume. The first run may take 10–30 minutes and uses 2 CPU / 8 GiB. It is
idempotent; a `.gateway_ready.json` marker skips completed models. The GPU
worker also compiles `fastvideo-kernel` for `sm_120a` during image build; the
PyPI wheel lacks the H3 FP4 prefix-split API.

### Deploy / update the worker

```bash
modal deploy worker/modal_app.py
modal app logs video-gateway-worker --tail 100
modal run worker/modal_app.py --action status
```

A code change creates a new image layer. The model Volume is untouched. The
gateway's FunctionCall handles from before a redeploy may stop resolving; the
result Volume is still checked and the UI reports a clear failure if a result is
missing. This is why automatic retries are disabled.

### Stop the GPU app

```bash
modal app stop video-gateway-worker
```

Stopping the app does not delete Volumes or results.

## Budget reconciliation

- The UI shows `billing_source: modal` after a successful sync, otherwise
  `estimated`.
- `Sync Modal billing` calls `Workspace.billing.summary()`. If the plan exposes
  it, the gateway stores the official metered cycle cost; if not, the UI keeps
  estimates and displays the error.
- `Enter invoice total` stores the current cycle's invoice amount and switches
  the source to `manual`. Use this after checking the Modal dashboard. The
  budget guard immediately uses that number (plus active reservations).
- The gateway never changes Modal's usage limit, spend limit or plan.

Recommended monthly routine:

1. Check Modal **Usage & Billing** for the cycle.
2. Enter the invoice total in Usage → "Enter invoice total".
3. Verify `remaining_usd` is still positive; if not, the queue is blocked until
   the next cycle (or you raise `MONTHLY_SOFT_BUDGET_USD` deliberately).
4. Review `data/` size and let retention (30 days) / 20 GB cap run.

## Retention and storage

- Completed videos older than `RETENTION_DAYS` (default 30) are soft-deleted and
  their files removed by an hourly maintenance pass.
- If `data/outputs + thumbs + uploads` exceeds `MAX_STORAGE_GB` (default 20),
  the oldest completed videos are removed until under the cap.
- `POST /v1/admin/cleanup` triggers both now.
- The Modal results Volume is not automatically pruned. Results are small
  (5–15 s MP4s) and Volume storage has 1 TiB free. Add a manual prune if you run
  thousands of clips:
  `modal volume ls video-gateway-results results | ...` or a scheduled Modal
  function using `Volume.remove_file`.
- Back up `data/gateway.db` and `data/outputs` if the clips matter. SQLite WAL
  means copy all three files (`gateway.db`, `-wal`, `-shm`) or use
  `sqlite3 data/gateway.db ".backup '/backup/gateway.db'"`.

## Logs and diagnostics

```bash
journalctl --user -u video-gateway -n 200 --no-pager
curl -sS http://127.0.0.1:8787/healthz | jq
curl -sS http://127.0.0.1:8787/v1/admin/diagnostics -H "Authorization: Bearer $KEY" | jq
modal app logs video-gateway-worker --tail 200 --timestamps
```

Database inspection:

```bash
sqlite3 data/gateway.db \
  "select id,model,status,progress,cost_usd,started_at,completed_at,error from videos order by created_at desc limit 20;"
```

## Common failures

| Symptom | Likely cause | Action |
|---|---|---|
| UI says DEMO | `VIDEO_BACKEND=auto` and no Modal token | `modal token set`, set `VIDEO_BACKEND=modal`, restart |
| `worker_not_configured` 503 | Same | Same |
| Job failed with "Modal call not found" | Worker redeployed or app stopped while a call was in flight | Check result Volume; retry manually if needed |
| Job failed with "Model files missing" | `prepare` not run / wrong Volume | `modal run worker/modal_app.py --action prepare` |
| OOM in worker | OmniRef/768p with insufficient RAM, or an oversized custom geometry | Keep 96 GiB; use V2 480p, or un-hide Trim for a draft; check `modal app logs` |
| I2V fails with "No precomputed AdaLN modulation" | Packed T2V table selected for an image/reference request | The worker now skips the table for I2V/Ref2VA; redeploy the latest worker |
| `budget_exceeded` 429 | Soft cap or reservations | Wait for cycle, cancel queued jobs, or reconcile invoice |
| Video 410 | File deleted by retention/storage cap | Regenerate; increase retention/cap |
| Billing source stays estimated | The plan does not expose a billing API | Enter invoice total manually |

## Upgrades

```bash
cd /opt/video-gateway
git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
systemctl --user restart video-gateway
```

For a worker code change, run `modal deploy` after the gateway restart. The
schema is created idempotently; no external migration tool is needed.

Schema v2 adds the per-key `owner` column to `videos` and `batches`. Existing
rows are assigned to slot 0 (the first `API_KEYS` entry) automatically. Back up
`data/gateway.db` before upgrading a production instance. Keep existing key
slots in place when rotating a key value; append a new slot for a new user.

## Security checklist

- [ ] `data/api_key` is 0600, or `API_KEYS` is a long random value.
- [ ] `.env` and `data/` are not committed (`git status`).
- [ ] Modal token lives in `~/.modal.toml`, not the repo.
- [ ] The public endpoint is HTTPS via Cloudflare Tunnel/reverse proxy.
- [ ] Port 8787 is not exposed directly to the internet.
- [ ] Backups of `data/gateway.db` exist if history matters.
- [ ] `MONTHLY_SOFT_BUDGET_USD` and Modal usage limit are both set.
