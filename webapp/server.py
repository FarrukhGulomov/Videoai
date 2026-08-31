#!/usr/bin/env python3
"""
Video Factory — local web app.

A thin HTTP layer over scripts/factory.py. It adds no new dependencies:
stdlib only, exactly like the CLI it wraps. Every generation call goes
through the same functions the CLI uses, so the two cannot drift apart.

Two properties this layer is responsible for keeping:

  1. FAL_KEY is read server-side from the environment and never leaves
     this process. No endpoint echoes it; the browser never sees it.

  2. The CLI's --i-approve-cost gate survives the move to a UI. The
     browser asks for a quote, shows it to the user, and must echo the
     exact quoted number back before any paid call runs. A mismatch is
     refused and costs nothing.

Run:  python3 webapp/server.py [--port 8000]
"""

import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import http.cookies
import importlib.util
import ipaddress
import json
import mimetypes
import os
import pathlib
import re
import shutil
import socket
import subprocess
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ledger
import payments
import supabase_client as db

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = pathlib.Path(__file__).resolve().parent / "static"
WORK = ROOT / "work"
JOBS_FILE = WORK / "webapp_jobs.jsonl"
TOPUPS_FILE = WORK / "webapp_topups.jsonl"
REPORTS_FILE = WORK / "webapp_reports.jsonl"

MAX_BODY_BYTES = 2 * 1024 * 1024  # prompts and settings only; media goes by URL

# "local" is the owner_id used only for reading job history when Supabase
# isn't configured (see _owner_id()) -- generation itself no longer has a
# LOCAL_OWNER bypass (see _current_user_or_raise()/_reserve_funds()): every
# paid action requires a real signed-in user regardless of deployment mode.
LOCAL_OWNER = "local"
SESSION_COOKIE = "vf_session"

