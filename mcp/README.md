# Video Factory — MCP server

Lets Claude (Desktop, Code, or any other MCP client) or ChatGPT generate
and enhance videos through this project by calling tools directly, instead
of you driving the CLI or the browser UI yourself.

Stdlib only, matching the rest of this project — no `mcp` pip package. Both
wire protocols below (JSON-RPC 2.0 the same either way) are implemented
directly against the protocol's own schema and transport spec, verified
from [modelcontextprotocol/modelcontextprotocol](https://github.com/modelcontextprotocol/modelcontextprotocol)'s
source (modelcontextprotocol.io itself is blocked by this project's sandbox
network policy, so both were fetched from the spec's own repo rather than
assumed).

## How it fits together

```
Claude Desktop/Code  <-- stdio JSON-RPC -------->  mcp/server.py  <-- HTTP -->  webapp/server.py  <-- HTTP -->  fal.ai
ChatGPT / remote client  <-- Streamable HTTP -->        ^
```

`mcp/server.py` speaks two transports (chosen at startup, same tool logic
either way — see "Two ways to run this" below) but never talks to fal.ai
directly and has no cost or pricing logic of its own: it's a thin client to
the already-running webapp's REST API. Every rate, every default, the
customer-facing markup (see webapp/README.md), and the entire
quote-then-approve safety gate live in exactly one place (`webapp/server.py`,
which itself reuses `scripts/factory.py`) — the same way the webapp already
reuses the CLI's code instead of reimplementing it. One concrete
consequence: every price these tools quote is already the marked-up,
customer-facing price — this file never reads `config.json` and never sees
the wholesale fal.ai cost.

**The webapp must be running first:**

```bash
export FAL_KEY=...
python3 webapp/server.py      # http://127.0.0.1:8000, leave this running
```

## Two ways to run this

### stdio — for Claude Desktop and Claude Code

The client launches `mcp/server.py` itself as a local subprocess and talks
to it over stdin/stdout. Nothing to deploy.

### Streamable HTTP — for claude.ai (browser), Claude Desktop's Connectors, ChatGPT, and any other remote MCP client

**ChatGPT's custom connectors, and claude.ai running in a browser tab,
only speak to a remote HTTPS endpoint** — neither can launch a local
subprocess the way stdio mode does, so this mode is required for both
no matter how they're configured. Claude Desktop can reach this project
either way: stdio (above) if you're comfortable editing
`claude_desktop_config.json` and have Python locally, **or** the same
remote endpoint described here, added the same way claude.ai/ChatGPT
are — Settings → Connectors → Add custom connector, pointed at the
deployed URL with the `Authorization: Bearer <token>` header. All three
are just different callers of the one endpoint below. Run:

```bash
export VIDEO_FACTORY_URL=http://127.0.0.1:8000   # or wherever the webapp lives
export MCP_HTTP_TOKEN='a long random secret'      # required -- see below
python3 mcp/server.py --http 8300
```

This is a minimal, spec-compliant implementation: a single endpoint that
accepts a POST'd JSON-RPC request and returns a JSON-RPC response —
no SSE stream, since none of these tools need the server to push messages
on its own. To make it reachable from ChatGPT, put a public HTTPS URL in
front of it, exactly like the webapp:

- **Easiest: deploy it as a second Railway service** alongside the webapp
  — see the repo root's `DEPLOY.md` §7 for the exact step-by-step
  (same `Dockerfile` build, just override the start command to
  `python3 mcp/server.py --http`; Railway injects `PORT`, which this
  reads the same way the webapp does). That section also covers setting
  `MCP_PUBLIC_URL` on the *webapp* service afterward, which is what
  makes the in-app "MCP" tab display this URL to users instead of
  "not deployed yet."
- Or run it behind any reverse proxy that terminates TLS (Caddy/nginx —
  see `DEPLOY.md` §5), pointed at `127.0.0.1:8300`.

**`MCP_HTTP_TOKEN` is not optional once this is reachable beyond your own
machine.** Every request must send it as `Authorization: Bearer <token>`,
or the server returns 401. Unlike the webapp's `WEBAPP_BASIC_AUTH_*` (which
gates a human clicking through a browser), this endpoint has no human in
the loop at all — anyone who can reach it can spend real money through the
webapp it's pointed at, immediately, with no confirmation step of any kind.
The server prints a loud warning at startup if this is unset. Register the
resulting URL + token in claude.ai's, Claude Desktop's, or ChatGPT's
connector settings (all currently: Settings → Connectors → Add custom
connector — check Anthropic's/OpenAI's current docs for the exact steps
and where the auth header is entered, since that UI moves on all sides).

