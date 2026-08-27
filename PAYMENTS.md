# Payments — setup and verification checklist

This project can accept credit top-ups via **Stripe**, **Payme**, and
**Click**. The code lives in `webapp/payments.py` (provider clients) and
`webapp/server.py` (`_create_topup`, the Payme/Click method handlers, and
the webhook endpoints). This document is the checklist for turning each
provider on for real, and it's honest about which parts are confirmed vs.
which parts still need your own testing.

No live credentials existed for any provider when this was built, so
nothing here has been exercised against a real payment. Read the
"Verification status" note for each provider before trusting it with real
money.

## Stripe

**Verification status: implemented against Stripe's own, reachable API
docs** (`https://stripe.com/docs/api`, `https://stripe.com/docs/webhooks`).
The Checkout Sessions flow and webhook signature scheme are stable, widely
used, and unlikely to have changed in ways that would break this. Still
worth a real test run before going live — untested code is untested code —
but there's no doc-access gap behind this one.

Setup:
1. Create a Stripe account, or use an existing one's **test mode** first.
2. `https://dashboard.stripe.com/apikeys` → copy the **Secret key**
   (`sk_test_...`) into `STRIPE_SECRET_KEY`, and the **Publishable key**
   into `STRIPE_PUBLISHABLE_KEY` (not currently used by the server, kept
   for a future client-side Stripe.js integration if ever needed).
3. `https://dashboard.stripe.com/webhooks` → **Add endpoint** →
   `https://<your-domain>/api/topup/stripe/webhook` → select the
   `checkout.session.completed` event → copy the **Signing secret**
   (`whsec_...`) into `STRIPE_WEBHOOK_SECRET`.
4. Test with a real checkout using Stripe's documented test card
   (`4242 4242 4242 4242`, any future expiry, any CVC) and confirm the
   balance updates after redirect.
5. Switch `STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET` to live-mode values
   only once step 4 works end to end.

## Payme

**Verification status: NOT confirmed against Payme's official docs.**
Payme's merchant API documentation (`developer.help.paycom.uz`) was
unreachable from the sandbox this was built in, so the JSON-RPC method
names, parameter shapes, and error-code numbers in `webapp/payments.py`
and `webapp/server.py`'s `_payme_*` functions were reconstructed from
public write-ups and a reference open-source integration
(`PayTechUz/payme-pkg` on GitHub) and cross-checked for consistency across
sources. The overall shape (JSON-RPC 2.0, HTTP Basic Auth with a fixed
`Paycom` login, `CheckPerformTransaction` → `CreateTransaction` →
`PerformTransaction` state machine, amounts in tiyin) is very likely
right — that structure is consistent everywhere it was described. What
is **not** independently confirmed:

- The exact numeric error codes in `payments.py` (`PAYME_ERR_*`).
- Whether `GetStatement`'s response shape matches what Payme's merchant
  cabinet actually expects.
- Edge cases in `CancelTransaction` after a completed payment (state
  `-2` vs `-1` — implemented per the docs found, unverified).

Before going live:
1. Register as a Payme merchant at `https://business.payme.uz`, get
   `PAYME_MERCHANT_ID` and `PAYME_MERCHANT_KEY` from the project's API
   settings.
2. Set `USD_TO_UZS_RATE`.
3. Use Payme's own merchant-cabinet **test/sandbox mode** (their cabinet
   has one) to run a full test transaction end to end, and read the actual
   `developer.help.paycom.uz` docs yourself at that point — they were not
   reachable while writing this code, but you may well have normal access.
4. Fix anything that doesn't match what Payme's sandbox actually sends —
   the `_payme_*` functions in `webapp/server.py` are the only place that
   needs to change.
5. Only then take real payments.

## Click

**Verification status: NOT confirmed against Click's official docs**, for
the same reason as Payme — `docs.click.uz` was unreachable from this
sandbox. The `_click_*` functions in `webapp/server.py` and
`click_verify_sign`/`click_checkout_url` in `webapp/payments.py` were
reconstructed from public write-ups and a reference integration
(`click-llc/click-integration-php` on GitHub). The overall shape (a
`prepare` call, then a `complete` call, MD5 `sign_string` verification,
amounts in plain UZS) is corroborated across sources. What is **not**
independently confirmed:

- The exact request body format — some references show JSON, others
  form-urlencoded. `_parse_click_body` accepts either, but this hasn't
  been tested against Click's real servers.
- The exact field names/order in the `sign_string` MD5 hash.
- The exact numeric error codes in `payments.py` (`CLICK_ERR_*`).

Before going live:
1. Register as a Click merchant at `https://my.click.uz`, get
   `CLICK_MERCHANT_ID`, `CLICK_SERVICE_ID`, `CLICK_SECRET_KEY`.
2. Set `USD_TO_UZS_RATE`.
3. Use Click's own test/sandbox tooling to run a full prepare + complete
   cycle, reading `docs.click.uz` directly at that point.
4. Fix anything that doesn't match — the `_click_*` functions in
   `webapp/server.py` are the only place that needs to change.
5. Only then take real payments.

## General notes that apply to all three

- **Idempotency**: `_credit_topup()` in `webapp/server.py` only credits a
  topup once (checked under a lock), and rolls the status back to
  "pending" if the credit itself fails (e.g. Supabase hiccup), so a
  provider's webhook retry gets another chance rather than being silently
  swallowed. This part of the design is provider-agnostic and doesn't
  depend on any of the doc-access gaps above.
- **The browser is never trusted to report a payment succeeded.** Only a
  provider's own webhook call — verified via signature (Stripe), HTTP
  Basic Auth (Payme), or `sign_string` (Click) — can credit an account.
  The `?topup=success` redirect the browser sees is purely a UI courtesy
  (see `handleTopupRedirect()` in `webapp/static/app.js`); it just
  refreshes the displayed balance, it never grants credit itself.
- Local storage: top-ups persist to `webapp_topups.jsonl` next to the
  existing `webapp_jobs.jsonl`, same append-only pattern, so a server
  restart doesn't lose in-flight top-up state.
