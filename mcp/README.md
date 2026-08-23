# Video Factory — MCP server

Lets Claude (Desktop, Code, or any other MCP client) or MCP-capable GPT
tooling generate and enhance videos through this project by calling tools
directly, instead of you driving the CLI or the browser UI yourself.

Stdlib only, matching the rest of this project — no `mcp` pip package. The
JSON-RPC 2.0 / stdio wire protocol (the same one the official SDKs use) is
implemented directly against the protocol's schema, verified from
[modelcontextprotocol/modelcontextprotocol](https://github.com/modelcontextprotocol/modelcontextprotocol)'s
source (modelcontextprotocol.io itself is blocked by this project's sandbox
network policy, so the schema was fetched from the spec's own repo rather
than assumed).

## How it fits together

```
Claude / GPT client  <-- stdio JSON-RPC -->  mcp/server.py  <-- HTTP -->  webapp/server.py  <-- HTTP -->  fal.ai
```

`mcp/server.py` does not talk to fal.ai directly and has no cost logic of
its own — it's a thin client to the already-running webapp's REST API.
Every rate, default, and the entire quote-then-approve safety gate live in
exactly one place (`webapp/server.py`, which itself reuses
`scripts/factory.py`), the same way the webapp already reuses the CLI's
code instead of reimplementing it.

**The webapp must be running first:**

```bash
export FAL_KEY=...
python3 webapp/server.py      # http://127.0.0.1:8000, leave this running
```

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

### Claude Code

```bash
claude mcp add video-factory python3 /absolute/path/to/Videoai/mcp/server.py \
  --env VIDEO_FACTORY_URL=http://127.0.0.1:8000
```

or add the same block as above to a project's `.mcp.json`.

### GPT / other MCP clients

Any client that speaks MCP over stdio works the same way — point it at
`python3 /absolute/path/to/Videoai/mcp/server.py` with the same
`VIDEO_FACTORY_URL` environment variable. As of this writing MCP support
varies across OpenAI's own tooling; check your specific client's docs for
how it wants a local stdio server configured — the server itself doesn't
care which client is driving it, the protocol is the same either way.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `VIDEO_FACTORY_URL` | `http://127.0.0.1:8000` | Where the webapp is running. Point this at a remote URL if the webapp is hosted elsewhere. |
| `VIDEO_FACTORY_SESSION` | unset | Only needed if the webapp is in multi-user mode (see webapp/README.md). Copy the `vf_session` cookie value after signing in through the browser once. This is a stopgap, not a real API-key mode — see "Known limits" below. |

## The tools

Nine tools, prefixed `video_factory_` for discoverability:

| Tool | Costs money? | What it does |
|---|---|---|
| `video_factory_get_info` | No | Presets, current pricing, valid durations. Call this first. |
| `video_factory_quote_images` | No | Prices generating starting-image variants. |
| `video_factory_create_images` | **Yes** | Generates them. Requires `approved_cost` from the quote. |
| `video_factory_quote_video` | No | Prices animating a chosen image. |
| `video_factory_create_video` | **Yes** | Generates the video. Requires `approved_cost` from the quote. |
| `video_factory_quote_enhancement` | No | Prices upscale / bgremove / subtitles / lipsync on an existing video. |
| `video_factory_enhance_video` | **Yes** | Runs it. Requires `approved_cost` from the quote. |
| `video_factory_check_job` | No | Polls a job that's still running. |
| `video_factory_list_my_videos` | No | Recent history. |

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
