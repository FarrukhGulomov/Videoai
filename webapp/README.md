# Video Factory — web app

A browser UI over the same pipeline `scripts/factory.py` drives. It is an
additive layer: the CLI is unchanged and still works standalone.

## Run

```bash
export FAL_KEY=...            # or put it in .env at the repo root
python3 webapp/server.py      # http://127.0.0.1:8000
```

No install step, no dependencies — stdlib only, same constraint as the CLI.
`--port` and `--host` are available.

## What it does

The main flow is four steps on one page:

1. **Describe the shot** — pick a preset, edit the text, choose aspect ratio
   and how many variants. Images are cheap, so the default is 3.
2. **Pick the starting frame** — video is always animated from a chosen
   image, never invented from text.
3. **Describe the motion** — choose model and duration, see the price, and
   confirm it.
4. **Preview and download.**

`History` collects everything generated in the workspace.

## How the money gate works

The CLI refuses to run a paid call unless `--i-approve-cost` matches the real
price. The web app keeps that property rather than dropping it for
convenience:

- `POST /api/quote` prices a call and spends nothing.
- The UI shows that number in a confirmation dialog.
- `POST /api/generate/video` requires `approved_cost` to match the
  server-computed price. A mismatch is refused and costs nothing.

The browser is never trusted to compute a price — it only echoes back the one
the server quoted.

## Security notes

- `FAL_KEY` is read server-side from the environment and never appears in any
  response. Verified: no endpoint echoes it.
- `/media/` is confined to `work/` — paths are resolved and checked against
  that root, so a crafted URL cannot read elsewhere on disk.
- Prompts are rendered as DOM text nodes, never `innerHTML`, so user text
  cannot inject markup.

## Deliberate limits

This is a **single-tenant local tool**, not a multi-user product. There is no
login, and no per-user isolation anywhere — `01-schema.sql` has no ownership
column, and RLS is enabled with no policies precisely because everything runs
through the service role. Serving this to more than one person requires a
schema migration and an auth layer first; see `docs/startup-strategy.md`.

The server uses `ThreadingHTTPServer`, which is fine for local single-user
use and demos. Putting it on the public internet would mean a real WSGI/ASGI
server behind a reverse proxy, plus the auth and per-user rate limiting that
do not exist yet.
