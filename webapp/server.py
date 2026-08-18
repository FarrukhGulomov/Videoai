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
import importlib.util
import json
import mimetypes
import pathlib
import re
import shutil
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = pathlib.Path(__file__).resolve().parent / "static"
WORK = ROOT / "work"
JOBS_FILE = WORK / "webapp_jobs.jsonl"

MAX_BODY_BYTES = 2 * 1024 * 1024  # prompts and settings only; media goes by URL


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


def _new_job(kind, **fields):
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "status": "queued",
        "stage": "Queued",
        "created_at": time.time(),
        "outputs": [],
        "error": None,
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

def _run_image_job(job_id, prompt, count, aspect, refs):
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

        factory.log_generation({
            "scene_id": f"web:{job_id}", "rung": 1, "model": model,
            "prompt": prompt, "cost_usd": round(
                CONFIG["rates"]["still_per_image_usd"] * count, 4),
            "status": "success", "output_url": urls[0] if urls else None,
            "request_id": result.get("_request_id"),
        })

        job = _update(job_id, status="done", stage="Done", outputs=urls,
                      request_id=result.get("_request_id"))
        _persist(job)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, logged raw
        factory.log_generation({
            "scene_id": f"web:{job_id}", "rung": 1, "prompt": prompt,
            "status": "failed", "error": str(exc)[:1500], "cost_usd": 0,
        })
        job = _update(job_id, status="error", stage="Failed",
                      error=friendly_error(exc), error_raw=str(exc)[:1500])
        _persist(job)


def _run_video_job(job_id, prompt, image_url, seconds, model, rate, audio, end_image_url):
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

        job = _update(job_id, status="done", stage="Done",
                      outputs=[url] if url else [],
                      request_id=result.get("_request_id"))
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


# ------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "VideoFactory"

    def log_message(self, fmt, *args):  # quieter, one line per request
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    # -- helpers ---------------------------------------------------------

    def _send(self, code, payload, ctype="application/json"):
        body = json.dumps(payload).encode() if ctype == "application/json" else payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
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
            if path == "/api/jobs":
                with _jobs_lock:
                    jobs = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
                return self._send(200, {"jobs": jobs[:60]})
            if path.startswith("/api/jobs/"):
                job_id = path.rsplit("/", 1)[-1]
                with _jobs_lock:
                    job = _jobs.get(job_id)
                if not job:
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
            if path == "/api/quote":
                return self._quote(body)
            if path == "/api/generate/image":
                return self._generate_image(body)
            if path == "/api/generate/video":
                return self._generate_video(body)
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
        return {
            "models": models,
            "presets": PRESETS,
            "aspect_ratios": ASPECT_RATIOS,
            "durations": DURATIONS,
            "image_cost": CONFIG["rates"]["still_per_image_usd"],
        }

    def _health_payload(self):
        import os
        return {
            "fal_key_configured": bool(os.environ.get("FAL_KEY", "").strip()),
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
        }

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

    def _generate_image(self, body):
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("Describe the shot before generating.")
        if len(prompt) > 5000:
            raise ValueError("That prompt is too long. Keep it under 5000 characters.")
        count = int(body.get("count") or 3)
        if not 1 <= count <= 4:
            raise ValueError("Variant count must be between 1 and 4.")
        aspect = body.get("aspect") or CONFIG["defaults"]["aspect_ratio"]
        if aspect not in ASPECT_RATIOS:
            raise ValueError("Unsupported aspect ratio.")

        job = _new_job(
            "image", prompt=prompt, count=count, aspect=aspect,
            cost_usd=round(CONFIG["rates"]["still_per_image_usd"] * count, 4),
        )
        threading.Thread(
            target=_run_image_job,
            args=(job["id"], prompt, count, aspect, body.get("refs") or []),
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

        job = _new_job(
            "video", prompt=prompt, image_url=image_url, seconds=seconds,
            model=model, cost_usd=cost,
        )
        threading.Thread(
            target=_run_video_job,
            args=(job["id"], prompt, image_url, seconds, model, rate,
                  bool(body.get("audio", CONFIG["defaults"]["final_audio"])),
                  body.get("end_image_url")),
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
