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

**Generating anything now always requires a signed-in user** — see "Multi-user
mode" below. There is no single-tenant bypass any more: `_current_user_or_raise()` +
`_reserve_funds()` in `webapp/server.py` is the one gate every paid endpoint
(image, video, post-production, avatar, motion transfer) calls through, and
it rejects an unauthenticated
request unconditionally, whether it came from the browser UI or from the MCP
server (itself just another caller of this same HTTP API). Leaving
`SUPABASE_*` unset means nobody can generate at all, not that generation is
open to anyone — set it up before expecting this to actually work.

Reading the page and job history is still unauthenticated by default in
that case, so putting a real access gate in front is still worth doing.
Set both of these to require HTTP Basic Auth on every request:

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
instead of running open-ended. **Leave all three unset and the page and job
history still load, but nobody — including the deployment's own owner — can
generate anything**; there is no single-tenant "no login, spend for free"
mode any more (see "Access control" above and "How the money gate works"
below).

1. Apply `02-multiuser-schema.sql` to your Supabase project (adds
   `owner_id` columns plus the `credits` / `credit_ledger` tables and their
   RLS policies), then `04-atomic-credits.sql` (adds the reservation table
   and the `reserve_credit`/`capture_credit_reservation`/
   `release_credit_reservation`/`refund_credit`/`debit_credit` functions
   every paid action and top-up now goes through — see that file's
   comments for the design).
2. Fill in the three `SUPABASE_*` values from Project Settings → API.
3. Either let users top themselves up (see "Payments" below), or grant
   credit by hand with `python3 -c "import ledger; ledger.SupabaseLedger().refund('USER_ID', 5.00, 'manual:some-unique-key', note='manual top-up')"`
   (run from `webapp/`, with `SUPABASE_*` set in the environment) or an
   equivalent insert into `credit_ledger` / `credits` from the Supabase
   SQL editor.
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

### Consent, disclosure, and moderation (talking avatar / motion transfer)

Both features that animate a photo of a specific person — the talking
avatar and motion transfer — require an explicit consent attestation
before they'll run at all: `POST /api/avatar/run` and
`POST /api/motion-transfer/run` reject the request unless the body
includes `consent_attested: true` and `consent_type` set to `"self"` or
`"authorized"` (`_require_identity_consent` in `webapp/server.py`). The
web UI's checkbox on both panels ("This is my own photo, or I have the
pictured person's permission") sends `consent_type: "authorized"`, the
more general of the two values a single checkbox can honestly represent.
This does **not** verify the claim — it makes generating without at
least claiming consent impossible through this API, and the attestation
is stored on the job record (`consent_type`, `consent_at`) as a durable
trail to point to if a report comes in later.

Three more pieces close this out:

- **Abuse rate limit.** Avatar generation is capped separately from every
  other paid endpoint and from the money-cost gate itself
  (`AVATAR_RATE_LIMIT`, default 10/day per account, `AVATAR_RATE_WINDOW`)
  — a funded account is not, on its own, a reason to allow mass
  production of videos of other people's likeness.
- **Disclosure.** Every avatar output gets a small "AI-generated"
  watermark burned in via ffmpeg (`_burn_avatar_disclosure`) before it's
  handed back — best-effort: if the font isn't installed (see the
  Dockerfile's `fonts-dejavu-core`) or the burn fails for any reason, the
  job still completes using the original, unwatermarked output rather
  than failing a paid generation over a disclosure step, and
  `job.disclosure` records which outcome happened (`"watermarked"` or
  `"label-only"`). The UI's own on-screen disclosure text is shown either
  way.
- **Reporting.** Any gallery item can be reported — `POST
  /api/jobs/<id>/report` with a `reason`, deliberately no sign-in
  required (the person whose likeness was used without consent is often
  not the account that generated it). Reporting immediately unpublishes
  the job from the gallery and appends an entry to
  `webapp_reports.jsonl`. There is no admin review UI at this project's
  stage — an operator reviews that file directly and decides on further
  action (removing the underlying media, contacting the account, etc.).

### Payments — letting users top up their own balance

A signed-in user can add credit to their own balance from the "Top up"
button next to it, instead of you granting credit by hand. Three providers
are supported, each entirely opt-in via its own env vars (see
`.env.example`): **Stripe** (card payments, works anywhere), **Payme**, and
**Click** (the two common Uzbek payment services). The "Top up" button only
shows the providers whose env vars are actually set — `GET /api/config`'s
`topup.providers` list reflects that, so an unconfigured provider is simply
absent from the UI rather than shown broken.

How a top-up works: `POST /api/topup/create` records a pending top-up and
asks the chosen provider for a checkout URL, then the browser is sent there.
The account is **not** credited at that point — only the provider's own
webhook, called back to this server independently of the browser, credits
it (`POST /api/topup/stripe/webhook`, `/api/topup/payme`,
`/api/topup/click/prepare` + `/api/topup/click/complete`). This means a
closed tab or a cancelled payment never leaves a half-finished charge: the
balance only moves once the provider itself confirms the money arrived.
Crediting is idempotent — a provider retrying its webhook (all three do)
never double-credits.

