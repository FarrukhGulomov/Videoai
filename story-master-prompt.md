# Story Master Prompt — the writer's brief

Standing instructions for the narrative layer — premise, character, conflict,
structure — attached to every project before a single frame is generated.
Companion to `fal-master-prompt.md`, which governs the visual layer once the
story below is locked: that file answers "does it look like him and does it
look real," this one answers "does anyone care and does it make sense."

Distilled from Lajos Egri's *The Art of Dramatic Writing* and Linda Seger's
*Making a Good Script Great*, translated into this project's actual
production artifacts — not general screenwriting theory, but what to
actually write down before `factory.py still` gets called.

---

## 1. The non-negotiables

1. **No premise, no generation.** A story is one sentence that must be
   proven. Every scene either proves it or gets cut — "it looks cool" is
   not a reason to keep a shot.
2. **The character has three dimensions before a single still is made** —
   physiology, sociology, psychology (section 3). A protagonist written on
   one dimension comes out flat on screen no matter how good the reference
   photo is.
3. **Every scene earns its place in one sentence, or it's cut.** If you
   can't say why a scene exists in one sentence, it doesn't survive the
   next pass.
4. **One goal per rewrite pass** — the same discipline `fal-master-prompt.md`
   §1 already applies to a single generation ("change exactly one thing —
   framing, or light, or motion — never two") applies to the *script* too:
   structure, then character, then dialogue/rhythm, then polish. Never all
   four in one pass — you won't know which fix actually worked.

---

## 2. Premise — one sentence, provable (Egri)

Every project starts with one sentence in this shape:

```
[Character trait] → [action/conflict] → [inevitable result]
```

Examples: *"Blind faith leads to betrayal."* *"Overcoming fear brings
freedom."* Not a topic ("a story about loneliness") — a claim that has to
be *proven* by the end. A premise-less shot list is a beautiful road with
no destination: every frame can be individually gorgeous and the whole
thing still goes nowhere.

**Rule:** any scene that doesn't help prove the premise is a scene to cut,
regardless of how strong the shot looks on its own.

---

## 3. Character — Egri's three dimensions, mapped to what actually gets written

Egri builds a character like a skeleton: skip one layer and the character
reads flat on screen, no matter how good the likeness-lock is.

| Egri's dimension | Production artifact | What actually gets written |
|---|---|---|
| **Physiology** — age, sex, appearance, health, way of moving | The character sheet (`identity.characters.<name>` in `config.json`) | Already covered by this project's existing identity-lock system — nothing new needed here, just don't skip it for a new character. |
| **Sociology** — origin, occupation, family, class, environment, beliefs | Location, wardrobe, props | Shown through objects in frame, never through on-screen text or narration. A worn ID badge, a specific desk, a particular car say more than a line of dialogue ever could — and cost nothing extra to generate once they're in the prompt. |
| **Psychology** — strongest desire, deepest fear, temperament, moral limits | The body-language clause in every motion prompt | Not "happy" — *"his jaw sets for a beat, his eyes drop, then his shoulders loosen."* This is the one layer that's easiest to skip under prompt-length pressure and the one that actually makes a generated clip read as a performance instead of a pose. |

Action grows out of character, not the other way around — decide *why* a
person does something before writing *what* they do, then let the "what"
follow from the "why."

---

## 4. Conflict — growing pressure, an equal opponent (Egri)

Three kinds, only one is usable as a spine:

- **Static conflict** — the situation doesn't move; characters argue but
  nothing shifts. The audience gets bored.
- **Jumping conflict** — emotion leaps straight to its peak. Trust breaks:
  nothing was earned.
- **Rising conflict** — pressure builds step by step. **This is the only
  correct type for a spine.**

Conflict isn't bolted on from outside — it comes from desire meeting an
obstacle. And the obstacle has to be a real match: **if the protagonist
could just walk out the door, there's no drama.** A weak antagonist (a
person, a system, or circumstance) makes a weak story regardless of how
strong the protagonist's performance is. Pressure should read as
increasing by roughly one notch every scene — track this explicitly when
planning the shot list, the same way `fal-master-prompt.md` §4 tracks rung
by rung.

