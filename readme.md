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

```bash
uv run seed.py                     # 30 fabricated frames + JPEGs (~2h of work)
uv run extract.py                  # OCR + Gemini -> extractions, extracted=1
uv run extract.py --no-llm         #   ...or OCR only, zero Gemini calls
uv run sessionize.py --flush       # frames -> sessions / threads / entities
uv run embed.py                    # gemini-embedding-001 -> vectorstore
uv run memory.py recall "cors bug this morning"
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

## Files

| file | does |
| --- | --- |
| `seed.py` | fabricates frames + Pillow-drawn JPEGs so OCR returns real text |
| `extract.py` | OCR (ocrmac) → Gemini batch of 8 with `response_schema`; deletes sensitive frames |
| `sessionize.py` | contiguous frames → sessions, threads, entities; absorbs short blips as `interruptions` |
| `vectorstore.py` | `upsert` / `search`; all Actian code behind one TODO block, SQLite cosine default |
| `embed.py` | `gemini-embedding-001` @ 768d; vector failures log and continue |
| `memory.py` | `recall_context` / `what_was_i_doing` / `todays_receipt` |
| `stub_extract.py` | dev only: fills `extractions` with no API calls; delete when done |

Tables: `frames` (capture-owned), `extractions`, `sessions`, `threads`,
`entities`, `vectors`. Nothing here alters `frames` beyond its `extracted` flag.
