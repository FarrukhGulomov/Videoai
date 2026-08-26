# Proposed changes to existing files — self-hosted Postgres support

Per the expansion brief's hard constraint (§1, rule 3): every new file for
this feature has already been written (`webapp/pg_client.py`,
`webapp/requirements-postgres.txt`, `docs/expansion/03-selfhosted-schema.sql`)
with zero existing files touched. Wiring the new backend in so it's
actually reachable needs three small edits to files that already exist.
None applied yet — this file is the proposal; say which of the three (all,
some, or none) to apply.

`pg_client.configured()` (true when `DATABASE_URL` is set) takes precedence
over `supabase_client.configured()` if both env vars were somehow set at
once, so a deployment picks exactly one backend deterministically.

---

## 1. `webapp/server.py` — activate the new backend (required)

This is the only change that actually turns the new modules on. Without it,
`pg_client.py` is dead code no request path reaches.

**Current (line 41):**
```python
import supabase_client as db
```

**Proposed:**
```python
import supabase_client
import pg_client

# pg_client (self-hosted Postgres) takes precedence over supabase_client
# if DATABASE_URL is set -- a deployment should configure exactly one of
# DATABASE_URL / SUPABASE_URL, but this makes the choice deterministic
# rather than undefined if both are ever set at once.
db = pg_client if pg_client.configured() else supabase_client
```

**Current (`_health_payload`, ~line 899-915):**
```python
    def _health_payload(self):
        payload = {
            "fal_key_configured": bool(os.environ.get("FAL_KEY", "").strip()),
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
            "auth_enabled": db.configured(),
        }
        if db.configured():
            # Both values are meant to be public -- the anon key is the
            # same "publishable" key already used for signup/login, and the
            # project URL is not a secret. Exposed only so the browser can
            # build the Supabase OAuth redirect itself; SUPABASE_SERVICE_ROLE_KEY
            # never appears in any response.
            payload["oauth"] = {
                "supabase_url": os.environ.get("SUPABASE_URL", "").rstrip("/"),
                "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
            }
        return payload
```

**Proposed (only the `if` condition changes):**
```python
        if db is supabase_client and db.configured():
```

Why: the Google-sign-in button in `webapp/static/app.js` (line 203) already
does `if (!state.health || !state.health.oauth) return;` before rendering
itself — it's entirely driven by whether this field is present. Self-hosted
Postgres mode has no Supabase project to build an OAuth redirect against
(Google sign-in wasn't part of the approved scope — password auth only, for
now), so simply never adding the `oauth` key makes the button not render.
**Zero changes needed to `app.js` or `index.html`** — this one condition is
the entire integration point on the frontend side.

Every other auth call site in `webapp/server.py` (`_owner_id`, `_auth_me`,
`_auth_signup`, `_auth_login`, `_auth_logout`) calls `db.<method>(...)`
generically and needs no changes — that's the reason `pg_client.py` was
written to mirror `supabase_client.py`'s exact function names, return
shapes, and exception class name (`SupabaseError`) in the first place.

`_auth_oauth_callback` (~line 985) stays as dead code reachable only from a
Supabase-mode redirect that self-hosted installs never trigger — harmless,
not worth deleting under the "don't touch existing code you didn't add"
spirit of this whole exercise.

---

## 2. `webapp/README.md` — document how to actually turn this on (recommended)

Without this, the feature exists but nobody deploying the project would
know `DATABASE_URL` does anything. Proposed: a new subsection immediately
after the existing "Multi-user mode (optional)" section (after the
Telegram sign-in sub-section, before "### Language").

```markdown
### Self-hosted Postgres (alternative to Supabase)

If you'd rather run your own Postgres server than pay for Supabase, set
`DATABASE_URL` instead of the `SUPABASE_*` variables and the app switches
to `webapp/pg_client.py` — same multi-user behavior (per-account login,
credit balances, job history) as Supabase mode, with your own database
instead. `DATABASE_URL` takes precedence if both are somehow set.

1. Install the one extra dependency this mode needs (the rest of the
   project stays dependency-free either way):
   ```bash
   pip install -r webapp/requirements-postgres.txt
   ```
2. Apply `01-schema.sql` (repo root) to your Postgres server, then
   `docs/expansion/03-selfhosted-schema.sql` — the second file creates its
   own `users`/`sessions` tables instead of relying on Supabase's
   `auth.users`, and adds `credits`/`credit_ledger` pointing at those.
3. Set `DATABASE_URL` (a standard `postgres://user:pass@host:5432/dbname`
   connection string) and restart the server.

Passwords are hashed with PBKDF2 (stdlib `hashlib`, no extra dependency for
that part); sessions are opaque bearer tokens, only their SHA-256 stored,
not the token itself. There's no Google sign-in in this mode (no Supabase
project to authorize against) and no password-reset flow yet — both are
natural follow-ups if this becomes the primary path rather than an
alternative to Supabase mode.
```

---

## 3. `DEPLOY.md` — one line in the environment table (optional, small)

**Current (§1 "Required environment" table, the Supabase row):**
```markdown
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | Optional | Switches to real multi-user mode (per-account login, credit balances). See webapp/README.md. |
```

**Proposed (new row directly below it):**
```markdown
| `DATABASE_URL` | Optional | Alternative to the `SUPABASE_*` row above — self-hosted Postgres instead of Supabase, same multi-user behavior. Takes precedence if both are set. See webapp/README.md's "Self-hosted Postgres". |
```

---

*Nothing above has been applied. Reply with which of 1/2/3 to make live —
1 is required for the feature to do anything at all; 2 and 3 are
documentation so it's discoverable.*