Payme and Click bill in UZS; this project's balances are USD, so
`USD_TO_UZS_RATE` (a plain number, so'm per dollar) must be set for either
of them to work, with no built-in fallback — you set the rate, so nobody is
ever charged against a stale one baked into the code.

**Verification status**: Stripe's implementation was built against Stripe's
own, reachable API docs and its protocol is stable — reasonably safe to
trust as-is once real keys are in place. Payme and Click could not be
verified against their official docs from the environment this was built
in (both doc domains were unreachable there), so their implementations were
reconstructed from secondary sources and cross-referenced for consistency,
but are **not yet confirmed correct**. See `PAYMENTS.md` at the repo root
for exactly what to check before relying on either of them with real money.

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

- **Every paid endpoint requires a signed-in user, unconditionally.**
  `_current_user_or_raise()` + `_reserve_funds()` in `webapp/server.py` is
  the gate image, video, post-production, motion transfer, and avatar
  generation all call through, and it now rejects a request with no
  verified session no matter what mode the deployment is in — there is no
  single-tenant/LOCAL_OWNER bypass for spending money any more. This also
  covers the MCP server (`mcp/server.py`), since it's just another HTTP
  caller of this same API.
- **Security headers on every response.** `SECURITY_HEADERS` in
  `webapp/server.py` is sent from the one `_send()` method every response
  (static files, JSON, media) goes through: a strict `Content-Security-Policy`
  (`script-src`/`style-src` limited to `'self'`, no inline scripts or styles
  anywhere in `webapp/static/`), `X-Frame-Options: DENY` and
  `frame-ancestors 'none'` (clickjacking), `Referrer-Policy`,
  `Permissions-Policy`, `Cross-Origin-Opener-Policy`, and
  `Strict-Transport-Security` (a no-op over plain HTTP, so safe for local
  dev — only takes effect once served over real TLS).
- **The session cookie gets the `Secure` flag automatically over HTTPS.**
  `_is_https()` checks `X-Forwarded-Proto` (set by Railway's own proxy, or
  any real TLS-terminating proxy per DEPLOY.md §5) and adds `Secure` to
  `Set-Cookie` when true — no manual step needed once this is behind real
  TLS, unlike before.
- **Login and signup are rate-limited** (`_check_auth_rate_limit()`): 8
  attempts per 5 minutes per IP (from `X-Forwarded-For` when present, the
  raw socket address otherwise), a simple in-memory sliding window aimed at
  credential stuffing / brute force against one deployment. Resets on
  restart — deliberately not persisted, since this process is the only
  server instance (`ThreadingHTTPServer`).
- **Passwords must be at least 8 characters** (`_auth_signup`), up from 6.
- `FAL_KEY` and `SUPABASE_SERVICE_ROLE_KEY` are read server-side from the
  environment and never appear in any response. Verified: no endpoint echoes
  them. `SUPABASE_URL` and `SUPABASE_ANON_KEY` **do** appear in
  `/api/health`'s `oauth` field when multi-user mode is on — deliberately:
  the anon key is the same publishable key already used for email/password
  signup, and the browser needs both to build the Google OAuth redirect
  itself.
- `/media/` is confined to `work/` — paths are resolved and checked against
  that root, so a crafted URL cannot read elsewhere on disk. Once Supabase
  is configured it's also gated to the file's own owner (or an explicitly
  published gallery item, see "Payments"/gallery notes) — a signed-in
  user can't read another account's generated media just by knowing or
  guessing its path.
- Prompts are rendered as DOM text nodes, never `innerHTML`, so user text
  cannot inject markup.
- The session cookie (`vf_session`, holding the Supabase access token) is
  `HttpOnly` and `SameSite=Lax` so page script can't read it and it isn't
  sent cross-site, plus `Secure` automatically whenever the request arrived
  over HTTPS (see `_is_https()` above) — no manual step needed behind a
  real TLS-terminating proxy (DEPLOY.md §5) or Railway's own domain.
- `credits`, `credit_ledger`, and `credit_reservations` carry no
  client-writable RLS policy at all (see `02-multiuser-schema.sql` and
  `04-atomic-credits.sql`) — the only way to move a balance is the
  `reserve_credit`/`capture_credit_reservation`/
  `release_credit_reservation`/`refund_credit`/`debit_credit` Postgres
  functions (`webapp/ledger.py`), called exclusively from the server with
  the service role key. A user's own token can only ever *read* their own
  balance and reservation history.
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

- **Self-serve top-ups exist** (Stripe/Payme/Click, see the "Payments"
  section above) alongside an admin granting credit by hand via a SQL
  insert or `webapp/ledger.py`'s `refund()`.
- **Balance updates are now atomic.** Every paid endpoint reserves the
  cost via a `SECURITY DEFINER` Postgres function (`reserve_credit` in
  `04-atomic-credits.sql`) *before* submitting to fal.ai — the row lock
  inside that function's transaction is what makes two concurrent
  requests from the same account correctly see each other's hold instead
  of both passing a stale balance check. The reservation is captured
  (turned into an actual charge) only after a validated, non-empty
  result comes back, or released if generation fails — see
  `webapp/ledger.py`'s module docstring for the full reserve → capture /
  release flow and its idempotency-key protection against duplicate
  requests double-charging.
- **No password reset / email verification UI.** Whatever your Supabase
  project's auth settings do (e.g. requiring email confirmation) is what
  happens — the app surfaces Supabase's own message but adds no flow of its
  own around it.
- **`ThreadingHTTPServer`**, which is fine for local use and small-scale
  demos. Putting either mode on the public internet still wants a real
  WSGI/ASGI server behind a reverse proxy. Login/signup are rate-limited
  (see "Security notes"); generation endpoints and everything else are
  not yet — the money-cost gate and the mandatory-auth requirement are the
  real brakes on abuse there today, not a request-rate limit.
- **`SUPABASE_*` unset means generation doesn't work at all, not that it's
  open to anyone.** There is no single-tenant "no login" mode for spending
  money any more (see "Access control" above) — leaving these variables
  unset just means nobody, including the deployment's own owner, can
  generate anything until they're set. The page itself and job history
  still load without them; only the paid endpoints are gated.
