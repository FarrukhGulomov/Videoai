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
import hmac
import http.cookies
import importlib.util
import json
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import supabase_client as db

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = pathlib.Path(__file__).resolve().parent / "static"
WORK = ROOT / "work"
JOBS_FILE = WORK / "webapp_jobs.jsonl"

MAX_BODY_BYTES = 2 * 1024 * 1024  # prompts and settings only; media goes by URL

# "local" is the owner_id for every job when Supabase isn't configured --
# this is what keeps the pre-multi-user single-tenant flow working exactly
# as before with zero config changes (see supabase_client.configured()).
LOCAL_OWNER = "local"
SESSION_COOKIE = "vf_session"


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


def _session_cookie_header(access_token, max_age):
    return ("Set-Cookie", f"{SESSION_COOKIE}={access_token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={max_age}")


def _clear_cookie_header():
    return ("Set-Cookie", f"{SESSION_COOKIE}=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0")


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


def _require_public_url(value, field_name, allow_data=False):
    """Boundary validation for every URL a client can hand us that later
    reaches resolve_image()/fal_upload() (which reads local files by
    path) or probe_duration()/ffmpeg (which shell out to fetch whatever
    it's given). Without this, a client could pass a local path (e.g.
    "../.env") or an internal address and have the server read or probe
    it on their behalf -- a local-file-disclosure / SSRF vector. Public
    http(s) is always allowed; data: URIs are self-contained (no fetch
    happens) so they're safe to allow where the caller opts in."""
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{field_name} is required.")
    schemes = ("http://", "https://") + (("data:",) if allow_data else ())
    if not value.startswith(schemes):
        allowed = "a public http(s) URL or a data: URI" if allow_data else "a public http(s) URL"
        raise ValueError(f"{field_name} must be {allowed}, not a local path.")
    return value


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
]

ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:3"]
DURATIONS = [4, 6, 8]

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

_jobs = {}
_jobs_lock = threading.Lock()


def _persist(job):
    """Append a terminal job to disk so history survives a restart. The
    per-generation ledger in factory.py stays the source of truth for
    spend; this file only backs the gallery."""
    WORK.mkdir(parents=True, exist_ok=True)
    with open(JOBS_FILE, "a") as fh:
        fh.write(json.dumps(job) + "\n")


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
        **fields,
    }
    with _jobs_lock:
        _jobs[job["id"]] = job
    return job


def _update(job_id, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)
            return dict(job)
    return None


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
        return "This model does not accept that duration. Pick 4, 6, or 8 seconds."
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

def _charge(job_id, charge_user_id, cost, note):
    """Deduct cost from charge_user_id's balance after a successful paid
    generation. No-op in single-tenant mode (charge_user_id is None). A
    deduction failure (e.g. service key missing) is recorded on the job
    but never undoes or blocks the already-completed generation -- the
    fal spend already happened regardless."""
    if not charge_user_id:
        return None
    try:
        db.record_spend(charge_user_id, -cost, note=note)
        return True
    except db.SupabaseError as exc:
        print(f"  WARNING: credit deduction failed for job {job_id}: {exc}")
        return False


def _run_image_job(job_id, prompt, count, aspect, refs, charge_user_id):
    _update(job_id, status="running", stage="Sending to fal.ai…")
    try:
        # `refs` are client-supplied and already validated as public
        # http(s)/data URLs by _generate_image -- used as-is, never
        # resolved against a local path. `canonical` is server-configured
        # (config.json, a repo-relative path) and is the only ref allowed
        # to go through the local-file upload path.
        canonical = CONFIG.get("identity", {}).get("canonical_face_ref")
        image_urls = list(refs or [])
        if canonical and (ROOT / canonical).exists():
            canonical_url = factory.resolve_image(str(ROOT / canonical))
            if canonical_url not in image_urls:
                image_urls.insert(0, canonical_url)

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
        result = factory.fal_run(model, payload)
        urls = factory.all_urls(result, "images", "image")

        cost = round(CONFIG["rates"]["still_per_image_usd"] * count, 4)
        factory.log_generation({
            "scene_id": f"web:{job_id}", "rung": 1, "model": model,
            "prompt": prompt, "cost_usd": cost,
            "status": "success", "output_url": urls[0] if urls else None,
            "request_id": result.get("_request_id"),
        })
        deducted = _charge(job_id, charge_user_id, cost, note=f"web:{job_id} image x{count}")

        job = _update(job_id, status="done", stage="Done", outputs=urls,
                      request_id=result.get("_request_id"), credit_deducted=deducted)
        _persist(job)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - surfaced to the user, logged raw
        # SystemExit must be caught explicitly: factory.py's die() (called
        # by resolve_image/fal_upload/build_video_payload/fal_key on any
        # bad input) raises it via sys.exit(), and SystemExit inherits
        # from BaseException, not Exception -- a bare `except Exception`
        # here lets it escape this daemon thread silently, leaving the
        # job stuck at status="running" forever with no error surfaced.
        factory.log_generation({
            "scene_id": f"web:{job_id}", "rung": 1, "prompt": prompt,
            "status": "failed", "error": str(exc)[:1500], "cost_usd": 0,
        })
        job = _update(job_id, status="error", stage="Failed",
                      error=friendly_error(exc), error_raw=str(exc)[:1500])
        _persist(job)


