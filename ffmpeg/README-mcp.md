# Use `livepeer/ffmpeg` from Claude (MCP)

`mcp_server.py` exposes the ffmpeg capability to Claude as **tools**, so a Claude
agent can transcode / clip / thumbnail a local file by calling the runner on your
orchestrator. **Offchain by default** (free, no wallet); set `LIVEPEER_SIGNER` for
the paid on-chain path. Batch ops on a persistent runner.

Tools: `ffmpeg_transcode(input_path, height)` · `ffmpeg_clip(input_path, start, end)`
· `ffmpeg_thumbnail(input_path, at)`. Each returns the output path.

## 1. Start the orchestrator + runner (offchain)

```sh
cd ffmpeg
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # expect livepeer/ffmpeg
```

## 2. Register the MCP server with Claude Code

Use the **absolute path** to this `ffmpeg/` dir. Config is passed as
**environment variables** — this is a local *stdio* server, so use `--env`, not
headers (headers are only for remote HTTP MCP servers):

| env | meaning |
| --- | --- |
| `LIVEPEER_DISCOVERY` | orchestrator discovery URL (default `https://localhost:8935/discovery`) |
| `LIVEPEER_SIGNER` | remote signer URL for the **paid** path; omit for offchain |

**a) CLI — offchain:**
```sh
claude mcp add livepeer-ffmpeg \
  --env LIVEPEER_DISCOVERY=https://localhost:8935/discovery \
  -- uv run --directory /ABS/PATH/TO/ffmpeg mcp_server.py
```

**On-chain (paid)** — add the signer (your running `signer` service on port 7936):
```sh
claude mcp add livepeer-ffmpeg \
  --env LIVEPEER_DISCOVERY=https://localhost:8935/discovery \
  --env LIVEPEER_SIGNER=http://localhost:7936 \
  -- uv run --directory /ABS/PATH/TO/ffmpeg mcp_server.py
```

> Changing env later? `add` won't overwrite an existing entry — remove first:
> `claude mcp remove livepeer-ffmpeg`, then re-add. Check with `claude mcp list`.

**b) `.mcp.json`** (project-scoped):
```json
{
  "mcpServers": {
    "livepeer-ffmpeg": {
      "command": "uv",
      "args": ["run", "--directory", "/ABS/PATH/TO/ffmpeg", "mcp_server.py"],
      "env": {
        "LIVEPEER_DISCOVERY": "https://localhost:8935/discovery",
        "LIVEPEER_SIGNER": "http://localhost:7936"
      }
    }
  }
}
```

`uv run --directory …` uses this folder's `pyproject.toml`, so it pulls `mcp` and
the `livepeer-gateway` SDK (from the `ja/live-runner` branch) automatically.

## 3. Use it in Claude

Restart Claude Code (or run `/mcp` to confirm `livepeer-ffmpeg` is connected and
its tools are listed). Then just ask:

```
Make a test clip, then transcode it to 480p with the livepeer ffmpeg tool.
```

Claude will call `ffmpeg_transcode` (etc.); the file rides through your
orchestrator to the `livepeer/ffmpeg` runner and the result is written next to
where you launched Claude. Paths are resolved relative to that working directory.

> Tip: make a test clip first —
> `ffmpeg -f lavfi -i testsrc=duration=3:size=640x480:rate=24 -pix_fmt yuv420p clip.mp4`

**Headless test** — each `claude -p` is a fresh session that loads the server. MCP
tools require permission, so pre-allow them in `-p` mode (no interactive prompt):

```sh
claude -p "Transcode clip.mp4 to 480p with the livepeer ffmpeg tool, output out480.mp4" \
  --allowedTools "mcp__livepeer-ffmpeg__ffmpeg_transcode"
ffprobe -v error -show_entries stream=width,height -of csv=p=0 out480.mp4   # expect ...,480
```

Interactively (plain `claude`), it just prompts you to approve the call.

## Ask Claude what it can do

Claude **already knows** the tools — the MCP server reports them automatically via
`tools/list` (you don't tell it, and `SKILL.md` isn't involved; that's for
file-reading agents). To discover or use them:

- **List them** — run `/mcp` in Claude Code, or just ask:
  > "What livepeer ffmpeg tools do you have, and what parameters do they take?"

  Claude answers from its live tool list — no extra wiring.
- **Use them by intent** — describe the task; you don't name the tool:

  | Ask Claude… | tool it calls |
  | --- | --- |
  | "transcode clip.mp4 to 480p" | `ffmpeg_transcode` |
  | "clip 1–3s of clip.mp4" | `ffmpeg_clip` |
  | "grab a thumbnail of clip.mp4 at 1.5s" | `ffmpeg_thumbnail` |
  | "extract the audio from clip.mp4" | `ffmpeg_extract_audio` |
  | "make a gif of clip.mp4" | `ffmpeg_gif` |
  | "crop clip.mp4 to 640×360" | `ffmpeg_crop` |
  | "convert clip.mp4 to mkv" | `ffmpeg_convert` |
  | "what codec / resolution / duration is clip.mp4?" | `ffmpeg_probe` |

In headless `-p` mode, pre-allow the tools you expect (comma-separated):
`--allowedTools "mcp__livepeer-ffmpeg__ffmpeg_transcode,mcp__livepeer-ffmpeg__ffmpeg_probe"`.

> **Adding ops?** Rebuild the runner (`docker compose up -d --build`) and start a
> **fresh** Claude session — that reloads the MCP server, and the new `@mcp.tool`
> shows up in `tools/list` automatically (no SKILL.md or `tools/list` edits needed).

## Notes

- **Offchain by default; paid with `LIVEPEER_SIGNER`.** Unset = no payment. The paid
  path needs a **funded** remote signer (deposit + reserve) and the runner must
  advertise a price (run it on-chain via `docker-compose.onchain.yml`). Verify the
  paid path works first with
  `uv run client.py --op transcode --height 480 --input clip.mp4 --output out.mp4 --signer http://localhost:7936`
  — if that succeeds, the MCP with `LIVEPEER_SIGNER` will too.
- The self-signed orchestrator cert on `https://localhost:8935` is handled by the
  SDK, same as `client.py`.
