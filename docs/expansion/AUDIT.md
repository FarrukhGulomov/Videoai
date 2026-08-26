# VideoAI Expansion — Phase 0 Audit

Written against the "Master Prompt: Building a Platform on Par With
Higgsfield & Montix" brief. Read-only phase — no existing file touched, no
feature code written. This is the report the brief asks for before any
further work starts.

---

## 1. What's actually in the repository today

This is **not** a greenfield project and **not** the stack the brief
assumes. It's a working, already-deployed product with real production
history (a 25-minute film project and a "robot fight" project are referenced
in the schema and docs as prior real runs). Stack:

| Layer | Brief assumes | Repo actually has |
|---|---|---|
| Frontend | Next.js + TS + Tailwind + shadcn | Hand-written HTML/CSS/vanilla JS (`webapp/static/`) served by a stdlib Python `http.server` |
| Backend | Node API routes / NestJS + worker | `webapp/server.py` — one file, `ThreadingHTTPServer`, no framework |
| DB | PostgreSQL (Supabase/Neon) | **Already Supabase Postgres** — `01-schema.sql` + `02-multiuser-schema.sql`, RLS-scoped, credits + ledger tables already exist |
| Auth | Email/password + Google + Telegram OAuth | Supabase auth already wired (email/password + Google OAuth working); Telegram explicitly deferred pending a real bot token (see `webapp/README.md`) |
| Storage | Cloudflare R2 / S3 | Local `work/` dir + fal.ai's own CDN (`v3.fal.media`) for generated assets; no object storage layer of its own |
| Queue | Redis + BullMQ, WebSocket/SSE progress | None — generation calls block synchronously (fal's own queue API is polled in-process, `factory.py`'s `fal_run()`) |
| AI integration | fal.ai/Replicate/Kie.ai aggregator | **Already fal.ai**, already multi-model (see §2) |
| Payments | Stripe + Payme/Click/Uzcard/Humo | **None implemented yet** — `docs/startup-strategy.md` already researched and recommended Payme first, Click second, explicitly rejected Stripe-first for this market |
| i18n | UZ/RU/EN | **Already done** — `webapp/static/i18n.js`, all three languages, browser auto-detect + persisted choice |
| MCP (AI-agent access) | not mentioned in brief | **Already built** — `mcp/server.py`, stdio + Streamable HTTP, deployed on Railway (this session's prior work) |

The core architectural decision already made and enforced throughout the
codebase — visible in comments in `webapp/server.py`, `mcp/server.py`,
`Dockerfile` — is **stdlib-only Python, no pip install step, no
dependencies**. This is deliberate and repeated, not an oversight: "Stdlib
only, matching the rest of this project — no `mcp` pip package" (mcp/README),
"this project is stdlib-only Python, so there's no `pip install` step"
(DEPLOY.md). Every prior increment of this project — including the MCP
server built earlier in this session — was built to preserve that
property.

This means **most of Section 3 of the brief (the suggested Next.js/Redis/
BullMQ/R2 architecture) does not match how this project has been built so
far, on purpose.** See §6 for the decision this raises.

---

## 2. Feature-by-feature gap against the brief

### 4.1 Generation studio
- ✅ Image-to-video, multi-model (Kling 3.0, Veo 3.1, Seedance 2.0/2.5, FLUX 3, LTX-2.3, Hailuo-02, PixVerse v6 — 8 video models total, tiered budget/standard/premium)
- ✅ First/last-frame locking (`factory.py`'s end-frame handling, model-gated allow-list)
- ✅ Post-production: upscale, subtitles, lipsync, background removal
- ✅ Talking-avatar generation (OmniHuman)
- ❌ Quick mode vs Pro mode split — today there's one flow with sensible defaults, not a deliberate two-tier UI
- ❌ Virtual camera control (lens/focal length/stackable pan-zoom-dolly-orbit) — not exposed as a UI concept; whatever a chosen model supports natively is all that's reachable
- ❌ Character consistency across shots via reference images — `templates.json`'s `{{CHARACTER_LOCK}}` slot is a prompt-engineering convention operators fill in by hand, not an automated reference-image pipeline
- ❌ Motion transfer from a reference video
- ❌ Side-by-side multi-model comparison UI (the CLI/webapp run one model per generation call today)
- ❌ Batch generation (2-4 variants) — `create_images` does support a count param for stills; video does not

### 4.2 Template library
- ✅ Template system exists (`templates.json`, `factory.py template harvest/use`) — but it's an **operator tool** (CLI-driven, slot-fill by hand), not a customer-facing "pick a template, one click" UI
- ❌ No template picker in the webapp UI at all currently
- ❌ No user-submitted / community templates

### 4.3 Credits and pricing
- ✅ Credit balance + ledger schema exists and is live (`02-multiuser-schema.sql`, `supabase_client.py`)
- ✅ Retail markup layer exists (`_retail_rate` in `webapp/server.py`, 1.12x over wholesale fal cost)
- ❌ No self-serve top-up flow — crediting an account today is a manual SQL insert or a call to `record_spend` (`webapp/README.md` says this explicitly: "there is no self-serve payment flow yet")
- ❌ No free starter credits / daily bonus mechanic
- ❌ No subscription tiers
- ❌ No referral program

### 4.4 Community and growth
- ❌ No public gallery/explore feed
- ❌ No social export/share integrations (TikTok/Instagram/YouTube)
- ❌ No Telegram bot (explicitly deferred — needs a real bot token first)

### 4.5 Admin panel
- ❌ No admin UI. `/api/health` plus direct Supabase SQL access are the only current visibility into usage/cost/margin

### 5. Safety, trust, compliance
- ❌ No consent flow for real-person likeness
- ❌ No C2PA/watermark provenance on outputs
- ❌ No automated moderation queue
- ❌ No DMCA/takedown flow
- 🟡 Data protection: Supabase RLS scopes every row to its owner, which is the right primitive, but no written policy doc exists

### 6. Design direction
- 🟡 UI is functional, mobile-responsive per `webapp/README.md`, but not the "dark-first, gradient, video-autoplay-gallery" aesthetic the brief describes — it's a plain utility UI today, not a marketing-grade product surface
- ❓ Lighthouse/perf numbers unmeasured (nothing to compare against yet)

### 7. Payments (brief §3/§4.3)
- ✅ **Already researched, not built.** `docs/startup-strategy.md` (Uzbek) already concluded: Payme first (lowest friction, ~1-1.5% fee), Click second (~1-2% fee, 3-5 day approval), Stripe deferred to international phase later. This matches the brief's own local-rails-first instinct almost exactly — it just hasn't been implemented.

---

## 3. Technical debt / risk notes (not asked for, but relevant to planning)

- **Synchronous generation blocking**: `factory.py`'s `fal_run()` polls in-process and the webapp's create endpoints block on it (the MCP server's docstring calls this out too — "polls internally for up to 5 minutes"). At the concurrency the brief's roadmap implies (many simultaneous users), this needs a real queue before it breaks, not after.
- **No atomic balance deduction under concurrent requests** — documented as a known limitation in `webapp/README.md` already, low severity today, becomes real at scale.
- **No per-IP/per-account rate limiting.**
- **`ThreadingHTTPServer` has no TLS of its own** — fine behind Railway's own domain termination (current deploy), would need a real proxy/LB if this ever moves off a single PaaS "Generate Domain" box.

---

## 4. Where the brief's roadmap (§7) already half-exists

| Brief phase | Status here |
|---|---|
| Phase 1 — MVP hardening | **Mostly done**: auth ✅, multi-model video ✅ (not just one), credit system ✅ (schema + ledger, not self-serve top-up), generation history ✅, dashboard 🟡 (functional, not a "dashboard" in the brief's visual sense) |
| Phase 2 — Multi-model studio | **Partially done**: 8 video models + comparison is possible model-by-model but no side-by-side UI; no template library UI; 2 templates recorded so far (`templates.json`), not 10-15 |
| Phase 3 — Pro controls | **Not started** as a UI concept (camera control, character consistency, batch) — some of the underlying capability may exist in specific fal models but isn't surfaced |
| Phase 4 — Monetization/localization | i18n ✅ done; payments 🔴 not started (but researched) |
| Phase 5 — Community/growth | 🔴 not started |
| Phase 6 — Polish/launch | 🔴 not started; MCP integration (not in the brief) already adds an agent-facing growth surface |

---

## 5. Constraint compliance check for this audit itself

Per the brief's Section 1: no existing file was modified to produce this
report. This file and its directory (`docs/expansion/`) are new. Work is on
a new branch, `feature/videoai-expansion`, branched from the current tip of
`claude/fal-ochildi-tekshir-y5y5na` (the most recent working state, including
the just-deployed MCP Railway endpoint) rather than from the repo's nominal
default branch, since that is where the actual current state of the code
lives. Say if a different base branch is wanted instead.

---

## 6. The decision this audit surfaces — needs your call before Phase 1 starts

The brief's Section 3 architecture (Next.js/TS/Tailwind, Node/NestJS,
Redis+BullMQ, S3/R2, PostHog) is explicitly framed as "guidance, not a
mandate — adapt to whatever stack the existing repository already uses...
match the existing choices" when the repo's own choices differ. They do
differ, substantially, and on purpose — the stdlib-only constraint is
asserted repeatedly across this codebase's own comments as a deliberate
design decision, not a starting point waiting to be professionalized.

Two honest paths forward, not a false choice — both are legitimate:

**A. Extend in place.** Keep the stdlib-Python monolith, existing Supabase
schema, existing i18n/webapp. Add features additively within that
architecture: a real queue can be a simple polling worker table in Postgres
instead of Redis/BullMQ; a template picker can be new static JS + new API
routes in the existing `webapp/server.py` pattern; Payme/Click integration
is a new module calling their REST APIs the same stdlib-only way fal.ai is
called today. Fastest path to feature parity, zero new infra to operate,
consistent with everything built so far — but a genuinely more limited
frontend (no component framework, no design system) and a from-scratch
queue if concurrency ever becomes a real problem.

**B. Build the brief's stack in parallel.** A real Next.js frontend and
Node backend, wired to the *same* Supabase project and (optionally) the
same fal.ai integration logic ported over, living in new
top-level directories (e.g. `web-next/`) per the brief's additive-only
rule. Gets the component framework, design system, and infra the brief
describes, at the cost of running and maintaining two stacks (or a real
future migration/cutover project) and a much larger build.

This is a product-direction and resourcing decision, not something to infer
from the code. Recommendation below is a default, not an override of
whatever you actually want.

---

*No code was written in this phase. Next: pick a direction in §6, then this
plan turns into a normal phased build against Section 7 of the brief,
adapted to the chosen path.*
