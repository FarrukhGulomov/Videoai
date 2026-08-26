#!/usr/bin/env python3
"""
Video Factory MCP server — lets Claude Desktop, Claude Code, or any other
MCP-speaking client (including MCP-capable GPT tooling) generate images and
videos through this project's webapp API.

Stdlib only, matching the rest of this project: no `mcp` pip package, the
JSON-RPC 2.0 / stdio wire protocol is implemented directly against the
official schema (verified against
raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol's
schema.json — modelcontextprotocol.io itself is blocked by this project's
sandbox network policy, so the schema was fetched from its source repo
instead of guessed).

This talks to a RUNNING `webapp/server.py` instance over HTTP -- it does
not call fal.ai directly. That is deliberate: every cost rule, every
default, and the whole quote-then-approve safety gate already live in
webapp/server.py (which itself reuses scripts/factory.py). Reimplementing
any of that here would be a second copy of the same logic, exactly what
this project has avoided everywhere else (the webapp reuses factory.py the
same way). It also means every price this server's tools quote is already
the customer-facing (marked-up) price webapp/server.py computes -- this
file never reads config.json or sees the wholesale fal.ai cost at all.

Two transports, chosen at startup, sharing the exact same tool logic
(TOOLS/HANDLERS/handle_message below have no transport-specific code):

  stdio (default) -- for Claude Desktop/Code: the client launches this
  script as a subprocess and speaks JSON-RPC over stdin/stdout. Nothing
  to deploy; see mcp/README.md for the config JSON.

  Streamable HTTP (--http / MCP_HTTP_PORT) -- for ChatGPT and any other
  remote MCP client: ChatGPT's custom connectors only speak to a remote
  HTTPS endpoint (Streamable HTTP or SSE), never to a local stdio
  subprocess, so stdio mode cannot serve it no matter how it's
  configured. This runs a small stdlib HTTP server instead, implementing
  the minimal server side of the Streamable HTTP transport (POST a
  JSON-RPC request, get a JSON-RPC response back -- no SSE stream, since
  none of these tools need server-initiated pushes). Deploy it like the
  webapp (e.g. a second Railway service) to get a public HTTPS URL.

Run:
  python3 webapp/server.py &        # the webapp must already be running
  python3 mcp/server.py             # stdio mode, launched by your MCP client
  python3 mcp/server.py --http 8300 # Streamable HTTP mode, for ChatGPT etc.

Configure via environment:
  VIDEO_FACTORY_URL         webapp base URL, default http://127.0.0.1:8000
  VIDEO_FACTORY_SESSION     optional -- a vf_session cookie value, only
                            needed if the webapp is in multi-user mode (see
                            mcp/README.md for how to obtain one; this is a
                            stopgap until a real API-key mode exists)
  VIDEO_FACTORY_BASIC_AUTH  optional -- "user:pass", only needed if the
                            webapp has WEBAPP_BASIC_AUTH_USER/PASS set
                            (see webapp/README.md's "Access control")
  MCP_HTTP_TOKEN            HTTP mode only -- a shared secret; every
                            request must send it as "Authorization: Bearer
                            <token>". Required once --http is used with a
                            non-loopback host, since this endpoint being
                            reachable at all means it can spend real money
                            through the webapp it's pointed at.

See mcp/README.md for the exact Claude Desktop / Claude Code / ChatGPT
setup, including how to get a public HTTPS URL for the HTTP mode.
"""

import base64
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_URL = os.environ.get("VIDEO_FACTORY_URL", "http://127.0.0.1:8000").rstrip("/")
SESSION_COOKIE = os.environ.get("VIDEO_FACTORY_SESSION", "").strip()
BASIC_AUTH = os.environ.get("VIDEO_FACTORY_BASIC_AUTH", "").strip()
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "video-factory"
SERVER_VERSION = "1.0.0"


def log(msg):
    """Stdout is reserved exclusively for JSON-RPC messages in stdio mode --
    the MCP stdio transport requires this, so all logging goes to stderr
    unconditionally (HTTP mode has no such constraint, but one log path
    for both modes is simpler and costs HTTP mode nothing)."""
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- webapp API