# Sent on every response via _send() -- static files, JSON, media, all of
# it, since they all funnel through that one method. No inline <script>,
# no inline `style="..."`, and no onclick="" attributes exist anywhere in
# webapp/static/ (every handler is wired with addEventListener, every
# style rule lives in app.css) -- verified before writing this, since a
# CSP this strict silently breaks the page if that ever stops being true.
# Generated media is served from fal.ai's CDN (v3.fal.media at the time of
# writing; the subdomain is wildcarded since fal has changed it before --
# see BOSHLASH.md), never any other third-party origin.
SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    # Only takes effect when the browser already sees this over HTTPS
    # (Railway's own domain, or a real TLS-terminating proxy per DEPLOY.md
    # §5) -- ignored by every browser on a plain-HTTP response, so this is
    # safe to send unconditionally, including for local development.
    ("Strict-Transport-Security", "max-age=15552000; includeSubDomains"),
    ("Content-Security-Policy", (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data: https://*.fal.media; "
        "media-src 'self' https://*.fal.media; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )),
]


# ---------------------------------------------------------------- factory reuse

def _load_factory():
    """Import scripts/factory.py as a module so the web layer reuses its
    fal client, payload builders, rate table, and ledger rather than
    reimplementing them."""
    spec = importlib.util.spec_from_file_location(
        "factory", ROOT / "scripts" / "factory.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_dotenv()
    return module


factory = _load_factory()
CONFIG = factory.load_config()
PRICING_VERSION = CONFIG.get("pricing_version")


# ---------------------------------------------------------------------- auth

def _session_token(handler):
    """Pull the Supabase access token out of the session cookie, or None."""
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    jar = http.cookies.SimpleCookie()
    jar.load(raw)
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def _current_user(handler):
    """Returns {"id", "email", "access_token"} for a valid session, or
    None -- both when Supabase isn't configured (single-tenant mode) and
    when the cookie is missing, expired, or invalid."""
    if not db.configured():
        return None
    token = _session_token(handler)
    if not token:
        return None
    user = db.get_user(token)
    if not user or not user.get("id"):
        return None
    return {"id": user["id"], "email": user.get("email"), "access_token": token}


def _session_cookie_header(access_token, max_age, secure=False):
    flags = "HttpOnly; Path=/; SameSite=Lax" + ("; Secure" if secure else "")
    return ("Set-Cookie", f"{SESSION_COOKIE}={access_token}; {flags}; Max-Age={max_age}")


def _clear_cookie_header(secure=False):
    flags = "HttpOnly; Path=/; SameSite=Lax" + ("; Secure" if secure else "")
    return ("Set-Cookie", f"{SESSION_COOKIE}=; {flags}; Max-Age=0")


def _sliding_window_check(store, lock, key, limit, window_seconds, message):
    """Generic in-memory sliding-window limiter shared by every rate
    limit in this file (see _check_auth_rate_limit, IP-keyed, and
    _check_avatar_rate_limit, user-keyed). This process is the only
    server instance (ThreadingHTTPServer, no external process pool) so
    an in-memory dict is enough for each -- it resets on restart, an
    acceptable trade-off for what these guard against. Raises
    ValueError, handled the same friendly-error way as every other
    rejection in this file."""
    now = time.time()
    with lock:
        attempts = [t for t in store.get(key, []) if now - t < window_seconds]
        if len(attempts) >= limit:
            raise ValueError(message)
        attempts.append(now)
        store[key] = attempts


_AUTH_ATTEMPTS = {}
_AUTH_ATTEMPTS_LOCK = threading.Lock()
AUTH_RATE_LIMIT = 8       # attempts
AUTH_RATE_WINDOW = 300    # seconds


def _client_ip(handler):
    """Best-effort client address for rate limiting only -- never used for
    anything security-critical beyond that. Prefers X-Forwarded-For (set by
    Railway's own proxy and any real TLS-terminating proxy per DEPLOY.md
    §5) since the raw socket address is just the proxy's own IP once one is
    in front of this server; falls back to the socket address directly for
    a bare/local run with no proxy."""
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return handler.client_address[0]


def _check_auth_rate_limit(handler):
    """Sliding-window limiter for /api/auth/login and /api/auth/signup,
    the two endpoints most worth throttling (credential stuffing, brute
    force, signup spam)."""
    _sliding_window_check(_AUTH_ATTEMPTS, _AUTH_ATTEMPTS_LOCK, _client_ip(handler),
                           AUTH_RATE_LIMIT, AUTH_RATE_WINDOW,
                           "Too many attempts. Wait a few minutes and try again.")


_AVATAR_ATTEMPTS = {}
_AVATAR_ATTEMPTS_LOCK = threading.Lock()
AVATAR_RATE_LIMIT = int(os.environ.get("AVATAR_RATE_LIMIT", "10"))       # generations
AVATAR_RATE_WINDOW = int(os.environ.get("AVATAR_RATE_WINDOW", "86400"))  # seconds (24h)


def _check_avatar_rate_limit(user_id):
    """Talking-avatar generation gets its own abuse brake, separate from
    every other paid endpoint and from the money-cost gate: a funded
    account paying for each one is still not a reason to allow mass
    production of videos of other people's likeness. Caps generations
    per rolling window per account regardless of balance -- see
    webapp/README.md's consent/moderation notes."""
    _sliding_window_check(
        _AVATAR_ATTEMPTS, _AVATAR_ATTEMPTS_LOCK, user_id, AVATAR_RATE_LIMIT, AVATAR_RATE_WINDOW,
        f"Avatar generation is limited to {AVATAR_RATE_LIMIT} per day per account, "
        "to prevent misuse of someone else's likeness. Try again later.")


_REPORT_ATTEMPTS = {}
_REPORT_ATTEMPTS_LOCK = threading.Lock()
REPORT_RATE_LIMIT = 20     # reports
REPORT_RATE_WINDOW = 3600  # seconds


def _check_report_rate_limit(handler):
    """IP-keyed, not user-keyed -- reporting deliberately needs no
    sign-in (see _report_job), so there's no account to key on. Loose
    enough not to block a person legitimately reporting several videos,
    tight enough to blunt a script trying to mass-unpublish content via
    fake reports."""
    _sliding_window_check(_REPORT_ATTEMPTS, _REPORT_ATTEMPTS_LOCK, _client_ip(handler),
                           REPORT_RATE_LIMIT, REPORT_RATE_WINDOW,
                           "Too many reports from this address. Try again later.")


def _basic_auth_credentials():
    """Optional deployment-time access gate: set WEBAPP_BASIC_AUTH_USER
    and WEBAPP_BASIC_AUTH_PASS to require HTTP Basic Auth on every
    request. Off by default (both unset), matching the existing local
    single-tenant behaviour with zero config changes. This is the
    recommended way to put single-tenant mode (no Supabase) behind a
    lock when exposing it on a public server -- multi-user mode has its
    own per-account auth and doesn't need this, but it's honoured either
    way if set."""
    user = os.environ.get("WEBAPP_BASIC_AUTH_USER", "")
    pw = os.environ.get("WEBAPP_BASIC_AUTH_PASS", "")
    if user and pw:
        return user, pw
    return None


def _require_identity_consent(body):
    """Talking-avatar and motion-transfer generation both animate a
    specific photo of a person -- require an explicit, informed
    attestation before either is allowed to run at all, rather than
    treating "the caller uploaded a photo" as implied permission to
    animate whoever is in it. The attestation is stored on the job
    record (see the callers, which pass consent_type/consent_at through
    to _new_job) as a durable, if unverified, consent trail: this does
    not confirm the claim is true, but it makes generating without at
    least claiming consent impossible through this API, and gives every
    job a clear record to point to if a report comes in later (see
    _report_job in the Handler class)."""
    consent_type = body.get("consent_type")
    if not body.get("consent_attested") or consent_type not in ("self", "authorized"):
        raise ValueError(
            "Confirm this is your own photo, or that you have the pictured "
            "person's permission to animate their likeness, before continuing."
        )
    return consent_type


def _is_public_ip(ip_str):
    """False for anything that isn't a normal, routable public address --
    loopback (127.0.0.1, ::1), link-local (169.254.x.x -- includes cloud
    metadata endpoints like 169.254.169.254), private ranges (10/8,
    172.16/12, 192.168/16, fc00::/7), multicast, reserved, and unspecified
    (0.0.0.0). Used to reject a server-side fetch aimed at internal
    infrastructure."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _require_public_url(value, field_name, allow_data=False):
    """Boundary validation for every URL a client can hand us that later
    reaches resolve_image()/fal_upload() (which reads local files by
    path) or probe_duration()/ffmpeg (which shell out to fetch whatever
    it's given). Without this, a client could pass a local path (e.g.
    "../.env") or an internal address and have the server read or probe
    it on their behalf -- a local-file-disclosure / SSRF vector. Public
    http(s) is always allowed; data: URIs are self-contained (no fetch
    happens) so they're safe to allow where the caller opts in.

    Beyond the scheme check, this also resolves the hostname and rejects
    it if any resolved address is private/loopback/link-local/etc (an
    attacker-controlled DNS name pointed at 127.0.0.1 or a cloud metadata
    endpoint, or a raw IP literal in the URL itself). This is validated
    here, at request time -- it is NOT a guarantee that the exact same
    resolution is used at fetch time a moment later; a name whose DNS
    answer changes between this check and the actual ffmpeg/urllib fetch
    (DNS rebinding) is a known residual gap this doesn't close. Closing
    that fully requires resolving once and connecting to the pinned IP
    for the real fetch too, which needs changes in scripts/factory.py's
    HTTP client and in how ffmpeg is given its input -- not done in this
    pass; see PAYMENTS.md-style follow-up notes."""
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{field_name} is required.")
    schemes = ("http://", "https://") + (("data:",) if allow_data else ())
    if not value.startswith(schemes):
        allowed = "a public http(s) URL or a data: URI" if allow_data else "a public http(s) URL"
        raise ValueError(f"{field_name} must be {allowed}, not a local path.")
    if value.startswith("data:"):
        return value

    hostname = urllib.parse.urlparse(value).hostname
    if not hostname:
        raise ValueError(f"{field_name} is not a valid URL.")
    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
    except socket.gaierror:
        raise ValueError(f"{field_name}'s host could not be resolved.") from None
    if not resolved or not all(_is_public_ip(ip) for ip in resolved):
        raise ValueError(f"{field_name} must be a public address, not an internal/private one.")
    return value


def _retail_rate(wholesale_rate):
    """Every dollar figure a customer sees -- quotes, the confirm dialog,
    credit-balance charges, MCP tool text (mcp/server.py talks to this
    API, never reads config.json directly, so it inherits this for free)
    -- goes through this first. The real fal.ai wholesale rate from
    config.json is never shown to a customer, only used internally (see
    the wholesale_cost/customer_charged_usd split in each _run_*_job
    below) so the operator can see real spend vs. what was actually
    billed. scripts/factory.py's CLI is unaffected: it's the operator's
    own tool against their own fal.ai balance, not customer-facing, so it
    keeps showing wholesale numbers unchanged."""
    return round(wholesale_rate * CONFIG["pricing"]["customer_markup_multiplier"], 6)


def _require_seedance25_ratio(model, image_url):
    """Web-native mirror of factory.require_seedance25_ratio: Seedance
    2.5's rate in config.json is a 16:9-only approximation of fal's real
    token-based (width x height x duration) pricing, confirmed to vary by
    aspect ratio on fal's own docs. Re-implemented here (rather than
    calling the CLI version directly) because factory.die() raises
    SystemExit with just an exit code, not a message -- catching it here
    would lose the actual reason; this raises ValueError with the real
    text instead, same fix already applied to postprod's probe_duration
    calls below. A no-op for every other model."""
    if "seedance-2.5" not in model and "seedance/2.5" not in model:
        return
    try:
        w, h = factory.probe_image_dimensions(image_url)
    except SystemExit:
        raise ValueError("Could not read that image's dimensions — is the URL still reachable?") from None
    ratio = w / h
    if abs(ratio - 16 / 9) > 0.03:
        raise ValueError(
            f"Seedance 2.5 only supports 16:9 shots (its price is a 16:9-only approximation "
            f"of fal's real pricing, which genuinely varies by aspect ratio). Your image is "
            f"{w}x{h} ({ratio:.2f}:1). Pick a different model, or a 16:9 image."
        )


# ------------------------------------------------------------------- presets

# Grounded in fal-master-prompt.md: explicit camera choreography, the
# identity-lock sentence, and named lens/grain/palette are the patterns
# that separated the reference clips from this project's early attempts.
IDENTITY_LOCK = (
    "The subject's face stays identical to the first frame throughout — "
    "same features, same proportions, no morphing, no re-lighting of "
    "facial structure."
)

PRESETS = [
    {
        "id": "product_reveal",
        "name": "Product reveal",
        "blurb": "Slow push-in on a product, soft studio light. Good for ads.",
        "motion": (
            "Camera pushes in slowly and steadily on the subject over the full "
            "duration, ending on a tight product shot. The subject stays still; "
            "only a soft highlight travels across its surface. "
            "Soft large key light, 50mm lens, fine film grain, clean neutral palette. "
            "No camera shake, no lens flare, no on-screen text."
        ),
        "seconds": 6,
    },
    {
        "id": "talking_head",
        "name": "Talking head",
        "blurb": "Locked-off portrait, natural micro-motion. Best face fidelity.",
        "motion": (
            "Camera is locked off with no movement. The person breathes naturally "
            "and blinks once, with one small natural head settle — no acting, no "
            "exaggerated expression. Soft flat key light from camera left, 85mm lens, "
            "shallow depth of field, fine film grain. " + IDENTITY_LOCK
        ),
        "seconds": 6,
        "model_hint": "fal-ai/veo3.1/image-to-video",
    },
    {
        "id": "cinematic_reveal",
        "name": "Cinematic reveal",
        "blurb": "Camera drops to a low angle as the scene opens up.",
        "motion": (
            "Camera starts high looking down, then pushes in and drops to a low "
            "three-quarter angle at chest height, ending on a medium shot. The "
            "environment reveals itself behind the subject as the camera settles. "
            "Warm directional light, anamorphic lens, fine film grain, long shadows. "
            + IDENTITY_LOCK
        ),
        "seconds": 8,
    },
    {
        "id": "action_beat",
        "name": "Action beat",
        "blurb": "Fast, continuous movement. No slow-motion drift.",
        "motion": (
            "Fast-paced real-time action, not slow motion, brisk and continuous "
            "with no pauses. The movement carries through the full duration and "
            "resolves on a settled stance. Camera remains static. "
            "35mm lens, motion blur on the moving limbs only, face stays sharp. "
            + IDENTITY_LOCK
        ),
        "seconds": 8,
    },
    {
        "id": "ambient_loop",
        "name": "Ambient / B-roll",
        "blurb": "Gentle drift for backgrounds and social filler.",
        "motion": (
            "Camera drifts gently sideways about 10 degrees over the full duration. "
            "Nothing in the scene moves abruptly; light shifts softly. "
            "Wide soft light, 35mm lens, fine film grain, muted palette. "
            "No camera shake, no on-screen text."
        ),
        "seconds": 6,
    },
    {
        "id": "ugc_testimonial",
        "name": "UGC testimonial",
        "blurb": "Handheld, natural, like a phone selfie video. Great for reviews.",
        "motion": (
            "Camera holds with visible handheld sway, natural amateur framing, as if "
            "held at arm's length. The person speaks naturally with small head "
            "movements and blinks. Available light, slight lens softness, no "
            "professional grade, no camera stabilization. " + IDENTITY_LOCK
        ),
        "seconds": 8,
    },
    {
        "id": "real_estate_walkthrough",
        "name": "Interior walkthrough",
        "blurb": "Slow glide forward through a room. Great for real estate.",
        "motion": (
            "Camera glides forward smoothly at a slow walking pace through the "
            "space, staying at eye level, ending on a wider view of the room. "
            "Natural daylight through windows, wide-angle lens, clean neutral "
            "palette, no camera shake, no on-screen text."
        ),
        "seconds": 8,
    },
    {
        "id": "food_closeup",
        "name": "Food close-up",
        "blurb": "Macro push-in with steam or texture detail. Great for restaurants.",
        "motion": (
            "Camera pushes in slowly, 20% over the full duration, on a close macro "
            "detail of the food's texture. Steam or a light garnish drifts "
            "naturally. Soft overhead light, shallow depth of field, 100mm macro "
            "lens, fine film grain, warm palette. No camera shake, no lens flare, "
            "no on-screen text."
        ),
        "seconds": 6,
    },
    {
        "id": "fashion_turn",
        "name": "Fashion lookbook",
        "blurb": "The subject turns to reveal the outfit from a new angle.",
        "motion": (
            "The subject turns 30 degrees toward camera and settles, revealing the "
            "outfit from a three-quarter angle. Camera holds steady. Soft diffused "
            "studio light, 50mm lens, clean neutral backdrop, fine film grain. "
            + IDENTITY_LOCK
        ),
        "seconds": 6,
    },
    {
        "id": "tech_unboxing",
        "name": "Product unboxing",
        "blurb": "Overhead reveal of a product on a clean surface.",
        "motion": (
            "Camera holds a steady overhead angle looking straight down. Hands "
            "enter frame naturally and open the packaging, revealing the product "
            "inside. Soft even light, no harsh shadows, 35mm lens, clean neutral "
            "surface. No camera shake, no on-screen text."
        ),
        "seconds": 8,
    },
    {
        "id": "travel_landscape",
        "name": "Travel / landscape",
        "blurb": "A wide, slow drift across a scenic view.",
        "motion": (
            "Camera drifts slowly sideways, 15% over the full duration, across the "
            "wide landscape. Clouds and light shift gently; nothing moves "
            "abruptly. Golden-hour directional light, wide lens, fine film grain, "
            "natural palette. No camera shake, no on-screen text."
        ),
        "seconds": 8,
    },
    {
        "id": "fitness_action",
        "name": "Fitness / gym",
        "blurb": "Continuous real-time movement through one exercise rep.",
        "motion": (
            "Fast-paced real-time motion through a single exercise repetition, not "
            "slow motion, resolving on a settled stance. Camera remains static at "
            "a low three-quarter angle. Bright even gym lighting, 35mm lens, "
            "motion blur on moving limbs only, face stays sharp. " + IDENTITY_LOCK
        ),
        "seconds": 6,
    },
]

ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:3"]

# Whitelist for the subtitles "style" param, which is embedded unquoted
# inside an ffmpeg -vf filtergraph string (force_style='...'). A value
# with a stray quote could break out of that literal and inject extra
# filtergraph directives, so this is checked rather than escaped -- ffmpeg's
# own escaping rules for filtergraphs are notoriously easy to get wrong,
# and a plain allowlist covers every legitimate ASS/SSA style key=value.
SAFE_FFMPEG_STYLE = re.compile(r"^[A-Za-z0-9 ,._=&-]+$")


# ------------------------------------------------------- post-production ops
#
# Web equivalents of factory.py's upscale/lipsync/subtitles/bgremove
# commands. Rates and model ids are read from CONFIG, never duplicated as
# literals here, so the CLI and the webapp cannot drift on price.

def _postprod_rate(op, params):
    rates = CONFIG["rates"]
    if op == "upscale":
        table = rates["upscale_per_second_usd_by_tier"]
        tier = params.get("tier")
        rate = table.get(tier) if tier else None
        if rate is None:
            choices = [k for k in table if not k.startswith("_")]
            raise ValueError(f"Pick a valid output resolution tier: {choices}")
        if params.get("fps") and int(params["fps"]) >= 60:
            rate *= 2
        return CONFIG["models"]["upscale"], rate
    if op == "lipsync":
        return CONFIG["models"]["lipsync"], rates["lipsync_per_second_usd"]
    if op == "subtitles":
        return CONFIG["models"]["transcribe"], rates["transcription_per_second_usd"]
    if op == "bgremove":
        return CONFIG["models"]["bg_remove"], rates["bg_remove_per_second_usd"]
    raise ValueError(f"Unknown post-production operation '{op}'.")


def _postprod_payload(op, file_url, params):
    """Payload for the ops fal itself runs directly (upscale/lipsync/
    bgremove). subtitles is handled separately in _run_postprod_job since
    it needs a local ffmpeg burn-in step after the fal transcription call,
    not just one fal call."""
    if op == "upscale":
        payload = {"video_url": file_url, "model": params.get("model") or "Proteus",
                   "upscale_factor": float(params.get("factor") or 2)}
        if params.get("fps"):
            payload["target_fps"] = int(params["fps"])
        return payload
    if op == "lipsync":
        audio_url = (params.get("audio_url") or "").strip()
        if not audio_url:
            raise ValueError("lipsync needs a direct URL to the voice track (audio_url).")
        return {"video_url": file_url, "audio_url": audio_url,
                "sync_mode": params.get("sync_mode") or "cut_off"}
    if op == "bgremove":
        return {"video_url": file_url, "background_color": params.get("background_color") or "Black",
                "preserve_audio": bool(params.get("preserve_audio", True))}
    raise ValueError(f"Unknown post-production operation '{op}'.")


# ---------------------------------------------------------------- job storage
#
# `_persist()` is called twice per job now, not once: immediately at
# creation (status="queued", before the provider is ever called) and
# again at the terminal state (done/error). Both writes append a line to
# the same JSONL file; `_load_persisted()` replays it in order and lets
# the last line for a given id win, so this is safe with the existing
# format -- a process that dies between the two writes leaves the
# "queued" line as the last known state, which is exactly what restart
# recovery below needs to notice and reconcile.

_jobs = {}
_jobs_lock = threading.Lock()
_idem_index = {}  # idempotency_key -> job_id, most recent attempt

# Bounded concurrency: every paid job (image/video/postprod/motion/avatar)
# is submitted here instead of an unbounded `threading.Thread(...).start()`
# per request, so a burst of requests queues instead of spawning unbounded
# provider calls and OS threads.
JOB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=int(os.environ.get("MAX_CONCURRENT_JOBS", "8")),
    thread_name_prefix="job",
)


def _persist(job):
    """Append this job's current state to disk. The per-generation ledger
    in factory.py stays the source of truth for spend; this file backs
    the gallery, job history, and restart recovery."""
    WORK.mkdir(parents=True, exist_ok=True)
    with open(JOBS_FILE, "a") as fh:
        fh.write(json.dumps(job) + "\n")


def _persist_report(report):
    """Append-only report log -- see Handler._report_job. There is no
    admin review UI yet; an operator inspects this file directly (see
    webapp/README.md's moderation notes) until one exists."""
    WORK.mkdir(parents=True, exist_ok=True)
    with open(REPORTS_FILE, "a") as fh:
        fh.write(json.dumps(report) + "\n")


def _load_persisted():
    if not JOBS_FILE.exists():
        return
    for line in JOBS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            job = json.loads(line)
        except json.JSONDecodeError:
            continue
        if job.get("id"):
            _jobs[job["id"]] = job
            if job.get("idempotency_key"):
                _idem_index[job["idempotency_key"]] = job["id"]


def _new_job(kind, owner_id, **fields):
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "owner_id": owner_id,
        "status": "queued",
        "stage": "Queued",
        "created_at": time.time(),
        "outputs": [],
        "error": None,
        "credit_deducted": None,
        "idempotency_key": None,
        "reservation_id": None,
        "billing_error": None,
        **fields,
    }
    with _jobs_lock:
        _jobs[job["id"]] = job
        if job.get("idempotency_key"):
            _idem_index[job["idempotency_key"]] = job["id"]
    return job


def _media_job_for_path(rel):
    """Finds the job (if any) whose recorded outputs include this media
    path -- see _serve_media, which uses this to decide who may read a
    file under work/. O(n) over in-memory jobs; fine at this project's
    scale and avoids a second index that could drift from _jobs itself."""
    target = f"/media/{rel}"
    with _jobs_lock:
        for job in _jobs.values():
            if target in (job.get("outputs") or []):
                return dict(job)
    return None


def _dedup_lookup(idem_key):
    """Returns the existing job dict if a request with this idempotency
    key is already queued, running, or already succeeded -- the caller
    should return it as-is instead of reserving/creating anything new.
    None if this is a first attempt, or if the only prior attempt with
    this key ended in error (a genuine failure should be retryable, not
    permanently stuck -- see _reservation_key_for)."""
    with _jobs_lock:
        job_id = _idem_index.get(idem_key)
        job = _jobs.get(job_id) if job_id else None
        return dict(job) if job and job.get("status") != "error" else None


def _reservation_key_for(idem_key):
    """The ledger idempotency key to reserve under: the base key itself
    on a first attempt, or a fresh attempt-scoped suffix if the previous
    job under this same key ended in error and released its hold --
    reserve() replays an existing reservation verbatim regardless of its
    status, so reusing a released key would permanently block a real
    retry instead of trying again."""
    with _jobs_lock:
        job_id = _idem_index.get(idem_key)
        job = _jobs.get(job_id) if job_id else None
    if job and job.get("status") == "error":
        return f"{idem_key}#retry-{uuid.uuid4().hex[:8]}"
    return idem_key


def _idempotency_key(endpoint, owner_id, body):
    """Deterministic dedup key for one logical paid action: the same
    user submitting the same endpoint with the same request body (a
    network-retried request, or a double-click) hashes to the same key,
    so it reserves/charges at most once (see _dedup_lookup /
    _reservation_key_for) instead of once per HTTP request received.
    `approved_cost` and an optional client-supplied `idempotency_key` are
    excluded from the hash -- they describe *approval*, not *what* is
    being requested. A client MAY pass its own `idempotency_key` in the
    body for a guarantee that survives the payload varying between
    retries (not required for the common case)."""
    explicit = str(body.get("idempotency_key") or "").strip()
    if explicit:
        return f"{endpoint}:{owner_id}:{explicit}"[:250]
    canonical = {k: v for k, v in body.items() if k not in ("approved_cost", "idempotency_key")}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()
    return f"{endpoint}:{owner_id}:{digest}"


def _checkpoint_fal_handle(job_id, handle):
    """Persist the fal.ai queue handle the moment it exists, before
    waiting for the result -- this is what makes restart recovery
    possible (see _recover_orphaned_jobs): even if this process dies
    while fal_poll() is still waiting, the handle survives on disk and a
    fresh process can resume polling the exact same in-flight fal
    request instead of losing track of it and leaving the reservation
    stuck."""
    job = _update(job_id, fal_request_id=handle["request_id"],
                  fal_status_url=handle["status_url"], fal_response_url=handle["response_url"])
    _persist(job)


def _validate_outputs(urls, kind="output"):
    """Refuse to capture a charge for an empty/missing output -- the
    provider call must have actually produced something before money
    moves. Checks presence/shape only (not full decodability -- a real
    ffprobe/decoder pass over every output is a further hardening step,
    not done here; see PAYMENTS.md-style verification notes)."""
    if not urls or not any(u for u in urls):
        raise RuntimeError(f"fal returned no usable {kind}")


def _capture(job_id, reservation_key, note):
    """Turn a reservation into an actual charge after a validated,
    successful generation. Fail-closed on the *reservation* side (nothing
    is ever captured without first being reserved), but not blocking on
    the *result* side: if Supabase happens to be down at this exact
    moment, the already-produced, already-fal-charged-to-us result is
    still handed to the customer (withholding it wouldn't undo the real
    provider spend, it would just also lose the customer's trust) while
    the job is flagged billing_error for manual reconciliation -- the
    reservation is left in "reserved" state, not silently dropped, so an
    operator can capture it later instead of the generation being free
    forever."""
    try:
        ledger.get_ledger().capture(reservation_key, note=note)
    except ledger.LedgerError as exc:
        print(f"  BILLING ALERT: capture failed for job {job_id} (reservation {reservation_key}): {exc}")
        _update(job_id, billing_error=str(exc)[:500])


def _release(job_id, reservation_key):
    """Drop a reservation after a failed/invalid generation -- nothing
    was ever actually charged."""
    try:
        ledger.get_ledger().release(reservation_key)
    except ledger.LedgerError as exc:
        print(f"  BILLING ALERT: release failed for job {job_id} (reservation {reservation_key}): {exc}")


def _recover_orphaned_jobs():
    """Runs once at startup, after _load_persisted(). Any job still in
    "queued" or "running" state is one this process was in the middle of
    when it (or a previous instance) stopped -- restart recovery for
    those:

      - A recorded fal handle (fal_status_url) exists: the fal request
        may have kept running on fal's side the whole time this process
        was down. Resume polling it in the background; capture on
        success, release on failure/timeout, exactly like a normal
        worker would.
      - No fal handle recorded: the process died before ever reaching
        fal (or before the handle was persisted) -- there is nothing to
        reconcile against, so release the hold and mark the job failed
        rather than leave an orphaned reservation and a job stuck at
        "queued"/"running" forever.

    Either way this fails closed: a reservation is only ever captured
    after a real, confirmed, validated fal result -- never assumed."""
    with _jobs_lock:
        orphans = [dict(j) for j in _jobs.values() if j.get("status") in ("queued", "running")]
    for job in orphans:
        job_id = job["id"]
        reservation_key = job.get("reservation_id")
        handle = {"status_url": job.get("fal_status_url"), "response_url": job.get("fal_response_url"),
                  "request_id": job.get("fal_request_id")}
        if not handle["status_url"] or not handle["response_url"]:
            print(f"  RECOVERY: job {job_id} had no fal handle recorded -- releasing and marking failed.")
            if reservation_key:
                _release(job_id, reservation_key)
            j = _update(job_id, status="error", stage="Failed",
                        error="Interrupted by a server restart before it reached the provider. Not charged.")
            _persist(j)
            continue

        def _resume(job_id=job_id, handle=handle, reservation_key=reservation_key, job=job):
            print(f"  RECOVERY: resuming job {job_id} (fal request {handle['request_id']})…")
            try:
                result = factory.fal_poll(handle, max_wait=1800)
                kind = job.get("kind")
                if kind == "image":
                    urls = factory.all_urls(result, "images", "image")
                    _validate_outputs(urls, "image")
                    outputs = urls
                else:
                    url = factory.first_url(result, "video", "videos")
                    _validate_outputs([url], "video")
                    outputs = [url]
                if reservation_key:
                    _capture(job_id, reservation_key, note=f"web:{job_id} recovered")
                j = _update(job_id, status="done", stage="Done", outputs=outputs,
                            request_id=result.get("_request_id"), credit_deducted=True)
                _persist(j)
                print(f"  RECOVERY: job {job_id} completed successfully.")
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - see _run_image_job
                print(f"  RECOVERY: job {job_id} could not be recovered: {exc}")
                if reservation_key:
                    _release(job_id, reservation_key)
                j = _update(job_id, status="error", stage="Failed",
                            error=friendly_error(exc), error_raw=str(exc)[:1500])
                _persist(j)

        JOB_EXECUTOR.submit(_resume)


def _update(job_id, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)
            return dict(job)
    return None


# ------------------------------------------------------------- topup storage
#
# Same shape and persistence pattern as _jobs/JOBS_FILE above, kept
# entirely separate: a topup tracks money coming IN from a payment
# provider, a job tracks a paid generation spending it back out, and
# conflating the two stores would make both harder to reason about for
# no real benefit.

_topups = {}
_topups_lock = threading.Lock()


def _persist_topup(topup):
    WORK.mkdir(parents=True, exist_ok=True)
    with open(TOPUPS_FILE, "a") as fh:
        fh.write(json.dumps(topup) + "\n")


def _load_persisted_topups():
    if not TOPUPS_FILE.exists():
        return
    for line in TOPUPS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            topup = json.loads(line)
        except json.JSONDecodeError:
            continue
        if topup.get("id"):
            _topups[topup["id"]] = topup


def _new_topup(user_id, amount_usd, provider):
    topup = {
        "id": uuid.uuid4().hex[:16],
        "user_id": user_id,
        "amount_usd": round(float(amount_usd), 2),
        "provider": provider,
        "status": "pending",
        "provider_transaction_id": None,
        "provider_prepare_id": None,
        "created_at": time.time(),
        "paid_at": None,
    }
    with _topups_lock:
        _topups[topup["id"]] = topup
    _persist_topup(topup)
    return topup


def _find_topup(topup_id=None, provider_transaction_id=None):
    with _topups_lock:
        if topup_id is not None:
            topup = _topups.get(topup_id)
            return dict(topup) if topup else None
        if provider_transaction_id is not None:
            for topup in _topups.values():
                if topup.get("provider_transaction_id") == provider_transaction_id:
                    return dict(topup)
    return None


def _update_topup(topup_id, **fields):
    with _topups_lock:
        topup = _topups.get(topup_id)
        if not topup:
            return None
        topup.update(fields)
        snapshot = dict(topup)
    _persist_topup(snapshot)
    return snapshot


def _credit_topup(topup_id, note):
    """Marks a topup paid and grants the credit -- idempotent: called
    twice for the same topup (Payme/Click/Stripe can all legitimately
    retry a webhook) does nothing the second time, since the status
    check happens under the same lock as the update. Returns the updated
    topup, or None if it was already paid (caller should still respond
    success to the provider either way -- see each provider's handler)."""
    with _topups_lock:
        topup = _topups.get(topup_id)
        if not topup or topup["status"] == "paid":
            return None
        topup["status"] = "paid"
        topup["paid_at"] = time.time()
        snapshot = dict(topup)
    try:
        # "topup:" prefix is what 04-atomic-credits.sql's refund_credit()
        # uses to classify this as reason='manual_topup' rather than
        # 'refund' in the ledger -- and topup_id itself is the DB-level
        # idempotency key, a second layer under the in-memory _topups
        # status check above (this survives a restart between the two;
        # the in-memory check alone wouldn't).
        ledger.get_ledger().refund(snapshot["user_id"], snapshot["amount_usd"],
                                    f"topup:{topup_id}", note=note)
    except ledger.LedgerError as exc:
        # Roll the local state back so a retry (the provider WILL retry a
        # non-2xx/non-success response) gets another chance to actually
        # credit the account, instead of being silently treated as
        # already-done by the idempotency check above.
        with _topups_lock:
            _topups[topup_id]["status"] = "pending"
            _topups[topup_id]["paid_at"] = None
        raise payments.PaymentError(f"Could not credit account: {exc}") from None
    _persist_topup(snapshot)
    return snapshot


def _usd_to_uzs_rate():
    raw = os.environ.get("USD_TO_UZS_RATE", "").strip()
    if not raw:
        raise payments.PaymentError(
            "USD_TO_UZS_RATE is not set -- required for Payme/Click, which bill in UZS. "
            "See webapp/README.md's Payments section."
        )
    try:
        rate = float(raw)
    except ValueError:
        raise payments.PaymentError("USD_TO_UZS_RATE must be a number.") from None
    if rate <= 0:
        raise payments.PaymentError("USD_TO_UZS_RATE must be positive.")
    return rate


# ---------------------------------------------------------- Payme methods
#
# One function per JSON-RPC method Payme calls on this server -- see
# webapp/payments.py's module docstring for the confidence caveat on the
# exact error-code values used here.

def _payme_check_perform_transaction(request_id, params):
    account = params.get("account") or {}
    topup = _find_topup(topup_id=account.get("order_id"))
    if not topup or topup["provider"] != "payme":
        return payments.payme_rpc_error(request_id, payments.PAYME_ERR_ACCOUNT_NOT_FOUND, "Order not found")
    if topup["status"] == "paid":
        return payments.payme_rpc_error(request_id, payments.PAYME_ERR_ACCOUNT_NOT_FOUND, "Order already paid")
    expected = int(round(topup["amount_usd"] * _usd_to_uzs_rate() * 100))
    if int(params.get("amount") or 0) != expected:
        return payments.payme_rpc_error(request_id, payments.PAYME_ERR_INVALID_AMOUNT, "Incorrect amount")
    return payments.payme_rpc_result(request_id, {"allow": True})


def _payme_create_transaction(request_id, params):
    payme_txn_id = params.get("id")
    existing = _find_topup(provider_transaction_id=payme_txn_id)
    if existing:
        # Payme's own spec: CreateTransaction is sent at least twice for
        # the same id; a repeat must get exactly the same response as
        # the first call, not be treated as a new/duplicate transaction.
        if existing["status"] == "paid":
            return payments.payme_rpc_error(request_id, payments.PAYME_ERR_CANNOT_PERFORM, "Already completed")
        return payments.payme_rpc_result(request_id, {
            "create_time": int(existing["created_at"] * 1000),
            "transaction": existing["id"],
            "state": payments.PAYME_STATE_CREATED,
        })
    account = params.get("account") or {}
    topup = _find_topup(topup_id=account.get("order_id"))
    if not topup or topup["provider"] != "payme":
        return payments.payme_rpc_error(request_id, payments.PAYME_ERR_ACCOUNT_NOT_FOUND, "Order not found")
    if topup["status"] == "paid":
        return payments.payme_rpc_error(request_id, payments.PAYME_ERR_ACCOUNT_NOT_FOUND, "Order already paid")
    if topup.get("provider_transaction_id") and topup["provider_transaction_id"] != payme_txn_id:
        return payments.payme_rpc_error(
            request_id, payments.PAYME_ERR_CANNOT_PERFORM, "A different transaction already exists for this order")
    expected = int(round(topup["amount_usd"] * _usd_to_uzs_rate() * 100))
    if int(params.get("amount") or 0) != expected:
        return payments.payme_rpc_error(request_id, payments.PAYME_ERR_INVALID_AMOUNT, "Incorrect amount")
    updated = _update_topup(topup["id"], provider_transaction_id=payme_txn_id)
    return payments.payme_rpc_result(request_id, {
        "create_time": int(updated["created_at"] * 1000),
        "transaction": updated["id"],
        "state": payments.PAYME_STATE_CREATED,
    })


def _payme_perform_transaction(request_id, params):
    payme_txn_id = params.get("id")
    topup = _find_topup(provider_transaction_id=payme_txn_id)
    if not topup:
        return payments.payme_rpc_error(request_id, payments.PAYME_ERR_TRANSACTION_NOT_FOUND, "Transaction not found")
    if topup["status"] != "paid":
        _credit_topup(topup["id"], note=f"payme:{payme_txn_id}")
        topup = _find_topup(topup_id=topup["id"])
    return payments.payme_rpc_result(request_id, {
        "transaction": topup["id"],
        "perform_time": int(topup["paid_at"] * 1000),
        "state": payments.PAYME_STATE_COMPLETED,
    })


def _payme_cancel_transaction(request_id, params):
    payme_txn_id = params.get("id")
    topup = _find_topup(provider_transaction_id=payme_txn_id)
    if not topup:
        return payments.payme_rpc_error(request_id, payments.PAYME_ERR_TRANSACTION_NOT_FOUND, "Transaction not found")
    was_paid = topup["status"] == "paid"
    if topup["status"] != "cancelled":
        if was_paid:
            try:
                ledger.get_ledger().debit(topup["user_id"], topup["amount_usd"],
                                           f"payme:{payme_txn_id}:refund", note="payme cancel after complete")
            except ledger.LedgerError as exc:
                raise payments.PaymentError(f"Could not reverse credit: {exc}") from None
        topup = _update_topup(topup["id"], status="cancelled", cancelled_at=time.time())
    state = payments.PAYME_STATE_CANCELLED_AFTER_COMPLETE if was_paid else payments.PAYME_STATE_CANCELLED
    return payments.payme_rpc_result(request_id, {
        "transaction": topup["id"],
        "cancel_time": int(topup.get("cancelled_at", time.time()) * 1000),
        "state": state,
    })


def _payme_check_transaction(request_id, params):
    topup = _find_topup(provider_transaction_id=params.get("id"))
    if not topup:
        return payments.payme_rpc_error(request_id, payments.PAYME_ERR_TRANSACTION_NOT_FOUND, "Transaction not found")
    if topup["status"] == "paid":
        state = payments.PAYME_STATE_COMPLETED
    elif topup["status"] == "cancelled":
        state = (payments.PAYME_STATE_CANCELLED_AFTER_COMPLETE if topup.get("paid_at")
                 else payments.PAYME_STATE_CANCELLED)
    else:
        state = payments.PAYME_STATE_CREATED
    return payments.payme_rpc_result(request_id, {
        "create_time": int(topup["created_at"] * 1000),
        "perform_time": int(topup["paid_at"] * 1000) if topup.get("paid_at") else 0,
        "cancel_time": int(topup["cancelled_at"] * 1000) if topup.get("cancelled_at") else 0,
        "transaction": topup["id"],
        "state": state,
    })


def _payme_get_statement(request_id, params):
    frm = (params.get("from") or 0) / 1000
    to = (params.get("to") or time.time() * 1000) / 1000
    with _topups_lock:
        rows = [dict(t) for t in _topups.values()
                if t["provider"] == "payme" and t.get("provider_transaction_id")
                and frm <= t["created_at"] <= to]
    rate = _usd_to_uzs_rate()
    transactions = []
    for t in rows:
        if t["status"] == "paid":
            state = payments.PAYME_STATE_COMPLETED
        elif t["status"] == "cancelled":
            state = (payments.PAYME_STATE_CANCELLED_AFTER_COMPLETE if t.get("paid_at")
                     else payments.PAYME_STATE_CANCELLED)
        else:
            state = payments.PAYME_STATE_CREATED
        transactions.append({
            "id": t["provider_transaction_id"],
            "time": int(t["created_at"] * 1000),
            "amount": int(round(t["amount_usd"] * rate * 100)),
            "account": {"order_id": t["id"]},
            "create_time": int(t["created_at"] * 1000),
            "perform_time": int(t["paid_at"] * 1000) if t.get("paid_at") else 0,
            "cancel_time": int(t["cancelled_at"] * 1000) if t.get("cancelled_at") else 0,
            "transaction": t["id"],
            "state": state,
            "reason": None,
        })
    return payments.payme_rpc_result(request_id, transactions)


# ---------------------------------------------------------- Click methods

def _parse_click_body(raw):
    """Click's request body format is inconsistent across the
    integration guides this was cross-referenced against -- some show
    JSON, some application/x-www-form-urlencoded. Accepts either rather
    than betting on one, since getting it wrong would just 400 every
    request with no way to diagnose why from this sandbox (see
    webapp/payments.py's module docstring)."""
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    return {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}


def _click_error_response(fields, error, error_note, **extra):
    resp = {
        "click_trans_id": fields.get("click_trans_id"),
        "merchant_trans_id": fields.get("merchant_trans_id"),
        "error": error,
        "error_note": error_note,
    }
    resp.update(extra)
    return resp


def _click_handle_prepare(fields):
    if not payments.click_verify_sign(fields):
        return _click_error_response(fields, payments.CLICK_ERR_SIGN_FAILED, "Sign check failed")
    topup = _find_topup(topup_id=fields.get("merchant_trans_id"))
    if not topup or topup["provider"] != "click":
        return _click_error_response(fields, payments.CLICK_ERR_ORDER_NOT_FOUND, "Order not found")
    if topup["status"] == "paid":
        return _click_error_response(fields, payments.CLICK_ERR_ALREADY_PAID, "Already paid")
    try:
        expected = round(topup["amount_usd"] * _usd_to_uzs_rate(), 2)
    except payments.PaymentError as exc:
        return _click_error_response(fields, payments.CLICK_ERR_REQUEST_FAILED, str(exc))
    try:
        given = round(float(fields.get("amount") or 0), 2)
    except (TypeError, ValueError):
        given = None
    if given != expected:
        return _click_error_response(fields, payments.CLICK_ERR_INVALID_AMOUNT, "Incorrect amount")
    _update_topup(topup["id"], provider_prepare_id=topup["id"])
    return _click_error_response(fields, payments.CLICK_ERR_SUCCESS, "Success", merchant_prepare_id=topup["id"])


def _click_handle_complete(fields):
    if not payments.click_verify_sign(fields):
        return _click_error_response(fields, payments.CLICK_ERR_SIGN_FAILED, "Sign check failed")
    topup = _find_topup(topup_id=fields.get("merchant_trans_id"))
    if not topup or topup["provider"] != "click":
        return _click_error_response(fields, payments.CLICK_ERR_ORDER_NOT_FOUND, "Order not found")
    if str(fields.get("merchant_prepare_id")) != str(topup.get("provider_prepare_id")):
        return _click_error_response(fields, payments.CLICK_ERR_TRANSACTION_NOT_FOUND, "Prepare id mismatch")
    if topup["status"] == "paid":
        return _click_error_response(fields, payments.CLICK_ERR_SUCCESS, "Success", merchant_confirm_id=topup["id"])
    if int(fields.get("error") or 0) < 0:
        _update_topup(topup["id"], status="cancelled", cancelled_at=time.time())
        return _click_error_response(
            fields, payments.CLICK_ERR_TRANSACTION_CANCELLED, "Transaction cancelled by Click")
    try:
        _credit_topup(topup["id"], note=f"click:{fields.get('click_trans_id')}")
    except payments.PaymentError as exc:
        return _click_error_response(fields, payments.CLICK_ERR_REQUEST_FAILED, str(exc))
    _update_topup(topup["id"], provider_transaction_id=fields.get("click_trans_id"))
    return _click_error_response(fields, payments.CLICK_ERR_SUCCESS, "Success", merchant_confirm_id=topup["id"])


# ------------------------------------------------------------------ errors

def friendly_error(exc):
    """Map raw fal/network failures onto something a non-technical user can
    act on. The raw text is kept separately for the ledger and the console."""
    if isinstance(exc, SystemExit):
        # factory.py's CLI-oriented die() raises SystemExit with just an
        # exit code, not a message -- the real reason was already printed
        # to this process's stderr by die() itself, so point there instead
        # of surfacing a bare "1".
        return (
            "Generation failed due to an invalid request or a missing "
            "server configuration. Check the server logs for the exact reason."
        )
    text = str(exc)
    if "Exhausted balance" in text or "TOP_UP" in text or "User is locked" in text:
        return (
            "Your fal.ai balance is empty, so the generation was refused and "
            "nothing was charged. Top up at fal.ai/dashboard/billing and try again."
        )
    if "content_policy_violation" in text or "content checker" in text:
        return (
            "The model's safety filter rejected this prompt. Nothing was charged. "
            "Try rewording the action in plainer, less violent language."
        )
    if "FAL_KEY is not set" in text or "FAL_KEY" in text and "not set" in text:
        return "No fal.ai API key is configured on the server. Set FAL_KEY and restart."
    if "422" in text and "duration" in text:
        return "This model does not accept that duration. Try one of its listed length options."
    if "network error" in text or "timed out" in text:
        return "Could not reach fal.ai. Check the connection and try again."
    if "no rate on file" in text:
        return "That model has no price on file, so it was refused before spending anything."
    if "HTTP 401" in text or "HTTP 403" in text:
        return (
            "The server's fal.ai API key was rejected. Nothing was charged. "
            "Check that FAL_KEY on the server is correct and still active."
        )
    if "HTTP 429" in text:
        return "fal.ai is rate-limiting this key right now. Wait a moment and try again."
    if "HTTP 5" in text:
        return "fal.ai is having trouble on its end right now. Nothing was charged — try again shortly."
    return f"Generation failed: {text[:300]}"


# -------------------------------------------------------------------- workers
#
# Every worker below follows the same shape: fal_submit() -> checkpoint
# the handle to disk -> fal_poll() -> validate the output -> capture the
# reservation -> mark done; any exception along the way releases the
# reservation and marks the job failed. See _checkpoint_fal_handle,
# _validate_outputs, _capture, _release above.

def _run_image_job(job_id, prompt, count, aspect, refs, cost, owner_id, reservation_key):
    _update(job_id, status="running", stage="Sending to fal.ai…")
    try:
        # `refs` are client-supplied and already validated as public
        # http(s)/data URLs by _generate_image -- used as-is. No global
        # canonical-face image is auto-injected any more: identity now
        # only ever comes from a character the user explicitly selected
        # (see the "Character (optional)" chip row / selectedCharacterId
        # in webapp/static/app.js), whose reference photos are already
        # included in `refs` by the time this runs.
        image_urls = list(refs or [])

        model = CONFIG["models"]["still_edit"] if image_urls else CONFIG["models"]["still"]
        payload = {
            "prompt": prompt,
            "num_images": count,
            "aspect_ratio": aspect,
        }
        if image_urls:
            _update(job_id, stage="Uploading reference images…")
            payload["image_urls"] = image_urls

        _update(job_id, stage="Generating variants…")
        handle = factory.fal_submit(model, payload)
        _checkpoint_fal_handle(job_id, handle)
        result = factory.fal_poll(handle, max_wait=900)
        urls = factory.all_urls(result, "images", "image")
        _validate_outputs(urls, "image")

        wholesale_cost = round(CONFIG["rates"]["still_per_image_usd"] * count, 4)
        factory.log_generation({
            "scene_id": f"web:{job_id}", "rung": 1, "model": model,
            "prompt": prompt, "cost_usd": wholesale_cost, "customer_charged_usd": cost,
            "status": "success", "output_url": urls[0] if urls else None,
            "request_id": result.get("_request_id"),
        })
        _capture(job_id, reservation_key, note=f"web:{job_id} image x{count}")

        job = _update(job_id, status="done", stage="Done", outputs=urls,
                      request_id=result.get("_request_id"), credit_deducted=True)
        _persist(job)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - surfaced to the user, logged raw
        # SystemExit must be caught explicitly: factory.py's die() (called
        # by resolve_image/fal_upload/build_video_payload/fal_key on any
        # bad input) raises it via sys.exit(), and SystemExit inherits
        # from BaseException, not Exception -- a bare `except Exception`
        # here lets it escape this pool thread silently, leaving the
        # job stuck at status="running" forever with no error surfaced.
        factory.log_generation({
            "scene_id": f"web:{job_id}", "rung": 1, "prompt": prompt,
            "status": "failed", "error": str(exc)[:1500], "cost_usd": 0,
        })
        _release(job_id, reservation_key)
        job = _update(job_id, status="error", stage="Failed",
                      error=friendly_error(exc), error_raw=str(exc)[:1500])
        _persist(job)


def _run_video_job(job_id, prompt, image_url, seconds, model, wholesale_rate, cost, audio,
                    end_image_url, owner_id, reservation_key):
    _update(job_id, status="running", stage="Sending to fal.ai…")
    try:
        payload = factory.build_video_payload(
            model, prompt, image_url, seconds,
            CONFIG["defaults"]["final_resolution"],
            CONFIG["defaults"]["negative_prompt"],
            audio, CONFIG, end_frame=end_image_url or None,
        )
        _update(job_id, stage="Rendering video — this usually takes 1–3 minutes…")
        handle = factory.fal_submit(model, payload)
        _checkpoint_fal_handle(job_id, handle)
        result = factory.fal_poll(handle, max_wait=1800)
        url = factory.first_url(result, "video", "videos")
        _validate_outputs([url], "video")

        wholesale_cost = round(wholesale_rate * seconds, 4)
        factory.log_generation({
            "scene_id": f"web:{job_id}", "rung": 3, "model": model,
            "prompt": prompt, "start_frame_url": image_url,
            "duration_seconds": seconds, "cost_usd": wholesale_cost, "customer_charged_usd": cost,
            "status": "success", "output_url": url,
            "request_id": result.get("_request_id"),
        })
        _capture(job_id, reservation_key, note=f"web:{job_id} video {seconds}s {model}")

        job = _update(job_id, status="done", stage="Done", outputs=[url],
                      request_id=result.get("_request_id"), credit_deducted=True)
        _persist(job)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - see _run_image_job
        factory.log_generation({
            "scene_id": f"web:{job_id}", "rung": 3, "model": model,
            "prompt": prompt, "duration_seconds": seconds,
            "status": "failed", "error": str(exc)[:1500], "cost_usd": 0,
        })
        _release(job_id, reservation_key)
        job = _update(job_id, status="error", stage="Failed",
                      error=friendly_error(exc), error_raw=str(exc)[:1500])
        _persist(job)


def _run_postprod_job(job_id, op, file_url, params, model, wholesale_cost, cost, owner_id, reservation_key):
    _update(job_id, status="running", stage="Sending to fal.ai…")
    out_dir = WORK / "postprod"
    try:
        if op == "subtitles":
            payload = {"audio_url": file_url, "task": "transcribe", "chunk_level": "segment"}
            if params.get("lang"):
                payload["language"] = params["lang"]
            _update(job_id, stage="Transcribing…")
            handle = factory.fal_submit(model, payload)
            _checkpoint_fal_handle(job_id, handle)
            result = factory.fal_poll(handle, max_wait=600)
            chunks = result.get("chunks") or []
            if not chunks:
                raise RuntimeError("transcription returned no timed segments")

            out_dir.mkdir(parents=True, exist_ok=True)
            srt_path = out_dir / f"{job_id}.srt"
            factory.write_srt(chunks, srt_path)
            out_path = out_dir / f"{job_id}.mp4"
            style = params.get("style") or CONFIG["defaults"]["subtitles"]["force_style"]
            if not SAFE_FFMPEG_STYLE.fullmatch(style):
                raise ValueError("Unsupported characters in subtitle style.")
            escaped_srt = str(srt_path).replace("\\", "\\\\").replace(":", "\\:")

            _update(job_id, stage="Burning captions in…")
            res = subprocess.run(
                ["ffmpeg", "-y", "-i", file_url,
                 "-vf", f"subtitles={escaped_srt}:force_style='{style}'",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-c:a", "copy", str(out_path)],
                capture_output=True, text=True, check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(f"ffmpeg subtitle burn-in failed: {res.stderr[-500:]}")
            outputs = [f"/media/postprod/{job_id}.mp4"]
            request_id = result.get("_request_id")
        else:
            payload = _postprod_payload(op, file_url, params)
            _update(job_id, stage="Processing…")
            handle = factory.fal_submit(model, payload)
            _checkpoint_fal_handle(job_id, handle)
            result = factory.fal_poll(handle, max_wait=1800)
            url = factory.first_url(result, "video", "videos")
            _validate_outputs([url], "postprod output")
            # Own subdirectory per job: factory.download() names the file
            # from the URL's own basename (needed since bgremove/Bria
            # defaults to webm, not mp4 -- a webm served as "video/mp4"
            # fails to play), and a shared directory would risk one job's
            # file silently overwriting another's if fal ever reused a name.
            job_dir = out_dir / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            local_path = factory.download(url, job_dir)
            outputs = [f"/media/postprod/{job_id}/{local_path.name}"]
            request_id = result.get("_request_id")

        factory.log_generation({
            "scene_id": f"web:{job_id}", "op": op, "model": model,
            "cost_usd": wholesale_cost, "customer_charged_usd": cost, "status": "success",
            "output_url": outputs[0] if outputs else None, "request_id": request_id,
        })
        _capture(job_id, reservation_key, note=f"web:{job_id} postprod {op}")
        job = _update(job_id, status="done", stage="Done", outputs=outputs,
                      request_id=request_id, credit_deducted=True)
        _persist(job)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - see _run_image_job
        factory.log_generation({
            "scene_id": f"web:{job_id}", "op": op, "status": "failed",
            "error": str(exc)[:1500], "cost_usd": 0,
        })
        _release(job_id, reservation_key)
        job = _update(job_id, status="error", stage="Failed",
                      error=friendly_error(exc), error_raw=str(exc)[:1500])
        _persist(job)


def _run_motion_transfer_job(job_id, image_url, video_url, prompt, wholesale_cost, cost, owner_id, reservation_key):
    """Applies a reference video's motion to a static character image
    (fal-ai/kling-video/v2.6/standard/motion-control -- see
    scripts/config.json's _motion_transfer_note). Same discipline as every
    other paid worker here: `cost` was computed once up front from the
    source video's real probed duration and is threaded through unchanged,
    never recomputed."""
    _update(job_id, status="running", stage="Sending to fal.ai…")
    try:
        model = CONFIG["models"]["motion_transfer"]
        payload = {
            "image_url": image_url,
            "video_url": video_url,
            # Orientation follows the reference video's own framing/motion
            # rather than the still image's camera -- the point of this
            # feature is reproducing that video's motion, not its own.
            "character_orientation": "video",
        }
        if prompt:
            payload["prompt"] = prompt

        _update(job_id, stage="Transferring the motion — this can take a few minutes…")
        handle = factory.fal_submit(model, payload)
        _checkpoint_fal_handle(job_id, handle)
        result = factory.fal_poll(handle, max_wait=1800)
        url = factory.first_url(result, "video", "videos")
        _validate_outputs([url], "video")

        factory.log_generation({
            "scene_id": f"web:{job_id}", "op": "motion_transfer", "model": model,
            "cost_usd": wholesale_cost, "customer_charged_usd": cost, "status": "success", "output_url": url,
            "request_id": result.get("_request_id"),
        })
        _capture(job_id, reservation_key, note=f"web:{job_id} motion_transfer")
        job = _update(job_id, status="done", stage="Done", outputs=[url],
                      request_id=result.get("_request_id"), credit_deducted=True)
        _persist(job)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - see _run_image_job
        factory.log_generation({
            "scene_id": f"web:{job_id}", "op": "motion_transfer",
            "status": "failed", "error": str(exc)[:1500], "cost_usd": 0,
        })
        _release(job_id, reservation_key)
        job = _update(job_id, status="error", stage="Failed",
                      error=friendly_error(exc), error_raw=str(exc)[:1500])
        _persist(job)


AVATAR_DISCLOSURE_FONT = pathlib.Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
AVATAR_DISCLOSURE_TEXT = "AI-generated"


def _burn_avatar_disclosure(job_id, source_url):
    """Best-effort visible disclosure watermark on every avatar output --
    downloads the fal-hosted result, burns a small "AI-generated" label
    into the corner via ffmpeg's drawtext, and returns the local
    /media/... path to use instead of the original fal URL, or None if
    the watermark can't be produced for any reason (missing font file,
    ffmpeg failing, a download error). Never raises -- a disclosure
    problem must not block or fail the underlying paid generation; the
    job records which outcome actually happened (see "disclosure" on the
    job dict) and the UI's own on-screen disclosure label is the
    fallback either way (see webapp/static/i18n.js's avatar.disclosure)."""
    if not AVATAR_DISCLOSURE_FONT.exists():
        return None
    out_dir = WORK / "avatar"
    raw_path = None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = factory.download(source_url, out_dir / f"{job_id}_src.mp4")
        out_path = out_dir / f"{job_id}.mp4"
        escaped_font = str(AVATAR_DISCLOSURE_FONT).replace("\\", "\\\\").replace(":", "\\:")
        drawtext = (
            f"drawtext=fontfile={escaped_font}:text='{AVATAR_DISCLOSURE_TEXT}':"
            "fontcolor=white:fontsize=h/28:box=1:boxcolor=black@0.55:boxborderw=8:"
            "x=w-tw-16:y=h-th-16"
        )
        res = subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw_path), "-vf", drawtext,
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "copy", str(out_path)],
            capture_output=True, text=True, check=False, timeout=600,
        )
        if res.returncode != 0 or not out_path.exists():
            print(f"  WARNING: avatar disclosure watermark failed for job {job_id}: {res.stderr[-500:]}")
            return None
        return f"/media/avatar/{job_id}.mp4"
    except Exception as exc:  # noqa: BLE001 - disclosure is best-effort, must never fail the job
        print(f"  WARNING: avatar disclosure watermark errored for job {job_id}: {exc}")
        return None
    finally:
        if raw_path is not None:
            raw_path.unlink(missing_ok=True)


