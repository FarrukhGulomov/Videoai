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
import http.cookies
import importlib.util
import json
import mimetypes
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
        canonical = CONFIG.get("identity", {}).get("canonical_face_ref")
        all_refs = list(refs or [])
        if canonical and canonical not in all_refs and (ROOT / canonical).exists():
            all_refs.insert(0, canonical)

        model = CONFIG["models"]["still_edit"] if all_refs else CONFIG["models"]["still"]
        payload = {
            "prompt": prompt,
            "num_images": count,
            "aspect_ratio": aspect,
        }
        if all_refs:
            _update(job_id, stage="Uploading reference images…")
            payload["image_urls"] = [
                factory.resolve_image(str(ROOT / r) if not r.startswith(("http", "data:")) else r)
                for r in all_refs
            ]

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
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, logged raw
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
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
        factory.log_generation({
            "scene_id": f"web:{job_id}", "op": op, "status": "failed",
            "error": str(exc)[:1500], "cost_usd": 0,
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

    def do_GET(self):
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
            return self._send(404, {"error": "Unknown endpoint."})
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"error": friendly_error(exc)})

    # -- endpoints -------------------------------------------------------

    def _config_payload(self):
        """Everything the UI needs to render. Deliberately contains no
        secrets — this response is safe to log or screenshot."""
        rates = CONFIG["rates"]["final_take_per_second_usd_by_model"]
        models = [
            {
                "id": CONFIG["models"]["final_take"],
                "name": "Kling 3.0",
                "note": "Best all-round motion. Cheapest.",
                "rate": rates[CONFIG["models"]["final_take"]],
                "default": True,
            },
            {
                "id": CONFIG["models"]["final_take_alt_veo"],
                "name": "Veo 3.1",
                "note": "Strongest face fidelity for close-ups.",
                "rate": rates[CONFIG["models"]["final_take_alt_veo"]],
            },
            {
                "id": CONFIG["models"]["final_take_alt_seedance"],
                "name": "Seedance 2.0",
                "note": "Best physics and camera precision. Premium.",
                "rate": rates[CONFIG["models"]["final_take_alt_seedance"]],
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
        }

    def _health_payload(self):
        import os
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

        cost = round(CONFIG["rates"]["still_per_image_usd"] * count, 4)
        owner, charge_user_id = self._require_funded_user(cost)

        job = _new_job(
            "image", owner, prompt=prompt, count=count, aspect=aspect, cost_usd=cost,
        )
        threading.Thread(
            target=_run_image_job,
            args=(job["id"], prompt, count, aspect, body.get("refs") or [], charge_user_id),
            daemon=True,
        ).start()
        return self._send(202, job)

    def _generate_video(self, body):
        """Paid. Mirrors the CLI's --i-approve-cost gate: the caller must
        echo back the exact cost the server quoted, or nothing runs."""
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("Describe the motion before generating.")
        image_url = (body.get("image_url") or "").strip()
        if not image_url:
            raise ValueError("Pick a starting image first — video is always generated from one.")

        seconds = int(body.get("seconds") or 6)
        if seconds not in DURATIONS:
            raise ValueError(f"Duration must be one of {DURATIONS} seconds.")

        model = body.get("model") or CONFIG["models"]["final_take"]
        rate = CONFIG["rates"]["final_take_per_second_usd_by_model"].get(model)
        if rate is None:
            raise ValueError("That model has no price on file, so it cannot be run.")

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
                  body.get("end_image_url"), charge_user_id),
            daemon=True,
        ).start()
        return self._send(202, job)

    # -- post-production ---------------------------------------------------

    def _quote_postprod(self, body):
        op = body.get("op")
        file_url = (body.get("file_url") or "").strip()
        if not file_url:
            raise ValueError("Pick a finished video first.")
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
        file_url = (body.get("file_url") or "").strip()
        if not file_url:
            raise ValueError("Pick a finished video first.")
        model, rate = _postprod_rate(op, body)
        try:
            duration = factory.probe_duration(file_url)
        except SystemExit:
            raise ValueError("Could not read that file's duration — is the URL still reachable?") from None
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

        job = _new_job("postprod", owner, op=op, model=model, cost_usd=cost,
                       duration_seconds=round(duration, 2))
        threading.Thread(
            target=_run_postprod_job,
            args=(job["id"], op, file_url, body, model, cost, charge_user_id),
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
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    _load_persisted()

    import os
    if not os.environ.get("FAL_KEY", "").strip():
        print("  WARNING: FAL_KEY is not set — the UI will load but generation will fail.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"  Video Factory running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
