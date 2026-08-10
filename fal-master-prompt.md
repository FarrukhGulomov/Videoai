# fal Master Prompt — the director's brief

> The original `fal-master-prompt.md` was not present in the project folder.
> This version is rebuilt to match the rules in `02-runbook.md`. Replace it
> wholesale if the original turns up — the pipeline reads it as plain text.

This file is the standing instruction attached to every image and video
generation. It is not a prompt for one shot; it is the grammar every shot is
built from.

---

## 1. The non-negotiables

1. **The face is never invented.** Every video call takes an approved still as
   its start frame. A video model that is asked to produce a face from text
   will produce a different person every time.
2. **The character lock is copied verbatim.** `{{CHARACTER_LOCK}}` is pasted
   into the prompt exactly as stored in `characters.lock_text`. It is never
   paraphrased, shortened, or "improved".
3. **One change per iteration.** If a take fails, change exactly one thing —
   framing, or light, or motion — never two. Two changes tell you nothing
   about which one worked.
4. **Every attempt is logged**, including the failures, with its cost.

---

## 2. Prompt skeleton

Every shot prompt is assembled in this fixed order. Do not reorder — models
weight the opening tokens most heavily, and identity has to come first.

```
{{CHARACTER_LOCK}},
{{WARDROBE}},
in {{LOCATION}},
{{FRAMING}}, {{ANGLE}},
{{LIGHT}},
{{LENS}},
{{MOOD}},
{{NEGATIVES}}
```

### Slot vocabulary

| Slot | Safe values (likeness holds) | Risky values (likeness breaks) |
|---|---|---|
| `FRAMING` | medium close-up, close-up, medium shot | wide shot, extreme wide, full body |
| `ANGLE` | frontal, slight 10–15° off-axis | 3/4 profile, full profile, low angle |
| `LIGHT` | soft flat light, large soft key, overcast | hard low-key, single hard rim, heavy chiaroscuro |
| `LENS` | 50mm, 85mm, shallow but not extreme | fisheye, 14mm, extreme bokeh |

`NEGATIVES` is always at least:

```
do not alter the face, no change to facial structure, no beautification,
no age change, no extra fingers, no warped hands, no text, no watermark
```

### 2.1 Rung-1 variant selection — automatic, before the user sees anything

`factory.py still` now defaults to `--count 3` (not 1). This exists because
face-identity misses are the single most common rung-1 failure this project
has hit, and picking the best of three matches the reference far more
reliably than judging one shot in isolation.

The selection step is **not** a separate coded face-recognition system —
fal has no face-similarity/verification endpoint (checked; the closest
thing is `ip-adapter-face-id`, a *generation* technique, not a comparator),
and adding one means either breaking this project's stdlib-only design with
a local embedding model, or onboarding a new third-party API (new account,
new domain to allowlist). Given the user's explicit choice, the comparator
is Claude itself:

1. Run `still` with the default 3 variants — `local_paths` in the result
   lists all three.
2. Read all three images plus `identity.canonical_face_ref` and the source
   photos in `work/refs/face/`.
3. Judge each variant against the reference on the things that actually
   drift — face shape/width, nose, jaw, eyebrows, not lighting or framing.
4. Discard any variant that's a clear miss without showing it to the user.
   Present only the winner (or top 2 if genuinely close) for approval, not
   all three by default — the point is to cut the user's back-and-forth,
   not move it downstream.

If this project later needs comparison speed/scale beyond what an LLM call
per batch gives, revisit the onnxruntime-local or third-party-API options
noted above — that trade-off should be made deliberately, not defaulted
into.

---

## 3. Motion vocabulary (rung 2 and 3)

Motion is described as **one** move with a magnitude and a duration. Never
stack moves.

| Move | Written as |
|---|---|
| Push in | `slow push in, 15% over 5 seconds` |
| Pull out | `slow pull back, 20% over 5 seconds` |
| Pan | `gentle pan left, 10° over 5 seconds` |
| Static | `locked off, no camera movement; subject breathes and blinks only` |

Rules:

- **No push-in combined with a light change.** This is the single most
  reliable way to break a likeness.
- Subject motion is described separately and kept small: *"turns head 10° to
  camera and settles"*, not *"turns and walks away"*.
- If the shot needs two moves, it is two shots.

---

## 4. Rung discipline

| Rung | What it is | Settings | Judge only |
|---|---|---|---|
| 1 | The still | Nano Banana Pro, reference photos attached, face untouched | Is it him? Is the framing right? |
| 2 | Motion test | Veo 3.1 Lite, 720p, 5s, no audio | Does the movement read? Any artifacts? |
| 3 | Final take | Full quality, approved duration | Everything |

At rung 2 you **ignore resolution and colour**. Judging colour on a Lite test
is how people spend rung-3 money re-testing things that were never broken.

At rung 3, cost is stated (`duration × rate`) and approved **before** the call,
one scene at a time. Never batched.

### When rung 2 can be skipped

Rung 2 exists because a still says nothing about how the *video* model will
animate it — pacing, direction, artifacts, and identity drift under motion
are failure modes a static image cannot show. Skipping straight to rung 3
is only cheaper if the shot is simple enough that motion is very unlikely to
surprise you; otherwise a failed rung-3 attempt costs ~5x more to redo than
a failed rung-2 one did.

