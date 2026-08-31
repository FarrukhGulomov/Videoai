"""
Stdlib-only clients for three credit-top-up providers: Stripe, Payme, Click.

Confidence differs sharply between them, and that difference is important
enough to say up front rather than bury in a comment:

  STRIPE  -- implemented directly against Stripe's own public API docs
             (api.stripe.com), which were reachable while this was written.
             Checkout Sessions + webhook signature verification, both
             standard, well-documented mechanisms. Confident this is
             correct as written.

  PAYME   -- Uzbekistan's Payme Business Merchant API. Its official docs
             (developer.help.paycom.uz) are BLOCKED by this project's
             sandbox network egress policy -- every attempt to fetch them
             during development returned EGRESS_BLOCKED, not a 404 or a
             timeout. What's implemented below is reconstructed from (a)
             search-result summaries of that same documentation and (b)
             several independent open-source Payme integration libraries
             on GitHub (payme-pkg, paycom-integration-php-template, and
             others), cross-checked against each other for the method
             names, field names, and overall JSON-RPC flow, which all
             agreed. The exact numeric error codes beyond the ones
             explicitly confirmed in a search result (-31050) are this
             module's best reconstruction, not a primary-source citation.

  CLICK   -- Uzbekistan's Click Merchant API. Same situation: official
             docs (docs.click.uz) are also EGRESS_BLOCKED from this
             sandbox. Reconstructed the same way, from search summaries
             plus cross-referencing several independent open-source Click
             integration libraries that all agree on the field names and
             sign_string formula.

Payme and Click both give every merchant a "test"/sandbox mode in their
own merchant cabinet that runs a fixed sequence of calls against your
live endpoint and tells you exactly which ones fail and why -- that is
the real verification path for both integrations below, not something
achievable from this sandbox. Do not point real customer traffic at
either until that sandbox test suite passes. See PAYMENTS.md at the repo
root for the exact checklist.

Every provider here is opt-in: none of it runs unless that provider's own
env vars are set (checked by each _configured() function), the same
pattern SUPABASE_*/WEBAPP_BASIC_AUTH_* already use elsewhere in this
project. Leaving all of a provider's variables unset means /api/config
simply doesn't list it -- nothing breaks, nothing is reachable.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class PaymentError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# ================================================================= Stripe

def stripe_configured():
    return bool(os.environ.get("STRIPE_SECRET_KEY", "").strip())


def stripe_publishable_key():
    return os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()


def _stripe_encode_form(params, prefix=""):
    """Stripe's API takes application/x-www-form-urlencoded with
    PHP-style bracket nesting for nested objects/arrays (e.g.
    line_items[0][price_data][unit_amount]=500). There's no stdlib
    helper for that shape, so it's built here by hand: recursively
    flatten dicts/lists into bracket-keyed (key, value) pairs, then
    urlencode the flat list normally."""
    pairs = []
    if isinstance(params, dict):
        for k, v in params.items():
            key = f"{prefix}[{k}]" if prefix else str(k)
            pairs.extend(_stripe_encode_form(v, key))
    elif isinstance(params, list):
        for i, v in enumerate(params):
            pairs.extend(_stripe_encode_form(v, f"{prefix}[{i}]"))
    else:
        pairs.append((prefix, str(params)))
    return pairs


def create_stripe_checkout_session(amount_usd, success_url, cancel_url, client_reference_id, metadata):
    """Creates a hosted Stripe Checkout Session and returns Stripe's own
    response dict (use result["url"] to redirect the browser, result["id"]
    is the session id). Payment confirmation always comes from the
    webhook (verify_stripe_webhook below) -- a browser reaching
    success_url proves nothing on its own (closed tab, replayed URL,
    flaky network after a real payment), so the caller must never credit
    an account just because this call succeeded or because the browser
    came back."""
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise PaymentError("STRIPE_SECRET_KEY is not set.")
    if amount_usd <= 0:
        raise PaymentError("Amount must be positive.")
    params = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": client_reference_id,
        "metadata": metadata,
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": int(round(amount_usd * 100)),
                "product_data": {"name": "Video Factory credit top-up"},
            },
        }],
    }
    body = urllib.parse.urlencode(_stripe_encode_form(params)).encode()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    req = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions", data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise PaymentError(exc.read().decode(errors="replace")[:400], status=exc.code) from None
    except urllib.error.URLError as exc:
        raise PaymentError(f"Could not reach Stripe: {exc.reason}") from None


def verify_stripe_webhook(payload_bytes, sig_header, tolerance=300):
    """Verifies the Stripe-Signature header per Stripe's documented
    scheme: 't=<unix ts>,v1=<hex hmac>[,v0=...]'. The signed payload is
    the literal bytes b"{t}." + raw_body, HMAC-SHA256 with the webhook
    signing secret, hex-encoded, compared with hmac.compare_digest (never
    plain ==, which leaks timing information about how many leading bytes
    matched). Also rejects a timestamp older than `tolerance` seconds, so
    a captured request can't be replayed later. Returns the parsed event
    dict on success, or raises PaymentError."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise PaymentError("STRIPE_WEBHOOK_SECRET is not set.")
    if not sig_header:
        raise PaymentError("Missing Stripe-Signature header.")
    parts = {}
    for piece in sig_header.split(","):
        if "=" in piece:
            k, v = piece.split("=", 1)
            parts[k.strip()] = v.strip()
    ts, v1 = parts.get("t"), parts.get("v1")
    if not ts or not v1:
        raise PaymentError("Malformed Stripe-Signature header.")
    try:
        ts_int = int(ts)
    except ValueError:
        raise PaymentError("Malformed Stripe-Signature timestamp.") from None
    if abs(time.time() - ts_int) > tolerance:
        raise PaymentError("Stripe webhook timestamp is too old.")
    signed_payload = f"{ts}.".encode() + payload_bytes
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        raise PaymentError("Stripe webhook signature mismatch.")
    return json.loads(payload_bytes)