def _run_avatar_job(job_id, image_url, audio_url, prompt, resolution, turbo, wholesale_cost, cost,
                     owner_id, reservation_key):
    """Turns a photo + voice track into a talking-head video (OmniHuman).
    `cost` is computed once up front (by _generate_avatar, from the audio's
    real probed duration) and threaded through unchanged -- not
    recomputed here -- so the amount actually charged always matches the
    number the caller approved, the same discipline every other paid
    worker in this file follows."""
    _update(job_id, status="running", stage="Sending to fal.ai…")
    try:
        model = CONFIG["models"]["avatar"]
        payload = {"image_url": image_url, "audio_url": audio_url, "resolution": resolution}
        if prompt:
            payload["prompt"] = prompt
        if turbo:
            payload["turbo_mode"] = True

        _update(job_id, stage="Animating the photo — this can take a few minutes…")
        handle = factory.fal_submit(model, payload)
        _checkpoint_fal_handle(job_id, handle)
        result = factory.fal_poll(handle, max_wait=1800)
        url = factory.first_url(result, "video", "videos")
        _validate_outputs([url], "video")

        _update(job_id, stage="Adding disclosure watermark…")
        watermarked = _burn_avatar_disclosure(job_id, url)
        final_url = watermarked or url
        disclosure = "watermarked" if watermarked else "label-only"

        factory.log_generation({
            "scene_id": f"web:{job_id}", "op": "avatar", "model": model,
            "cost_usd": wholesale_cost, "customer_charged_usd": cost, "status": "success", "output_url": url,
            "request_id": result.get("_request_id"),
        })
        _capture(job_id, reservation_key, note=f"web:{job_id} avatar")
        job = _update(job_id, status="done", stage="Done", outputs=[final_url],
                      request_id=result.get("_request_id"), credit_deducted=True, disclosure=disclosure)
        _persist(job)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - see _run_image_job
        factory.log_generation({
            "scene_id": f"web:{job_id}", "op": "avatar",
            "status": "failed", "error": str(exc)[:1500], "cost_usd": 0,
        })
        _release(job_id, reservation_key)
        job = _update(job_id, status="error", stage="Failed",
                      error=friendly_error(exc), error_raw=str(exc)[:1500])
        _persist(job)


