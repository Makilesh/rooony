# Deploying rooony

Everything runs on one Mac. Nothing is uploaded: screenshots, OCR, embeddings
and the database all stay on the machine. The only time screen content leaves
is when an MCP client calls a tool and the returned text goes to that model
provider as part of your conversation.

Read `readme.md` for what the parts do. This file is how to get it running.

---

## 1. Requirements

| | |
| --- | --- |
| macOS | required — `ocrmac` is Apple Vision, `pyobjc` is the capture side |
| Python | 3.11 (`uv venv --python 3.11`) |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Disk | ~530 KB per frame. At the default 10 s interval that is **~190 MB/hour**, ~1.5 GB per working day. See §8. |
| Docker | only if you want the Actian backend. Not needed otherwise. |
| Gemini API key | optional. The default pipeline is fully local. See §6. |

First install downloads ~2 GB of torch plus the 130 MB BGE model, once.

---

## 2. Install

```bash
git clone https://github.com/Makilesh/rooony.git
cd rooony

uv venv --python 3.11
uv pip install -r requirements.txt        # torch + BGE, a few minutes, once
```

> If `.venv` already exists, `uv venv` stops and asks
> `A virtual environment already exists at .venv. Do you want to replace it? [y/n]`.
> Answer **n** or skip the line — answering `y` deletes the venv and forces the
> whole torch download again.

The capture daemon has its own venv (it needs `pyobjc`, nothing else):

```bash
cd screencapture
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cd ..
```

Configure:

```bash
cp .env.example .env                      # edit if you want Gemini or Actian
cp screencapture/config.example.json screencapture/config.json
```

`screencapture/config.json`:

```json
{
  "interval_seconds": 10,
  "output_dir": "~/mem/frames",
  "capture_app_name": true,
  "max_files": 5000,
  "max_age_days": 7
}
```

`interval_seconds` must be **10–300**; anything lower is rejected at startup.
`max_files` and `max_age_days` are `null` in the example, which means **no
retention and unbounded disk growth** — set them. See §8.

---

## 3. Screen Recording permission

macOS grants this **per binary**, not per script. The binary that must be
approved is whichever Python actually runs the daemon:

- launching by hand from a terminal → approve **Terminal** (or iTerm)
- launching via launchd → approve `screencapture/venv/bin/python3`

Grant it in **System Settings → Privacy & Security → Screen & System Audio
Recording**, then check:

```bash
uv run screencapture/src/smoke_test.py
```

It writes `/tmp/_smoke.png`. The size heuristic is not sufficient — **open the
file and confirm you can see your actual windows.** A screenshot showing only
wallpaper and the menu bar means permission is missing for that binary.

```bash
open /tmp/_smoke.png
```

---

## 4. Verify before running

```bash
export VECTOR_BACKEND=sqlite
uv run doctor.py
```

It must end with `all checks passed`.

If capture has already run without an indexer you will see
`FAIL N captured images are not in 'frames'`. That is expected and not a
problem — clear the backlog and re-check:

```bash
uv run indexer.py
uv run doctor.py
```

`doctor.py --live` additionally loads the embedding model and, if
`GEMINI_API_KEY` is set, spends one API call.

---

## 5. Run

### Option A — foreground, one command (development, demos)

```bash
uv run run_all.py
```

Starts the capture daemon, indexer, extractor and a periodic
sessionize+embed roller. Ctrl-C stops all of them.

**This is fully local by default — no Gemini calls.** Pass `--llm` to opt in.

```bash
uv run run_all.py --llm            # needs GEMINI_API_KEY
uv run run_all.py --no-capture     # capture already running under launchd
uv run run_all.py --roll 60        # sessionize+embed every 60s
```

Do not run this while the launchd agent is also loaded — you get two capture
daemons writing to the same directory. Stop one first (§5B).

### Option B — always-on under launchd (daily use)

```bash
./screencapture/install.sh
```

Installs `~/Library/LaunchAgents/com.thryambak.screenshotdaemon.plist` and
loads it. This runs **capture only** — it writes JPEGs and no database rows.
You still need the rest of the pipeline:

```bash
uv run run_all.py --no-capture
```

Managing the agent:

```bash
launchctl list | grep screenshotdaemon                  # running?
launchctl bootout   gui/$(id -u)/com.thryambak.screenshotdaemon    # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.thryambak.screenshotdaemon.plist   # start
./screencapture/uninstall.sh                            # remove
```

Logs: `screencapture/logs/launchd-stdout.log`, `launchd-stderr.log`.

### Ask it things

```bash
uv run memory.py recall "what was that cors bug"
uv run memory.py doing 15
uv run memory.py receipt
```

These are safe to run in a second terminal while the pipeline runs.

---

## 6. Gemini (optional)

The **embedding path never uses Gemini** — OCR text is embedded locally with
`BAAI/bge-small-en-v1.5` at 384 dims. `GEMINI_API_KEY` is only used by
`extract.py` (per-frame semantic labels), `sessionize.py` (session narratives)
and `memory.py`'s query time-window parser. Without it everything still runs;
you get app-derived thread slugs and deterministic narratives instead.

```bash
export GEMINI_API_KEY=...
uv run run_all.py --llm
```

**Free-tier keys are capped at 20 `gemini-2.5-flash` requests per day**, which
does not cover even one 30-frame backlog (4 batches of 8, plus one call per
closed session). On exhaustion you get:

```
429 RESOURCE_EXHAUSTED ... quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier
```

That is a *daily* quota, so retrying cannot clear it — the code treats it as
terminal rather than burning backoff. Use a billed key, or stay on the default
local path.

---

## 7. Connect an MCP client