# ================================================================== Payme
#
# See the module docstring's PAYME section for the confidence caveat --
# reconstructed from search summaries + cross-referenced open-source
# integration libraries, official docs unreachable from this sandbox.

def payme_configured():
    return bool(
        os.environ.get("PAYME_MERCHANT_ID", "").strip()
        and os.environ.get("PAYME_MERCHANT_KEY", "").strip()
    )


# Standard JSON-RPC errors plus Payme's own range. -31050 is the one
# value a search result explicitly confirmed ("Incorrect order code");
# the rest of this range is this module's best reconstruction from
# cross-referenced third-party integration libraries -- verify against
# your own merchant cabinet's test suite before trusting these numbers.
PAYME_ERR_INVALID_AMOUNT = -31001
PAYME_ERR_TRANSACTION_NOT_FOUND = -31003
PAYME_ERR_CANNOT_CANCEL_COMPLETED = -31007
PAYME_ERR_CANNOT_PERFORM = -31008
PAYME_ERR_ACCOUNT_NOT_FOUND = -31050
PAYME_ERR_METHOD_NOT_FOUND = -32601
PAYME_ERR_INVALID_AUTH = -32504
PAYME_ERR_PARSE_ERROR = -32700
PAYME_ERR_SYSTEM = -32400  # e.g. USD_TO_UZS_RATE unset, or crediting the account failed

# Payme transaction states, per its documented state machine.
PAYME_STATE_CREATED = 1
PAYME_STATE_COMPLETED = 2
PAYME_STATE_CANCELLED = -1
PAYME_STATE_CANCELLED_AFTER_COMPLETE = -2


def payme_check_auth(auth_header):
    """Payme authenticates its own webhook calls to the merchant with
    HTTP Basic Auth, login "Paycom" (literal, not the merchant's own
    name) and password = the merchant's secret key from their cabinet.
    Returns True/False; never raises, so a caller can return a proper
    JSON-RPC -32504 error instead of a bare 401."""
    key = os.environ.get("PAYME_MERCHANT_KEY", "").strip()
    if not key or not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
    except Exception:
        return False
    login, _, password = decoded.partition(":")
    return hmac.compare_digest(login, "Paycom") and hmac.compare_digest(password, key)


