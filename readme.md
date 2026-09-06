# rooony — screen memory

Three parts, one SQLite file, one vector index.

```
screencapture/   JPEGs of your screen every N seconds        (teammate 1)
indexer.py       JPEGs  -> frames rows
extract.py       frames -> OCR + Gemini -> extractions
sessionize.py    extractions -> sessions / threads / entities
embed.py         sessions|frames -> BGE-small-en-v1.5 (local) -> vector index
memory.py        recall_context / what_was_i_doing / todays_receipt
src/             MCP server exposing all of the above           (teammate 3)
```

SQLite (`~/mem/mem.db`, WAL) is the system of record. The vector index —
Actian VectorAI or a built-in SQLite cosine scan — is disposable: if it dies,
everything still answers, just less well.

## Install

```bash
uv venv --python 3.11
uv pip install -r requirements.txt
```

`ocrmac` (Apple Vision) and `pyobjc` are macOS-only; the requirements file
marks them so, everything else runs anywhere. `sentence-transformers` pulls in
torch — the first `embed.py` run downloads bge-small-en-v1.5 once and caches it.

## Env

The default pipeline makes **no API calls**: ocrmac → BGE-small-en-v1.5 @384d
→ Actian. Gemini is opt-in (`extract.py --llm`, `sessionize.py --llm`) and only
writes summaries, slugs and narratives — it is never in the embedding or
retrieval path.

```bash
export GEMINI_API_KEY=...          # OPTIONAL, only for --llm
                                   # NOT used for embeddings - those are local
export VECTOR_BACKEND=sqlite       # sqlite (default) | actian
# optional
export MEM_DIR=~/mem               # MEM_DB and MEM_FRAMES derive from it
export MEM_FAKE_EMBED=1            # deterministic offline vectors, no API calls
# only when VECTOR_BACKEND=actian
export VECTORAI_HOST=localhost:6574
export VECTORAI_COLLECTION=screen_activity
```

## Run

```bash
uv run doctor.py                   # check every seam before anything else
uv run run_all.py                  # capture + indexer + extractor + roller
uv run memory.py recall "cors bug this morning"
```

One stage at a time:

```bash
uv run indexer.py                  # captured JPEGs -> frames rows (extracted=0)
uv run extract.py                  # OCR + Gemini -> extractions, extracted=1
uv run extract.py --no-llm         #   ...or OCR only, zero Gemini calls
uv run sessionize.py --flush       # frames -> sessions / threads / entities
uv run embed.py                    # bge-small-en-v1.5 (local) -> vectorstore
uv run memory.py doing 30
uv run memory.py receipt

uv run seed.py                     # no capture yet? 30 fabricated frames + JPEGs
```

Every stage is independently runnable and idempotent. Re-running one does
nothing to rows it already handled.

Offline demo path (no API key at all):

```bash
uv run seed.py && uv run stub_extract.py && uv run sessionize.py --no-llm --flush
MEM_FAKE_EMBED=1 uv run embed.py && MEM_FAKE_EMBED=1 uv run memory.py receipt
```

## MCP server

```bash
python -m src.mcp_server
```

`.mcp.json` registers it as `screen-memory`. Six tools:

| tool | level | what it answers |
| --- | --- | --- |
| `recall(query, limit)` | session | "what was I working on with CORS?" — ranked, expanded to the whole thread arc |
| `what_was_i_doing(minutes_ago)` | session | the session covering that moment, its interruptions, the next action |
| `todays_receipt()` | day | what got done today, framed as work accomplished |
| `search_by_text(query, …)` | frame | semantic search over individual captured frames |
| `search_by_time(start, end)` | frame | pure time-range lookup, no embeddings |
| `ask(question)` | frame | time expression + semantic search combined |

All six work on either vector backend.

Prefer the session-level tools for "what was I doing" questions: a session
carries a narrative and a resume hint, a single frame does not.

## Vector backends

`vectorstore.py` exposes exactly two calls — `upsert(session_id, vector,
metadata)` and `search(vector, k, time_from, time_to)` — and everything
downstream works identically on either backend.

- **sqlite** (default): brute-force cosine over float32 blobs in the same
  `mem.db`. At hackathon scale it answers in milliseconds.
- **actian**: `docker compose up -d`, then `VECTOR_BACKEND=actian`. The real
  SDK calls live in `src/vectorstore.py`; the root `ActianBackend` is only an
  adapter over them, so there is exactly one place that talks to Actian.

Records may be per-frame or per-session — several vectors can share a
`session_id`, and `search()` collapses them to each session's best score.
`points()` is the same query without that collapse, for frame-level answers.

