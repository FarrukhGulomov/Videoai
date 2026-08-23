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

`factory.py still` defaults to `--count 5` (raised from an earlier default
of 3). Face-identity misses are the single most common rung-1 failure this
project has hit; more samples plus a stricter judge (below) narrows that
failure rate further than either change alone would.

The selection step is **not** a separate coded face-recognition system —
fal has no face-similarity/verification endpoint (checked). Two categories
of alternative were evaluated and deliberately not adopted:

- A **local embedding model** (onnxruntime + a face-recognition net) would
  break this project's stdlib-only design.
- **Identity-preserving generation techniques** on fal — `ip-adapter-face-id`,
  `pulid`, `instant-id` — were checked directly (real, callable schemas, not
  guessed). They aren't comparators, they're a *different way to generate*
  the still, and were set aside for now: all three are older, SD1.5/SDXL-era
  techniques, not the same generation family as the current still model
  (`nano-banana-pro`, a modern instruction-following multimodal model), and
  their own documented trade-off is real — InstantID in particular locks
  pose/expression along with identity, which fights this project's
  "medium close-up, frontal, safe FRAMING/ANGLE only" discipline (section 2,
  slot vocabulary) rather than helping it. None of the three has been
  rung-1 A/B tested against nano-banana-pro's actual output on this
  project's actual protagonist. Don't switch the default still model on an
  unverified face-similarity claim when the current one has a real,
  if imperfect, production track record (13+ shots). If nano-banana-pro's
  best-of-5 keeps hard-failing on a specific difficult character (see the
  checklist below), that's the trigger to spend a small rung-1 budget
  actually testing one of these three side by side — not before.

Given all that, the comparator stays Claude itself, run through an explicit
checklist rather than one holistic impression — a structured pass catches
drift that a "looks about right" judgment call misses:

1. Run `still` with the default 5 variants — `local_paths` in the result
   lists all five.