def api(method, path, body=None, timeout=300):
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if SESSION_COOKIE:
        headers["Cookie"] = f"vf_session={SESSION_COOKIE}"
    if BASIC_AUTH:
        headers["Authorization"] = "Basic " + base64.b64encode(BASIC_AUTH.encode()).decode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw).get("error") or raw.decode()
        except Exception:  # noqa: BLE001
            detail = raw.decode(errors="replace")
        raise RuntimeError(detail) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach the Video Factory server at {BASE_URL} ({exc.reason}). "
            f"Is `python3 webapp/server.py` running?"
        ) from None


def positive_int_arg(args, key, default):
    """args.get(key) or default treats an explicitly-passed 0 the same as
    "omitted" and silently substitutes the default -- for a PAID tool,
    that means a caller asking for 0 of something gets billed for the
    default amount instead of a clear rejection (and, for count in
    particular, it was quietly defeating h_quote_images's own `1 <=
    count <= 6` check, since the check ran on the already-substituted 5,
    never on the real 0 that was passed in). Missing/None uses the
    default unchanged; any explicit non-positive value raises instead."""
    value = args.get(key)
    if value is None:
        return default
    value = int(value)
    if value <= 0:
        raise RuntimeError(f"{key} must be a positive number, got {value}.")
    return value


def optional_positive_int(args, key):
    """Like positive_int_arg, but with no fallback default -- for `seconds`,
    where the "right" default now depends on which model is selected (each
    has its own valid duration set; see video_factory_get_info). Omitting
    `seconds` from the webapp request body entirely lets the webapp apply
    ITS model-aware default rather than this file guessing one that might
    not even be valid for the model in play. Returns None if omitted;
    raises on an explicit non-positive value, same as positive_int_arg."""
    value = args.get(key)
    if value is None:
        return None
    value = int(value)
    if value <= 0:
        raise RuntimeError(f"{key} must be a positive number, got {value}.")
    return value


def media_url(path):
    """Postprod results come back as server-relative paths (/media/...);
    still/video results are already absolute fal CDN URLs. Normalize both
    to something a caller outside this process can actually open."""
    if not path:
        return path
    return path if path.startswith("http") else f"{BASE_URL}{path}"


def wait_for_job(job_id, max_wait, poll_seconds=3):
    """Blocks until the job finishes or max_wait elapses -- mirrors
    scripts/factory.py's own fal_run(), which blocks the CLI the same way.
    Simpler for a calling LLM than requiring it to implement its own
    polling loop across multiple tool calls."""
    waited = 0
    while waited < max_wait:
        job = api("GET", f"/api/jobs/{job_id}")
        if job.get("status") in ("done", "error"):
            return job
        time.sleep(poll_seconds)
        waited += poll_seconds
    return {
        "status": "pending", "id": job_id,
        "note": (f"Still running after {max_wait}s. Call video_factory_check_job "
                 f"with job_id=\"{job_id}\" in a little while to get the result."),
    }


# -------------------------------------------------------------- tool handlers
#
# Every PAID tool requires an explicit approved_cost argument that must
# exactly match a fresh quote from the matching video_factory_quote_* tool
# -- the webapp API refuses a mismatch server-side, the same non-negotiable
# gate the CLI (--i-approve-cost) and the browser UI both use. This is not
# optional politeness: an LLM calling this server autonomously has no human
# clicking a confirm button in front of it, so the tool descriptions below
# state the requirement explicitly and the server initialize response
# repeats it once more as a standing instruction.

