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
    """Read the caller's own balance under their own RLS-scoped token --
    the 'own credit balance' select policy in 02-multiuser-schema.sql is
    what makes this safe to call with a user token instead of the service
    role key. No row yet (nothing granted) reads as a balance of 0."""
    url = f"{_base_url()}/rest/v1/credits?user_id=eq.{user_id}&select=balance_usd"
    headers = {"apikey": _anon_key(), "Authorization": f"Bearer {access_token}"}
    rows = _request("GET", url, headers)
    if rows:
        return float(rows[0]["balance_usd"])
    return 0.0


def record_spend(user_id, delta_usd, note=None):
    """Move a user's balance by delta_usd (negative to spend, positive to
    grant/refund) and append the immutable ledger row that explains why.
    Requires the service role key: credits/credit_ledger carry no
    client-writable RLS policy by design, so this is the only path able
    to move a balance at all.

    Not atomic across the read-then-write below -- acceptable for an MVP
    with admin-driven top-ups and low concurrency per user; a Postgres
    function (SECURITY DEFINER, single round trip) is the fix if this
    becomes a real race in practice."""
    key = _service_key()
    if not key:
        raise SupabaseError("SUPABASE_SERVICE_ROLE_KEY is not set -- cannot record a credit change.")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    ledger_url = f"{_base_url()}/rest/v1/credit_ledger"
    reason = "manual_topup" if delta_usd > 0 else "generation"
    _request("POST", ledger_url, headers, {
        "user_id": user_id, "delta_usd": delta_usd, "reason": reason, "note": note,
    })

    balance_url = f"{_base_url()}/rest/v1/credits?user_id=eq.{user_id}&select=balance_usd"
    rows = _request("GET", balance_url, headers)
    current = float(rows[0]["balance_usd"]) if rows else 0.0
    new_balance = max(0.0, round(current + delta_usd, 4))

    upsert_url = f"{_base_url()}/rest/v1/credits?on_conflict=user_id"
    upsert_headers = dict(headers, Prefer="resolution=merge-duplicates")
    _request("POST", upsert_url, upsert_headers, {"user_id": user_id, "balance_usd": new_balance})
    return new_balance


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
