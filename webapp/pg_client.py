"""
Self-hosted Postgres client -- an alternative to supabase_client.py for
installs that want their own Postgres server instead of paying for
Supabase. Activates when DATABASE_URL is set (and takes precedence over
SUPABASE_URL if both are somehow set -- see PROPOSED_CHANGES.md for the
webapp/server.py wiring that picks one).

Deliberately mirrors supabase_client.py's exact function names and return
shapes (configured, sign_up, sign_in, get_user, get_balance, record_spend,
and an exception class literally named SupabaseError) so that whichever
module server.py imports as `db`, every existing call site -- `db.configured()`,
`db.sign_up(...)`, `except db.SupabaseError`, etc. -- keeps working
unmodified. This is the one place this project accepts a non-stdlib
dependency: Python's stdlib has no Postgres wire-protocol client, and
reimplementing one from scratch is a much larger and riskier undertaking
than depending on psycopg, the standard, well-maintained driver for this.
See webapp/requirements-postgres.txt -- only needed in this mode; the
single-tenant and Supabase paths remain 100% dependency-free.

Auth model (deliberately simple, no external identity provider):
  - Passwords hashed with hashlib.pbkdf2_hmac (stdlib, no extra dependency
    for hashing itself) -- format "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>".
  - Signing in issues an opaque random bearer token (secrets.token_urlsafe);
    only its sha256 is stored (public.sessions.token_hash), so a stolen
    database dump alone can't be replayed as a live session.
  - No refresh-token rotation, no email verification, no password reset
    flow yet -- matches the scope actually asked for (self-hosted Postgres,
    own auth, Google sign-in deferred). Session lifetime is long (30 days)
    specifically because there is no refresh flow to silently extend it.
"""

import hashlib
import hmac
import os
import secrets
import time

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


class SupabaseError(Exception):
    """Named to match supabase_client.SupabaseError on purpose -- see the
    module docstring above. server.py catches `db.SupabaseError` without
    knowing which backend module `db` actually is."""
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


SESSION_TTL_SECONDS = 30 * 24 * 3600
PBKDF2_ITERATIONS = 200_000


def configured():
    return bool(os.environ.get("DATABASE_URL", "").strip())


def _require_psycopg():
    if psycopg is None:
        raise SupabaseError(
            "DATABASE_URL is set but psycopg is not installed -- run "
            "`pip install -r webapp/requirements-postgres.txt`."
        )


def _connect():
    _require_psycopg()
    try:
        return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    except psycopg.Error as exc:
        raise SupabaseError(f"Could not reach the database: {exc}") from None


# ------------------------------------------------------------ passwords

def _hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password, encoded):
    try:
        algo, iterations, salt_hex, hash_hex = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def _new_token():
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash


# ---------------------------------------------------------------- auth

def sign_up(email, password):
    password_hash = _hash_password(password)
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("select 1 from public.users where email = %s", (email,))
            if cur.fetchone():
                raise SupabaseError("An account with this email already exists.")
            cur.execute(
                "insert into public.users (email, password_hash) values (%s, %s) returning id",
                (email, password_hash),
            )
            user_id = cur.fetchone()["id"]
            token, token_hash = _new_token()
            expires_at = time.time() + SESSION_TTL_SECONDS
            cur.execute(
                "insert into public.sessions (token_hash, user_id, expires_at) "
                "values (%s, %s, to_timestamp(%s))",
                (token_hash, user_id, expires_at),
            )
    except psycopg.Error as exc:
        raise SupabaseError(f"Could not create account: {exc}") from None
    return {
        "access_token": token,
        "expires_in": SESSION_TTL_SECONDS,
        "user": {"id": str(user_id), "email": email},
    }


def sign_in(email, password):
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("delete from public.sessions where expires_at < now()")
            cur.execute(
                "select id, password_hash from public.users where email = %s", (email,)
            )
            row = cur.fetchone()
            if not row or not _verify_password(password, row["password_hash"]):
                raise SupabaseError("Incorrect email or password.")
            user_id = row["id"]
            token, token_hash = _new_token()
            expires_at = time.time() + SESSION_TTL_SECONDS
            cur.execute(
                "insert into public.sessions (token_hash, user_id, expires_at) "
                "values (%s, %s, to_timestamp(%s))",
                (token_hash, user_id, expires_at),
            )
    except psycopg.Error as exc:
        raise SupabaseError(f"Could not sign in: {exc}") from None
    return {
        "access_token": token,
        "expires_in": SESSION_TTL_SECONDS,
        "user": {"id": str(user_id), "email": email},
    }


def get_user(access_token):
    """Look up the session by token, or None if it's missing, expired, or
    invalid -- mirrors supabase_client.get_user()'s contract of never
    raising, just returning None, so _current_user() in server.py doesn't
    need to know which backend it's calling."""
    if not access_token or psycopg is None:
        return None
    token_hash = hashlib.sha256(access_token.encode()).hexdigest()
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select u.id, u.email from public.sessions s "
                "join public.users u on u.id = s.user_id "
                "where s.token_hash = %s and s.expires_at > now()",
                (token_hash,),
            )
            row = cur.fetchone()
    except (SupabaseError, psycopg.Error):
        return None
    return {"id": str(row["id"]), "email": row["email"]} if row else None


# ------------------------------------------------------------- credits

def get_balance(user_id, access_token=None):
    """access_token is accepted (unused) only to keep the same call
    signature as supabase_client.get_balance() -- self-hosted mode has no
    per-request RLS token to check; the caller has already been
    authenticated via get_user() before this is reached (see
    _owner_id()/_auth_me() in webapp/server.py)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select balance_usd from public.credits where user_id = %s", (user_id,)
        )
        row = cur.fetchone()
    return float(row["balance_usd"]) if row else 0.0


def record_spend(user_id, delta_usd, note=None):
    """Move a user's balance by delta_usd and append the ledger row that
    explains why -- a single upsert statement, unlike
    supabase_client.record_spend()'s read-then-write over two HTTP calls,
    so this version is also a fix for the "not atomic" limitation that
    module's docstring documents (real here because a direct Postgres
    connection can do it in one round trip; PostgREST can't)."""
    reason = "manual_topup" if delta_usd > 0 else "generation"
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "insert into public.credit_ledger (user_id, delta_usd, reason, note) "
                "values (%s, %s, %s, %s)",
                (user_id, delta_usd, reason, note),
            )
            cur.execute(
                "insert into public.credits (user_id, balance_usd) "
                "values (%(user_id)s, greatest(0, %(delta)s)) "
                "on conflict (user_id) do update "
                "set balance_usd = greatest(0, public.credits.balance_usd + %(delta)s), "
                "    updated_at = now() "
                "returning balance_usd",
                {"user_id": user_id, "delta": delta_usd},
            )
            new_balance = float(cur.fetchone()["balance_usd"])
    except psycopg.Error as exc:
        raise SupabaseError(f"Could not record credit change: {exc}") from None
    return new_balance