What has been indexed is tracked in SQLite (`vector_log`), not by asking the
vector store — so re-running `embed.py` is a no-op on either backend, and a
vector store that is down or wiped never causes silent re-embedding of
everything. `embed.py --reset` clears it; on Actian that also drops and
recreates the collection, which is how you change embedding dimensions (an
existing collection's vector size cannot be altered in place).

## The record

```json
{
  "session_id": 25,
  "timestamp": "2026-09-06T12:15:00",
  "embedding": ["…384 floats…"],
  "application": "VS Code",
  "text": "roony group chat",
  "source": "screen_image"
}
```

`embed.py --level frame` (default) emits one per frame with the raw OCR text
and `source: "screen_image"` — no LLM in that path. `--level session` emits one
per session (title + narrative + entities) with `source: "session_summary"`.
`--out records.jsonl` dumps them without indexing. The Actian payload carries
the same fields plus `description` (alias of `text`), `activity`, `thread_slug`
and `timestamp_epoch`, which is what the frame-level MCP tools read.

## How `screencapture/` connects

The capture daemon writes **files only** — no database:

```
~/mem/frames/{unix_ts}_{App-Name}.jpg
```

`indexer.py` is the bridge. It scans that directory and inserts one `frames`
row per new JPEG at `extracted=0`, parsing the timestamp and app name out of
the filename, computing a dhash, and counting consecutive near-duplicates into
`dup_count`. It is idempotent (`image_path` is unique) and `--prune` drops rows
whose JPEG the daemon's retention has since deleted, keeping any already
extracted.

`window_title` is always NULL: capture detects the frontmost app but not the
window title. Summaries lean on the OCR text instead, which is why the OCR
check in `doctor.py` matters.

After indexing, this side only ever **reads** `frames` and flips `extracted` —
it never adds, renames or rewrites a column there, so nothing here can break
the capture writer. `extract.py` also resolves the capture table's columns at
runtime (`FRAME_COLUMNS`), tolerates `ts` as int/float/ms/ISO 8601, and
resolves absolute, relative or bare-filename image paths. If `frames` has no
`extracted` column at all, a frame counts as done once it has an `extractions`
row.

Both processes write to the same SQLite file, so keep WAL on (`doctor.py`
checks) and never hold a write transaction open across an API call.

## One model, one dimension

`embed.py` owns `EMBED_MODEL` and `EMBED_DIM` (`BAAI/bge-small-en-v1.5`, 384).
`src/config.py` imports them rather than declaring its own. If the query side
and the write side ever disagree on model or dims, cosine between a query and a
stored vector is meaningless and search silently returns nonsense — so there is
deliberately only one definition. We never embed images: screenshots become
text via ocrmac and the text is embedded.

The model runs **locally** via sentence-transformers — no API key, no network
after the first download (~130 MB, cached in `~/.cache/huggingface`). Vectors
are L2-normalised at encode time, so cosine is a plain dot product everywhere.

bge is trained asymmetrically: a **query** is prefixed with `Represent this
sentence for searching relevant passages: `, a stored **passage** is not.
`embed_query()` applies the prefix, `embed_texts(..., TASK_DOCUMENT)` does not.
Prefixing both sides (or neither) is what makes every cosine collapse to a
constant, which is the failure mode that looks like "search returns nonsense".
Note bge's absolute cosine floor is high — ~0.4 between unrelated English
sentences — so judge hits by the *gap*, not the raw number.

## Files

| file | does |
| --- | --- |
| `screencapture/` | the capture daemon (JPEGs only, no DB) |
| `indexer.py` | captured JPEGs → `frames` rows; dhash, dup_count, `--prune` |
| `seed.py` | fabricates frames + Pillow-drawn JPEGs so OCR returns real text |
| `extract.py` | OCR (ocrmac) → Gemini batch of 8 with `response_schema`; deletes sensitive frames |
| `sessionize.py` | contiguous frames → sessions, threads, entities; absorbs short blips as `interruptions` |
| `vectorstore.py` | `upsert` / `search`; SQLite cosine default, Actian adapter |
| `embed.py` | `BAAI/bge-small-en-v1.5` @ 384d, local; vector failures log and continue |
| `memory.py` | `recall_context` / `what_was_i_doing` / `todays_receipt` |
| `src/mcp_server.py` | the MCP server; 6 tools |
| `src/vectorstore.py` | the real Actian SDK calls |
| `src/time_parse.py` | pulls "at 16:00" / "around 4pm yesterday" out of a question |
| `run_all.py` | capture + indexer + extractor + periodic sessionize/embed, one ctrl-c |
| `doctor.py` | verifies every seam: deps, keys, capture dir, schema, dims, state |
| `stub_extract.py` | dev only: fills `extractions` with no API calls |

Tables: `frames` (capture-owned), `extractions`, `sessions`, `threads`,
`entities`, `vectors`.
