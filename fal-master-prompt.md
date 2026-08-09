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

---

## 5. Template harvest

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