The server is **stdio** — a local process on pipes.

| client | works | why |
| --- | --- | --- |
| Claude Code | yes | `.mcp.json` in the repo; start a session from this directory |
| Claude Desktop | yes | needs a config entry, below |
| claude.ai in a browser | **no** | a website cannot spawn a local process; browser Claude only supports remote HTTPS connectors |
| ChatGPT | **no** | same reason, and it has no local-stdio mechanism at all |

Exposing this over HTTP to reach a browser client would put your screen history
on a public URL. Don't.

**Claude Desktop** — create
`~/Library/Application Support/Claude/claude_desktop_config.json` with absolute
paths, then restart the app:

```json
{
  "mcpServers": {
    "screen-memory": {
      "command": "/ABSOLUTE/PATH/TO/rooony/.venv/bin/python3",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/ABSOLUTE/PATH/TO/rooony",
      "env": {
        "VECTOR_BACKEND": "sqlite",
        "MEM_DIR": "/Users/YOU/mem"
      }
    }
  }
}
```

The interpreter **must** be the venv's. Bare `python3` resolves to the system
Python, which has neither `mcp` nor `sentence_transformers`, and the server
dies with `ModuleNotFoundError` before the client sees it.

Six tools are exposed: `recall`, `what_was_i_doing`, `todays_receipt` (session
level) and `search_by_text`, `search_by_time`, `ask` (frame level). The first
call loads the embedding model and takes ~13 s; after that they answer in
well under a second.

Test the server by hand:

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
 | VECTOR_BACKEND=sqlite .venv/bin/python3 -m src.mcp_server
```

---

## 8. Disk, retention, backup

At a 10 s interval expect **~190 MB/hour**. Set retention in
`screencapture/config.json` (`max_files`, `max_age_days`) — both default to
`null`, meaning nothing is ever deleted.

The daemon deletes files; it does not touch the database. After it prunes,
drop the orphaned rows:

```bash
uv run indexer.py --prune        # drops rows whose JPEG is gone, keeps extractions
```

**`~/mem/mem.db` is the system of record. The vector index is disposable** — if
it is lost or corrupted, rebuild it:

```bash
uv run embed.py --reset
uv run embed.py
```

Back up `~/mem/mem.db` (plus `-wal`/`-shm`) and, if you want the images,
`~/mem/frames`. Stop the pipeline first so the WAL is quiet.

---

## 9. Actian VectorAI (optional)

SQLite brute-force cosine is the default and is fast enough at this scale.
Actian is only a swap of the index; SQLite stays the system of record.

```bash
docker compose up -d
export VECTOR_BACKEND=actian
uv run embed.py --reset          # drop + recreate the collection at 384 dims
uv run embed.py
uv run memory.py recall "cors preflight"
```

A collection's vector size **cannot be changed in place**. If you switch
embedding models, `--reset` is mandatory; without it every upsert fails or
every cosine comes back 0.0. The code now refuses a dimension mismatch with an
explicit error rather than returning silent zeros.

If Actian is down, `VECTOR_BACKEND=sqlite` answers identically. That is the
demo fallback — keep it in your back pocket.

---

## 10. Troubleshooting

| symptom | cause | fix |
| --- | --- | --- |
| smoke test shows only wallpaper | Screen Recording not granted to *that* binary | §3 — approve the exact interpreter, not "the script" |
| `doctor.py`: `N captured images are not in 'frames'` | capture ran with no indexer | `uv run indexer.py` |
| `sqlite3.OperationalError: database is locked` | a writer held the lock across slow work | fixed in `b5f6183`; if it recurs, check nothing holds a transaction across OCR or an API call |
| `[run_all] indexer exited with 1` then everything stops | run_all tears down on any child failure | read the traceback above it — usually the lock error |
| `ModuleNotFoundError: No module named 'mcp'` | client launched system `python3` | point the config at `.venv/bin/python3` |
| `429 ... GenerateRequestsPerDayPerProjectPerModel-FreeTier` | free-tier daily Gemini cap | §6 — billed key, or drop `--llm` |
| `ConnectionError: Server at 'localhost:6574' is not reachable` | Actian not up | `docker compose up -d`, or `VECTOR_BACKEND=sqlite` |
| every cosine is 0.0 on Actian | collection built at another model's dimensions | `uv run embed.py --reset` |
| `uv venv` hangs on a prompt | `.venv` already exists | answer `n`, or use `--clear` if you really mean it |
| two sets of screenshots per tick | launchd agent *and* `run_all.py` both capturing | stop one — §5B |

---

## 11. Known limitations

Be aware of these before relying on it:

- **Lock-screen frames are captured and stored.** In one 382-frame sample, 137
  (36%) were the black lock screen, carrying no information. Nothing skips them
  yet.
- **26% of real frames truncate** at `MAX_OCR_CHARS = 2500` (worst case lost
  1232 characters). Summary quality rests entirely on OCR, so dense screens
  lose their bottom.
- **`window_title` is always NULL** — capture detects the frontmost app but not
  the window title.
- **The app label can be misleading.** It records the frontmost *app*, which is
  not necessarily what fills the screen: a frame labelled `WhatsApp` can be
  entirely VS Code.
- **Sensitive deletion is irreversible** — the row *and* the JPEG. The
  `--no-llm` path uses a keyword/secret-shape heuristic, not judgement. On the
  382-frame sample it deletes 5 frames, all of which genuinely showed a live
  API key, but it is a heuristic and it deletes.
- **Secrets on screen get captured.** Frames sat in `~/mem/frames` containing a
  live API key in plain view. Treat `~/mem` as sensitive: it is unencrypted at
  rest beyond FileVault.