2. Read all five images plus `identity.canonical_face_ref` and the source
   photos in `work/refs/face/` (or the named character's `source_sheet`).
3. For each variant, check each of these against the reference and record
   an explicit pass/fail — not a vibe:
   - face shape/width and jawline
   - nose (bridge width, length, tip shape)
   - eyes (shape, spacing) and eyebrows (shape, thickness)
   - mouth/lip shape
   - hairline and hair texture/color (grey pattern, if the character has one)
   - skin tone and any distinguishing marks (moles, scars, wrinkle pattern)
   Ignore lighting, framing, and wardrobe — those aren't identity and
   shouldn't cost a variant its pass.
4. **Any single hard-fail discards the variant.** Do not average an
   otherwise-good variant against one clear miss — a wrong nose or jaw on
   an otherwise perfect variant is still a wrong face.
5. Present only the winner (or top 2 if genuinely close after step 4) for
   approval, not all five by default — the point is to cut the user's
   back-and-forth, not move it downstream.

If every variant in a batch hard-fails the same feature, that's a signal
the prompt or reference needs to change, not that one more variant will fix
it by luck — stop and adjust before re-running.

### 2.2 `--no-canonical` for inserts with OTHER people, not the protagonist

`cmd_still` auto-prepends `identity.canonical_face_ref` to every `--ref`
list (§2.1's whole point). This is correct for any shot with the
protagonist in it, and harmless for a shot with no people at all. It is
**wrong** for a shot depicting other, unrelated people the protagonist is
not part of — e.g. this film's S07 (old anonymous team photos on a desk).
Hit this directly: the first S07 generation put a recognizable
worker-like face into two of the "anonymous" photos, reading as an
accidental de-aged cameo — exactly what section 0.2 rules out.

Pass `--no-canonical` for any insert/background shot whose people are
explicitly *not* the protagonist. In this film that's S07 and the two
S27/S28 "new executive, senior manager" shots — none of those three should
carry any trace of the worker reference.

### 2.3 Multi-person shots: "he" is ambiguous, name the actor explicitly

Hit this twice in immediate succession (S09, S12) before the pattern was
obvious. When a motion prompt says "he lifts his hand" or "he speaks" in a
multi-person frame, the model does not reliably resolve *which* person —
it tends to animate whoever reads as most prominent/foreground in the
frame, not whichever person the prompt-writer had in mind. Both misses put
action on a foreground colleague instead of the protagonist, which is a
story-breaking error in a film specifically about the protagonist going
unnoticed — the opposite of an ordinary artifact.

The fix that worked both times: name the actor by a visible, unambiguous
trait ("the grey-bearded man on the right, near the screen"). Do this by
default for any shot with more than one person where only one of them
should perform the scripted action — don't wait for the miss to happen
first.

**Correction, added after user feedback that the first 13 shots read as
"an animated photo," not video (see section 3.1):** the other half of
this fix — "the men in the foreground do not move at all" — went too far.
Freezing everyone else's *idle* motion, not just their reaction to the
scripted beat, is a direct cause of the cinemagraph look: one moving part
against a frozen tableau. Say "do not react to X / do not look toward X,"
never "do not move at all." Background people keep their own small idle
motion regardless.

---

## 3. Motion vocabulary (rung 2 and 3)

Motion is described as **one** move with a magnitude and a duration. Never
stack moves.

| Move | Written as |
|---|---|
| Push in | `slow push in, 15% over 5 seconds` |
| Pull out | `slow pull back, 20% over 5 seconds` |
| Pan | `gentle pan left, 10° over 5 seconds` |
| Held | `camera holds, faint handheld presence, imperceptible drift; everyone in frame keeps small natural idle motion` |

Rules:

- **No push-in combined with a light change.** This is the single most
  reliable way to break a likeness.
- Subject motion is described separately and kept small: *"turns head 10° to
  camera and settles"*, not *"turns and walks away"*.
- If the shot needs two moves, it is two shots.

### 3.1 Ambient life by default — or it reads as an animated photo, not video

User feedback after the first 13 shots: they all read as "a photo brought to
life," not footage. Diagnosed, not guessed at: every one of those 13 prompts
used `camera static` / `camera completely static` combined with exactly one
described action and, in multi-person shots, an explicit freeze on everyone
else (see 2.3's correction above). One moving part against an otherwise
frozen frame *is* the definition of a cinemagraph — this wasn't a model
failure, it was what the prompts asked for.

Two things change by default from here on, for every shot with people in it,
face-critical or not:

1. **The camera is never truly locked.** Replace `camera static` with
   `camera holds, faint handheld presence, imperceptible drift` (or similar).
   Real footage — even a tripod — carries a trace of this; its total absence
   is what reads as artificial.
2. **Every visible person keeps idle motion, always**, independent of
   whatever the scripted beat is: small weight shifts, blinking, a pen
   turning in someone's fingers, breathing. State this explicitly rather
   than assuming it: *"everyone in frame keeps their own small natural idle
   motion throughout — blinking, small weight shifts."* Silence on this
   point is what produced the mannequin effect in S09–S13.

This does not conflict with 2.3 (naming the actor for the scripted action) or
with the face-lock rules above (no push-in + light change, small subject
motion) — it layers underneath them. The protagonist's face-lock instruction
stays exactly as strict; what's added is that the *world around him* is no
longer told to hold still.

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

Four models are wired into `config.json`. Pick per shot based on what the
shot actually needs, then say which one and why before running rung 3 — this
is a real decision each time, not a fixed default. New fal video models
appear often (LTX-2.3 and Seedance 2.0 both shipped after this project
started) — check `fal.ai/models` periodically rather than assuming this list
is final, but **verify a candidate's real schema and price before wiring it
in** (WebFetch the model's `/api` page for a literal JSON example, the way
LTX-2.3 was added) — never guess a payload shape from a marketing page.

| Model | `--model` value | $/s (audio on) | Strongest at | Use for |
|---|---|---|---|---|
| **Kling 3.0 Standard** | `fal-ai/kling-video/v3/standard/image-to-video` | $0.126 | Smooth natural motion, cinematic color grading, multi-character dialogue with phoneme-level lip-sync. Proven on this user's own approved Higgsfield clips (`model=kling3_0, mode=std, cfg_scale=0.5, sound=on`) **and** on 13 real shots of this film. | **Default for everything** — action, fights, walking, most dialogue. The only one of the four with an actual track record on this project. |
| **LTX-2.3** | `fal-ai/ltx-2.3/image-to-video` | $0.06 (1080p) | Cheapest by a wide margin (half of Kling); up to 20s in **one** continuous call, avoiding the multi-clip-concat audio-loss bug this project already hit once; vertical-native, which matches this film's 9:16 frame directly. | Cost-sensitive or ambient shots, and any shot that wants a long uncut take instead of several stitched clips. **Not yet tested on this project's actual likeness-holding** — run it through a rung-2 test before trusting it on a face-critical shot, same as any new model. |
| **Veo 3.1** | `fal-ai/veo3.1/image-to-video` | $0.15 | Face rendering and identity consistency specifically; reduces uncanny-valley artifacts; strong audio-driven dialogue. | Face-critical calm close-ups where Kling's (or LTX's) result drifted from the reference face after a fair retry. |
| **Seedance 2.0** | `bytedance/seedance-2.0/image-to-video` | $0.3024 | Best overall benchmark quality; best camera-movement precision (91% reference match); most realistic cloth/liquid/hair/particle physics. | Only a single hero/signature shot where precise camera choreography or physics simulation (explosion debris, cloth, spray) is the entire point — never the default, it's 2.4x Kling's price and 5x LTX's. |

Rates live in `rates.final_take_per_second_usd_by_model`, keyed by exact fal
model id. `cmd_cost`/`cmd_final` refuse to run for a `--model` not in that
table rather than guess a price — add the real rate there first if a new
model gets added.

**Decision order for a new shot:** start from Kling (it is right most of the
time, is cheap, and is the only one with a real track record here). Reach
for LTX when cost or a long single take matters more than a proven identity
track record — but sanity-check the face on a cheap rung-2 test first, don't
promote it straight to a face-critical final. Switch to Veo only if a
face-critical shot's identity is drifting on the cheaper options. Reach for
Seedance only when the shot's success genuinely depends on camera-move
precision or physics simulation the other three have already shown they
can't deliver — and say so explicitly before spending the extra money, same
as any other rung-3 cost.

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

## 7. Post-production toolkit — beyond the video rungs

Five commands exist for polish and compositing that no video-generation call
can give you, each verified against fal's own `/api` docs before wiring in
(same rule as section 5). Four are paid and follow rung 3's exact
discipline: state the cost, approve the exact number, nothing runs
otherwise. One is free.

| Command | Cost | What it's for | Use when |
|---|---|---|---|
| `polish --deflicker` | Free (ffmpeg, local) | Removes frame-to-frame luminance flicker — a generated-video tell that reads as "artificial" independent of anything the prompt controls. | Every final clip, by default. It's free — there's no reason to skip it. Complements section 3.1's ambient-motion rules; it doesn't replace them. |
| `upscale` | $0.01–0.08/s by output tier (`fal-ai/topaz/upscale/video`) | Real detail-adding upscale — distinct from `polish --upscale`'s free scale+sharpen, which adds no new information. | The final assembled cut only, once, not every rung-2/3 test — this is expensive enough that upscaling a take you might discard is wasted money. `--tier` must match the real output resolution or the approved cost won't match what fal bills. |
| `lipsync` | ~$0.1333/s (`fal-ai/sync-lipsync/v3`, $8/min) | Syncs a separately-recorded or cloned voice onto an existing clip's mouth movement — ADR-style fixes, or swapping in a cleaner voice take after the fact. | Only when the video's own built-in audio (Kling/Veo's `generate_audio`) isn't good enough and a real voice performance needs to be dropped in instead. Not a default step. |
| `subtitles` | ~$0.0008/s, **rate unconfirmed** — see `config.json`'s `_post_production_note` (`fal-ai/wizper`) | Transcribes dialogue and burns real per-line-timed captions in via ffmpeg/libass, using `DejaVu Sans` (confirmed to render Cyrillic correctly). | Any film with dialogue in a script that needs on-screen text — replaces hand-timing a drawtext burn-in the way this project did once for "25 yil." |
| `bgremove` | $0.0042/s (`bria/video/background-removal/v3`) | Cuts the subject out of its background. | Compositing the protagonist into a different plate than what was generated, or keying a shot for a VFX-style comp. Not a default step. |

`upscale`/`lipsync`/`bgremove` accept local files directly — `resolve_image()`
uploads any file type despite its name, video and audio included.
`subtitles` feeds the whole video file straight to Wizper (it accepts mp4
natively), no local audio-extraction step needed.

### 7.1 Multi-shot continuity — `final --shots-json` (Kling v3 only)

Kling v3's own `/api` docs confirm a real `multi_prompt` parameter — a list
of `{"prompt": ..., "duration": ...}` elements — that renders several shots
as one continuous take in a single call, instead of several separate
`final` calls stitched afterward in `assemble`. `factory.py final` exposes
this as `--shots-json`, taking either an inline JSON list or `@file.json` in
that exact shape, replacing `--motion`/`--seconds` entirely for that call.

This is Kling-v3-only (`build_video_payload` refuses any other model with a
clear error) and rung-3-only — there's no reason to burn a rung-2 Lite test
on stitching shots together; test each shot's own motion individually the
normal way first, then reach for `--shots-json` only once you're combining
already-proven shots into one take. Cost is billed at the same per-second
rate as any other Kling call, summed across the shots' durations — that's
an assumption from fal billing every video call by total output duration,
not a separately documented multi-shot price, so verify it on the first
real invoice.