def payme_checkout_url(order_id, amount_usd, uzs_rate, return_url=None):
    """Builds the checkout.paycom.uz pay link Payme's own docs describe:
    base64("m=<merchant_id>;ac.order_id=<order_id>;a=<amount_in_tiyin>")
    appended to the checkout host. Payme bills in UZS tiyin (1/100 of a
    so'm) -- uzs_rate converts this project's USD amount at the
    operator-configured USD_TO_UZS_RATE (see webapp/README.md; there is
    deliberately no hardcoded fallback rate here, since a stale guessed
    exchange rate would silently over- or under-charge)."""
    merchant_id = os.environ.get("PAYME_MERCHANT_ID", "").strip()
    if not merchant_id:
        raise PaymentError("PAYME_MERCHANT_ID is not set.")
    tiyin = int(round(amount_usd * uzs_rate * 100))
    params = f"m={merchant_id};ac.order_id={order_id};a={tiyin}"
    if return_url:
        params += f";c={return_url}"
    encoded = base64.b64encode(params.encode()).decode()
    return f"https://checkout.paycom.uz/{encoded}"


def payme_rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def payme_rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


# =================================================================== Click
#
# See the module docstring's CLICK section for the confidence caveat --
# same reconstruction-from-secondary-sources situation as Payme.

def click_configured():
    return bool(
        os.environ.get("CLICK_MERCHANT_ID", "").strip()
        and os.environ.get("CLICK_SERVICE_ID", "").strip()
        and os.environ.get("CLICK_SECRET_KEY", "").strip()
    )


# error=0 always means success on both prepare and complete. Negative
# codes below are Click's own conventional set, cross-referenced across
# several independent third-party integration libraries that all agree
# on them -- same "verify against your merchant cabinet's test suite"
# caveat as Payme's error constants above.
CLICK_ERR_SUCCESS = 0
CLICK_ERR_SIGN_FAILED = -1
CLICK_ERR_INVALID_AMOUNT = -2
CLICK_ERR_ALREADY_PAID = -4
CLICK_ERR_ORDER_NOT_FOUND = -5
CLICK_ERR_TRANSACTION_NOT_FOUND = -6
CLICK_ERR_FAILED_TO_UPDATE = -7
CLICK_ERR_REQUEST_FAILED = -8
CLICK_ERR_TRANSACTION_CANCELLED = -9


def click_verify_sign(fields):
    """Recomputes Click's sign_string and compares it (constant-time)
    against the one the request supplied. Formula (action=0/prepare vs
    action=1/complete take slightly different field sets, per Click's
    documented convention cross-referenced across several third-party
    integration libraries):
      prepare:  MD5(click_trans_id + service_id + SECRET_KEY + merchant_trans_id + amount + action + sign_time)
      complete: MD5(click_trans_id + service_id + SECRET_KEY + merchant_trans_id + merchant_prepare_id + amount + action + sign_time)
    `fields` is the raw POST body dict Click sent. Returns True/False."""
    secret = os.environ.get("CLICK_SECRET_KEY", "").strip()
    if not secret:
        return False
    action = str(fields.get("action", ""))
    parts = [
        str(fields.get("click_trans_id", "")),
        str(fields.get("service_id", "")),
        secret,
        str(fields.get("merchant_trans_id", "")),
    ]
    if action == "1":
        parts.append(str(fields.get("merchant_prepare_id", "")))
    parts += [str(fields.get("amount", "")), action, str(fields.get("sign_time", ""))]
    expected = hashlib.md5("".join(parts).encode()).hexdigest()
    return hmac.compare_digest(expected, str(fields.get("sign_string", "")))


def click_checkout_url(order_id, amount_usd, uzs_rate, return_url=None):
    """Builds a my.click.uz pay link. Click bills in plain UZS (not
    tiyin, unlike Payme) -- see payme_checkout_url's docstring for why
    there's no hardcoded USD->UZS fallback rate."""
    merchant_id = os.environ.get("CLICK_MERCHANT_ID", "").strip()
    service_id = os.environ.get("CLICK_SERVICE_ID", "").strip()
    if not merchant_id or not service_id:
        raise PaymentError("CLICK_MERCHANT_ID / CLICK_SERVICE_ID are not set.")
    uzs = round(amount_usd * uzs_rate, 2)
    query = {
        "service_id": service_id,
        "merchant_id": merchant_id,
        "amount": uzs,
        "transaction_param": order_id,
    }
    if return_url:
        query["return_url"] = return_url
    return f"https://my.click.uz/services/pay?{urllib.parse.urlencode(query)}"