## Setup

### Claude Desktop

Add to your `claude_desktop_config.json`
(`~/Library/Application Support/Claude/claude_desktop_config.json` on
macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "video-factory": {
      "command": "python3",
      "args": ["/absolute/path/to/Videoai/mcp/server.py"],
      "env": {
        "VIDEO_FACTORY_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

Restart Claude Desktop. The tools appear with the `video_factory_` prefix.

This is the *local* (stdio) route — it needs Python and this file on the
same machine as Claude Desktop, and it does NOT work for claude.ai
running in a browser tab (a web page can't launch a local subprocess).
Claude Desktop can also add this project as a *remote* connector
instead, exactly like claude.ai in a browser or ChatGPT does (no local
Python needed) — see "ChatGPT (and claude.ai / Claude Desktop
Connectors)" below; the same URL/token works for all of them, since
they're just different callers of the same Streamable HTTP endpoint.

### Claude Code

```bash
claude mcp add video-factory python3 /absolute/path/to/Videoai/mcp/server.py \
  --env VIDEO_FACTORY_URL=http://127.0.0.1:8000
```

or add the same block as above to a project's `.mcp.json`. Claude Code can
also add a *remote* server (the Streamable HTTP mode above) the same way
any HTTP MCP server is added — point it at the deployed URL with the
`Authorization: Bearer <MCP_HTTP_TOKEN>` header.

### ChatGPT (and claude.ai / Claude Desktop Connectors)

See "Streamable HTTP" above — deploy it first, then add the resulting
HTTPS URL as a custom connector, with the `Authorization: Bearer <token>`
header where the UI asks for one. This works the same way whether the
custom connector is being added in ChatGPT's settings, on claude.ai in
a browser, or in Claude Desktop's Settings → Connectors — all three
point at the identical endpoint. claude.ai (browser) in particular has
no other option here: unlike Claude Desktop it can't fall back to the
stdio route above, since a browser tab can't launch a local subprocess.

### Other MCP clients

Any client that speaks MCP over stdio works the same way as Claude
Desktop/Code; any that speaks Streamable HTTP works the same way as
ChatGPT. The server itself doesn't care which client is driving it — the
protocol is the same either way, only the transport differs.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `VIDEO_FACTORY_URL` | `http://127.0.0.1:8000` | Where the webapp is running. Point this at a remote URL if the webapp is hosted elsewhere. |
| `VIDEO_FACTORY_SESSION` | unset | **Required for any paid tool to work.** The webapp's `_require_funded_user()` gate now rejects every generation request with no verified session, unconditionally — there is no single-tenant free-generation mode any more (see webapp/README.md's "Access control"), and this MCP server is just another HTTP caller of that same API. Copy the `vf_session` cookie value after signing in through the browser once. This is a stopgap, not a real API-key mode — see "Known limits" below. |
| `VIDEO_FACTORY_BASIC_AUTH` | unset | Only needed if the webapp has `WEBAPP_BASIC_AUTH_USER`/`PASS` set (see webapp/README.md's "Access control"). Format `user:pass` — sent as an HTTP Basic Authorization header on every call to the webapp. |
| `MCP_HTTP_TOKEN` | unset | **HTTP mode only.** The shared secret every caller must send as `Authorization: Bearer <token>`. No default on purpose — see the warning above. |
| `MCP_HTTP_PORT` | `8300` | HTTP mode only. Alternative to `python3 mcp/server.py --http <port>` — setting this env var alone also switches to HTTP mode, which is what a PaaS "start command" with no arguments needs. Falls back to the platform's own `PORT` env var (Railway, etc.) if set, then 8300. |

## The tools

Eleven tools, prefixed `video_factory_` for discoverability:

| Tool | Costs money? | What it does |
|---|---|---|
| `video_factory_get_info` | No | Presets, every available model (with its tier and price), valid durations. Call this first. |
| `video_factory_quote_images` | No | Prices generating starting-image variants. |
| `video_factory_create_images` | **Yes** | Generates them. Requires `approved_cost` from the quote. |
| `video_factory_quote_video` | No | Prices animating a chosen image with a chosen model (`model` is optional -- omit for the default, cheapest full-quality option; pass the exact id from `get_info` to reach a pricier "top quality" model). |
| `video_factory_create_video` | **Yes** | Generates the video. Requires `approved_cost` from the quote. |
| `video_factory_quote_enhancement` | No | Prices upscale / bgremove / subtitles / lipsync on an existing video. |
| `video_factory_enhance_video` | **Yes** | Runs it. Requires `approved_cost` from the quote. |
| `video_factory_quote_avatar` | No | Prices turning a photo + a voice-track URL into a new talking-head video (OmniHuman). |
| `video_factory_create_talking_avatar` | **Yes** | Generates it. Requires `approved_cost` from the quote. |
| `video_factory_check_job` | No | Polls a job that's still running. |
| `video_factory_list_my_videos` | No | Recent history. |

### Picking a model

`video_factory_get_info` lists every video model with a `tier`: `budget`
(LTX-2.3, cheapest), `standard` (Kling 3.0 -- the default if `model` is
omitted -- and Veo 3.1), and `premium` (Seedance 2.0, FLUX 3, Seedance
2.5). The premium tier is a genuine, often 2-4x price jump over the
default, not a marketing label -- only reach for it when a user has said
quality matters more than cost. Seedance 2.5 additionally only supports
16:9 shots (the webapp/CLI both refuse to run it otherwise, since its
price is a 16:9-only approximation of fal's real per-request cost).

### The money gate, one more time

Every tool whose name starts with `create_` or `enhance_` is paid and
**requires an `approved_cost` argument that must exactly match a quote
obtained moments earlier** from the matching `quote_*` tool — the webapp
API refuses a mismatch server-side (same `--i-approve-cost` discipline the
CLI has always had, same gate the browser UI's confirmation dialog uses).
This isn't a suggestion to the calling model — it's enforced by
`webapp/server.py` regardless of what the MCP layer does, so even a client
that ignores the tool descriptions can't spend past a quote it never
showed the user.

The server's `initialize` response also states this as a standing
instruction, since an LLM calling tools autonomously has no human clicking
a confirm button in front of it the way the browser UI does — the burden
of actually pausing for a human's go-ahead sits with whatever's driving
this MCP server, not with the server itself.

`video_factory_create_images` is the one exception worth naming: the
webapp's browser UI doesn't require a confirmation click for images at all
(they're cheap enough that a modal was a net usability loss — see the
webapp redesign notes), so `approved_cost` is optional at the HTTP API
level for that one endpoint. The MCP tool schema marks it **required**
anyway, because an autonomous caller should still confirm real spend even
when a human clicking a button wouldn't have needed to.

## Known limits

- **Multi-user auth is a stopgap.** `VIDEO_FACTORY_SESSION` needs a cookie
  value copied by hand from a browser session, and Supabase session
  cookies expire (typically an hour). A real API-key mode for
  programmatic/MCP access — a long-lived token scoped to one account,
  checked independently of the browser cookie flow — is the natural next
  step if MCP access becomes a primary usage path rather than an add-on.
- **Blocking tool calls.** `create_video`/`enhance_video` poll internally
  for up to 5 minutes (matching `factory.py`'s own `fal_run()` blocking
  behavior) rather than returning immediately with a job id. If a
  generation runs long, the tool returns a "still running" message with
  the job id to check later via `video_factory_check_job` instead of
  hanging the calling client indefinitely.
- **No resources or prompts**, only tools — this server's whole job is
  running paid generations safely, which the tools interface covers
  completely; resources/prompts weren't needed for that and add protocol
  surface with nothing pulling it in the other direction.
- **HTTP mode's auth is a shared bearer token, not OAuth.** The MCP spec's
  2025-06-18 authorization flow (and the newer draft's Client ID Metadata
  Documents) describes a full OAuth dance for remote servers; this
  implementation deliberately skips it in favor of one shared secret
  (`MCP_HTTP_TOKEN`) checked on every request, which every current MCP
  client that supports custom headers can already use. Fine for "this
  person/team has one token"; a real per-user OAuth flow is the natural
  next step if this needs to support many independent end users each
  authenticating as themselves, the same way the webapp's Supabase mode
  does for the browser.
- **HTTP mode has no SSE / server-initiated messages**, only the
  request-in-response-out half of Streamable HTTP — GET returns 405.
  Nothing here needs the server to push a message on its own initiative,
  so this isn't a gap in practice, but a client that specifically probes
  for the SSE stream before deciding a server is "real" would see the 405.