def h_get_info(_args):
    cfg = api("GET", "/api/config")
    lines = ["PRESETS (pass the id's motion style, or just describe your own idea):"]
    for p in cfg["presets"]:
        lines.append(f"  - {p['id']}: {p['name']} — {p['blurb']}")
    lines.append("")
    lines.append(f"IMAGES: ${cfg['image_cost']}/variant, 5 generated by default "
                 f"(${round(cfg['image_cost'] * 5, 2)} total) so the best one can be picked.")
    lines.append("")
    lines.append(
        "VIDEO MODELS -- Kling 3.0 is used if you don't pass `model`. Pass the exact "
        "`id` string below as `model` to video_factory_quote_video / video_factory_create_video "
        "to pick a different one. Ask the user which tier they want if price matters to them: "
        "budget < standard < premium is a real, meaningful price jump, not a marketing label -- "
        "premium exists for a user who has said quality matters more than cost, not as a silent "
        "upgrade."
    )
    for m in cfg["models"]:
        tag = " <- default" if m.get("default") else ""
        lock = f" [{m['aspect_ratio_lock']} shots only]" if m.get("aspect_ratio_lock") else ""
        durations = m.get("durations") or []
        lines.append(
            f"  - {m['id']} [{m.get('tier', '?')}] \"{m['name']}\": ${m['rate']}/s — {m['note']}{lock}{tag}\n"
            f"      durations (seconds): {durations}"
        )
    av = cfg.get("avatar")
    if av:
        lines.append("")
        lines.append(
            f"TALKING AVATAR: turns a photo + a voice-track URL into a new talking-head "
            f"video (no motion prompt -- the voice track drives the performance). "
            f"${av['rate']}/s, priced off the voice track's own length. Use "
            f"video_factory_quote_avatar / video_factory_create_talking_avatar, not "
            f"video_factory_create_video, for this."
        )
    pp = cfg.get("postprod")
    if pp:
        lines.append("")
        lines.append("ENHANCEMENTS (post-production, run on an existing video):")
        tiers = ", ".join(f"{k}=${v}/s" for k, v in pp["upscale_tiers"].items())
        lines.append(f"  - upscale: {tiers} (real detail added, not just resizing)")
        lines.append(f"  - bgremove: ${pp['bgremove_rate']}/s")
        lines.append(f"  - subtitles: ${pp['subtitles_rate']}/s (transcribes + burns in captions)")
        lines.append(f"  - lipsync: ${pp['lipsync_rate']}/s (needs a separate audio_url)")
    return "\n".join(lines)


def h_quote_images(args):
    cfg = api("GET", "/api/config")
    count = positive_int_arg(args, "count", 5)
    if not 1 <= count <= 6:
        raise RuntimeError("count must be between 1 and 6.")
    cost = round(cfg["image_cost"] * count, 4)
    return (f"{count} image variant(s) = ${cost}. Call video_factory_create_images with "
            f"approved_cost={cost} (and the same count, if you changed it from the default) to proceed.")


def h_create_images(args):
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("prompt is required.")
    count = positive_int_arg(args, "count", 5)
    approved = args.get("approved_cost")
    if approved is None:
        raise RuntimeError(
            "approved_cost is required. Call video_factory_quote_images first, "
            "show the user that price, and only then pass its exact cost_usd here."
        )
    job = api("POST", "/api/generate/image",
               {"prompt": prompt, "count": count, "approved_cost": approved})
    result = wait_for_job(job["id"], max_wait=90)
    if result.get("status") == "error":
        raise RuntimeError(f"Image generation failed: {result.get('error')}")
    if result.get("status") == "pending":
        return result["note"]
    outputs = result.get("outputs") or []
    if not outputs:
        return "The model finished but returned no images."
    lines = [f"Generated {len(outputs)} image(s) for ${result.get('cost_usd', 0)}:"]
    lines += [f"  {i}. {url}" for i, url in enumerate(outputs, 1)]
    lines.append("")
    lines.append("Pick one URL and pass it as image_url to video_factory_quote_video / "
                 "video_factory_create_video.")
    return "\n".join(lines)


def h_quote_video(args):
    seconds = optional_positive_int(args, "seconds")
    body = {}
    if seconds is not None:
        body["seconds"] = seconds
    model = (args.get("model") or "").strip()
    if model:
        body["model"] = model
    quote = api("POST", "/api/quote", body)
    return (f"{quote['shown_as']} using {quote['model']}. Call video_factory_create_video with "
            f"approved_cost={quote['cost_usd']}, seconds={quote['seconds']}"
            f"{f', model={model!r}' if model else ''} to proceed.")


