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
  factory.py assemble --clips work/clips --out work/final.mp4 [--voice work/voice.mp3]
  factory.py ledger  [--scene-id S]
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
    """A ref is either an http(s)/data URL already, or a local path to upload."""
    if ref.startswith(("http://", "https://", "data:")):
        return ref
    return fal_upload(ref)


def build_video_payload(model, prompt, start_frame, seconds, resolution, negative_prompt, audio, cfg):
    """Veo and Kling take differently-shaped payloads. Branch on the model family."""
    image_url = resolve_image(start_frame)
    if "kling" in model:
        payload = {
            "prompt": prompt,
            "start_image_url": image_url,
            "duration": seconds,
            "generate_audio": audio,
        }
        if cfg["defaults"].get("kling_cfg_scale") is not None:
            payload["cfg_scale"] = cfg["defaults"]["kling_cfg_scale"]
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


# ------------------------------------------------------------------- rungs

def cmd_still(args, cfg):
    """Rung 1 — the cheap still. Face is locked by the reference images."""
    model = args.model or (
        cfg["models"]["still_edit"] if args.ref else cfg["models"]["still"]
    )
    rate = cfg["rates"]["still_per_image_usd"]

    payload = {
        "prompt": args.prompt,
        "num_images": args.count,
        "aspect_ratio": args.aspect or cfg["defaults"]["aspect_ratio"],
    }
    if args.ref:
        payload["image_urls"] = [resolve_image(r) for r in args.ref]

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

    url = first_url(result, "images", "image")
    record.update(status="success", output_url=url, request_id=result.get("_request_id"))
    log_generation(record)

    out = None
    if url and args.out:
        out = download(url, args.out)
        record["local_path"] = str(out)

    emit(record)


def cmd_motion(args, cfg):
    """Rung 2 — the Lite motion test. Start frame is mandatory."""
    model = args.model or cfg["models"]["motion_test"]
    seconds = args.seconds or cfg["defaults"]["motion_test_duration"]
    rate = cfg["rates"]["motion_test_per_second_usd"]
    cost = round(rate * seconds, 4)

    payload = build_video_payload(
        model, args.motion, args.start_frame, seconds,
        cfg["defaults"]["motion_test_resolution"],
        args.negative if args.negative is not None else cfg["defaults"]["negative_prompt"],
        cfg["defaults"]["motion_test_audio"],
        cfg,
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


def cmd_cost(args, cfg):
    key = {2: "motion_test_per_second_usd", 3: "final_take_per_second_usd"}.get(args.rung)
    if args.rung == 1:
        total = cfg["rates"]["still_per_image_usd"] * (args.count or 1)
        emit({"rung": 1, "images": args.count or 1, "cost_usd": round(total, 4)})
        return
    rate = cfg["rates"][key]
    total = round(rate * args.seconds, 4)
    emit({
        "rung": args.rung, "seconds": args.seconds,
        "rate_per_second_usd": rate, "cost_usd": total,
        "shown_as": f"{args.seconds}s x ${rate}/s = ${total}",
    })


def cmd_final(args, cfg):
    """Rung 3 — real money. Refuses to run unless the stated cost was approved."""
    model = args.model or cfg["models"]["final_take"]
    rate = cfg["rates"]["final_take_per_second_usd"]
    cost = round(rate * args.seconds, 4)

    if args.i_approve_cost is None:
        die(
            f"rung 3 costs ${cost} ({args.seconds}s x ${rate}/s).\n"
            f"  Nothing was spent. To proceed, re-run with:  --i-approve-cost {cost}"
        )
    if abs(args.i_approve_cost - cost) > 0.005:
        die(
            f"approved ${args.i_approve_cost} but this call costs ${cost}. "
            "Nothing was spent. Re-confirm the real number."
        )

    audio = cfg["defaults"]["final_audio"] if args.audio is None else args.audio

    payload = build_video_payload(
        model, args.motion, args.start_frame, args.seconds,
        args.resolution or cfg["defaults"]["final_resolution"],
        args.negative if args.negative is not None else cfg["defaults"]["negative_prompt"],
        audio,
        cfg,
    )

    record = {
        "scene_id": args.scene_id, "rung": 3, "model": model,
        "prompt": args.motion, "start_frame_url": args.start_frame,
        "duration_seconds": args.seconds, "cost_usd": cost,
    }

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


def cmd_assemble(args, cfg):
    """Stitch approved clips in filename order, then lay the voiceover over them."""
    clips_dir = pathlib.Path(args.clips)
    clips = sorted(p for p in clips_dir.glob("*.mp4") if p.is_file())
    if not clips:
        die(f"no .mp4 files in {clips_dir}")

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
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--aspect")
    p.add_argument("--model")
    p.add_argument("--out", default=str(WORK / "stills"))
    p.set_defaults(func=cmd_still)

    p = sub.add_parser("motion", help="rung 2 — Lite motion test")
    p.add_argument("--scene-id", required=True)
    p.add_argument("--start-frame", required=True, help="approved still URL or path")
    p.add_argument("--motion", required=True)
    p.add_argument("--seconds", type=int)
    p.add_argument("--negative", help="override the default negative prompt")
    p.add_argument("--model")
    p.add_argument("--out", default=str(WORK / "tests"))
    p.set_defaults(func=cmd_motion)

    p = sub.add_parser("cost", help="state a cost without spending anything")
    p.add_argument("--rung", type=int, required=True, choices=[1, 2, 3])
    p.add_argument("--seconds", type=int, default=5)
    p.add_argument("--count", type=int, default=1)
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("final", help="rung 3 — final take (requires approved cost)")
    p.add_argument("--scene-id", required=True)
    p.add_argument("--start-frame", required=True)
    p.add_argument("--motion", required=True)
    p.add_argument("--seconds", type=int, required=True)
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