def _run_video_job(job_id, prompt, image_url, seconds, model, rate, audio, end_image_url, charge_user_id):
    _update(job_id, status="running", stage="Sending to fal.ai…")
    try:
        payload = factory.build_video_payload(
            model, prompt, image_url, seconds,
            CONFIG["defaults"]["final_resolution"],
            CONFIG["defaults"]["negative_prompt"],
            audio, CONFIG, end_frame=end_image_url or None,
        )
        _update(job_id, stage="Rendering video — this usually takes 1–3 minutes…")
        result = factory.fal_run(model, payload, max_wait=1800)
        url = factory.first_url(result, "video", "videos")

        cost = round(rate * seconds, 4)
        factory.log_generation({
            "scene_id": f"web:{job_id}", "rung": 3, "model": model,
            "prompt": prompt, "start_frame_url": image_url,
            "duration_seconds": seconds, "cost_usd": cost,
            "status": "success", "output_url": url,
            "request_id": result.get("_request_id"),
        })

        deducted = _charge(job_id, charge_user_id, cost, note=f"web:{job_id} video {seconds}s {model}")

        job = _update(job_id, status="done", stage="Done",
                      outputs=[url] if url else [],
                      request_id=result.get("_request_id"), credit_deducted=deducted)
        _persist(job)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - see _run_image_job
        factory.log_generation({
            "scene_id": f"web:{job_id}", "rung": 3, "model": model,
            "prompt": prompt, "duration_seconds": seconds,
            "status": "failed", "error": str(exc)[:1500], "cost_usd": 0,
        })
        job = _update(job_id, status="error", stage="Failed",
                      error=friendly_error(exc), error_raw=str(exc)[:1500])
        _persist(job)


def _run_postprod_job(job_id, op, file_url, params, model, cost, charge_user_id):
    _update(job_id, status="running", stage="Sending to fal.ai…")
    out_dir = WORK / "postprod"
    try:
        if op == "subtitles":
            payload = {"audio_url": file_url, "task": "transcribe", "chunk_level": "segment"}
            if params.get("lang"):
                payload["language"] = params["lang"]
            _update(job_id, stage="Transcribing…")
            result = factory.fal_run(model, payload, max_wait=600)
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
            result = factory.fal_run(model, payload, max_wait=1800)
            url = factory.first_url(result, "video", "videos")
            if not url:
                raise RuntimeError("no output returned")
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
            "cost_usd": cost, "status": "success",
            "output_url": outputs[0] if outputs else None, "request_id": request_id,
        })
        deducted = _charge(job_id, charge_user_id, cost, note=f"web:{job_id} postprod {op}")
        job = _update(job_id, status="done", stage="Done", outputs=outputs,
                      request_id=request_id, credit_deducted=deducted)
        _persist(job)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - see _run_image_job
        factory.log_generation({
            "scene_id": f"web:{job_id}", "op": op, "status": "failed",
            "error": str(exc)[:1500], "cost_usd": 0,
        })
        job = _update(job_id, status="error", stage="Failed",
                      error=friendly_error(exc), error_raw=str(exc)[:1500])
        _persist(job)