def h_create_video(args):
    image_url = (args.get("image_url") or "").strip()
    if not image_url:
        raise RuntimeError(
            "image_url is required -- generate a starting image first with "
            "video_factory_create_images and pass one of its URLs here. Video is always "
            "animated from a chosen image, never invented from text alone."
        )
    seconds = optional_positive_int(args, "seconds")
    approved = args.get("approved_cost")
    if approved is None:
        raise RuntimeError(
            "approved_cost is required. Call video_factory_quote_video first, "
            "show the user that price, and only then pass its exact cost_usd here."
        )
    prompt = (args.get("motion") or "").strip() or "Camera holds steady, natural ambient motion."
    body = {"prompt": prompt, "image_url": image_url, "approved_cost": approved}
    if seconds is not None:
        body["seconds"] = seconds
    model = (args.get("model") or "").strip()
    if model:
        body["model"] = model
    job = api("POST", "/api/generate/video", body)
    result = wait_for_job(job["id"], max_wait=300)
    if result.get("status") == "error":
        raise RuntimeError(f"Video generation failed: {result.get('error')}")
    if result.get("status") == "pending":
        return result["note"]
    outputs = result.get("outputs") or []
    if not outputs:
        return "The model finished but returned no video."
    return f"Video ready (${result.get('cost_usd', 0)}): {media_url(outputs[0])}"


def _postprod_body(args):
    op = args.get("operation")
    video_url = (args.get("video_url") or "").strip()
    if op not in ("upscale", "bgremove", "subtitles", "lipsync"):
        raise RuntimeError("operation must be one of: upscale, bgremove, subtitles, lipsync.")
    if not video_url:
        raise RuntimeError("video_url is required.")
    body = {"op": op, "file_url": video_url}
    if args.get("quality_tier"):
        body["tier"] = args["quality_tier"]
    if args.get("language"):
        body["lang"] = args["language"]
    if args.get("audio_url"):
        body["audio_url"] = args["audio_url"]
    if args.get("background_color"):
        body["background_color"] = args["background_color"]
    if op == "lipsync" and not body.get("audio_url"):
        raise RuntimeError("lipsync needs audio_url -- a direct link to the voice track.")
    if op == "upscale" and not body.get("tier"):
        body["tier"] = "upto1080p"
    return body


def h_quote_enhancement(args):
    body = _postprod_body(args)
    quote = api("POST", "/api/postprod/quote", body)
    return (f"{quote['shown_as']} using {quote['model']}. "
            f"Call video_factory_enhance_video with approved_cost={quote['cost_usd']} to proceed.")


def h_enhance_video(args):
    body = _postprod_body(args)
    approved = args.get("approved_cost")
    if approved is None:
        raise RuntimeError(
            "approved_cost is required. Call video_factory_quote_enhancement first, "
            "show the user that price, and only then pass its exact cost_usd here."
        )
    body["approved_cost"] = approved
    job = api("POST", "/api/postprod/run", body)
    result = wait_for_job(job["id"], max_wait=300)
    if result.get("status") == "error":
        raise RuntimeError(f"Enhancement failed: {result.get('error')}")
    if result.get("status") == "pending":
        return result["note"]
    outputs = result.get("outputs") or []
    if not outputs:
        return "Finished but returned no file."
    return f"Ready (${result.get('cost_usd', 0)}): {media_url(outputs[0])}"


def _avatar_body(args):
    image_url = (args.get("image_url") or "").strip()
    audio_url = (args.get("audio_url") or "").strip()
    if not image_url:
        raise RuntimeError("image_url is required -- the photo to animate.")
    if not audio_url:
        raise RuntimeError("audio_url is required -- a direct URL to the voice track that drives the performance.")
    body = {"image_url": image_url, "audio_url": audio_url}
    if args.get("prompt"):
        body["prompt"] = args["prompt"]
    if args.get("resolution"):
        body["resolution"] = args["resolution"]
    if args.get("turbo"):
        body["turbo"] = True
    return body


def h_quote_avatar(args):
    body = _avatar_body(args)
    quote = api("POST", "/api/avatar/quote", body)
    return (f"{quote['shown_as']}. Call video_factory_create_talking_avatar with "
            f"approved_cost={quote['cost_usd']} to proceed.")


def h_create_talking_avatar(args):
    body = _avatar_body(args)
    approved = args.get("approved_cost")
    if approved is None:
        raise RuntimeError(
            "approved_cost is required. Call video_factory_quote_avatar first, "
            "show the user that price, and only then pass its exact cost_usd here."
        )
    body["approved_cost"] = approved
    job = api("POST", "/api/avatar/run", body)
    result = wait_for_job(job["id"], max_wait=300)
    if result.get("status") == "error":
        raise RuntimeError(f"Avatar generation failed: {result.get('error')}")
    if result.get("status") == "pending":
        return result["note"]
    outputs = result.get("outputs") or []
    if not outputs:
        return "Finished but returned no video."
    return f"Video ready (${result.get('cost_usd', 0)}): {media_url(outputs[0])}"


