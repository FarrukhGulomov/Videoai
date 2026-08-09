# Production loop — operator's card

How the runbook's Part 2 is actually executed. The user gives an idea and
approves rung 3; everything else runs from here.

Supabase project: `hbxuywvahhxnmiefnpro` (`FarrukhGulomov's Project`, ap-southeast-1)
Driven over the Supabase MCP tools. `scripts/factory.py` handles fal.ai,
downloads, cost math, and ffmpeg.

---

## 0. Once per project

```sql
insert into projects (name, brief, budget_usd)
values ('<name>', '<the idea in one paragraph>', 10.00)
returning id;
```

Budget defaults to the fal credit on hand. Stop at 80% (`budget_used_pct`
in `project_economics`) and re-plan rather than pushing through.

## 1. Brief → scenes

Write the scene list. One row per shot, `status = 'draft'`, no generation yet.

```sql
insert into scenes (project_id, scene_number, title, description,
                    character_id, prompt, motion, duration_seconds, has_face)
values (...);
```

`prompt` is assembled from `fal-master-prompt.md` §2 — character lock first,
verbatim, then wardrobe, location, framing, angle, light, lens, mood,
negatives. Reuse a `templates` row whenever one fits, and bump its
`times_used`.

## 2. Rung 1 — the still

Every scene with `has_face = true`. Never skipped.

```bash
python3 scripts/factory.py still \
  --scene-id <uuid> \
  --prompt "<assembled prompt>" \
  --ref <reference photo url or path>   # repeatable
```

Show the image to the user, get a yes/no. On yes:

```sql
insert into assets (scene_id, project_id, kind, url, rung, approved, is_start_frame)
values (<scene>, <project>, 'still', '<url>', 1, true, true);

update scenes set status = 'still_approved', updated_at = now() where id = <scene>;
```

On no: change **one** thing (framing, or light, or angle — never two) and
re-run. Log the rejection either way.

## 3. Rung 2 — the motion test

Start frame is the approved still. ~$0.15 per test. Runs without asking.

```bash
python3 scripts/factory.py motion \
  --scene-id <uuid> \
  --start-frame "<approved still url>" \
  --motion "<one move, magnitude, duration>" \
  --seconds 5
```

Judge **only**: does the movement read, are there artifacts. Ignore
resolution and colour. On pass, `update scenes set status = 'approved'`.

Log every attempt, including failures — `factory.py` writes them to
`work/generations.jsonl` automatically; mirror into `generations` with
`rung = 2` and the real `cost_usd`.

## 4. Rung 3 — the final take ⬅️ **the only place the user is asked**

Only for `status = 'approved'`. One scene at a time, never batched.

```bash
python3 scripts/factory.py cost --rung 3 --seconds <n>   # spends nothing
```

State the number to the user in Uzbek, get an explicit yes, then:

```bash
python3 scripts/factory.py final \
  --scene-id <uuid> \
  --start-frame "<approved still url>" \
  --motion "<same motion that passed rung 2>" \
  --seconds <n> \
  --i-approve-cost <the exact number shown above> \
  --out work/clips/<NN>.mp4
```

The script refuses to run without `--i-approve-cost`, and refuses again if
the number does not match what the call actually costs. Both refusals spend
nothing.

## 5. Voice and assembly

ElevenLabs for the voiceover → `work/voice.mp3`. Then:

```bash
python3 scripts/factory.py assemble \
  --clips work/clips --voice work/voice.mp3 --out work/final.mp4
```

Clips concatenate in filename order, so name them `01.mp4`, `02.mp4`, …

> Gotcha: the runbook's `-shortest` flag means a voiceover shorter than the
> video **truncates the video**. Check `factory.py probe` on the result and
> pad the voice track if the durations disagree.

## 6. Harvest the template

Any scene that landed in ≤3 iterations:

```sql
insert into templates (name, category, prompt_skeleton, motion, times_used)
values ('<shot type>', '<category>',
        '{{CHARACTER_LOCK}} in {{LOCATION}}, ...', '<motion>', 1);
```

This is the step that makes the next video cheaper. Skipping it means
`cost_per_scene` stays flat.

## 7. Close the books

```sql
select * from project_economics where project_id = '<id>';
select scene_number, title, total_generations, total_cost_usd
from scene_economics where project_id = '<id>' order by total_cost_usd desc;
```

The most expensive scene names the shot type that needs a better template.

---

## Guardrails, enforced

| Runbook rule | How it is enforced |
|---|---|
| Never batch rung-3 calls | `final` takes one `--scene-id`, no loop |
| Never let a model invent a face | `motion`/`final` require `--start-frame` |
| State cost before spending | `--i-approve-cost` must equal the computed cost |
| Log failures | every exception is written to `work/generations.jsonl` before exit |
| Stop at 80% of budget | `project_economics.budget_used_pct` checked before each rung 3 |
