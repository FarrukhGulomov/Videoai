"""
Thin stdlib-only client for Supabase Auth + PostgREST.

Video Factory's webapp is single-tenant by default (see README's
"Deliberate limits"). This module activates multi-user mode only when
SUPABASE_URL and SUPABASE_ANON_KEY are set in the environment --
server.py checks configured() and falls back to the original,
un-authenticated single-tenant behaviour when it's False, so an
existing local install keeps working with zero configuration changes.

No new dependency: same urllib-only constraint as scripts/factory.py.
"""

import json
import os
import urllib.error
import urllib.request


class SupabaseError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def configured():
    return bool(
        os.environ.get("SUPABASE_URL", "").strip()
        and os.environ.get("SUPABASE_ANON_KEY", "").strip()
    )


def _base_url():
    return os.environ.get("SUPABASE_URL", "").rstrip("/")


def _anon_key():
    return os.environ.get("SUPABASE_ANON_KEY", "").strip()


def _service_key():
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()


def _request(method, url, headers, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw)
            detail = parsed.get("msg") or parsed.get("message") or parsed.get("error_description") or raw.decode()[:300]
        except Exception:
            detail = raw.decode(errors="replace")[:300]
        raise SupabaseError(detail, status=exc.code) from None
    except urllib.error.URLError as exc:
        raise SupabaseError(f"Could not reach Supabase: {exc.reason}") from None


def rpc(function_name, args, use_service_key=True):
    """Call a Postgres function exposed by PostgREST at /rest/v1/rpc/<name>.
    Service-role by default -- every function this project defines under
    this path (see 04-atomic-credits.sql) is SECURITY DEFINER and mutates
    a balance, so it's never meant to be reachable with a user's own
    token."""
    key = _service_key() if use_service_key else _anon_key()
    if use_service_key and not key:
        raise SupabaseError("SUPABASE_SERVICE_ROLE_KEY is not set -- cannot call a billing function.")
    url = f"{_base_url()}/rest/v1/rpc/{function_name}"
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return _request("POST", url, headers, args)


# ---------------------------------------------------------------- auth

def sign_up(email, password):
    url = f"{_base_url()}/auth/v1/signup"
    headers = {"apikey": _anon_key(), "Content-Type": "application/json"}
    return _request("POST", url, headers, {"email": email, "password": password})


def sign_in(email, password):
    url = f"{_base_url()}/auth/v1/token?grant_type=password"
    headers = {"apikey": _anon_key(), "Content-Type": "application/json"}
    return _request("POST", url, headers, {"email": email, "password": password})


def get_user(access_token):
    """Validate a token against Supabase and return the user record, or
    None if it's missing, expired, or invalid. One network round trip --
    fine here since every caller already waits on a multi-second job."""
    if not access_token:
        return None
    url = f"{_base_url()}/auth/v1/user"
    headers = {"apikey": _anon_key(), "Authorization": f"Bearer {access_token}"}
    try:
        return _request("GET", url, headers)
    except SupabaseError:
        return None


# ------------------------------------------------------------- credits

def get_balance(user_id, access_token):
    """Read the caller's own *spendable* balance under their own
    RLS-scoped token -- balance_usd minus whatever is currently held by
    an in-flight reservation (see 04-atomic-credits.sql / webapp/ledger.py),
    since money tied up in a reservation isn't available to spend again
    until it's captured or released. The 'own credit balance' select
    policy in 02-multiuser-schema.sql is what makes this safe to call
    with a user token instead of the service role key. No row yet
    (nothing granted) reads as a balance of 0."""
    url = f"{_base_url()}/rest/v1/credits?user_id=eq.{user_id}&select=balance_usd,reserved_usd"
    headers = {"apikey": _anon_key(), "Authorization": f"Bearer {access_token}"}
    rows = _request("GET", url, headers)
    if rows:
        return float(rows[0]["balance_usd"]) - float(rows[0].get("reserved_usd") or 0)
    return 0.0


# ---------------------------------------------------------- characters

def list_characters(user_id, access_token):
    """List the caller's own characters, RLS-scoped by their own token --
    same pattern as get_balance(): the 'own characters' policy in
    02-multiuser-schema.sql is what makes this safe with a user token
    instead of the service role key."""
    url = (f"{_base_url()}/rest/v1/characters?owner_id=eq.{user_id}"
           "&select=id,name,lock_text,reference_urls,notes,created_at"
           "&order=created_at.desc")
    headers = {"apikey": _anon_key(), "Authorization": f"Bearer {access_token}"}
    return _request("GET", url, headers)


def create_character(user_id, access_token, name, lock_text, reference_urls, notes=None):
    """Insert under the caller's own token -- the 'own characters' RLS
    policy's WITH CHECK requires owner_id = auth.uid(), so this can't
    create a row for anyone else even if the caller tried to pass a
    different user_id."""
    url = f"{_base_url()}/rest/v1/characters"
    headers = {
        "apikey": _anon_key(),
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    body = {
        "owner_id": user_id, "name": name, "lock_text": lock_text,
        "reference_urls": reference_urls, "notes": notes,
    }
    result = _request("POST", url, headers, body)
    return result[0] if isinstance(result, list) and result else result


def delete_character(user_id, access_token, character_id):
    """owner_id=eq.{user_id} in the filter (on top of RLS) means this is a
    no-op, not an error, against a character that exists but belongs to
    someone else -- PostgREST just matches zero rows."""
    url = f"{_base_url()}/rest/v1/characters?id=eq.{character_id}&owner_id=eq.{user_id}"
    headers = {"apikey": _anon_key(), "Authorization": f"Bearer {access_token}"}
    _request("DELETE", url, headers)
