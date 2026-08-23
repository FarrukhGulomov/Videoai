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

### Multi-user mode (optional)

Set `SUPABASE_URL` and `SUPABASE_ANON_KEY` (plus `SUPABASE_SERVICE_ROLE_KEY`
to allow spending) in `.env` and the app switches from single-tenant to
multi-user: a sign-in bar appears, each account gets its own job history and
credit balance, and every paid generation is billed against that balance
instead of running open-ended. Leave all three unset and the app behaves
exactly as it did before this existed — no login, one shared workspace.

1. Apply `02-multiuser-schema.sql` to your Supabase project (adds
   `owner_id` columns plus the `credits` / `credit_ledger` tables and their
   RLS policies — see that file's comments for the design).
2. Fill in the three `SUPABASE_*` values from Project Settings → API.
3. Grant a new user credit with `supabase_client.record_spend(user_id,
   +5.00, note="manual top-up")` (there is no self-serve payment flow yet —
   see "Deliberate limits" below) or an equivalent insert into
   `credit_ledger` / `credits` from the Supabase SQL editor.
4. Restart the server. `/api/health`'s `auth_enabled` flag reports whether
   the UI is in multi-user mode.

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

### Post-production — "Enhance…"

Any finished video (in step 4 or in History) has an **Enhance…** button
that opens the same four paid ops `scripts/factory.py` exposes on the CLI:

- **Upscale** — real detail-adding upscale (Topaz), priced by output tier.
- **Remove background** — cut the subject out of its plate.
- **Transcribe + burn in subtitles** — timed captions, auto-detected
  language by default.
- **Lip-sync a separate voice track** — needs a direct URL to the voice
  file; there's no upload widget for it yet, so paste a link.

Same money gate as image/video generation: a quote first, a confirmation
dialog showing the exact price, then the paid call — never the reverse.
Results land in History as an "Enhanced (op)" card once done. Multi-shot
continuity (`final --shots-json`) is a CLI-only, authoring-time decision
(you choose it before generating, not after) and isn't exposed here.

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

- `FAL_KEY` and `SUPABASE_SERVICE_ROLE_KEY` are read server-side from the
  environment and never appear in any response. Verified: no endpoint echoes
  them.
- `/media/` is confined to `work/` — paths are resolved and checked against
  that root, so a crafted URL cannot read elsewhere on disk.
- Prompts are rendered as DOM text nodes, never `innerHTML`, so user text
  cannot inject markup.
- The session cookie (`vf_session`, holding the Supabase access token) is
  `HttpOnly` and `SameSite=Lax` so page script can't read it and it isn't
  sent cross-site. It is **not** marked `Secure`, because this is still a
  local-first tool typically served over plain HTTP — add `Secure` (and put
  a real TLS-terminating proxy in front) before exposing multi-user mode on
  the public internet.
- `credits` and `credit_ledger` carry no client-writable RLS policy at all
  (see `02-multiuser-schema.sql`) — the only way to move a balance is
  `supabase_client.record_spend()` using the service role key, called
  exclusively from the server after a generation actually succeeds. A user's
  own token can only ever *read* their own balance.
- The pre-existing cost-confirmation gate is unchanged and still runs first:
  `approved_cost` must match the server-quoted price before a paid call is
  even attempted. The credit-balance check in multi-user mode is a second,
  independent gate on top of it, not a replacement.

## Deliberate limits

Multi-user mode is genuinely multi-tenant (per-user auth, per-user job
history, per-user credit balance, real RLS on every table) but it is still
an MVP, not a finished product:

- **No self-serve payments.** Credits are granted by an admin running
  `supabase_client.record_spend()` or a SQL insert by hand. See
  `docs/startup-strategy.md` for the phased plan toward Payme/Click.
- **No atomic balance updates.** `record_spend()` reads the balance, then
  writes it back — two round trips, not one SQL statement. Fine for an MVP's
  low concurrency per user; a `SECURITY DEFINER` Postgres function is the
  fix if it ever becomes a real race.
- **No password reset / email verification UI.** Whatever your Supabase
  project's auth settings do (e.g. requiring email confirmation) is what
  happens — the app surfaces Supabase's own message but adds no flow of its
  own around it.
- **`ThreadingHTTPServer`**, which is fine for local use and small-scale
  demos. Putting either mode on the public internet still wants a real
  WSGI/ASGI server behind a reverse proxy and per-user rate limiting, which
  do not exist yet.
- **Single-tenant mode is still the default.** Leave the `SUPABASE_*`
  variables unset and none of the above applies — the original one-workspace
  behavior is unchanged.
