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

### Access control (recommended before exposing this on a real server)

Single-tenant mode (the default — no `SUPABASE_*` set) has no login at all;
anyone who can reach the port can generate against your `FAL_KEY` and read
your job history. Set both of these to require HTTP Basic Auth on every
request:

```bash
export WEBAPP_BASIC_AUTH_USER=admin
export WEBAPP_BASIC_AUTH_PASS='a long random password'
```

Off by default (leave both unset for local use — identical to before this
existed). The server prints a warning at startup if it's bound to a
non-local address with neither Basic Auth nor multi-user mode configured.
Multi-user mode has its own per-account auth and doesn't need this, but
Basic Auth is honoured on top of it either way if both are set.

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

#### Google sign-in (optional, needs multi-user mode)

The UI shows a "Continue with Google" button automatically whenever
`SUPABASE_URL`/`SUPABASE_ANON_KEY` are set (`/api/health`'s `oauth` field).
Clicking it navigates to Supabase's own `/auth/v1/authorize?provider=google`
and redirects back to the app with a session — verified server-side by
`_auth_oauth_callback` (`webapp/server.py`) before a cookie is ever set,
the same as every other sign-in path.

The button appearing does **not** mean Google sign-in actually works yet —
that also requires, done once in the Supabase dashboard:

1. A Google Cloud OAuth client (Console → APIs & Services → Credentials →
   "OAuth client ID", type Web application), with your Supabase project's
   callback URL (`https://<ref>.supabase.co/auth/v1/callback`) added as an
   authorized redirect URI.
2. That client's ID and secret entered under Supabase → Authentication →
   Providers → Google, toggled on.
3. Your app's real URL added to Supabase → Authentication → URL
   Configuration → Redirect URLs, or the callback is rejected.

None of this can be done from the codebase or by Claude — it's an account
-level setup step for whoever owns the Supabase project.

#### Telegram sign-in

Not built yet. It needs a real Telegram bot (created via @BotFather) before
any code can be written against it — the bot's username and token are
required to render the Telegram Login Widget and verify its signed payload.
Ask for this once you have a bot; there is no placeholder UI for it here on
purpose, since a login button that doesn't work is worse than no button.

### Language

Uzbek, Russian, and English, switchable from the top bar
(`webapp/static/i18n.js`). Auto-detected from the browser's language on
first visit, remembered after in `localStorage`. AI-facing prompt text
(what's actually sent to fal.ai) stays in English regardless of UI
language — every model this project uses was verified against English
prompts, and translating the *generation* text is a different, unverified
claim from translating the *interface* around it.

## What it does

Deliberately hides the technical decisions (which model, what resolution,
how many variants) behind sensible defaults — the target user for this UI
is anyone, including someone who has never generated an AI video before.
The CLI (`scripts/factory.py`) is where those choices are still exposed
for anyone who wants them.

The main flow is four steps on one page:

1. **Describe the shot** — pick a preset or write your own. Five variants
   are always generated (best-of-5, see `fal-master-prompt.md` 2.1) and the
   model/aspect ratio are the server's own defaults — no picker for either.
2. **Pick the starting frame** — video is always animated from a chosen
   image, never invented from text.
3. **Describe the motion** (optional) — pick Short/Medium/Long instead of
   typing a number of seconds, see the price, confirm it.
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
  them. `SUPABASE_URL` and `SUPABASE_ANON_KEY` **do** appear in
  `/api/health`'s `oauth` field when multi-user mode is on — deliberately:
  the anon key is the same publishable key already used for email/password
  signup, and the browser needs both to build the Google OAuth redirect
  itself.
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
- **Every client-supplied URL that reaches disk or a subprocess is validated
  at the API boundary.** `image_url`, `end_image_url`, each `refs` entry
  (image generation), and `file_url` (post-production) must be `http://`,
  `https://`, or (for image refs only) a self-contained `data:` URI — never
  a local path. Without this a request could hand `resolve_image()` /
  `fal_upload()` a path like `../.env` and have the server upload that file's
  *contents* to fal.ai storage on the caller's behalf, or hand
  `probe_duration()` an internal/file URL for `ffprobe`/`ffmpeg` to fetch
  (SSRF). `_require_public_url()` in `webapp/server.py` is the one place
  this is enforced; every endpoint that accepts a URL calls it before the
  value goes anywhere near `factory.py`.
- **The subtitles `style` param is checked against an allowlist**
  (`SAFE_FFMPEG_STYLE` in `webapp/server.py`) before being embedded in the
  `ffmpeg -vf "subtitles=...:force_style='{style}'"` argument. It's
  unescaped in that filtergraph, so a stray `'` could otherwise break out of
  the quoted literal and inject extra filtergraph directives — checked
  against a whitelist rather than escaped, since ffmpeg's own filtergraph
  escaping rules are easy to get subtly wrong.
- **Background job threads catch `SystemExit`, not just `Exception`.**
  `factory.py`'s CLI-oriented `die()` (reused here for payload validation,
  e.g. an end-frame on a model that doesn't support one) calls `sys.exit()`,
  which raises `SystemExit` — a `BaseException`, not an `Exception`. Before
  this fix, hitting `die()` from inside `_run_image_job` / `_run_video_job`
  / `_run_postprod_job` silently killed the daemon thread and left the job
  stuck at `status="running"` forever with nothing shown to the user. All
  three now catch `(Exception, SystemExit)` and resolve to `status="error"`.

## Deliberate limits

Multi-user mode is genuinely multi-tenant (per-user auth, per-user job
history, per-user credit balance, real RLS on every table) but it is still
an MVP, not a finished product:

- **No self-serve payments.** Credits are granted by an admin running
  `supabase_client.record_spend()` or a SQL insert by hand. See
  `docs/startup-strategy.md` for the phased plan toward Payme/Click.
- **No atomic balance updates, in two places.** `record_spend()` reads the
  balance then writes it back (two round trips, not one SQL statement), and
  separately `_require_funded_user()` checks the balance *before* a
  generation runs while the actual deduction happens after it succeeds —
  two concurrent requests from the same account can both pass the
  pre-check against the same starting balance and spend past it. Fine for
  an MVP's low concurrency per user; the real fix for both is the same one:
  a single `SECURITY DEFINER` Postgres function that checks-and-deducts
  atomically in one round trip, called instead of the current
  read-then-write in `supabase_client.py`.
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
  behavior is unchanged. It also has no login of its own — see "Access
  control" above before putting it on a public server.