---

## 5. Orchestration — characters have to contrast, or the frame reads as one blur (Egri)

When two characters share a scene, give them visibly different values,
pace, and manner — silhouette, color, pace of movement, position in frame.
Two characters who read the same on screen collapse into one blurred
presence, even if their dialogue is written to be different. This is a
*visual* instruction as much as a writing one — it belongs in the same
motion-prompt pass that names the actor explicitly (`fal-master-prompt.md`
§2.3's fix for the "ambiguous 'he'" failure). Naming who does what isn't
enough on its own if both people also look and move identically.

---

## 6. Transition — emotion changes through steps, never a jump (Egri)

**The most overlooked rule, and the one this project is most likely to
drop under prompt-length pressure.** Egri: emotion doesn't jump, it shifts
through stages — *love → indifference → doubt → resentment → hate.* Skip an
intermediate stage and the audience can't say why, but they stop believing
it. This is the *narrative* version of the exact failure `fal-master-prompt.md`
§3.1 diagnosed on the *visual* side (the "animated photo" problem): both
come from skipping information the audience needs to feel continuity, not
from the underlying material being wrong.

**In this project's terms:** an emotional beat that needs more than one
step is two shots, not one — the same "if the shot needs two moves, it is
two shots" rule `fal-master-prompt.md` §3 already applies to camera motion,
applied here to feeling instead of movement. Where budget allows, bridge
consecutive shots literally: the outgoing clip's last frame becomes the
next clip's start frame (`--end-frame`, already supported by
`build_video_payload` for Kling/Seedance/LTX) so the transition is carried
in the pixels, not just implied by the cut.

---

## 7. Structure — three acts, mapped to real seconds (Seger)

Format dictates the map. Write the act breakdown in actual seconds before
writing a single shot description — this becomes the spine the shot list
is built against.

| Format | Act 1 — setup | Act 2 — rising | Act 3 — resolution |
|---|---|---|---|
| 30s ad | 0–6s: hook + catalyst together | 6–22s: two rising obstacles | 22–30s: resolution + call to action |
| 60s Reels / VSL | 0–8s: the problem is made clear | 8–45s: three obstacles + a midpoint | 45–60s: climax + offer |
| 3-min short film | 0–45s: world + protagonist | 45–135s: pressure + a real midpoint turn | 135–180s: climax + close |

**Catalyst** — the event that breaks the protagonist's equilibrium. The
earlier it lands, the better; in a Reels-length piece it should be *in the
first frame*, introduction second, never the reverse.

**Turning points** — end each act on an event that raises a question, not
one that answers one. Mark turning points visually (a location change, a
light change, a music change) so the audience's attention re-engages right
there without needing a line of dialogue to explain it.

---

## 8. Scene, sequence, act — and the one-sentence test (Seger)

A scene has its own beginning, middle, end. Several scenes form a
sequence; sequences build an act. Before writing a scene, apply the test:
**if you can't say why the scene is necessary in one sentence, it's cut** —
then check whether removing it actually breaks the story. If it doesn't,
it wasn't necessary; the "I love this shot" instinct is exactly what the
mistake list below warns against.

---

## 9. Setup/payoff, subplot, and the character arc (Seger)

- **Setup and payoff.** Any meaningful detail shown on screen has to pay
  off later. A prop that reappears in the final clip is what makes a
  sequence of AI clips read as *a film* instead of *a reel of clips* — and
  it costs nothing extra to write into two prompts instead of one.
- **Subplot (a B-line).** Only for formats longer than ~60 seconds. In a
  short-format piece a subplot eats attention and the story's clarity —
  don't write one just because a feature-length script would have one.
- **Character arc.** Who they were → what they realized → who they became.
  The cheapest, strongest way to show it on screen: same composition in the
  first and last shot, different character state. This is a shot-list
  decision to make explicitly, not something that emerges by accident.
