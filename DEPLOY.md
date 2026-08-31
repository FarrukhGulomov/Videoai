# Deploying Video Factory

This covers putting `webapp/server.py` on a real server, reachable from
outside your own machine. Local use (`python3 webapp/server.py`) needs
none of this — see `webapp/README.md`.

## 1. Required environment

| Variable | Required | Purpose |
|---|---|---|
| `FAL_KEY` | Yes | fal.ai API key. Without it the server boots but every generation fails. |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | **Required for anyone to generate anything** | Every paid endpoint (image, video, post-production, avatar, motion transfer, top-up) unconditionally requires a signed-in Supabase user — there is no single-tenant/no-login mode that can spend money any more. Without these three set, the page and job history still load, but every generation request is rejected with "Sign in to generate." See webapp/README.md's "Access control". |
| `WEBAPP_BASIC_AUTH_USER` / `WEBAPP_BASIC_AUTH_PASS` | Recommended, in addition to Supabase | An extra HTTP Basic Auth layer in front of the whole app (including the login page itself) — useful for a private beta, not a substitute for Supabase. It does **not** satisfy the sign-in requirement above; a visitor who gets past Basic Auth still has to create an account and be funded before anything paid will run. |

Deploying without `SUPABASE_*` set is only useful for demoing the UI and
job history — nobody, including you, can generate anything until those
three are set (see webapp/README.md's "Access control" and the "How the
money gate works" section). Basic Auth is optional hardening on top of
that, not an alternative to it; the server prints a startup warning if
it's bound to a non-local address with neither Basic Auth nor Supabase
configured.

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
  (`python3 webapp/server.py --host 0.0.0.0`, which listens on the `PORT`
  env var if set, else 8000) was verified directly on the host, but the
  image itself has not been built inside a container in this environment
  (no Docker daemon was available here). Build and smoke-test it
  (`curl localhost:8000/api/health`) once in your own environment before
  relying on it.

## 3. Run it: Railway

Railway builds straight from the repo's `Dockerfile` — no changes needed
to deploy this repo as-is.

1. **New Project → Deploy from GitHub repo**, pick this repo and the
   branch that has the merged changes (the repo's default branch).
   Railway detects the `Dockerfile` automatically (confirmed by the
   `railway.json` in the repo root, which pins `builder: DOCKERFILE`
   explicitly rather than relying on auto-detection).
2. **Variables tab** — add at minimum:
   - `FAL_KEY` = your real fal.ai key.
   - `WEBAPP_BASIC_AUTH_USER` / `WEBAPP_BASIC_AUTH_PASS` — set both, since
     this will be reachable from the public internet the moment it
     deploys (see "Access control" in webapp/README.md). Skip these only
     if you're setting up full Supabase multi-user mode instead.
   - Do **not** set `PORT` yourself — Railway injects it automatically,
     and `webapp/server.py` already reads it (`--host 0.0.0.0` is baked
     into the Dockerfile's `CMD`, so nothing else to configure).
3. **Settings → Networking → Generate Domain** to get a public
   `*.up.railway.app` URL (Railway terminates TLS for you here — you do
   *not* need the Caddy/nginx step below on Railway specifically).
4. **Settings → Volumes → New Volume**, mount path `/app/work`. Without
   this, job history and generated media are wiped on every redeploy. The
   Dockerfile deliberately has no `VOLUME` instruction (Railway's builder
   rejects images that declare one — "docker VOLUME ... is not
   supported, use Railway Volumes"); the mount is entirely a platform-side
   step, this one.
5. Deploy. Watch the build logs for the `pip`-free build finishing (just
   `apt-get install ffmpeg`, no dependency resolution to go wrong) and
   then `Video Factory running at http://0.0.0.0:<port>` in the runtime
   logs.
6. Verify with §6 below, using your `*.up.railway.app` URL.

## 4. Run it: systemd (no Docker)

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

## 5. Put a real TLS-terminating reverse proxy in front

Skip this section on Railway — its "Generate Domain" step already
terminates TLS for you. It applies to a bare VM/systemd deployment (§4).

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

## 6. Verify after deploying

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

`MAX_CONCURRENT_JOBS` (optional, default 8) caps how many paid
generations run at once in this process — a burst of requests beyond
that queues instead of spawning unbounded provider calls. Raise it on a
bigger instance, lower it if fal.ai rate-limits this key under load.

Then run one real, cheap generation through the UI (or `/api/generate/image`
with a 1-image count) to confirm the deployed server can actually reach
fal.ai — a firewall or egress rule blocking outbound HTTPS is the most
common deployment-specific failure mode this checklist can't catch for you.

## 7. Deploy the MCP remote endpoint (optional — needed for browser connectors)

The webapp itself is already done at this point. This section is a
*second, separate* deployment that lets Claude (claude.ai in a browser,
or Claude Desktop's Settings → Connectors) and ChatGPT connect as a
remote custom connector — the in-app "MCP" tab shows this URL to users
once it exists, and shows "not deployed yet" until it does (see
`mcp/README.md`'s "Streamable HTTP" section for the full protocol
background). Skip this section entirely if only the local
Claude Desktop/Claude Code (stdio) setup matters for now.

1. **New Service** in the same Railway project as the webapp (Railway
   project page → **+ New → GitHub Repo**, same repo, same branch). This
   reuses the same `Dockerfile`/`railway.json`, so nothing to change there.
2. **Settings → Deploy → Custom Start Command**, override it to:
   ```
   python3 mcp/server.py --http
   ```
   (No port argument — like the webapp, it reads Railway's injected
   `PORT` automatically.)
3. **Variables tab** on this *new* service — add:
   - `VIDEO_FACTORY_URL` = the webapp service's URL. Prefer Railway's
     private networking address (Settings → Networking on the webapp
     service shows it, something like `http://videofactory.railway.internal:8000`)
     over the public `*.up.railway.app` one — it's faster and doesn't
     leave the two services' traffic on the public internet.
   - `MCP_HTTP_TOKEN` = a long random secret **you generate yourself**
     (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`)
     — never reuse a value suggested to you in chat or anywhere it could
     have been logged. Treat it like a password: every caller (Claude,
     ChatGPT) must send it back as `Authorization: Bearer <token>`, and
     anyone who has it can spend real money through the webapp
     immediately, with no confirmation step. Store it in a password
     manager, not just in Railway's UI.
4. **Settings → Networking → Generate Domain** on this new service to
   get its own public `*.up.railway.app` URL (separate from the
   webapp's).
5. Deploy, then confirm it's actually enforcing the token:
   ```bash
   curl -s -X POST https://<mcp-service>.up.railway.app/ \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
   # expect: 401 (no Authorization header sent)
   curl -s -X POST https://<mcp-service>.up.railway.app/ \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer <the MCP_HTTP_TOKEN you set>" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
   # expect: 200 with a JSON-RPC result
   ```
6. **Back on the webapp service's own Variables tab**, add:
   - `MCP_PUBLIC_URL` = `https://<mcp-service>.up.railway.app` (the new
     service's URL from step 4, no trailing slash). This is the one
     variable that makes the in-app "MCP" tab actually display the
     address instead of "not deployed yet" — it does nothing on its
     own until this is set.
   - The webapp redeploys automatically when a variable changes; reload
     the "MCP" tab afterward to confirm the address now appears.

The `MCP_HTTP_TOKEN` value itself is never shown anywhere in the UI —
by design, since anyone with it can spend money unattended (see
`mcp/README.md`'s "The money gate, one more time"). Give it out of
band, to people you trust with that, the same way you'd share any other
production secret.

## 8. What's already handled, what isn't

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