def h_check_job(args):
    job_id = (args.get("job_id") or "").strip()
    if not job_id:
        raise RuntimeError("job_id is required.")
    job = api("GET", f"/api/jobs/{job_id}")
    status = job.get("status")
    if status == "error":
        return f"Failed: {job.get('error')}"
    if status != "done":
        return f"Status: {status} — {job.get('stage', '')}"
    outputs = [media_url(u) for u in (job.get("outputs") or [])]
    return f"Done (${job.get('cost_usd', 0)}): " + (", ".join(outputs) if outputs else "no output")


def h_list_my_videos(_args):
    data = api("GET", "/api/jobs")
    jobs = data.get("jobs") or []
    if not jobs:
        return "Nothing generated yet."
    lines = []
    for j in jobs[:20]:
        outputs = j.get("outputs") or []
        url = media_url(outputs[0]) if outputs else ""
        lines.append(f"- [{j.get('kind')}] {j.get('status')} ${j.get('cost_usd', 0)} "
                     f"id={j.get('id')} {url}".rstrip())
    return "\n".join(lines)


# ------------------------------------------------------------------- tools

TOOLS = [
    {
        "name": "video_factory_get_info",
        "description": ("Read-only. Lists available style presets, current model/enhancement "
                        "pricing, and available video durations. Call this first to know what's "
                        "possible before generating anything."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "video_factory_quote_images",
        "description": "Read-only, spends nothing. Prices generating starting-image variants for a video.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 1, "maximum": 6,
                           "description": "How many image variants (default 5 -- best-of-5 for identity match)."},
            },
        },
    },
    {
        "name": "video_factory_create_images",
        "description": ("PAID. Generates image variants from a text description -- the starting "
                        "frame a video will later be animated from. Requires approved_cost from "
                        "video_factory_quote_images, obtained after the user has agreed to the price."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What the image should show, in plain English."},
                "count": {"type": "integer", "minimum": 1, "maximum": 6, "description": "Must match the quoted count (default 5)."},
                "approved_cost": {"type": "number", "description": "The exact cost_usd from video_factory_quote_images. Required."},
            },
            "required": ["prompt", "approved_cost"],
        },
    },
    {
        "name": "video_factory_quote_video",
        "description": "Read-only, spends nothing. Prices animating a chosen image into a video of a given length and model.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "description": "Video length in seconds -- see video_factory_get_info for valid values (default 6)."},
                "model": {"type": "string", "description": ("The exact model id from video_factory_get_info's VIDEO MODELS list, e.g. "
                                                              "\"bytedance/seedance-2.5/image-to-video\" for the top-quality tier. Omit "
                                                              "for the default (Kling 3.0, cheapest full-quality option). Only ask a "
                                                              "user to pick a premium-tier model if they've indicated cost isn't the "
                                                              "deciding factor -- it's a real price jump, not a free upgrade.")},
            },
        },
    },
    {
        "name": "video_factory_create_video",
        "description": ("PAID. Animates one chosen starting image into a video. Requires "
                        "approved_cost from video_factory_quote_video, obtained after the user "
                        "has agreed to the price. Video is always generated from a real image "
                        "(from video_factory_create_images), never invented from text alone."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "A URL returned by video_factory_create_images -- the exact frame to animate."},
                "motion": {"type": "string", "description": "What should move/happen, in plain English. Optional -- a calm default is used if omitted."},
                "seconds": {"type": "integer", "description": "Must match the quoted duration (default 6)."},
                "model": {"type": "string", "description": "Must match whatever model_id (if any) was passed to video_factory_quote_video for this quote."},
                "approved_cost": {"type": "number", "description": "The exact cost_usd from video_factory_quote_video. Required."},
            },
            "required": ["image_url", "approved_cost"],
        },
    },
    {
        "name": "video_factory_quote_enhancement",
        "description": "Read-only, spends nothing. Prices upscaling, background removal, subtitles, or lip-sync on an existing video.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["upscale", "bgremove", "subtitles", "lipsync"]},
                "video_url": {"type": "string", "description": "URL of an already-generated video (from video_factory_create_video or history)."},
                "quality_tier": {"type": "string", "enum": ["le720p", "upto1080p", "above1080p"],
                                  "description": "upscale only -- output resolution tier, sets the price. Defaults to upto1080p."},
                "language": {"type": "string", "description": "subtitles only -- language code, e.g. 'ru', 'uz'. Omit to auto-detect."},
                "audio_url": {"type": "string", "description": "lipsync only -- REQUIRED for lipsync: a direct URL to the voice track."},
                "background_color": {"type": "string", "description": "bgremove only -- e.g. 'Black' (default), 'White'."},
            },
            "required": ["operation", "video_url"],
        },
    },
    {
        "name": "video_factory_enhance_video",
        "description": ("PAID. Runs upscale/bgremove/subtitles/lipsync on an existing video. "
                        "Requires approved_cost from video_factory_quote_enhancement, obtained "
                        "after the user has agreed to the price."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["upscale", "bgremove", "subtitles", "lipsync"]},
                "video_url": {"type": "string"},
                "quality_tier": {"type": "string", "enum": ["le720p", "upto1080p", "above1080p"]},
                "language": {"type": "string"},
                "audio_url": {"type": "string"},
                "background_color": {"type": "string"},
                "approved_cost": {"type": "number", "description": "The exact cost_usd from video_factory_quote_enhancement. Required."},
            },
            "required": ["operation", "video_url", "approved_cost"],
        },
    },
    {
        "name": "video_factory_quote_avatar",
        "description": ("Read-only, spends nothing. Prices turning a photo + a voice-track URL into a new "
                        "talking-head video. Different from video_factory_quote_video: there's no motion "
                        "prompt, the voice track itself drives the performance, and the price is set by "
                        "the voice track's own length."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "The photo/portrait to animate."},
                "audio_url": {"type": "string", "description": "Direct URL to the voice track that drives the performance."},
                "prompt": {"type": "string", "description": "Optional text guidance for expression/motion style."},
                "resolution": {"type": "string", "enum": ["720p", "1080p"], "description": "Default 1080p."},
                "turbo": {"type": "boolean", "description": "Faster generation, some quality trade-off."},
            },
            "required": ["image_url", "audio_url"],
        },
    },
    {
        "name": "video_factory_create_talking_avatar",
        "description": ("PAID. Generates the talking-head video. Requires approved_cost from "
                        "video_factory_quote_avatar, obtained after the user has agreed to the price."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_url": {"type": "string"},
                "audio_url": {"type": "string"},
                "prompt": {"type": "string"},
                "resolution": {"type": "string", "enum": ["720p", "1080p"]},
                "turbo": {"type": "boolean"},
                "approved_cost": {"type": "number", "description": "The exact cost_usd from video_factory_quote_avatar. Required."},
            },
            "required": ["image_url", "audio_url", "approved_cost"],
        },
    },
    {
        "name": "video_factory_check_job",
        "description": "Read-only. Checks the status of a job that timed out waiting, or that another tool call started.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "video_factory_list_my_videos",
        "description": "Read-only. Lists recent images/videos already generated, newest first.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

HANDLERS = {
    "video_factory_get_info": h_get_info,
    "video_factory_quote_images": h_quote_images,
    "video_factory_create_images": h_create_images,
    "video_factory_quote_video": h_quote_video,
    "video_factory_create_video": h_create_video,
    "video_factory_quote_enhancement": h_quote_enhancement,
    "video_factory_enhance_video": h_enhance_video,
    "video_factory_quote_avatar": h_quote_avatar,
    "video_factory_create_talking_avatar": h_create_talking_avatar,
    "video_factory_check_job": h_check_job,
    "video_factory_list_my_videos": h_list_my_videos,
}


# --------------------------------------------------------------- JSON-RPC

def ok(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def rpc_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(msg):
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return ok(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Video Factory generates AI images and videos and can enhance existing "
                "ones (upscale, subtitles, background removal, lip-sync). EVERY tool whose "
                "name starts with video_factory_create_ or video_factory_enhance_ spends "
                "real money and REQUIRES an approved_cost argument. Always call the matching "
                "video_factory_quote_* tool first, tell the user the exact price, wait for "
                "their explicit go-ahead, and only then call the paid tool with that exact "
                "cost_usd as approved_cost. Never guess a price, never skip the quote step, "
                "and never reuse an old quote after the user changed what they asked for."
            ),
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return ok(msg_id, {})
    if method == "tools/list":
        return ok(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            return ok(msg_id, {"content": [{"type": "text", "text": f"Unknown tool '{name}'."}], "isError": True})
        try:
            text = handler(args)
            return ok(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a tool error, not a crash
            return ok(msg_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})

    if msg_id is not None:
        return rpc_error(msg_id, -32601, f"Method not found: {method}")
    return None  # unrecognized notification -- nothing to reply with


def main_stdio():
    log(f"Video Factory MCP server (stdio) starting, backend at {BASE_URL}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"Ignoring non-JSON line on stdin: {line[:200]}")
            continue
        try:
            response = handle_message(msg)
        except Exception as exc:  # noqa: BLE001 - never let a bad message kill the process
            log(f"Error handling message: {exc}")
            response = rpc_error(msg.get("id"), -32603, str(exc)) if msg.get("id") is not None else None
        if response is not None:
            print(json.dumps(response), flush=True)


# ----------------------------------------------------------- HTTP transport
#
# The minimal server side of Streamable HTTP (MCP spec): a single POST
# endpoint, no SSE stream -- verified against the spec's transport doc
# rather than guessed, since getting the wire format wrong would make a
# real client (ChatGPT, or Claude configured for a remote server) fail to
# connect with no useful error. A request (has "id") gets a synchronous
# 200 + application/json response with the JSON-RPC result; a notification
# or a response-from-client (no "id") gets a bare 202. GET (the optional
# server-initiated-message stream) returns 405 -- nothing here needs it,
# every tool is a synchronous request/response.

class MCPHTTPHandler(BaseHTTPRequestHandler):
    server_version = "VideoFactoryMCP"

    def log_message(self, fmt, *args):
        log(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def _require_token(self):
        """Returns True if the request may proceed. A no-op (always True)
        only if MCP_HTTP_TOKEN is unset -- unlike the webapp's optional
        WEBAPP_BASIC_AUTH, this is not "recommended", it is load-bearing:
        this endpoint being reachable at all means whoever can reach it
        can spend real money through the webapp it's pointed at, and
        there is no browser confirmation click standing between an
        autonomous caller and that spend the way there is for a human at
        the webapp's UI."""
        token = os.environ.get("MCP_HTTP_TOKEN", "").strip()
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        given = header[7:] if header.startswith("Bearer ") else ""
        if hmac.compare_digest(given, token):
            return True
        body = b'{"error":"Unauthorized. Send Authorization: Bearer <token>."}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass
        return False

    def do_POST(self):
        if not self._require_token():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            msg = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = b'{"error":"Invalid JSON."}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass
            return

        msg_id = msg.get("id")
        try:
            response = handle_message(msg)
        except Exception as exc:  # noqa: BLE001 - never let a bad message kill the server
            log(f"Error handling message: {exc}")
            response = rpc_error(msg_id, -32603, str(exc)) if msg_id is not None else None

        if msg_id is None:
            # A notification, or a response the client sent us -- no reply
            # expected either way.
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"status": "healthy"}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()


def main_http(port):
    if not os.environ.get("MCP_HTTP_TOKEN", "").strip():
        log(
            "WARNING: MCP_HTTP_TOKEN is not set -- this HTTP endpoint has no access "
            "control. Anyone who can reach it can spend real money through the webapp "
            "it's pointed at. Set MCP_HTTP_TOKEN before exposing this beyond localhost."
        )
    log(f"Video Factory MCP server (Streamable HTTP) starting on :{port}, backend at {BASE_URL}")
    server = ThreadingHTTPServer(("0.0.0.0", port), MCPHTTPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")


def main():
    args = sys.argv[1:]
    http_requested = "--http" in args or bool(os.environ.get("MCP_HTTP_PORT", "").strip())
    if not http_requested:
        return main_stdio()

    port = None
    if "--http" in args:
        idx = args.index("--http")
        if idx + 1 < len(args) and args[idx + 1].isdigit():
            port = int(args[idx + 1])
    if port is None:
        port = int(os.environ.get("MCP_HTTP_PORT") or os.environ.get("PORT") or 8300)
    main_http(port)


if __name__ == "__main__":
    main()
