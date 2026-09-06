# rooony

MCP/RAG server that answers questions like "what was I working on in Gmail?"
or "what was I doing at 16:00?" by querying a local **Actian VectorAI DB**
collection of screen-activity embeddings.

This repo is the **read/query side only**. A separate pipeline (owned by
teammates) captures screenshots, generates embeddings, and writes them into
the same collection — see "Assumptions to revisit" below for the interface
contract between the two.

## Stack

- **Actian VectorAI DB** — local Docker instance, Python SDK (pip package
  `actian-vectorai-client`, imported as `actian_vectorai`)
- **Python** MCP server (`mcp` SDK v2, `MCPServer`)
- **CLIP** (`sentence-transformers`, model `clip-ViT-B-32`, 512-dim) to embed
  both query text and screenshot images into the same vector space —
  placeholder until teammates confirm their real model (see below)

## Setup

1. Start the local vector DB:

   ```bash
   docker compose up -d
   ```

   If this fails with something like
   `open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`,
   the Docker Desktop **app** isn't running (having the `docker` CLI on PATH
   isn't enough) — launch Docker Desktop from the Start menu, wait for it to
   report "Engine running" in its tray icon, then retry.

2. Install Python deps:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and adjust if needed.

4. Seed some data so there's something to query before the real capture
   pipeline exists — either fully synthetic:

   ```bash
   python -m src.seed_sample_data
   ```

   or your 5 most recent real screenshots (from
   `C:\Users\khyaa_0k6t362\OneDrive\Pictures\Screenshots`), embedded for real
   with CLIP, with hand-written descriptions standing in for the real
   OCR/captioning pipeline:

   ```bash
   python -m src.seed_real_screenshots --recreate
   ```

   (`--recreate` drops and recreates the collection at the current
   `EMBED_DIM` — needed the first time you run this, or any time you change
   `EMBED_MODEL`/`EMBED_DIM`, since an existing collection's vector size
   can't change in place. Omit it on later runs against the same model.)

   `search_by_text`/`ask` should now return genuinely relevant matches — e.g.
   asking about "python code" or "debugging" should rank the two Claude Code
   screenshots above the two Elastic/Kibana ones.

5. Run the MCP server:

   ```bash
   python -m src.mcp_server
   ```

   Point your MCP client (Claude Desktop, Claude Code, etc.) at this command
   to register the `search_by_time`, `search_by_text`, and `ask` tools.

## Assumptions to revisit once the real schema/pipeline lands

- **Record shape** (`src/vectorstore.py` docstring): `timestamp` (ISO 8601),
  `embedding`, `activity`, `application`, `description`, `source`. Update
  `upsert_activity`/`_from_point` when the teammates' schema is finalized —
  this is the only file that should need to change.
- **Embedding model**: assumed `clip-ViT-B-32` (512-dim), chosen because it
  can embed both text queries and screenshot images into one comparable
  space. The query embedder here **must match** whatever model the
  capture/embedding pipeline uses on the write side, or cosine similarity is
  meaningless. Update `EMBED_MODEL`/`EMBED_DIM` in `.env`, then re-seed with
  `--recreate`.
- **`application`/`activity`/`source` fields**: likely need to be derived
  from the screenshot itself (OCR/classification) by the capture pipeline,
  not guessed here.
- **Distance metric**: using native cosine distance via the VectorAI SDK
  (`Distance.Cosine`). Confirmed to exist in the SDK; not yet load-tested at
  real corpus size.
- **Corpus size / ANN vs brute-force**: unconfirmed. VectorAI DB supports
  HNSW indexing, which should be fine unless volumes are unusually large.
- **Time parsing** (`src/time_parse.py`): a minimal regex-based extractor for
  natural-language time mentions ("at 16:00", "around 4pm yesterday"). Good
  enough to start; replace if question phrasing turns out more varied.