# ------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "VideoFactory"

    def log_message(self, fmt, *args):  # quieter, one line per request
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    # -- helpers ---------------------------------------------------------

    def _is_https(self):
        """True when this request reached us over HTTPS -- via a
        TLS-terminating proxy's X-Forwarded-Proto header (Railway's own
        domain sets this; see DEPLOY.md §3/§5), since this server itself
        always speaks plain HTTP (see Dockerfile). Used only to decide
        whether the session cookie gets the Secure flag; never trusted for
        anything else. Defaults to false (no Secure flag) so local plain-HTTP
        development keeps working unchanged."""
        return self.headers.get("X-Forwarded-Proto", "").strip().lower() == "https"

    def _send(self, code, payload, ctype="application/json", extra_headers=None):
        body = json.dumps(payload).encode() if ctype == "application/json" else payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for header in SECURITY_HEADERS:
            self.send_header(*header)
        for header in extra_headers or []:
            self.send_header(*header)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(length))

    def _read_raw_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        return self.rfile.read(length)

    # -- routing ---------------------------------------------------------

    def _require_basic_auth(self):
        """Returns True if the request may proceed. Sends the 401 itself
        and returns False otherwise. A no-op (always True) unless both
        WEBAPP_BASIC_AUTH_USER and WEBAPP_BASIC_AUTH_PASS are set."""
        creds = _basic_auth_credentials()
        if not creds:
            return True
        expected_user, expected_pass = creds
        header = self.headers.get("Authorization", "")
        given_user = given_pass = ""
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                given_user, _, given_pass = decoded.partition(":")
            except Exception:
                pass
        if hmac.compare_digest(given_user, expected_user) and hmac.compare_digest(given_pass, expected_pass):
            return True
        body = b'{"error": "Authentication required."}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Video Factory"')
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for header in SECURITY_HEADERS:
            self.send_header(*header)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass
        return False

    def do_GET(self):
        if not self._require_basic_auth():
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/config":
                return self._send(200, self._config_payload())
            if path == "/api/health":
                return self._send(200, self._health_payload())
            if path == "/api/auth/me":
                return self._auth_me()
            if path == "/api/characters":
                return self._list_characters()
            if path == "/api/jobs":
                owner = self._owner_id()
                if owner is None:
                    return self._send(401, {"error": "Sign in to see your jobs."})
                with _jobs_lock:
                    jobs = sorted(
                        (j for j in _jobs.values() if j.get("owner_id") == owner),
                        key=lambda j: j["created_at"], reverse=True,
                    )
                return self._send(200, {"jobs": jobs[:60]})
            if path.startswith("/api/jobs/"):
                owner = self._owner_id()
                if owner is None:
                    return self._send(401, {"error": "Sign in to see your jobs."})
                job_id = path.rsplit("/", 1)[-1]
                with _jobs_lock:
                    job = _jobs.get(job_id)
                if not job or job.get("owner_id") != owner:
                    return self._send(404, {"error": "No such job."})
                return self._send(200, job)
            if path == "/api/gallery":
                return self._list_gallery()
            if path.startswith("/media/"):
                return self._serve_media(path[len("/media/"):])
            if path == "/mcp/server.py":
                return self._serve_mcp_server_file()
            return self._serve_static(path)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"error": friendly_error(exc)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        # Payment-provider webhooks authenticate themselves -- Stripe via
        # Stripe-Signature, Payme via its own HTTP Basic Auth (a fixed
        # "Paycom" login, not this deployment's WEBAPP_BASIC_AUTH_*), Click
        # via sign_string. None of them know this server's own Basic Auth
        # credentials, so _require_basic_auth() would wrongly block every
        # one of them; and Stripe's signature must be verified against the
        # exact raw body bytes, not a value round-tripped through
        # json.loads the way every other route's body is. Handled entirely
        # separately, before both of those.
        if path in ("/api/topup/stripe/webhook", "/api/topup/payme",
                    "/api/topup/click/prepare", "/api/topup/click/complete"):
            return self._handle_payment_webhook(path)

        if not self._require_basic_auth():
            return
        try:
            body = self._read_json()
            if path == "/api/auth/signup":
                return self._auth_signup(body)
            if path == "/api/auth/login":
                return self._auth_login(body)
            if path == "/api/auth/logout":
                return self._auth_logout()
            if path == "/api/auth/oauth-callback":
                return self._auth_oauth_callback(body)
            if path == "/api/characters":
                return self._create_character(body)
            if path == "/api/characters/delete":
                return self._delete_character(body)
            if path == "/api/topup/create":
                return self._create_topup(body)
            if path == "/api/quote":
                return self._quote(body)
            if path == "/api/generate/image":
                return self._generate_image(body)
            if path == "/api/generate/video":
                return self._generate_video(body)
            if path == "/api/postprod/quote":
                return self._quote_postprod(body)
            if path == "/api/postprod/run":
                return self._generate_postprod(body)
            if path == "/api/avatar/quote":
                return self._quote_avatar(body)
            if path == "/api/avatar/run":
                return self._generate_avatar(body)
            if path == "/api/motion-transfer/quote":
                return self._quote_motion_transfer(body)
            if path == "/api/motion-transfer/run":
                return self._generate_motion_transfer(body)
            if path.startswith("/api/jobs/") and path.endswith("/publish"):
                job_id = path[len("/api/jobs/"):-len("/publish")]
                return self._toggle_publish(job_id, body)
            if path.startswith("/api/jobs/") and path.endswith("/report"):
                job_id = path[len("/api/jobs/"):-len("/report")]
                return self._report_job(job_id, body)
            return self._send(404, {"error": "Unknown endpoint."})
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"error": friendly_error(exc)})

    # -- endpoints -------------------------------------------------------

    def _config_payload(self):
        """Everything the UI needs to render. Deliberately contains no
        secrets — this response is safe to log or screenshot.

        Every model is listed -- including the two expensive "top quality,
        cost no object" ones -- rather than hidden behind a picker only
        power users find. `rate` and `tier` are still included for callers
        that need them (the MCP tools' get_info reasons about price/tier
        explicitly when an AI is picking a model on a user's behalf -- see
        mcp/README.md's "Picking a model") -- the website's own UI simply
        doesn't render either field: models are shown as one flat list of
        looks/styles with no dollar amount attached, and the final price is
        surfaced once, at the confirm-before-charging step, not while
        choosing a model."""
        rates = CONFIG["rates"]["final_take_per_second_usd_by_model"]
        models = [
            {
                "id": CONFIG["models"]["final_take_alt_hailuo"],
                "name": "Hailuo 02",
                "note": "Cheapest option of all.",
                "rate": _retail_rate(rates[CONFIG["models"]["final_take_alt_hailuo"]]),
                "tier": "budget",
                "durations": factory.duration_options_for_model(CONFIG["models"]["final_take_alt_hailuo"]),
            },
            {
                "id": CONFIG["models"]["final_take_alt_ltx"],
                "name": "LTX-2.3",
                "note": "Reliable, budget-friendly option.",
                "rate": _retail_rate(rates[CONFIG["models"]["final_take_alt_ltx"]]),
                "tier": "budget",
                "durations": factory.duration_options_for_model(CONFIG["models"]["final_take_alt_ltx"]),
            },
            {
                "id": CONFIG["models"]["final_take"],
                "name": "Kling 3.0",
                "note": "Best all-round motion. Recommended default.",
                "rate": _retail_rate(rates[CONFIG["models"]["final_take"]]),
                "tier": "standard",
                "default": True,
                "durations": factory.duration_options_for_model(CONFIG["models"]["final_take"]),
            },
            {
                "id": CONFIG["models"]["final_take_alt_veo"],
                "name": "Veo 3.1",
                "note": "Strongest face fidelity for close-ups.",
                "rate": _retail_rate(rates[CONFIG["models"]["final_take_alt_veo"]]),
                "tier": "standard",
                "durations": factory.duration_options_for_model(CONFIG["models"]["final_take_alt_veo"]),
            },
            {
                "id": CONFIG["models"]["final_take_alt_pixverse"],
                "name": "PixVerse 6",
                "note": "Stylized looks: anime, 3D, comic, cyberpunk.",
                "rate": _retail_rate(rates[CONFIG["models"]["final_take_alt_pixverse"]]),
                "tier": "standard",
                "durations": factory.duration_options_for_model(CONFIG["models"]["final_take_alt_pixverse"]),
            },
            {
                "id": CONFIG["models"]["final_take_alt_seedance"],
                "name": "Seedance 2.0",
                "note": "Best physics and camera precision.",
                "rate": _retail_rate(rates[CONFIG["models"]["final_take_alt_seedance"]]),
                "tier": "premium",
                "durations": factory.duration_options_for_model(CONFIG["models"]["final_take_alt_seedance"]),
            },
            {
                "id": CONFIG["models"]["final_take_alt_flux3"],
                "name": "FLUX 3",
                "note": "Black Forest Labs' newest model. Native audio, up to 20s.",
                "rate": _retail_rate(rates[CONFIG["models"]["final_take_alt_flux3"]]),
                "tier": "premium",
                "durations": factory.duration_options_for_model(CONFIG["models"]["final_take_alt_flux3"]),
            },
            {
                "id": CONFIG["models"]["final_take_alt_seedance25"],
                "name": "Seedance 2.5",
                "note": "Highest quality available. 16:9 shots only, significantly pricier.",
                "rate": _retail_rate(rates[CONFIG["models"]["final_take_alt_seedance25"]]),
                "tier": "premium",
                "aspect_ratio_lock": "16:9",
                "durations": factory.duration_options_for_model(CONFIG["models"]["final_take_alt_seedance25"]),
            },
        ]
        upscale_tiers = {k: _retail_rate(v) for k, v in CONFIG["rates"]["upscale_per_second_usd_by_tier"].items()
                         if not k.startswith("_")}
        return {
            "models": models,
            "presets": PRESETS,
            "aspect_ratios": ASPECT_RATIOS,
            "image_cost": _retail_rate(CONFIG["rates"]["still_per_image_usd"]),
            "postprod": {
                "upscale_tiers": upscale_tiers,
                "lipsync_rate": _retail_rate(CONFIG["rates"]["lipsync_per_second_usd"]),
                "subtitles_rate": _retail_rate(CONFIG["rates"]["transcription_per_second_usd"]),
                "bgremove_rate": _retail_rate(CONFIG["rates"]["bg_remove_per_second_usd"]),
            },
            "avatar": {
                "rate": _retail_rate(CONFIG["rates"]["avatar_per_second_usd"]),
                "resolutions": ["720p", "1080p"],
            },
            "topup": {
                # Each provider is opt-in per its own env vars (see
                # webapp/payments.py) -- listed here only once actually
                # configured, so the UI shows exactly the payment buttons
                # that will work rather than every button this project
                # knows how to build.
                "providers": [p for p, ok in (
                    ("stripe", payments.stripe_configured()),
                    ("payme", payments.payme_configured()),
                    ("click", payments.click_configured()),
                ) if ok],
            },
            "mcp": {
                # MCP_PUBLIC_URL is set by the operator only after deploying
                # mcp/server.py's Streamable HTTP mode as its own public
                # service (see mcp/README.md) -- empty until then, which the
                # /mcp instructions tab uses to show "not yet available"
                # instead of a URL nobody can actually reach.
                "http_url": os.environ.get("MCP_PUBLIC_URL", "").rstrip("/"),
            },
        }

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

    # -- auth --------------------------------------------------------------

    def _owner_id(self):
        """The job/history owner for this request: the signed-in user's id
        when Supabase is configured, or the constant LOCAL_OWNER when it
        isn't -- so single-tenant installs keep seeing all their own jobs
        with no login step, exactly as before this feature existed."""
        if not db.configured():
            return LOCAL_OWNER
        user = _current_user(self)
        return user["id"] if user else None

    def _auth_me(self):
        if not db.configured():
            return self._send(200, {"auth_enabled": False, "user": None})
        user = _current_user(self)
        if not user:
            return self._send(200, {"auth_enabled": True, "user": None})
        try:
            balance = db.get_balance(user["id"], user["access_token"])
        except db.SupabaseError:
            balance = None
        return self._send(200, {
            "auth_enabled": True,
            "user": {"email": user["email"], "id": user["id"]},
            "balance_usd": balance,
        })

    # -- characters (identity locks, reusable across shots) ---------------

    def _require_current_user(self):
        """Every characters endpoint needs a real signed-in user -- the
        same requirement generation itself now has (see
        _current_user_or_raise). Characters live in Supabase's `characters`
        table via PostgREST, RLS-scoped to owner_id = auth.uid(), so the
        caller's own access token is all that's needed here; no service
        role key involved, same as get_balance()."""
        user = _current_user(self)
        if not user:
            raise ValueError("Sign in to manage characters.")
        return user

    def _list_characters(self):
        user = self._require_current_user()
        try:
            rows = db.list_characters(user["id"], user["access_token"])
        except db.SupabaseError as exc:
            raise ValueError(str(exc)) from None
        return self._send(200, {"characters": rows})

    def _create_character(self, body):
        user = self._require_current_user()
        name = (body.get("name") or "").strip()
        lock_text = (body.get("lock_text") or "").strip()
        if not name:
            raise ValueError("Give this character a name.")
        if len(name) > 80:
            raise ValueError("Keep the name under 80 characters.")
        if not lock_text:
            raise ValueError("Describe what should stay the same in every shot.")
        if len(lock_text) > 2000:
            raise ValueError("Keep the description under 2000 characters.")
        refs = [_require_public_url(r, "Reference image", allow_data=True)
                for r in (body.get("reference_urls") or [])]
        if len(refs) > 4:
            raise ValueError("Up to 4 reference images per character.")
        notes = (body.get("notes") or "").strip() or None
        try:
            row = db.create_character(user["id"], user["access_token"], name, lock_text, refs, notes)
        except db.SupabaseError as exc:
            message = str(exc)
            if "duplicate key" in message or "already exists" in message:
                raise ValueError(f'You already have a character named "{name}".') from None
            raise ValueError(message) from None
        return self._send(200, {"character": row})

    def _delete_character(self, body):
        user = self._require_current_user()
        char_id = (body.get("id") or "").strip()
        if not char_id:
            raise ValueError("Missing character id.")
        try:
            db.delete_character(user["id"], user["access_token"], char_id)
        except db.SupabaseError as exc:
            raise ValueError(str(exc)) from None
        return self._send(200, {"ok": True})

    # -- gallery (opt-in public sharing of a user's own finished jobs) ---

    def _list_gallery(self):
        """No auth required -- these are the jobs their own owners
        explicitly opted into showing (see _toggle_publish), so viewing
        them is the whole point. Only public-safe fields go out: no
        owner_id, no cost_usd, nothing that identifies who made it or what
        it cost them."""
        with _jobs_lock:
            public_jobs = sorted(
                (j for j in _jobs.values() if j.get("public") and j.get("status") == "done" and j.get("outputs")),
                key=lambda j: j["created_at"], reverse=True,
            )
        shown = [{"id": j["id"], "kind": j["kind"], "outputs": j["outputs"], "created_at": j["created_at"]}
                 for j in public_jobs[:60]]
        return self._send(200, {"jobs": shown})

    def _toggle_publish(self, job_id, body):
        """Only the job's own owner can publish or un-publish it -- same
        ownership check /api/jobs/<id> already does for reading it."""
        owner = self._owner_id()
        if owner is None:
            raise ValueError("Sign in to share a video.")
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job or job.get("owner_id") != owner:
                raise ValueError("No such job.")
            if job.get("status") != "done" or not job.get("outputs"):
                raise ValueError("Only a finished generation can be shared.")
            job["public"] = bool(body.get("public"))
            snapshot = dict(job)
        _persist(snapshot)
        return self._send(200, {"public": snapshot["public"]})

    def _report_job(self, job_id, body):
        """Anyone can report a job -- deliberately no sign-in required:
        the person whose likeness was used without consent is very often
        NOT the account that generated it, and requiring an account would
        defeat the point. Reporting immediately and automatically
        unpublishes the job from the gallery (a soft moderation action
        taken without waiting for manual review, since a report already
        means someone is objecting to it being visible) and appends an
        entry to webapp_reports.jsonl for an operator to review -- there
        is no admin review UI yet at this project's stage; see
        webapp/README.md's moderation notes."""
        _check_report_rate_limit(self)
        reason = (body.get("reason") or "").strip()
        if not reason:
            raise ValueError("Describe why you're reporting this.")
        if len(reason) > 2000:
            raise ValueError("Keep the report under 2000 characters.")
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                raise ValueError("No such job.")
            job["public"] = False
            job["reported"] = True
            snapshot = dict(job)
        _persist(snapshot)
        _persist_report({
            "id": uuid.uuid4().hex[:12],
            "job_id": job_id,
            "reporter_ip": _client_ip(self),
            "reason": reason,
            "created_at": time.time(),
        })
        return self._send(200, {"received": True})

    # -- credit top-up (Stripe / Payme / Click) ---------------------------

    def _create_topup(self, body):
        """Starts a top-up: records it locally (status "pending"), then
        asks the chosen provider for a URL to send the browser to. The
        account is NOT credited here -- only the provider's own webhook
        (verified independently below) can do that. `origin` is the
        browser's own window.location.origin, validated the same way
        every other client-supplied URL in this file is
        (_require_public_url) -- used to build Stripe's success/cancel
        redirect targets and passed to Payme/Click as their own return
        url."""
        user = self._require_current_user()
        try:
            amount = float(body.get("amount_usd"))
        except (TypeError, ValueError):
            raise ValueError("Enter a valid amount.") from None
        if not (1 <= amount <= 1000):
            raise ValueError("Amount must be between $1 and $1000.")
        provider = (body.get("provider") or "").strip()
        origin = _require_public_url(body.get("origin"), "Origin")

        topup = _new_topup(user["id"], amount, provider)

        if provider == "stripe":
            if not payments.stripe_configured():
                raise ValueError("Stripe is not configured on this server.")
            try:
                session = payments.create_stripe_checkout_session(
                    amount, f"{origin}/?topup=success", f"{origin}/?topup=cancelled",
                    topup["id"], {"user_id": user["id"], "topup_id": topup["id"]},
                )
            except payments.PaymentError as exc:
                raise ValueError(str(exc)) from None
            _update_topup(topup["id"], provider_transaction_id=session.get("id"))
            return self._send(200, {"redirect_url": session["url"]})

        if provider == "payme":
            if not payments.payme_configured():
                raise ValueError("Payme is not configured on this server.")
            try:
                url = payments.payme_checkout_url(topup["id"], amount, _usd_to_uzs_rate(), return_url=origin)
            except payments.PaymentError as exc:
                raise ValueError(str(exc)) from None
            return self._send(200, {"redirect_url": url})

        if provider == "click":
            if not payments.click_configured():
                raise ValueError("Click is not configured on this server.")
            try:
                url = payments.click_checkout_url(topup["id"], amount, _usd_to_uzs_rate(), return_url=origin)
            except payments.PaymentError as exc:
                raise ValueError(str(exc)) from None
            return self._send(200, {"redirect_url": url})

        raise ValueError("Unknown payment provider.")

    def _handle_payment_webhook(self, path):
        """Entry point for every provider callback (see do_POST) -- none
        of these run through the normal do_POST try/except (that one
        calls _read_json() unconditionally, and ValueError -> 400 the
        way every other endpoint's errors do; a payment provider expects
        its own specific response shape and status code on failure, not
        this project's generic {"error": ...} body), so this has its own
        top-level exception handling."""
        try:
            raw = self._read_raw_body()
            if path == "/api/topup/stripe/webhook":
                return self._stripe_webhook(raw)
            if path == "/api/topup/payme":
                return self._payme_rpc(raw)
            if path == "/api/topup/click/prepare":
                return self._click_prepare(raw)
            if path == "/api/topup/click/complete":
                return self._click_complete(raw)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"error": "internal error"})

    def _stripe_webhook(self, raw):
        sig = self.headers.get("Stripe-Signature", "")
        try:
            event = payments.verify_stripe_webhook(raw, sig)
        except payments.PaymentError as exc:
            print(f"  WARNING: Stripe webhook rejected: {exc}")
            return self._send(400, {"error": str(exc)})
        if event.get("type") == "checkout.session.completed":
            session = event.get("data", {}).get("object", {})
            topup_id = session.get("client_reference_id")
            if topup_id and session.get("payment_status") == "paid":
                try:
                    _credit_topup(topup_id, note=f"stripe:{session.get('id')}")
                except payments.PaymentError as exc:
                    print(f"  WARNING: Stripe topup crediting failed for {topup_id}: {exc}")
                    # Non-2xx tells Stripe to retry this webhook later,
                    # instead of losing the payment because our own
                    # crediting step (e.g. Supabase) hiccuped.
                    return self._send(500, {"error": "credit failed, will retry"})
        return self._send(200, {"received": True})

    def _payme_rpc(self, raw):
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            return self._send(200, payments.payme_rpc_error(None, payments.PAYME_ERR_PARSE_ERROR, "Parse error"))
        request_id = req.get("id")
        if not payments.payme_check_auth(self.headers.get("Authorization", "")):
            return self._send(200, payments.payme_rpc_error(
                request_id, payments.PAYME_ERR_INVALID_AUTH, "Invalid authorization"))
        method = req.get("method")
        params = req.get("params") or {}
        try:
            handlers = {
                "CheckPerformTransaction": _payme_check_perform_transaction,
                "CreateTransaction": _payme_create_transaction,
                "PerformTransaction": _payme_perform_transaction,
                "CancelTransaction": _payme_cancel_transaction,
                "CheckTransaction": _payme_check_transaction,
                "GetStatement": _payme_get_statement,
            }
            handler = handlers.get(method)
            if not handler:
                return self._send(200, payments.payme_rpc_error(
                    request_id, payments.PAYME_ERR_METHOD_NOT_FOUND, "Method not found"))
            return self._send(200, handler(request_id, params))
        except payments.PaymentError as exc:
            return self._send(200, payments.payme_rpc_error(request_id, payments.PAYME_ERR_SYSTEM, str(exc)))

    def _click_prepare(self, raw):
        fields = _parse_click_body(raw)
        return self._send(200, _click_handle_prepare(fields))

    def _click_complete(self, raw):
        fields = _parse_click_body(raw)
        return self._send(200, _click_handle_complete(fields))

    def _auth_signup(self, body):
        if not db.configured():
            raise ValueError("Sign-up is not enabled on this server.")
        _check_auth_rate_limit(self)
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or "@" not in email:
            raise ValueError("Enter a valid email address.")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters.")
        try:
            result = db.sign_up(email, password)
        except db.SupabaseError as exc:
            raise ValueError(str(exc)) from None
        token = result.get("access_token")
        if not token:
            return self._send(200, {
                "user": None,
                "message": "Account created. Check your email to confirm before signing in.",
            })
        return self._send(200, {"user": {"email": email}},
                          extra_headers=[_session_cookie_header(
                              token, result.get("expires_in", 3600), secure=self._is_https())])

    def _auth_login(self, body):
        if not db.configured():
            raise ValueError("Sign-in is not enabled on this server.")
        _check_auth_rate_limit(self)
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or not password:
            raise ValueError("Enter your email and password.")
        try:
            result = db.sign_in(email, password)
        except db.SupabaseError as exc:
            raise ValueError("Incorrect email or password.") from None
        token = result.get("access_token")
        if not token:
            raise ValueError("Incorrect email or password.")
        user = result.get("user") or {}
        return self._send(200, {"user": {"email": user.get("email", email)}},
                          extra_headers=[_session_cookie_header(
                              token, result.get("expires_in", 3600), secure=self._is_https())])

    def _auth_oauth_callback(self, body):
        """Completes Google (or any future Supabase OAuth provider) sign-in.
        The browser lands here after Supabase's own /auth/v1/authorize
        redirect returns an access_token in the URL fragment -- the token is
        never trusted blindly, db.get_user() re-validates it against
        Supabase itself before a session cookie is ever set, exactly like
        every other path that ends in _session_cookie_header."""
        if not db.configured():
            raise ValueError("Sign-in is not enabled on this server.")
        token = (body.get("access_token") or "").strip()
        if not token:
            raise ValueError("Missing access token.")
        user = db.get_user(token)
        if not user or not user.get("id"):
            raise ValueError("Could not verify that sign-in. Try again.")
        expires_in = int(body.get("expires_in") or 3600)
        return self._send(200, {"user": {"email": user.get("email")}},
                          extra_headers=[_session_cookie_header(token, expires_in, secure=self._is_https())])

    def _auth_logout(self):
        return self._send(200, {"ok": True}, extra_headers=[_clear_cookie_header(secure=self._is_https())])

    def _quote(self, body):
        """Price a paid call without spending anything — the web equivalent
        of `factory.py cost`."""
        model = body.get("model") or CONFIG["models"]["final_take"]
        valid_durations = factory.duration_options_for_model(model)
        seconds = int(body.get("seconds") or valid_durations[0])
        if seconds not in valid_durations:
            raise ValueError(f"This model supports these durations: {valid_durations} seconds.")
        wholesale_rate = CONFIG["rates"]["final_take_per_second_usd_by_model"].get(model)
        if wholesale_rate is None:
            raise ValueError("That model has no price on file, so it cannot be run.")
        rate = _retail_rate(wholesale_rate)
        cost = round(rate * seconds, 4)
        return self._send(200, {
            "model": model, "seconds": seconds, "rate": rate, "cost_usd": cost,
            "pricing_version": PRICING_VERSION,
            "shown_as": f"{seconds}s x ${rate}/s = ${cost}",
        })

    def _current_user_or_raise(self):
        user = _current_user(self)
        if not user:
            raise ValueError("Sign in to generate.")
        return user

    def _require_pricing_version(self, body):
        """Optional, backward-compatible SKU-parity check: if the caller
        echoes back the pricing_version a quote gave it and it no longer
        matches this process's current CONFIG, the price may have been
        computed under different rules since that quote was issued (e.g. a
        redeploy between quote and confirm) -- reject and ask for a fresh
        quote rather than trust a stale one. A caller that doesn't send it
        (older client, or an endpoint like image generation that skips the
        quote step) isn't blocked by this -- the approved_cost check next
        to every call site is the hard money gate either way."""
        quoted = body.get("pricing_version")
        if quoted is not None and quoted != PRICING_VERSION:
            raise ValueError(
                "Pricing was updated on the server since this was quoted. "
                "Get a fresh price and try again."
            )

    def _reserve_funds(self, user, cost, reservation_key, note):
        """Reserve `cost` from user's spendable balance before submitting
        anything to the provider -- the one choke point every paid
        endpoint (image, video, postprod, avatar, motion transfer) calls
        through. Fails closed: raises ValueError (nothing reserved,
        nothing submitted) if the user doesn't have enough balance, or if
        billing can't be verified at all right now (ledger/Supabase
        unreachable) -- a paid action never silently runs for free just
        because billing was unavailable to check. Returns the
        ledger.Reservation on success."""
        try:
            return ledger.get_ledger().reserve(user["id"], cost, reservation_key, note=note)
        except ledger.InsufficientFundsError:
            balance = db.get_balance(user["id"], user["access_token"])
            raise ValueError(
                f"Not enough credits: balance is ${balance:.2f}, this costs ${cost:.2f}. "
                "Ask an admin to top up your account."
            ) from None
        except ledger.LedgerError as exc:
            raise ValueError(f"Could not verify billing right now: {exc}") from None

    def _start_paid_job(self, endpoint, user, body, cost, kind, worker, worker_args, note, **job_fields):
        """Shared tail end of every paid endpoint below: dedupe on the
        request's idempotency key, reserve funds, persist the job BEFORE
        the provider is ever called (durable checkpoint -- see
        _recover_orphaned_jobs), then hand it to the bounded job pool.
        `worker_args` is everything the worker needs *except* the job id
        (prepended) and the reservation key (appended) -- both threaded
        through here so every call site doesn't have to repeat that
        wiring. Returns the (202, job) response tuple's job dict; the
        caller sends it."""
        idem_key = _idempotency_key(endpoint, user["id"], body)
        existing = _dedup_lookup(idem_key)
        if existing is not None:
            return existing
        reservation_key = _reservation_key_for(idem_key)
        self._reserve_funds(user, cost, reservation_key, note)
        job = _new_job(kind, user["id"], cost_usd=cost,
                       idempotency_key=idem_key, reservation_id=reservation_key, **job_fields)
        _persist(job)
        JOB_EXECUTOR.submit(worker, job["id"], *worker_args, user["id"], reservation_key)
        return job

    def _generate_image(self, body):
        """approved_cost is OPTIONAL here, unlike video/postprod: the web UI
        shows the live price inline and treats a deliberate button click as
        approval, no modal needed -- images are cheap enough that adding a
        confirm dialog was a net usability loss (see webapp redesign notes).
        A caller that DOES pass approved_cost gets the exact same hard
        enforcement as every paid endpoint -- this is for programmatic
        callers (the MCP server) that have no human clicking a button in
        front of them and need the real confirmation round trip instead."""
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("Describe the shot before generating.")
        if len(prompt) > 5000:
            raise ValueError("That prompt is too long. Keep it under 5000 characters.")
        count = int(body.get("count") or 5)
        if not 1 <= count <= 6:
            raise ValueError("Variant count must be between 1 and 6.")
        aspect = body.get("aspect") or CONFIG["defaults"]["aspect_ratio"]
        if aspect not in ASPECT_RATIOS:
            raise ValueError("Unsupported aspect ratio.")
        refs = [_require_public_url(r, "Reference image", allow_data=True) for r in (body.get("refs") or [])]

        rate = _retail_rate(CONFIG["rates"]["still_per_image_usd"])
        cost = round(rate * count, 4)
        approved = body.get("approved_cost")
        if approved is not None and abs(float(approved) - cost) > 0.005:
            raise ValueError(
                f"The price changed to ${cost} since it was quoted. "
                "Nothing was charged — re-confirm to continue."
            )

        user = self._current_user_or_raise()
        job = self._start_paid_job(
            "image", user, body, cost, "image", _run_image_job,
            (prompt, count, aspect, refs, cost), f"web:image x{count}",
            prompt=prompt, count=count, aspect=aspect,
        )
        return self._send(202, job)

    def _generate_video(self, body):
        """Paid. Mirrors the CLI's --i-approve-cost gate: the caller must
        echo back the exact cost the server quoted, or nothing runs."""
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("Describe the motion before generating.")
        if not (body.get("image_url") or "").strip():
            raise ValueError("Pick a starting image first — video is always generated from one.")
        image_url = _require_public_url(body.get("image_url"), "Starting image", allow_data=True)
        end_image_url = body.get("end_image_url")
        if end_image_url:
            end_image_url = _require_public_url(end_image_url, "End frame image", allow_data=True)

        model = body.get("model") or CONFIG["models"]["final_take"]
        wholesale_rate = CONFIG["rates"]["final_take_per_second_usd_by_model"].get(model)
        if wholesale_rate is None:
            raise ValueError("That model has no price on file, so it cannot be run.")

        valid_durations = factory.duration_options_for_model(model)
        seconds = int(body.get("seconds") or valid_durations[0])
        if seconds not in valid_durations:
            raise ValueError(f"This model supports these durations: {valid_durations} seconds.")

        _require_seedance25_ratio(model, image_url)

        rate = _retail_rate(wholesale_rate)
        cost = round(rate * seconds, 4)
        approved = body.get("approved_cost")
        if approved is None:
            raise ValueError(f"This costs ${cost}. Confirm the cost to continue.")
        if abs(float(approved) - cost) > 0.005:
            raise ValueError(
                f"The price changed to ${cost} since it was quoted. "
                "Nothing was charged — re-confirm to continue."
            )
        self._require_pricing_version(body)

        user = self._current_user_or_raise()
        audio = bool(body.get("audio", CONFIG["defaults"]["final_audio"]))
        job = self._start_paid_job(
            "video", user, body, cost, "video", _run_video_job,
            (prompt, image_url, seconds, model, wholesale_rate, cost, audio, end_image_url),
            f"web:video {seconds}s {model}",
            prompt=prompt, image_url=image_url, seconds=seconds, model=model,
        )
        return self._send(202, job)

    # -- post-production ---------------------------------------------------

    def _quote_postprod(self, body):
        op = body.get("op")
        file_url = _require_public_url(body.get("file_url"), "Video file")
        model, wholesale_rate = _postprod_rate(op, body)
        try:
            duration = factory.probe_duration(file_url)
        except SystemExit:
            raise ValueError("Could not read that file's duration — is the URL still reachable?") from None
        rate = _retail_rate(wholesale_rate)
        cost = round(rate * duration, 4)
        return self._send(200, {
            "op": op, "model": model, "duration_seconds": round(duration, 2),
            "rate": rate, "cost_usd": cost, "pricing_version": PRICING_VERSION,
            "shown_as": f"{duration:.1f}s x ${rate}/s = ${cost}",
        })

    def _generate_postprod(self, body):
        """Paid (except a $0 estimate is still possible for a near-zero-length
        clip). Same gate as image/video: the caller must echo back the exact
        cost this endpoint just quoted, or nothing runs."""
        op = body.get("op")
        file_url = _require_public_url(body.get("file_url"), "Video file")
        model, wholesale_rate = _postprod_rate(op, body)
        try:
            duration = factory.probe_duration(file_url)
        except SystemExit:
            raise ValueError("Could not read that file's duration — is the URL still reachable?") from None
        rate = _retail_rate(wholesale_rate)
        cost = round(rate * duration, 4)
        wholesale_cost = round(wholesale_rate * duration, 4)

        if op == "subtitles":
            style = body.get("style") or CONFIG["defaults"]["subtitles"]["force_style"]
            if not SAFE_FFMPEG_STYLE.fullmatch(style):
                raise ValueError("Unsupported characters in subtitle style.")

        approved = body.get("approved_cost")
        if approved is None:
            raise ValueError(f"This costs ${cost}. Confirm the cost to continue.")
        if abs(float(approved) - cost) > 0.005:
            raise ValueError(
                f"The price changed to ${cost} since it was quoted. "
                "Nothing was charged — re-confirm to continue."
            )
        self._require_pricing_version(body)

        user = self._current_user_or_raise()
        job = self._start_paid_job(
            "postprod", user, body, cost, "postprod", _run_postprod_job,
            (op, file_url, body, model, wholesale_cost, cost), f"web:postprod {op}",
            op=op, model=model, duration_seconds=round(duration, 2),
        )
        return self._send(202, job)

    # -- talking avatar (image + voice track -> new video) ---------------

    # -- motion transfer (reference video's motion -> a static photo) ----

    def _quote_motion_transfer(self, body):
        """Priced off the reference video's own probed length, same
        pattern as avatar/lipsync."""
        image_url = _require_public_url(body.get("image_url"), "Character photo", allow_data=True)
        video_url = _require_public_url(body.get("video_url"), "Reference video")
        rate = _retail_rate(CONFIG["rates"]["motion_transfer_per_second_usd"])
        try:
            duration = factory.probe_duration(video_url)
        except SystemExit:
            raise ValueError("Could not read that video's duration — is the URL still reachable?") from None
        cost = round(rate * duration, 4)
        return self._send(200, {
            "duration_seconds": round(duration, 2), "rate": rate, "cost_usd": cost,
            "pricing_version": PRICING_VERSION,
            "shown_as": f"{duration:.1f}s x ${rate}/s = ${cost}",
        })

    def _generate_motion_transfer(self, body):
        image_url = _require_public_url(body.get("image_url"), "Character photo", allow_data=True)
        video_url = _require_public_url(body.get("video_url"), "Reference video")
        prompt = (body.get("prompt") or "").strip()
        consent_type = _require_identity_consent(body)

        wholesale_rate = CONFIG["rates"]["motion_transfer_per_second_usd"]
        try:
            duration = factory.probe_duration(video_url)
        except SystemExit:
            raise ValueError("Could not read that video's duration — is the URL still reachable?") from None
        rate = _retail_rate(wholesale_rate)
        cost = round(rate * duration, 4)
        wholesale_cost = round(wholesale_rate * duration, 4)

        approved = body.get("approved_cost")
        if approved is None:
            raise ValueError(f"This costs ${cost}. Confirm the cost to continue.")
        if abs(float(approved) - cost) > 0.005:
            raise ValueError(
                f"The price changed to ${cost} since it was quoted. "
                "Nothing was charged — re-confirm to continue."
            )
        self._require_pricing_version(body)

        user = self._current_user_or_raise()
        job = self._start_paid_job(
            "motion_transfer", user, body, cost, "motion_transfer", _run_motion_transfer_job,
            (image_url, video_url, prompt, wholesale_cost, cost), "web:motion_transfer",
            duration_seconds=round(duration, 2),
            consent_type=consent_type, consent_at=time.time(),
        )
        return self._send(202, job)

    def _quote_avatar(self, body):
        """Price a talking-avatar generation without spending anything.
        Priced off the voice track's own probed length, same as lipsync."""
        image_url = _require_public_url(body.get("image_url"), "Photo")
        audio_url = _require_public_url(body.get("audio_url"), "Voice track")
        rate = _retail_rate(CONFIG["rates"]["avatar_per_second_usd"])
        try:
            duration = factory.probe_duration(audio_url)
        except SystemExit:
            raise ValueError("Could not read that audio file's duration — is the URL still reachable?") from None
        cost = round(rate * duration, 4)
        return self._send(200, {
            "duration_seconds": round(duration, 2), "rate": rate, "cost_usd": cost,
            "pricing_version": PRICING_VERSION,
            "shown_as": f"{duration:.1f}s x ${rate}/s = ${cost}",
        })

    def _generate_avatar(self, body):
        image_url = _require_public_url(body.get("image_url"), "Photo")
        audio_url = _require_public_url(body.get("audio_url"), "Voice track")
        prompt = (body.get("prompt") or "").strip()
        resolution = body.get("resolution") or "1080p"
        if resolution not in ("720p", "1080p"):
            raise ValueError("Resolution must be 720p or 1080p.")
        turbo = bool(body.get("turbo"))
        consent_type = _require_identity_consent(body)

        wholesale_rate = CONFIG["rates"]["avatar_per_second_usd"]
        try:
            duration = factory.probe_duration(audio_url)
        except SystemExit:
            raise ValueError("Could not read that audio file's duration — is the URL still reachable?") from None
        rate = _retail_rate(wholesale_rate)
        cost = round(rate * duration, 4)
        wholesale_cost = round(wholesale_rate * duration, 4)

        approved = body.get("approved_cost")
        if approved is None:
            raise ValueError(f"This costs ${cost}. Confirm the cost to continue.")
        if abs(float(approved) - cost) > 0.005:
            raise ValueError(
                f"The price changed to ${cost} since it was quoted. "
                "Nothing was charged — re-confirm to continue."
            )
        self._require_pricing_version(body)

        user = self._current_user_or_raise()
        _check_avatar_rate_limit(user["id"])
        job = self._start_paid_job(
            "avatar", user, body, cost, "avatar", _run_avatar_job,
            (image_url, audio_url, prompt, resolution, turbo, wholesale_cost, cost), "web:avatar",
            duration_seconds=round(duration, 2),
            consent_type=consent_type, consent_at=time.time(),
        )
        return self._send(202, job)

    # -- files -----------------------------------------------------------

    def _serve_media(self, rel):
        """Serve generated files out of work/. Path is resolved and confined
        to work/ so a crafted URL cannot read elsewhere on disk -- and, in
        multi-tenant mode, gated to the job's own owner or an explicitly
        published gallery item (see _media_job_for_path): a job's real
        output URL isn't guessable in a way that matters (job ids are
        random), but a signed-in user's own browser history / shared link
        shouldn't rely on that alone, and someone else's file must not be
        readable just because its path is known.

        Single-tenant mode (no Supabase configured) keeps the previous
        open behaviour deliberately -- there's no concept of "another
        tenant" to protect against there; _owner_id() already treats every
        request as the same LOCAL_OWNER for job history too."""
        rel = urllib.parse.unquote(rel)
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", rel or ""):
            return self._send(400, {"error": "Bad media path."})
        target = (WORK / rel).resolve()
        if not str(target).startswith(str(WORK.resolve()) + "/") or not target.is_file():
            return self._send(404, {"error": "Not found."})

        if db.configured():
            job = _media_job_for_path(rel)
            if not job or not job.get("public"):
                owner = self._owner_id()
                if not job or not owner or job.get("owner_id") != owner:
                    return self._send(404, {"error": "Not found."})

        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype=ctype)

    def _serve_static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            return self._send(404, {"error": "Not found."})
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            return self._send(404, {"error": "Not found."})
        ctype = mimetypes.guess_type(target.name)[0] or "text/plain"
        self._send(200, target.read_bytes(), ctype=ctype)

    def _serve_mcp_server_file(self):
        """Lets a user download the actual mcp/server.py this deployment
        runs, straight from the /mcp instructions tab, without needing the
        whole repo -- it's a single stdlib-only file (see mcp/README.md).
        Fixed path, no user input in it, so no traversal risk."""
        target = ROOT / "mcp" / "server.py"
        if not target.is_file():
            return self._send(404, {"error": "Not found."})
        self._send(200, target.read_bytes(), ctype="text/x-python; charset=utf-8")


