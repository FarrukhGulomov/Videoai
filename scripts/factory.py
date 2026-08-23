#!/usr/bin/env python3
"""
Video Factory — the hands of the production loop in 02-runbook.md.

Stdlib only. No pip install needed.

Rungs:
  1  still        cheap image, face locked, must be approved before rung 2
  2  motion       Lite video test from the approved still  (~$0.15)
  3  final        full-quality take — REFUSES to run without --i-approve-cost

Every call, success or failure, is appended to work/generations.jsonl.
That file is the failure log the runbook insists on.

Usage:
  factory.py still   --scene-id S --prompt "..." [--ref URL ...] [--out DIR]
  factory.py motion  --scene-id S --start-frame URL --motion "..." [--seconds 5]
  factory.py cost    --seconds 8 --rung 3
  factory.py final   --scene-id S --start-frame URL --motion "..." --seconds 8 \
                     --i-approve-cost 1.20
  factory.py fetch   --url URL --out work/clips/03.mp4
  factory.py probe   --file work/clips/03.mp4
  factory.py frames  --file work/clips/03.mp4 --count 3 --out work/qc/S03
  factory.py polish  --file work/clips/03.mp4 --deflicker --smooth --out work/polished/03.mp4
  factory.py upscale --file work/final.mp4 --tier upto1080p --i-approve-cost 1.20
  factory.py lipsync --file work/clips/03.mp4 --audio work/voice/03.wav --i-approve-cost 1.07
  factory.py subtitles --file work/final.mp4 --lang ru --i-approve-cost 0.02
  factory.py bgremove --file work/clips/03.mp4 --i-approve-cost 0.03
  factory.py final --scene-id S --start-frame URL --shots-json '[{"prompt":"...","duration":5},
                     {"prompt":"...","duration":4}]' --i-approve-cost 1.13   # Kling v3 only
  factory.py assemble --clips work/clips --out work/final.mp4 [--voice work/voice.mp3]
  factory.py ledger  [--scene-id S]

Post-production (upscale/lipsync/subtitles) are real fal.ai calls, priced
and gated exactly like rung 3 -- state the cost, approve the exact number,
nothing runs otherwise. `polish` stays free/local (ffmpeg only).
"""

import argparse
import base64
import json
import mimetypes
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
LEDGER = WORK / "generations.jsonl"
CONFIG_PATH = pathlib.Path(__file__).resolve().parent / "config.json"

FAL_QUEUE = "https://queue.fal.run"
FAL_UPLOAD_INITIATE = "https://rest.alpha.fal.ai/storage/upload/initiate"


# ---------------------------------------------------------------- utilities

def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_config():
    with open(CONFIG_PATH) as fh:
        return json.load(fh)


def fal_key():
    key = os.environ.get("FAL_KEY", "").strip()
    if not key:
        die(
            "FAL_KEY is not set.\n"
            "  Put your fal.ai key in the file  .env  at the project root as:\n"
            "      FAL_KEY=xxxxxxxx\n"
            "  then re-run. (Get one at https://fal.ai/dashboard/keys)"
        )
    return key


def load_dotenv():
    """Load ROOT/.env into os.environ without overwriting real env vars."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def http(url, method="GET", body=None, headers=None, timeout=180):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1200]
        raise RuntimeError(f"HTTP {exc.code} from {url}\n{detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error calling {url}: {exc.reason}") from None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw.decode("utf-8", "replace")}


def log_generation(record):
    """Append one attempt to the ledger. Failures included — that is the point."""
    WORK.mkdir(parents=True, exist_ok=True)
    record.setdefault("logged_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    with open(LEDGER, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def emit(payload):
    """Machine-readable result on stdout — this is what the orchestrator reads."""
    print("---RESULT---")
    print(json.dumps(payload, indent=2))


# ------------------------------------------------------------------ fal.ai

def fal_headers():
    return {"Authorization": f"Key {fal_key()}"}


def fal_run(model_id, payload, poll_seconds=5, max_wait=900):
    """Submit to the fal queue and block until it finishes. Returns the result dict."""
    submit_url = f"{FAL_QUEUE}/{model_id}"
    job = http(submit_url, method="POST", body=payload, headers=fal_headers())

    status_url = job.get("status_url")
    response_url = job.get("response_url")
    request_id = job.get("request_id")
    if not status_url or not response_url:
        raise RuntimeError(f"fal did not return a queue handle: {json.dumps(job)[:600]}")

    print(f"  queued: {request_id}", file=sys.stderr)
    waited = 0
    while waited < max_wait:
        time.sleep(poll_seconds)
        waited += poll_seconds
        state = http(status_url, headers=fal_headers())
        status = state.get("status")
        if status == "COMPLETED":
            break
        if status in ("FAILED", "ERROR", "CANCELLED"):
            raise RuntimeError(f"fal job {status}: {json.dumps(state)[:800]}")
        print(f"  {status or 'WAITING'} ({waited}s)", file=sys.stderr)
    else:
        raise RuntimeError(f"fal job timed out after {max_wait}s (request_id={request_id})")

    result = http(response_url, headers=fal_headers())
    result["_request_id"] = request_id
    return result


def fal_upload(path):
    """Upload a local file to fal storage; fall back to a data URI if unavailable."""
    p = pathlib.Path(path)
    if not p.exists():
        die(f"file not found: {path}")
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    try:
        init = http(
            FAL_UPLOAD_INITIATE,
            method="POST",
            body={"content_type": ctype, "file_name": p.name},
            headers=fal_headers(),
        )
        upload_url = init.get("upload_url")
        file_url = init.get("file_url")
        if upload_url and file_url:
            req = urllib.request.Request(
                upload_url, data=p.read_bytes(),
                headers={"Content-Type": ctype}, method="PUT",
            )
            urllib.request.urlopen(req, timeout=300).read()
            return file_url
    except Exception as exc:  # noqa: BLE001 - fall through to data URI
        print(f"  (fal upload unavailable: {exc}; using inline data URI)", file=sys.stderr)
    b64 = base64.b64encode(p.read_bytes()).decode()
    return f"data:{ctype};base64,{b64}"


def resolve_image(ref):
    """A ref is either an http(s)/data URL already, or a local path to upload.
    Despite the name this uploads any file type -- the post-production
    commands (upscale/lipsync/subtitles) reuse it for video and audio too."""
    if ref.startswith(("http://", "https://", "data:")):
        return ref
    return fal_upload(ref)


def probe_duration(path):
    """Seconds of media at `path`, via ffprobe. Used to price the
    duration-billed post-production ops (upscale/lipsync/subtitles) before
    they run, same discipline as the video rungs."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        die(f"ffprobe failed to read duration for {path}:\n{probe.stderr[:600]}")
    return float(probe.stdout.strip())


