# rooony — screen memory: extraction + embedding layer

SQLite (`~/mem/mem.db`, WAL) is the system of record. The vector index is
disposable — everything downstream works whether it is Actian or the built-in
SQLite cosine scan.

## Install

```bash
uv venv --python 3.11
uv pip install google-genai ocrmac pillow numpy pydantic
```

`ocrmac` pulls pyobjc/Apple Vision and is macOS-only. Everything else runs
anywhere.

## Env

```bash
export GEMINI_API_KEY=...          # required for extract / sessionize / recall
export VECTOR_BACKEND=sqlite       # sqlite (default) | actian
# optional
export MEM_DIR=~/mem               # MEM_DB and MEM_FRAMES derive from it
export MEM_FAKE_EMBED=1            # deterministic offline vectors, no API calls
# only when VECTOR_BACKEND=actian
export ACTIAN_VECTOR_URL=... ACTIAN_VECTOR_API_KEY=... ACTIAN_VECTOR_DB=...
```

## Pipeline

```
screencapture/  --JPEGs-->  indexer.py  --rows-->  extract.py  --OCR+Gemini-->
extractions  -->  sessionize.py  -->  sessions/threads/entities  -->
embed.py  -->  vectors  -->  memory.py   (the API the MCP side imports)
```

Everything at once:

```bash
uv run run_all.py                  # capture + indexer + extractor + roller
uv run run_all.py --no-capture     # the daemon is already running under launchd
uv run run_all.py --no-llm         # OCR only, zero Gemini calls
```

Or one stage at a time:

```bash
uv run indexer.py                  # captured JPEGs -> frames rows (extracted=0)
uv run extract.py                  # OCR + Gemini -> extractions, extracted=1
uv run extract.py --no-llm         #   ...or OCR only, zero Gemini calls
uv run sessionize.py --flush       # frames -> sessions / threads / entities
uv run embed.py                    # gemini-embedding-001 -> vectorstore
uv run memory.py recall "cors bug this morning"

uv run seed.py                     # no capture yet? 30 fabricated frames + JPEGs
```

Every stage is independently runnable and idempotent. Re-running a stage does
nothing to rows it already handled.

Offline demo path (no API key at all):

```bash
uv run seed.py && uv run stub_extract.py && uv run sessionize.py --no-llm --flush
MEM_FAKE_EMBED=1 uv run embed.py && MEM_FAKE_EMBED=1 uv run memory.py receipt
```

## Record emitted to the vector layer / endpoint

```json
{
  "session_id": 25,
  "timestamp": "2026-09-06T12:15:00",
  "embedding": [ "…768 floats…" ],
  "application": "VS Code",
  "text": "roony group chat",
  "source": "screen_image"
}
```

`embed.py --level frame` (default) emits one per frame with the raw OCR text
and `source: "screen_image"` — no LLM in that path. `--level session` emits one
per session (title + narrative + entities) with `source: "session_summary"`.
Several frame records may share a `session_id`; `vectorstore.search()` collapses
them to the best hit per session, so `memory.py` is identical either way.
`--out records.jsonl` dumps them without indexing.

## How `screencapture/` connects

The capture daemon writes **files only** — no database:

```
~/mem/frames/{unix_ts}_{App-Name}.jpg
```

`indexer.py` is the bridge. It scans that directory and inserts one `frames`
row per new JPEG at `extracted=0`, parsing the timestamp and app name out of the
filename, computing a dhash, and counting consecutive near-duplicates into
`dup_count`. It is idempotent (`image_path` is unique) and `--prune` drops rows
whose JPEG the daemon's retention has since deleted, keeping any that were
already extracted.

`window_title` is always NULL: the capture side detects the frontmost app but
not the window title. Summaries lean on the OCR text instead, which is why the
OCR check in `doctor.py` matters.

After indexing, this side only ever **reads** `frames` and flips `extracted` —
it never adds, renames or rewrites a column there, so nothing here can break the
capture writer.

Point both halves at the same database (`MEM_DB`, default `~/mem/mem.db`), then:

```bash
uv run doctor.py            # checks the seam end to end; exit 1 if anything is broken
uv run doctor.py --live     # also spends ~2 API calls to prove the keys work
```

`extract.py` resolves the capture table's columns at runtime, so a rename on
their side does not break the merge. It already accepts:

| we need | names accepted |
| --- | --- |
| id | `id`, `frame_id`, `rowid` |
| ts | `ts`, `timestamp`, `captured_at`, `capture_time`, `time`, `epoch` |
| app | `app`, `app_name`, `application`, `process` |
| window_title | `window_title`, `title`, `window`, `window_name` |
| image_path | `image_path`, `path`, `img_path`, `file_path`, `image`, `screenshot` |
| extracted | `extracted`, `processed`, `is_extracted`, `done` |

Anything else: add it to `FRAME_COLUMNS` in `extract.py`. Also tolerated —
`ts` as int, float, digit-string, milliseconds or ISO 8601 (`to_epoch`), and
`image_path` absolute, relative to `MEM_DIR`, or a bare filename
(`resolve_image`). If `frames` has **no** `extracted` column at all, a frame
counts as done once it has a row in `extractions`, so the pass stays idempotent
either way.

Both processes write to the same SQLite file, so keep WAL on (`doctor.py`
checks) and never hold a write transaction open across an API call.

## Files

| file | does |
| --- | --- |
| `seed.py` | fabricates frames + Pillow-drawn JPEGs so OCR returns real text |
| `extract.py` | OCR (ocrmac) → Gemini batch of 8 with `response_schema`; deletes sensitive frames |
| `sessionize.py` | contiguous frames → sessions, threads, entities; absorbs short blips as `interruptions` |
| `vectorstore.py` | `upsert` / `search`; all Actian code behind one TODO block, SQLite cosine default |
| `embed.py` | `gemini-embedding-001` @ 768d; vector failures log and continue |
| `memory.py` | `recall_context` / `what_was_i_doing` / `todays_receipt` |
| `screencapture/` | teammate 1: the capture daemon (JPEGs only, no DB) |
| `indexer.py` | captured JPEGs → `frames` rows; dhash, dup_count, `--prune` |
| `run_all.py` | capture + indexer + extractor + periodic sessionize/embed, one ctrl-c |
| `doctor.py` | verifies the capture seam, deps, keys, dims, pipeline state |
| `stub_extract.py` | dev only: fills `extractions` with no API calls; delete when done |

Tables: `frames` (capture-owned), `extractions`, `sessions`, `threads`,
`entities`, `vectors`. Nothing here alters `frames` beyond its `extracted` flag.
