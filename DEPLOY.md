# Deploying Video Factory

This covers putting `webapp/server.py` on a real server, reachable from
outside your own machine. Local use (`python3 webapp/server.py`) needs
none of this — see `webapp/README.md`.

## 1. Required environment

| Variable | Required | Purpose |
|---|---|---|
| `FAL_KEY` | Yes | fal.ai API key. Without it the server boots but every generation fails. |
| `WEBAPP_BASIC_AUTH_USER` / `WEBAPP_BASIC_AUTH_PASS` | Recommended for single-tenant mode | Gates every request behind HTTP Basic Auth. See webapp/README.md's "Access control". |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | Optional | Switches to real multi-user mode (per-account login, credit balances). See webapp/README.md. |

At least one of **Basic Auth** or **Supabase multi-user mode** should be
set before this is reachable from outside your own network — plain
single-tenant mode with neither has no access control at all, and the
server prints a startup warning if it detects that combination on a
non-local bind address.

## 2. Run it: Docker (recommended)

```bash
docker build -t videofactory .
docker run -d \
  --name videofactory \
  -p 8000:8000 \
  -e FAL_KEY=your-fal-key \
  -e WEBAPP_BASIC_AUTH_USER=admin \
  -e WEBAPP_BASIC_AUTH_PASS='a long random password' \
  -v videofactory_work:/app/work \
  videofactory
```

- The `-v` volume is where generated media, job history
  (`webapp_jobs.jsonl`), and the generation ledger live — mount it so a
  redeploy doesn't lose history.
- The image installs `ffmpeg` (needed for subtitle burn-in and duration
  probing) and runs as a non-root user; nothing else is installed — this
  project is stdlib-only Python, so there's no `pip install` step.
- **Note:** this Dockerfile was written and the exact command it runs
  (`python3 webapp/server.py --host 0.0.0.0 --port 8000`) was verified
  directly on the host, but the image itself has not been built inside a
  container in this environment (no Docker daemon was available here).
  Build and smoke-test it (`curl localhost:8000/api/health`) once in your
  own environment before relying on it.

## 3. Run it: systemd (no Docker)

```ini
# /etc/systemd/system/videofactory.service
[Unit]
Description=Video Factory
After=network.target

[Service]
Type=simple
User=videofactory
WorkingDirectory=/opt/videoai
EnvironmentFile=/opt/videoai/.env
ExecStart=/usr/bin/python3 webapp/server.py --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Bind to `127.0.0.1` here (not `0.0.0.0`) and put the reverse proxy (step 4)
in front — nothing but the proxy should be able to reach this port
directly. `.env` holds `FAL_KEY`, the Basic Auth pair, and/or the
`SUPABASE_*` values, in the same `KEY=value` format `factory.py` already
reads. Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now videofactory
sudo journalctl -u videofactory -f     # logs
```

## 4. Put a real TLS-terminating reverse proxy in front

`ThreadingHTTPServer` (what `webapp/server.py` runs) speaks plain HTTP
only — no TLS, no HTTP/2, no built-in rate limiting. Terminate TLS and
proxy to it instead of exposing its port directly. Caddy is the least
config for a standard setup:

```
# /etc/caddy/Caddyfile
your-domain.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy fetches and renews the certificate automatically. An nginx +
certbot setup works the same way if that's already your stack — the only
requirement is that `127.0.0.1:8000` (or the Docker container's published
port) is not reachable from outside except through the proxy.

Once this is in place, set the session cookie's `Secure` flag (currently
omitted because the server may be running over plain HTTP locally — see
`webapp/README.md`'s Security notes) by adding `; Secure` to
`_session_cookie_header()` in `webapp/server.py` if you're running
multi-user mode over HTTPS.

## 5. Verify after deploying

```bash
curl -u admin:yourpassword https://your-domain.com/api/health
# {"fal_key_configured": true, "ffmpeg_available": true, "auth_enabled": false}
```

- `fal_key_configured: false` → `FAL_KEY` isn't set in the running
  environment.
- `ffmpeg_available: false` → post-production (upscale/subtitles/lipsync/
  bgremove) will fail; install ffmpeg or use the provided Docker image,
  which already includes it.
- A 401 with no credentials confirms Basic Auth is actually gating the
  server, not just configured.

Then run one real, cheap generation through the UI (or `/api/generate/image`
with a 1-image count) to confirm the deployed server can actually reach
fal.ai — a firewall or egress rule blocking outbound HTTPS is the most
common deployment-specific failure mode this checklist can't catch for you.

## 6. What's already handled, what isn't

Handled by the codebase itself, not something you need to configure:

- The `--i-approve-cost` money gate (server-side, cannot be bypassed by a
  modified client — see webapp/README.md's "How the money gate works").
- Path traversal on `/media/` and static file serving (confined to `work/`
  and `webapp/static/` respectively, checked with `Path.resolve()`).
- SSRF / local-file-disclosure on every client-supplied URL (`image_url`,
  `end_image_url`, image `refs`, post-production `file_url`) — validated
  at the API boundary to be `http(s)://` (or `data:` where that's safe)
  before it reaches `ffprobe`/`ffmpeg`/`fal_upload()`.

Still a known limitation (see `webapp/README.md`'s "Deliberate limits" for
the full list) — most relevant to a real deployment:

- No atomic balance deduction in multi-user mode under concurrent
  requests from the same account (documented, low-severity for the
  expected usage pattern of one person per account).
- No per-IP or per-account rate limiting — a proxy-level rate limit
  (Caddy/nginx) is the near-term mitigation if this becomes a problem.