def _run_avatar_job(job_id, image_url, audio_url, prompt, resolution, turbo, cost, charge_user_id):
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
        result = factory.fal_run(model, payload, max_wait=1800)
        url = factory.first_url(result, "video", "videos")

        factory.log_generation({
            "scene_id": f"web:{job_id}", "op": "avatar", "model": model,
            "cost_usd": cost, "status": "success", "output_url": url,
            "request_id": result.get("_request_id"),
        })
        deducted = _charge(job_id, charge_user_id, cost, note=f"web:{job_id} avatar")
        job = _update(job_id, status="done", stage="Done",
                      outputs=[url] if url else [],
                      request_id=result.get("_request_id"), credit_deducted=deducted)
        _persist(job)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - see _run_image_job
        factory.log_generation({
            "scene_id": f"web:{job_id}", "op": "avatar",
            "status": "failed", "error": str(exc)[:1500], "cost_usd": 0,
        })
        job = _update(job_id, status="error", stage="Failed",
                      error=friendly_error(exc), error_raw=str(exc)[:1500])
        _persist(job)


# ------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "VideoFactory"

    def log_message(self, fmt, *args):  # quieter, one line per request
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    # -- helpers ---------------------------------------------------------

    def _send(self, code, payload, ctype="application/json", extra_headers=None):
        body = json.dumps(payload).encode() if ctype == "application/json" else payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
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
            if path.startswith("/media/"):
                return self._serve_media(path[len("/media/"):])
            return self._serve_static(path)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"error": friendly_error(exc)})

    def do_POST(self):
        if not self._require_basic_auth():
            return
        path = urllib.parse.urlparse(self.path).path
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
        power users find. `tier` lets the UI group them (budget/standard/
        premium) so a price-conscious user sees Kling first and a
        quality-first user can deliberately reach past it, without either
        one being hidden from the other."""
        rates = CONFIG["rates"]["final_take_per_second_usd_by_model"]
        models = [
            {
                "id": CONFIG["models"]["final_take_alt_ltx"],
                "name": "LTX-2.3",
                "note": "Cheapest option. Long single takes up to 20s.",
                "rate": rates[CONFIG["models"]["final_take_alt_ltx"]],
                "tier": "budget",
            },
            {
                "id": CONFIG["models"]["final_take"],
                "name": "Kling 3.0",
                "note": "Best all-round motion. Recommended default.",
                "rate": rates[CONFIG["models"]["final_take"]],
                "tier": "standard",
                "default": True,
            },
            {
                "id": CONFIG["models"]["final_take_alt_veo"],
                "name": "Veo 3.1",
                "note": "Strongest face fidelity for close-ups.",
                "rate": rates[CONFIG["models"]["final_take_alt_veo"]],
                "tier": "standard",
            },
            {
                "id": CONFIG["models"]["final_take_alt_seedance"],
                "name": "Seedance 2.0",
                "note": "Best physics and camera precision.",
                "rate": rates[CONFIG["models"]["final_take_alt_seedance"]],
                "tier": "premium",
            },
            {
                "id": CONFIG["models"]["final_take_alt_flux3"],
                "name": "FLUX 3",
                "note": "Black Forest Labs' newest model. Native audio, up to 20s.",
                "rate": rates[CONFIG["models"]["final_take_alt_flux3"]],
                "tier": "premium",
            },
            {
                "id": CONFIG["models"]["final_take_alt_seedance25"],
                "name": "Seedance 2.5",
                "note": "Highest quality available. 16:9 shots only, significantly pricier.",
                "rate": rates[CONFIG["models"]["final_take_alt_seedance25"]],
                "tier": "premium",
                "aspect_ratio_lock": "16:9",
            },
        ]
        upscale_tiers = {k: v for k, v in CONFIG["rates"]["upscale_per_second_usd_by_tier"].items()
                         if not k.startswith("_")}
        return {
            "models": models,
            "presets": PRESETS,
            "aspect_ratios": ASPECT_RATIOS,
            "durations": DURATIONS,
            "image_cost": CONFIG["rates"]["still_per_image_usd"],
            "postprod": {
                "upscale_tiers": upscale_tiers,
                "lipsync_rate": CONFIG["rates"]["lipsync_per_second_usd"],
                "subtitles_rate": CONFIG["rates"]["transcription_per_second_usd"],
                "bgremove_rate": CONFIG["rates"]["bg_remove_per_second_usd"],
            },
            "avatar": {
                "rate": CONFIG["rates"]["avatar_per_second_usd"],
                "resolutions": ["720p", "1080p"],
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

    def _auth_signup(self, body):
        if not db.configured():
            raise ValueError("Sign-up is not enabled on this server.")
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or "@" not in email:
            raise ValueError("Enter a valid email address.")
        if len(password) < 6:
            raise ValueError("Password must be at least 6 characters.")
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
                          extra_headers=[_session_cookie_header(token, result.get("expires_in", 3600))])

    def _auth_login(self, body):
        if not db.configured():
            raise ValueError("Sign-in is not enabled on this server.")
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
                          extra_headers=[_session_cookie_header(token, result.get("expires_in", 3600))])

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
                          extra_headers=[_session_cookie_header(token, expires_in)])

    def _auth_logout(self):
        return self._send(200, {"ok": True}, extra_headers=[_clear_cookie_header()])

    def _quote(self, body):
        """Price a paid call without spending anything — the web equivalent
        of `factory.py cost`."""
        model = body.get("model") or CONFIG["models"]["final_take"]
        seconds = int(body.get("seconds") or 6)
        if seconds not in DURATIONS:
            raise ValueError(f"Duration must be one of {DURATIONS} seconds.")
        rate = CONFIG["rates"]["final_take_per_second_usd_by_model"].get(model)
        if rate is None:
            raise ValueError("That model has no price on file, so it cannot be run.")
        cost = round(rate * seconds, 4)
        return self._send(200, {
            "model": model, "seconds": seconds, "rate": rate, "cost_usd": cost,
            "shown_as": f"{seconds}s x ${rate}/s = ${cost}",
        })

    def _require_funded_user(self, cost):
        """Shared gate for both generate endpoints: who is this request
        for, and (in multi-user mode) can they afford it. Returns
        (owner_id, charge_user_id) or raises ValueError with a message
        safe to show the caller."""
        owner = self._owner_id()
        if owner is None:
            raise ValueError("Sign in to generate.")
        if owner == LOCAL_OWNER:
            return owner, None
        user = _current_user(self)
        balance = db.get_balance(user["id"], user["access_token"])
        if balance < cost:
            raise ValueError(
                f"Not enough credits: balance is ${balance:.2f}, this costs ${cost:.2f}. "
                "Ask an admin to top up your account."
            )
        return owner, owner

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

        cost = round(CONFIG["rates"]["still_per_image_usd"] * count, 4)
        approved = body.get("approved_cost")
        if approved is not None and abs(float(approved) - cost) > 0.005:
            raise ValueError(
                f"The price changed to ${cost} since it was quoted. "
                "Nothing was charged — re-confirm to continue."
            )

        owner, charge_user_id = self._require_funded_user(cost)

        job = _new_job(
            "image", owner, prompt=prompt, count=count, aspect=aspect, cost_usd=cost,
        )
        threading.Thread(
            target=_run_image_job,
            args=(job["id"], prompt, count, aspect, refs, charge_user_id),
            daemon=True,
        ).start()
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

        seconds = int(body.get("seconds") or 6)
        if seconds not in DURATIONS:
            raise ValueError(f"Duration must be one of {DURATIONS} seconds.")

        model = body.get("model") or CONFIG["models"]["final_take"]
        rate = CONFIG["rates"]["final_take_per_second_usd_by_model"].get(model)
        if rate is None:
            raise ValueError("That model has no price on file, so it cannot be run.")
        _require_seedance25_ratio(model, image_url)

        cost = round(rate * seconds, 4)
        approved = body.get("approved_cost")
        if approved is None:
            raise ValueError(f"This costs ${cost}. Confirm the cost to continue.")
        if abs(float(approved) - cost) > 0.005:
            raise ValueError(
                f"The price changed to ${cost} since it was quoted. "
                "Nothing was charged — re-confirm to continue."
            )

        owner, charge_user_id = self._require_funded_user(cost)

        job = _new_job(
            "video", owner, prompt=prompt, image_url=image_url, seconds=seconds,
            model=model, cost_usd=cost,
        )
        threading.Thread(
            target=_run_video_job,
            args=(job["id"], prompt, image_url, seconds, model, rate,
                  bool(body.get("audio", CONFIG["defaults"]["final_audio"])),
                  end_image_url, charge_user_id),
            daemon=True,
        ).start()
        return self._send(202, job)

    # -- post-production ---------------------------------------------------

    def _quote_postprod(self, body):
        op = body.get("op")
        file_url = _require_public_url(body.get("file_url"), "Video file")
        model, rate = _postprod_rate(op, body)
        try:
            duration = factory.probe_duration(file_url)
        except SystemExit:
            raise ValueError("Could not read that file's duration — is the URL still reachable?") from None
        cost = round(rate * duration, 4)
        return self._send(200, {
            "op": op, "model": model, "duration_seconds": round(duration, 2),
            "rate": rate, "cost_usd": cost,
            "shown_as": f"{duration:.1f}s x ${rate}/s = ${cost}",
        })

    def _generate_postprod(self, body):
        """Paid (except a $0 estimate is still possible for a near-zero-length
        clip). Same gate as image/video: the caller must echo back the exact
        cost this endpoint just quoted, or nothing runs."""
        op = body.get("op")
        file_url = _require_public_url(body.get("file_url"), "Video file")
        model, rate = _postprod_rate(op, body)
        try:
            duration = factory.probe_duration(file_url)
        except SystemExit:
            raise ValueError("Could not read that file's duration — is the URL still reachable?") from None
        cost = round(rate * duration, 4)

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

        owner, charge_user_id = self._require_funded_user(cost)

        job = _new_job("postprod", owner, op=op, model=model, cost_usd=cost,
                       duration_seconds=round(duration, 2))
        threading.Thread(
            target=_run_postprod_job,
            args=(job["id"], op, file_url, body, model, cost, charge_user_id),
            daemon=True,
        ).start()
        return self._send(202, job)

    # -- talking avatar (image + voice track -> new video) ---------------

    def _quote_avatar(self, body):
        """Price a talking-avatar generation without spending anything.
        Priced off the voice track's own probed length, same as lipsync."""
        image_url = _require_public_url(body.get("image_url"), "Photo")
        audio_url = _require_public_url(body.get("audio_url"), "Voice track")
        rate = CONFIG["rates"]["avatar_per_second_usd"]
        try:
            duration = factory.probe_duration(audio_url)
        except SystemExit:
            raise ValueError("Could not read that audio file's duration — is the URL still reachable?") from None
        cost = round(rate * duration, 4)
        return self._send(200, {
            "duration_seconds": round(duration, 2), "rate": rate, "cost_usd": cost,
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

        rate = CONFIG["rates"]["avatar_per_second_usd"]
        try:
            duration = factory.probe_duration(audio_url)
        except SystemExit:
            raise ValueError("Could not read that audio file's duration — is the URL still reachable?") from None
        cost = round(rate * duration, 4)

        approved = body.get("approved_cost")
        if approved is None:
            raise ValueError(f"This costs ${cost}. Confirm the cost to continue.")
        if abs(float(approved) - cost) > 0.005:
            raise ValueError(
                f"The price changed to ${cost} since it was quoted. "
                "Nothing was charged — re-confirm to continue."
            )

        owner, charge_user_id = self._require_funded_user(cost)

        job = _new_job("avatar", owner, cost_usd=cost, duration_seconds=round(duration, 2))
        threading.Thread(
            target=_run_avatar_job,
            args=(job["id"], image_url, audio_url, prompt, resolution, turbo, cost, charge_user_id),
            daemon=True,
        ).start()
        return self._send(202, job)

    # -- files -----------------------------------------------------------

    def _serve_media(self, rel):
        """Serve generated files out of work/. Path is resolved and confined
        to work/ so a crafted URL cannot read elsewhere on disk."""
        rel = urllib.parse.unquote(rel)
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", rel or ""):
            return self._send(400, {"error": "Bad media path."})
        target = (WORK / rel).resolve()
        if not str(target).startswith(str(WORK.resolve()) + "/") or not target.is_file():
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

    if not os.environ.get("FAL_KEY", "").strip():
        print("  WARNING: FAL_KEY is not set — the UI will load but generation will fail.")
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