- **Theme** is shown, not stated — carried by the final image or the final
  line, never spelled out in narration or on-screen text.

---

## 10. Rewrite in layers — one goal per pass (Seger)

Seger's rewrite discipline, applied to prompt iteration exactly the way it
applies to a script:

1. **Pass 1 — structure.** Acts, turning points, the shape of the whole
   piece.
2. **Pass 2 — character.** Motive, arc, the three dimensions from section 3.
3. **Pass 3 — dialogue and rhythm.** Line reads, pacing between beats.
4. **Pass 4 — polish.** Everything else.

Trying to fix all four at once is the slowest path to the least result —
exactly the reasoning behind `fal-master-prompt.md`'s "one change per
iteration": change five parameters in one generation and you learn nothing
about which one mattered. Same logic, script layer instead of image layer.

---

## 11. Pre-generation checklist

Run this before the first `factory.py still` call, not after the shot list
is already written — catching a structural problem here costs nothing;
catching it after ten shots are generated costs real money.

- [ ] Does the premise fit in one sentence?
- [ ] Does every planned shot prove that premise?
- [ ] Are all three of the protagonist's dimensions written down (not just
      the physical one the reference photo covers)?
- [ ] Are their strongest desire and deepest fear both named?
- [ ] Is the opposing force genuinely equal in strength to the protagonist?
- [ ] Is it actually impossible for the protagonist to just walk away?
- [ ] Does conflict rise shot to shot, rather than sitting static?
- [ ] Is there no single shot where emotion jumps without an intermediate
      step?
- [ ] Is body language written into every motion prompt, not just named
      once and assumed?
- [ ] Is the character reference consistent across every planned shot?
- [ ] Does the act breakdown fit real seconds for the target format
      (section 7)?
- [ ] Is the catalyst inside the first few seconds?
- [ ] Does every setup have a matching payoff planned?
- [ ] Does the final shot show the premise's result?
- [ ] Does the whole thing hold together with the sound off?

---

## 12. Common failure patterns

From Egri, Seger, and this project's own hit failures — watch for these
specifically:

- **Pretty frames, no premise.** The result reads as a slideshow, not a
  story — no matter how strong any individual shot is.
- **Desire with no real obstacle.** The protagonist wants something and
  nothing meaningfully resists them.
- **Emotion peaks in the first shot.** There's nowhere left to rise to.
- **A "favorite shot" survives that doesn't serve the story.** Keep it out
  of the final cut if it doesn't pass the one-sentence test in section 8,
  no matter how good it looks.
- **The hook lands on shot 5 or 6 instead of shot 1.** By then the viewer
  is already gone, especially in short-form.
- **Act 2 is flat** — every obstacle is the same weight, so the rise never
  registers even though pressure is technically increasing on paper.
- **The call to action lands before the climax**, spending the emotional
  peak on the wrong beat.
- **Face drifts shot to shot** because a shot was generated without the
  identity reference (`fal-master-prompt.md` §2.1/2.2) — a story failure
  and a technical failure at once, since a drifting face breaks the
  character-arc payoff in section 9 as much as it breaks the likeness.

---

## 13. Fill-in templates

**Premise and character worksheet** — fill in before writing the shot list:

```
Premise (one sentence):

Protagonist's desire:

Protagonist's deepest fear:

Opposing force (equal in strength):

Obstacles (rising order, 1 → 2 → 3):

Final shot (proves the premise):
```

**30-second spot timing map** — adjust proportionally for other formats
using the table in section 7:

```
0–6s   · catalyst
6–14s  · obstacle 1
14–22s · obstacle 2 (stronger)
22–27s · climax
27–30s · resolution + CTA

Setup → payoff detail:
```

---

This document is an independent, project-adapted synthesis of Lajos Egri's
*The Art of Dramatic Writing* (1946) and Linda Seger's *Making a Good
Script Great* — not a replacement for either book. Use it as the writing
gate that runs before `fal-master-prompt.md` takes over for the visual
generation itself.