def _srt_timestamp(seconds):
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(chunks, path):
    """chunks: fal Wizper's [{"timestamp": [start, end], "text": ...}, ...]."""
    lines = []
    n = 0
    for chunk in chunks:
        text = (chunk.get("text") or "").strip()
        ts = chunk.get("timestamp") or [0, 0]
        if not text:
            continue
        n += 1
        lines.append(str(n))
        lines.append(f"{_srt_timestamp(ts[0])} --> {_srt_timestamp(ts[1])}")
        lines.append(text)
        lines.append("")
    pathlib.Path(path).write_text("\n".join(lines), encoding="utf-8")


def build_video_payload(model, prompt, start_frame, seconds, resolution, negative_prompt, audio, cfg, end_frame=None, shots=None):
    """Veo, Kling, Seedance, and LTX each take a differently-shaped payload. Branch on model family."""
    if end_frame and not any(fam in model for fam in ("kling", "seedance", "ltx")):
        die(f"model '{model}' has no documented end-frame support on fal -- drop --end-frame or switch to Kling/Seedance/LTX")
    if shots and "kling" not in model:
        die(f"model '{model}' has no documented multi-shot support on fal -- multi_prompt is Kling-v3-only, drop --shots-json or switch to Kling")

    image_url = resolve_image(start_frame)
    end_url = resolve_image(end_frame) if end_frame else None
    if "kling" in model:
        payload = {
            "start_image_url": image_url,
            "generate_audio": audio,
        }
        # multi_prompt replaces prompt+duration entirely for a multi-shot
        # continuous take (confirmed on Kling v3's own /api docs -- each
        # element is {"prompt": ..., "duration": ...}, one per shot).
        if shots:
            payload["multi_prompt"] = shots
        else:
            payload["prompt"] = prompt
            payload["duration"] = seconds
        if end_url:
            payload["end_image_url"] = end_url
        if cfg["defaults"].get("kling_cfg_scale") is not None:
            payload["cfg_scale"] = cfg["defaults"]["kling_cfg_scale"]
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
    elif "seedance" in model:
        payload = {
            "prompt": prompt,
            "image_url": image_url,
            "duration": seconds,
            "resolution": resolution,
            "generate_audio": audio,
        }
        if end_url:
            payload["end_image_url"] = end_url
        # Seedance has no negative_prompt param -- fold the standard negatives
        # into the positive prompt instead of silently dropping them.
        if negative_prompt:
            payload["prompt"] = f"{prompt}\nAvoid: {negative_prompt}"
    elif "ltx" in model:
        payload = {
            "prompt": prompt,
            "image_url": image_url,
            "duration": seconds,
            "resolution": resolution,
            "aspect_ratio": cfg["defaults"]["aspect_ratio"],
            "generate_audio": audio,
        }
        if end_url:
            payload["end_image_url"] = end_url
        # LTX has no negative_prompt param either -- same fold-in as Seedance.
        if negative_prompt:
            payload["prompt"] = f"{prompt}\nAvoid: {negative_prompt}"
    else:
        payload = {
            "prompt": prompt,
            "image_url": image_url,
            "duration": f"{seconds}s",
            "resolution": resolution,
            "generate_audio": audio,
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
    return payload


def first_url(result, *keys):
    """Pull the first output URL out of fal's varied response shapes."""
    for key in keys:
        node = result.get(key)
        if isinstance(node, dict) and node.get("url"):
            return node["url"]
        if isinstance(node, list) and node and isinstance(node[0], dict) and node[0].get("url"):
            return node[0]["url"]
        if isinstance(node, str) and node.startswith("http"):
            return node
    return None


def all_urls(result, *keys):
    """Pull every output URL out of fal's varied response shapes (e.g. num_images > 1)."""
    for key in keys:
        node = result.get(key)
        if isinstance(node, list) and node:
            urls = [item.get("url") for item in node if isinstance(item, dict) and item.get("url")]
            if urls:
                return urls
        if isinstance(node, dict) and node.get("url"):
            return [node["url"]]
        if isinstance(node, str) and node.startswith("http"):
            return [node]
    return []


# ------------------------------------------------------------------- rungs

def cmd_still(args, cfg):
    """Rung 1 — the cheap still. Face is locked by the reference images."""
    refs = list(args.ref or [])
    canonical = cfg.get("identity", {}).get("canonical_face_ref")
    if canonical and not args.no_canonical and canonical not in refs:
        refs.insert(0, canonical)

    model = args.model or (
        cfg["models"]["still_edit"] if refs else cfg["models"]["still"]
    )
    rate = cfg["rates"]["still_per_image_usd"]

    payload = {
        "prompt": args.prompt,
        "num_images": args.count,
        "aspect_ratio": args.aspect or cfg["defaults"]["aspect_ratio"],
    }
    if refs:
        payload["image_urls"] = [resolve_image(r) for r in refs]

    record = {
        "scene_id": args.scene_id, "rung": 1, "model": model,
        "prompt": args.prompt, "cost_usd": round(rate * args.count, 4),
    }

    try:
        result = fal_run(model, payload)
    except Exception as exc:  # noqa: BLE001
        record.update(status="failed", error=str(exc)[:1500])
        log_generation(record)
        emit(record)
        die(f"rung 1 failed: {exc}")

    urls = all_urls(result, "images", "image")
    record.update(
        status="success", output_url=urls[0] if urls else None,
        output_urls=urls, request_id=result.get("_request_id"),
    )
    log_generation(record)

    local_paths = []
    if urls and args.out:
        out_dir = pathlib.Path(args.out)
        if len(urls) == 1:
            local_paths.append(str(download(urls[0], args.out)))
        else:
            for i, u in enumerate(urls, start=1):
                suffix = pathlib.Path(u.split("?")[0]).suffix or ".jpg"
                local_paths.append(str(download(u, out_dir / f"variant_{i}{suffix}")))
        record["local_path"] = local_paths[0]
        record["local_paths"] = local_paths

    emit(record)


def motion_test_rate(args, cfg):
    """Rung 2 isn't approval-gated, so an unknown --model degrades to a
    warning instead of refusing to run -- but it still tries the final-take
    rate table first so overriding to Kling/Seedance for a test doesn't
    silently under-report cost against the Veo Lite default."""
    model = args.model or cfg["models"]["motion_test"]
    if model == cfg["models"]["motion_test"]:
        return model, cfg["rates"]["motion_test_per_second_usd"]
    rate = cfg["rates"]["final_take_per_second_usd_by_model"].get(model)
    if rate is None:
        rate = cfg["rates"]["motion_test_per_second_usd"]
        print(
            f"  (no rate on file for '{model}' -- logging cost at the Lite "
            f"rate ${rate}/s, which may not match what fal actually charges)",
            file=sys.stderr,
        )
    return model, rate


def cmd_motion(args, cfg):
    """Rung 2 — the Lite motion test. Start frame is mandatory."""
    model, rate = motion_test_rate(args, cfg)
    seconds = args.seconds or cfg["defaults"]["motion_test_duration"]
    cost = round(rate * seconds, 4)

    payload = build_video_payload(
        model, args.motion, args.start_frame, seconds,
        cfg["defaults"]["motion_test_resolution"],
        args.negative if args.negative is not None else cfg["defaults"]["negative_prompt"],
        cfg["defaults"]["motion_test_audio"],
        cfg,
        end_frame=args.end_frame,
    )

    record = {
        "scene_id": args.scene_id, "rung": 2, "model": model,
        "prompt": args.motion, "start_frame_url": args.start_frame,
        "duration_seconds": seconds, "cost_usd": cost,
    }

    try:
        result = fal_run(model, payload)
    except Exception as exc:  # noqa: BLE001
        record.update(status="failed", error=str(exc)[:1500])
        log_generation(record)
        emit(record)
        die(f"rung 2 failed: {exc}")

    url = first_url(result, "video", "videos")
    record.update(status="success", output_url=url, request_id=result.get("_request_id"))
    log_generation(record)

    if url and args.out:
        record["local_path"] = str(download(url, args.out))

    emit(record)


def final_take_rate(args, cfg):
    """Look up the per-second rate for whichever final-take model is in play.
    Refuses to guess a price for a model that isn't in the rate table."""
    model = args.model or cfg["models"]["final_take"]
    rate = cfg["rates"]["final_take_per_second_usd_by_model"].get(model)
    if rate is None:
        die(
            f"no rate on file for model '{model}'.\n"
            f"  Add its real per-second price to rates.final_take_per_second_usd_by_model "
            f"in config.json (check fal.ai/pricing) before running rung 3 with it."
        )
    return model, rate


def cmd_cost(args, cfg):
    if args.rung == 1:
        total = cfg["rates"]["still_per_image_usd"] * (args.count or 1)
        emit({"rung": 1, "images": args.count or 1, "cost_usd": round(total, 4)})
        return
    if args.rung == 2:
        _, rate = motion_test_rate(args, cfg)
    else:
        _, rate = final_take_rate(args, cfg)
    total = round(rate * args.seconds, 4)
    emit({
        "rung": args.rung, "seconds": args.seconds,
        "rate_per_second_usd": rate, "cost_usd": total,
        "shown_as": f"{args.seconds}s x ${rate}/s = ${total}",
    })


def parse_shots(raw):
    """--shots-json: a JSON string or @path/to/file.json, matching Kling
    v3's own multi_prompt shape: [{"prompt": "...", "duration": 5}, ...].
    Reusing fal's own schema directly instead of inventing a CLI mini-syntax
    for it."""
    text = pathlib.Path(raw[1:]).read_text() if raw.startswith("@") else raw
    try:
        shots = json.loads(text)
    except json.JSONDecodeError as exc:
        die(f"--shots-json is not valid JSON: {exc}")
    if not isinstance(shots, list) or not shots:
        die('--shots-json must be a non-empty JSON list of {"prompt": ..., "duration": ...}')
    for i, s in enumerate(shots):
        if not isinstance(s, dict) or "prompt" not in s or "duration" not in s:
            die(f"shot #{i + 1} in --shots-json is missing 'prompt' or 'duration'")
    return shots


def cmd_final(args, cfg):
    """Rung 3 — real money. Refuses to run unless the stated cost was approved."""
    model, rate = final_take_rate(args, cfg)

    shots = parse_shots(args.shots_json) if args.shots_json else None
    if shots:
        if args.motion or args.seconds:
            die("--shots-json replaces --motion/--seconds for a multi-shot take -- pass one or the other, not both")
        seconds = sum(s["duration"] for s in shots)
        motion_desc = " | ".join(s["prompt"] for s in shots)
    else:
        if not args.motion or not args.seconds:
            die("either both --motion and --seconds, or --shots-json, is required")
        seconds = args.seconds
        motion_desc = args.motion

    # fal bills a video call by its total output duration regardless of how
    # many shots compose it -- this is the same per-second rate as a
    # single-shot call of the same length, not a separate documented
    # multi-shot price. Verify this assumption on the first real multi-shot
    # invoice, same as any other newly wired capability.
    cost = round(rate * seconds, 4)

    if args.i_approve_cost is None:
        die(
            f"rung 3 costs ${cost} ({seconds}s x ${rate}/s).\n"
            f"  Nothing was spent. To proceed, re-run with:  --i-approve-cost {cost}"
        )
    if abs(args.i_approve_cost - cost) > 0.005:
        die(
            f"approved ${args.i_approve_cost} but this call costs ${cost}. "
            "Nothing was spent. Re-confirm the real number."
        )

    audio = cfg["defaults"]["final_audio"] if args.audio is None else args.audio

    payload = build_video_payload(
        model, motion_desc, args.start_frame, seconds,
        args.resolution or cfg["defaults"]["final_resolution"],
        args.negative if args.negative is not None else cfg["defaults"]["negative_prompt"],
        audio,
        cfg,
        end_frame=args.end_frame,
        shots=shots,
    )

    record = {
        "scene_id": args.scene_id, "rung": 3, "model": model,
        "prompt": motion_desc, "start_frame_url": args.start_frame,
        "duration_seconds": seconds, "cost_usd": cost,
    }
    if shots:
        record["shots"] = shots

    try:
        result = fal_run(model, payload, max_wait=1800)
    except Exception as exc:  # noqa: BLE001
        record.update(status="failed", error=str(exc)[:1500])
        log_generation(record)
        emit(record)
        die(f"rung 3 failed: {exc}")

    url = first_url(result, "video", "videos")
    record.update(status="success", output_url=url, request_id=result.get("_request_id"))
    log_generation(record)

    if url and args.out:
        record["local_path"] = str(download(url, args.out))

    emit(record)


# ------------------------------------------------------------ post-production
#
# Three paid ops beyond the video rungs, each verified against fal's own
# /api docs page before wiring in (same rule as the rung-3 models -- see
# fal-master-prompt.md section 5). Each follows rung 3's cost discipline:
# state the price, refuse to run without --i-approve-cost matching it.

def upscale_rate(args, cfg):
    rates = cfg["rates"]["upscale_per_second_usd_by_tier"]
    rate = rates.get(args.tier)
    if rate is None:
        choices = [k for k in rates if not k.startswith("_")]
        die(f"no rate on file for tier '{args.tier}'. Choices: {choices}")
    if args.fps and args.fps >= 60:
        rate *= 2  # fal's own pricing panel: "Price doubles for 60fps output"
    return rate


def cmd_upscale(args, cfg):
    """Real detail-adding upscale (Topaz Video AI on fal) -- distinct from
    `polish --upscale`, which is a free local scale+sharpen with no new
    detail. Priced by OUTPUT resolution tier x duration, so --tier is
    mandatory and drives the quote; get it wrong and the approved cost
    won't match what fal actually renders at."""
    model = cfg["models"]["upscale"]
    rate = upscale_rate(args, cfg)
    duration = probe_duration(args.file)
    cost = round(rate * duration, 4)

    if args.i_approve_cost is None:
        die(
            f"upscale costs ${cost} ({duration:.1f}s x ${rate}/s at tier '{args.tier}').\n"
            f"  Nothing was spent. To proceed, re-run with:  --i-approve-cost {cost}"
        )
    if abs(args.i_approve_cost - cost) > 0.005:
        die(f"approved ${args.i_approve_cost} but this call costs ${cost}. Nothing was spent.")

    video_url = resolve_image(args.file)
    payload = {"video_url": video_url, "model": args.model, "upscale_factor": args.factor}
    if args.fps:
        payload["target_fps"] = args.fps

    record = {"scene_id": args.scene_id, "op": "upscale", "model": model,
              "duration_seconds": round(duration, 2), "cost_usd": cost}
    try:
        result = fal_run(model, payload, max_wait=1800)
    except Exception as exc:  # noqa: BLE001
        record.update(status="failed", error=str(exc)[:1500])
        log_generation(record)
        emit(record)
        die(f"upscale failed: {exc}")

    url = first_url(result, "video", "videos")
    record.update(status="success", output_url=url, request_id=result.get("_request_id"))
    log_generation(record)
    if url and args.out:
        record["local_path"] = str(download(url, args.out))
    emit(record)


def cmd_lipsync(args, cfg):
    """Sync a separately-recorded or cloned voice track onto an existing
    clip's mouth movement -- distinct from a video model's own built-in
    lip-sync (Kling/Veo), which only ever matches audio it generated itself
    in that same call. Useful for ADR-style dialogue fixes or swapping in a
    cleaner voice take after the fact."""
    model = cfg["models"]["lipsync"]
    rate = cfg["rates"]["lipsync_per_second_usd"]
    duration = probe_duration(args.file)
    cost = round(rate * duration, 4)

    if args.i_approve_cost is None:
        die(
            f"lipsync costs ${cost} ({duration:.1f}s x ${rate}/s, priced off the video's own "
            f"length).\n  Nothing was spent. To proceed, re-run with:  --i-approve-cost {cost}"
        )
    if abs(args.i_approve_cost - cost) > 0.005:
        die(f"approved ${args.i_approve_cost} but this call costs ${cost}. Nothing was spent.")

    video_url = resolve_image(args.file)
    audio_url = resolve_image(args.audio)
    payload = {"video_url": video_url, "audio_url": audio_url, "sync_mode": args.sync_mode}

    record = {"scene_id": args.scene_id, "op": "lipsync", "model": model,
              "duration_seconds": round(duration, 2), "cost_usd": cost}
    try:
        result = fal_run(model, payload, max_wait=1800)
    except Exception as exc:  # noqa: BLE001
        record.update(status="failed", error=str(exc)[:1500])
        log_generation(record)
        emit(record)
        die(f"lipsync failed: {exc}")

    url = first_url(result, "video", "videos")
    record.update(status="success", output_url=url, request_id=result.get("_request_id"))
    log_generation(record)
    if url and args.out:
        record["local_path"] = str(download(url, args.out))
    emit(record)


def cmd_subtitles(args, cfg):
    """Transcribe dialogue (fal Wizper) and burn timed captions into the
    video locally via ffmpeg/libass -- generalizes the manual Cyrillic
    drawtext burn-in this project already did once for the '25 yil' film
    into a repeatable command with real per-line timing instead of a
    hand-placed guess."""
    model = cfg["models"]["transcribe"]
    rate = cfg["rates"]["transcription_per_second_usd"]
    duration = probe_duration(args.file)
    cost = round(rate * duration, 4)

    if args.i_approve_cost is None:
        die(
            f"subtitles cost an estimated ${cost} ({duration:.1f}s x ${rate}/s -- this rate is "
            f"not confirmed on fal's own pricing page, see config.json's _post_production_note; "
            f"the real charge should be small either way).\n"
            f"  Nothing was spent. To proceed, re-run with:  --i-approve-cost {cost}"
        )
    if abs(args.i_approve_cost - cost) > 0.005:
        die(f"approved ${args.i_approve_cost} but this call is estimated at ${cost}. Nothing was spent.")

    audio_url = resolve_image(args.file)  # Wizper accepts mp4 directly, no local extraction needed
    payload = {"audio_url": audio_url, "task": "transcribe", "chunk_level": "segment"}
    if args.lang:
        payload["language"] = args.lang

    record = {"scene_id": args.scene_id, "op": "subtitles", "model": model,
              "duration_seconds": round(duration, 2), "cost_usd": cost}
    try:
        result = fal_run(model, payload, max_wait=600)
    except Exception as exc:  # noqa: BLE001
        record.update(status="failed", error=str(exc)[:1500])
        log_generation(record)
        emit(record)
        die(f"transcription failed: {exc}")

    chunks = result.get("chunks") or []
    if not chunks:
        record.update(status="failed", error="transcription returned no timed segments")
        log_generation(record)
        die("transcription returned no timed segments -- nothing to burn in")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    srt_path = out.with_suffix(".srt")
    write_srt(chunks, srt_path)

    # ffmpeg's filter graph syntax treats ':' as an option separator, so a
    # path (esp. on Windows, or with a drive-letter-style prefix) has to be
    # escaped before it can sit inside -vf.
    escaped_srt = str(srt_path).replace("\\", "\\\\").replace(":", "\\:")
    style = args.style or cfg["defaults"]["subtitles"]["force_style"]
    vf = f"subtitles={escaped_srt}:force_style='{style}'"
    res = subprocess.run(
        ["ffmpeg", "-y", "-i", args.file, "-vf", vf,
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "copy", str(out)],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        die(f"ffmpeg subtitle burn-in failed:\n{res.stderr[-1500:]}")

    record.update(status="success", request_id=result.get("_request_id"),
                  local_path=str(out), srt_path=str(srt_path))
    log_generation(record)
    emit(record)


def cmd_bgremove(args, cfg):
    """Cuts the subject out of its background (Bria VRMBG 3.0 on fal) --
    for compositing the protagonist into a different plate than what was
    generated, or keying a shot for a VFX-style comp. Both the schema and
    the $0.0042/s rate are confirmed directly on the model's own fal.ai
    page, not a secondary source."""
    model = cfg["models"]["bg_remove"]
    rate = cfg["rates"]["bg_remove_per_second_usd"]
    duration = probe_duration(args.file)
    cost = round(rate * duration, 4)

    if args.i_approve_cost is None:
        die(
            f"background removal costs ${cost} ({duration:.1f}s x ${rate}/s).\n"
            f"  Nothing was spent. To proceed, re-run with:  --i-approve-cost {cost}"
        )
    if abs(args.i_approve_cost - cost) > 0.005:
        die(f"approved ${args.i_approve_cost} but this call costs ${cost}. Nothing was spent.")

    video_url = resolve_image(args.file)
    payload = {
        "video_url": video_url,
        "background_color": args.background_color,
        "preserve_audio": not args.no_audio,
    }

    record = {"scene_id": args.scene_id, "op": "bg_remove", "model": model,
              "duration_seconds": round(duration, 2), "cost_usd": cost}
    try:
        result = fal_run(model, payload, max_wait=900)
    except Exception as exc:  # noqa: BLE001
        record.update(status="failed", error=str(exc)[:1500])
        log_generation(record)
        emit(record)
        die(f"background removal failed: {exc}")

    url = first_url(result, "video", "videos")
    record.update(status="success", output_url=url, request_id=result.get("_request_id"))
    log_generation(record)
    if url and args.out:
        record["local_path"] = str(download(url, args.out))
    emit(record)


# ---------------------------------------------------------------- local ops

def download(url, out_path):
    out = pathlib.Path(out_path)
    if out.is_dir() or str(out_path).endswith("/"):
        out = out / url.split("/")[-1].split("?")[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "video-factory"})
    with urllib.request.urlopen(req, timeout=600) as resp, open(out, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    print(f"  saved: {out} ({out.stat().st_size / 1e6:.1f} MB)", file=sys.stderr)
    return out


def cmd_fetch(args, cfg):
    path = download(args.url, args.out)
    emit({"url": args.url, "local_path": str(path), "bytes": path.stat().st_size})


def cmd_probe(args, cfg):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", args.file],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        die(f"ffprobe failed: {out.stderr[:600]}")
    info = json.loads(out.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    emit({
        "file": args.file,
        "duration_seconds": round(float(info["format"].get("duration", 0)), 2),
        "resolution": f"{video.get('width')}x{video.get('height')}",
        "codec": video.get("codec_name"),
        "has_audio": any(s["codec_type"] == "audio" for s in info["streams"]),
    })


def cmd_frames(args, cfg):
    """Pull N evenly-spaced frames from a clip for identity/artifact review.
    A still-only face check can't catch drift that happens mid-motion --
    this is what makes checking the rendered output possible at all."""
    duration = probe_duration(args.file)

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = args.count
    # Evenly spaced, avoiding the very first/last frame where encoders often
    # have artifacts unrelated to the actual generation.
    timestamps = [duration * (i + 1) / (n + 1) for i in range(n)]

    paths = []
    for i, t in enumerate(timestamps, start=1):
        frame_path = out_dir / f"frame_{i}_{t:.2f}s.jpg"
        res = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", args.file,
             "-frames:v", "1", "-q:v", "2", str(frame_path)],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0:
            die(f"ffmpeg frame extraction failed at {t:.2f}s:\n{res.stderr[-800:]}")
        paths.append(str(frame_path))

    emit({
        "file": args.file,
        "duration_seconds": round(duration, 2),
        "frames": paths,
        "cost_usd": 0,
    })


def cmd_polish(args, cfg):
    """Free, local post-processing pass: color grade, optional motion smoothing
    and upscale/sharpen. No API calls, no cost -- just ffmpeg."""
    grade = cfg["defaults"]["polish"]["grade_filter"]
    filters = [grade] if args.grade else []
    if args.deflicker:
        # Removes frame-to-frame luminance flicker -- a common generated-video
        # tell that reads as "artificial" independent of anything the prompt
        # controls. Free, local, ffmpeg native (see fal-master-prompt.md 3.1
        # for the rest of the naturalness rules this complements).
        filters.append("deflicker=size=5:mode=am")
    if args.smooth:
        fps = cfg["defaults"]["polish"]["smooth_fps"]
        filters.append(
            f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:vsbmc=1"
        )
    if args.upscale:
        filters.append(f"scale={args.upscale}:flags=lanczos,unsharp=5:5:0.8:5:5:0.4")

    if not filters:
        die("nothing to do -- pass at least one of --grade / --smooth / --upscale")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", args.file,
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        die(f"ffmpeg polish failed:\n{res.stderr[-1500:]}")

    emit({
        "input": args.file,
        "output": str(out),
        "filters_applied": filters,
        "bytes": out.stat().st_size,
        "cost_usd": 0,
    })


def has_audio_stream(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=False,
    )
    return bool(out.stdout.strip())


def cmd_assemble(args, cfg):
    """Stitch approved clips in filename order, then lay the voiceover over them."""
    clips_dir = pathlib.Path(args.clips)
    clips = sorted(p for p in clips_dir.glob("*.mp4") if p.is_file())
    if not clips:
        die(f"no .mp4 files in {clips_dir}")

    # The concat demuxer silently drops audio from every clip if even one clip
    # in the list has a different stream layout (e.g. one clip has no audio
    # track). Normalize first: any clip missing audio gets a silent track
    # muxed in, so every clip concat sees has the same video+audio layout.
    audio_flags = [has_audio_stream(p) for p in clips]
    if any(audio_flags) and not all(audio_flags):
        norm_dir = pathlib.Path(args.out).parent / "_normalized"
        norm_dir.mkdir(parents=True, exist_ok=True)
        normalized = []
        for clip, has_audio in zip(clips, audio_flags):
            if has_audio:
                normalized.append(clip)
                continue
            silent = norm_dir / clip.name
            res = subprocess.run(
                ["ffmpeg", "-y", "-i", str(clip),
                 "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                 "-c:v", "copy", "-c:a", "aac", "-shortest", str(silent)],
                capture_output=True, text=True, check=False,
            )
            if res.returncode != 0:
                die(f"ffmpeg silent-audio mux failed for {clip}:\n{res.stderr[-1500:]}")
            normalized.append(silent)
            print(f"  {clip.name} had no audio track -- added silence so concat doesn't drop everyone else's", file=sys.stderr)
        clips = normalized

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    list_file = out.parent / "list.txt"
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in clips))

    stitched = out.parent / "stitched.mp4"
    concat = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
              "-i", str(list_file), "-c", "copy", str(stitched)]
    res = subprocess.run(concat, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        # Codecs differ between clips — re-encode instead of stream-copying.
        print("  stream copy failed, re-encoding", file=sys.stderr)
        res = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", str(stitched)],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0:
            die(f"ffmpeg concat failed:\n{res.stderr[-1500:]}")

    if args.voice:
        res = subprocess.run(
            ["ffmpeg", "-y", "-i", str(stitched), "-i", args.voice,
             "-c:v", "copy", "-c:a", "aac", "-shortest", str(out)],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0:
            die(f"ffmpeg mux failed:\n{res.stderr[-1500:]}")
    else:
        stitched.replace(out)

    emit({
        "clips": [p.name for p in clips],
        "clip_count": len(clips),
        "voice": args.voice,
        "output": str(out),
        "bytes": out.stat().st_size,
    })


def cmd_ledger(args, cfg):
    if not LEDGER.exists():
        emit({"entries": [], "total_cost_usd": 0})
        return
    rows = [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]
    if args.scene_id:
        rows = [r for r in rows if r.get("scene_id") == args.scene_id]
    emit({
        "entries": rows,
        "count": len(rows),
        "failed": sum(1 for r in rows if r.get("status") == "failed"),
        "total_cost_usd": round(sum(float(r.get("cost_usd", 0)) for r in rows), 4),
    })


# -------------------------------------------------------------------- main

def main():
    load_dotenv()
    cfg = load_config()

    ap = argparse.ArgumentParser(description="Video Factory production loop")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("still", help="rung 1 — generate a still")
    p.add_argument("--scene-id", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--ref", action="append", help="reference image URL or local path (repeatable)")
    p.add_argument("--no-canonical", action="store_true",
                    help="don't auto-prepend identity.canonical_face_ref from config.json")
    p.add_argument("--count", type=int, default=5,
                    help="variants to generate (default 5 -- best-of-5 for identity match, see fal-master-prompt.md 2.1)")
    p.add_argument("--aspect")
    p.add_argument("--model")
    p.add_argument("--out", default=str(WORK / "stills"))
    p.set_defaults(func=cmd_still)

    p = sub.add_parser("motion", help="rung 2 — Lite motion test")
    p.add_argument("--scene-id", required=True)
    p.add_argument("--start-frame", required=True, help="approved still URL or path")
    p.add_argument("--end-frame", help="optional end-frame URL or path (Kling/Seedance only)")
    p.add_argument("--motion", required=True)
    p.add_argument("--seconds", type=int)
    p.add_argument("--negative", help="override the default negative prompt")
    p.add_argument("--model")
    p.add_argument("--out", default=str(WORK / "tests"))
    p.set_defaults(func=cmd_motion)

    p = sub.add_parser("cost", help="state a cost without spending anything")
    p.add_argument("--rung", type=int, required=True, choices=[1, 2, 3])
    p.add_argument("--seconds", type=int, default=5)
    p.add_argument("--count", type=int, default=5, help="rung 1 only: variants, matches `still`'s own default")
    p.add_argument("--model", help="rung 3 only: which final-take model, to price it correctly")
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("final", help="rung 3 — final take (requires approved cost)")
    p.add_argument("--scene-id", required=True)
    p.add_argument("--start-frame", required=True)
    p.add_argument("--end-frame", help="optional end-frame URL or path (Kling/Seedance only)")
    p.add_argument("--motion", help="single-shot motion description (omit if using --shots-json)")
    p.add_argument("--seconds", type=int, help="single-shot duration (omit if using --shots-json)")
    p.add_argument("--shots-json",
                    help='Kling-v3-only, replaces --motion/--seconds: a JSON list (or @file.json) '
                         'of {"prompt": ..., "duration": ...} shots for multi-shot continuity in '
                         'one call, e.g. \'[{"prompt":"...","duration":5},{"prompt":"...","duration":4}]\'')
    p.add_argument("--resolution")
    p.add_argument("--audio", action=argparse.BooleanOptionalAction, default=None,
                    help="generate synced audio (default: on, from config defaults.final_audio)")
    p.add_argument("--negative", help="override the default negative prompt")
    p.add_argument("--model")
    p.add_argument("--i-approve-cost", type=float, default=None)
    p.add_argument("--out", default=str(WORK / "clips"))
    p.set_defaults(func=cmd_final)

    p = sub.add_parser("fetch", help="download a URL")
    p.add_argument("--url", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("probe", help="inspect a media file")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("frames", help="extract N evenly-spaced frames for identity/artifact review")
    p.add_argument("--file", required=True)
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("polish", help="free local post-process: color grade / smooth motion / upscale")
    p.add_argument("--file", required=True)
    p.add_argument("--grade", action=argparse.BooleanOptionalAction, default=True,
                    help="apply the cinematic color-grade filter (default: on)")
    p.add_argument("--smooth", action="store_true",
                    help="motion-compensated frame interpolation (smoother motion, slower to render)")
    p.add_argument("--upscale", help="target resolution for scale+sharpen, e.g. 3840:2160")
    p.add_argument("--deflicker", action="store_true",
                    help="remove temporal luminance flicker between frames (free, ffmpeg native)")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_polish)

    p = sub.add_parser("upscale", help="real detail-adding upscale (Topaz on fal) -- paid, needs --i-approve-cost")
    p.add_argument("--scene-id")
    p.add_argument("--file", required=True)
    p.add_argument("--tier", required=True, choices=["le720p", "upto1080p", "above1080p"],
                    help="OUTPUT resolution tier -- sets the per-second rate, see config.json")
    p.add_argument("--model", default="Proteus",
                    help="Topaz model name (default Proteus -- verify other spellings on the fal "
                         "playground before use, see rates._note in config.json)")
    p.add_argument("--factor", type=float, default=2, help="upscale_factor, e.g. 2 doubles width/height")
    p.add_argument("--fps", type=int, help="target output fps (60 doubles the rate)")
    p.add_argument("--i-approve-cost", type=float, default=None)
    p.add_argument("--out", default=str(WORK / "upscaled"))
    p.set_defaults(func=cmd_upscale)

    p = sub.add_parser("lipsync", help="sync a separate voice track onto an existing clip -- paid, needs --i-approve-cost")
    p.add_argument("--scene-id")
    p.add_argument("--file", required=True, help="the video to lip-sync")
    p.add_argument("--audio", required=True, help="the voice track to sync onto it")
    p.add_argument("--sync-mode", default="cut_off",
                    choices=["cut_off", "loop", "bounce", "silence", "remap"])
    p.add_argument("--i-approve-cost", type=float, default=None)
    p.add_argument("--out", default=str(WORK / "lipsync"))
    p.set_defaults(func=cmd_lipsync)

    p = sub.add_parser("subtitles", help="transcribe dialogue and burn timed captions in -- paid (small), needs --i-approve-cost")
    p.add_argument("--scene-id")
    p.add_argument("--file", required=True)
    p.add_argument("--lang", help="language code, e.g. 'ru' -- omit to auto-detect")
    p.add_argument("--style", help="override the default ASS force_style string from config.json")
    p.add_argument("--i-approve-cost", type=float, default=None)
    p.add_argument("--out", default=str(WORK / "subtitled.mp4"))
    p.set_defaults(func=cmd_subtitles)

    p = sub.add_parser("bgremove", help="cut the subject out of its background -- paid, needs --i-approve-cost")
    p.add_argument("--scene-id")
    p.add_argument("--file", required=True)
    p.add_argument("--background-color", default="Black",
                    help="fal default is 'Black' -- check the live playground for other valid "
                         "enum values before relying on one that isn't confirmed here")
    p.add_argument("--no-audio", action="store_true", help="drop the audio track instead of preserving it")
    p.add_argument("--i-approve-cost", type=float, default=None)
    p.add_argument("--out", default=str(WORK / "bgremoved"))
    p.set_defaults(func=cmd_bgremove)

    p = sub.add_parser("assemble", help="stitch clips + voiceover with ffmpeg")
    p.add_argument("--clips", default=str(WORK / "clips"))
    p.add_argument("--voice")
    p.add_argument("--out", default=str(WORK / "final.mp4"))
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("ledger", help="show the local generation log")
    p.add_argument("--scene-id")
    p.set_defaults(func=cmd_ledger)

    args = ap.parse_args()
    args.func(args, cfg)


if __name__ == "__main__":
    main()