def main():
    ap = argparse.ArgumentParser(description="Video Factory web app")
    # Railway (and most PaaS platforms) assign a container a port at
    # runtime via the PORT env var and route traffic to exactly that port
    # -- an app that ignores it and always binds 8000 is unreachable
    # there. --port still wins if passed explicitly; PORT is just the
    # default's source, so nothing changes for a plain local run.
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    _load_persisted()
    _load_persisted_topups()
    _recover_orphaned_jobs()

    if not os.environ.get("FAL_KEY", "").strip():
        print("  WARNING: FAL_KEY is not set — the UI will load but generation will fail.")
    if not db.configured():
        print(
            "  NOTE: SUPABASE_URL/SUPABASE_ANON_KEY are not set. The page and job "
            "history still load, but nobody can generate anything -- every paid "
            "endpoint requires a signed-in user unconditionally now (see "
            "webapp/README.md's 'Access control'). Set them up before expecting "
            "generation to work."
        )
    if _basic_auth_credentials():
        print("  HTTP Basic Auth is ON (WEBAPP_BASIC_AUTH_USER/PASS set).")
    elif args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "  WARNING: binding to a non-local address with no access control. "
            "Set WEBAPP_BASIC_AUTH_USER/WEBAPP_BASIC_AUTH_PASS, put this behind "
            "a reverse proxy with auth, or run in multi-user (Supabase) mode."
        )

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"  Video Factory running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