| Shot type | Rung 2 required? |
|---|---|
| Simple, low-risk motion: calm walking, small head turn, static hold, slow push/pull | **Skip** — go still → final directly |
| Complex motion: fights, impacts, falls, fast camera moves, multi-beat action | **Mandatory** — a rung-3 attempt without a passing rung-2 test is not allowed |

When in doubt, treat the shot as complex. This is a per-shot call, made and
stated each time, not a blanket rule to stop applying judgment.

### Checking the rendered output, not just the start frame

A rung-1 face check only proves the *still* matches. Motion can still drift
the face mid-clip — this project hit that exact failure today (an approved
still, then a rung-3 take where the face read as thinner than the
reference). The still-only check cannot catch this because the drift
doesn't exist yet at that point.

After every rung-2 and rung-3 output, before presenting it:

```
factory.py frames --file <clip> --count 3 --out work/qc/<scene_id>
```

Read the extracted frames alongside `identity.canonical_face_ref`, same
judgment as the rung-1 selection (2.1): face shape, nose, jaw, eyebrows,
not lighting or resolution (rung 2 still ignores those per above). If a
rung-3 final drifts, that's a failed take — say so plainly and re-run
rather than presenting a mismatched result and letting the user catch it.
This costs nothing (`cmd_frames` is pure ffmpeg) and takes seconds, so
there's no reason to skip it.

---

## 5. Model choice (rung 3) — pick per shot, don't default blindly

Three models are wired into `config.json`. Pick per shot based on what the
shot actually needs, then say which one and why before running rung 3 — this
is a real decision each time, not a fixed default.

| Model | `--model` value | $/s (audio on) | Strongest at | Use for |
|---|---|---|---|---|
| **Kling 3.0 Standard** | `fal-ai/kling-video/v3/standard/image-to-video` | $0.126 | Smooth natural motion, cinematic color grading, multi-character dialogue with phoneme-level lip-sync. Proven on this user's own approved Higgsfield clips (`model=kling3_0, mode=std, cfg_scale=0.5, sound=on`). | **Default for everything** — action, fights, walking, most dialogue. Cheapest of the three. |
| **Veo 3.1** | `fal-ai/veo3.1/image-to-video` | $0.15 | Face rendering and identity consistency specifically; reduces uncanny-valley artifacts; strong audio-driven dialogue. | Face-critical calm close-ups where Kling's result drifted from the reference face after a fair retry. |
| **Seedance 2.0** | `bytedance/seedance-2.0/image-to-video` | $0.3024 | Best overall benchmark quality; best camera-movement precision (91% reference match); most realistic cloth/liquid/hair/particle physics. | Only a single hero/signature shot where precise camera choreography or physics simulation (explosion debris, cloth, spray) is the entire point — never the default, it's 2.4x Kling's price. |

Rates live in `rates.final_take_per_second_usd_by_model`, keyed by exact fal
model id. `cmd_cost`/`cmd_final` refuse to run for a `--model` not in that
table rather than guess a price — add the real rate there first if a new
model gets added.

**Decision order for a new shot:** start from Kling (it is right most of the
time and is the cheapest). Switch to Veo only if a face-critical shot's
identity is drifting. Reach for Seedance only when the shot's success
genuinely depends on camera-move precision or physics simulation that the
other two have already shown they can't deliver — and say so explicitly
before spending the extra money, same as any other rung-3 cost.

## 5.1 Prompt density (learned from the Higgsfield reference clips)

The reference prompts that produced the liked results were far denser than
what this pipeline was writing early on. Match this density for rung-3 final
prompts:

- **Choreograph the camera explicitly**, as a sequence: "camera starts high
  looking down... then pushes in and drops to a low three-quarter angle...
  ending on a medium shot." Not just "push in."
- **Repeat the identity lock as a hard sentence inside the motion prompt
  itself**, not only in `{{CHARACTER_LOCK}}`: *"His face stays identical to
  the first frame throughout — same features, same proportions, no
  morphing, no re-lighting of facial structure."* This exact phrasing
  recurs across every one of the user's approved Higgsfield clips.
- **Name the finish**: lens (anamorphic, 35mm/50mm), grain (fine film
  grain), palette (dusty ochre / cool blue-teal / whatever fits the scene),
  and light falloff. This is what makes stills and video read as "shot on a
  camera" instead of "generated."
- **State what does NOT happen**, in-line, not just in `NEGATIVES`: "no
  camera shake, no lens flare, no on-screen text, no modern objects" tuned
  to the specific scene, in addition to the standard negative list.

## 6. Template harvest

A shot that landed in three iterations or fewer is written back to `templates`
with its variable parts replaced:

```
{{CHARACTER_LOCK}} in {{LOCATION}}, medium close-up, frontal,
soft flat key from camera left, 85mm, calm and level,
do not alter the face, no text, no watermark
```

The point of the whole system is that the second video reuses the first
video's solved shots. A template library that isn't growing means the cost per
scene will not fall.
