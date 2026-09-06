#!/usr/bin/env python3
"""
extract.py -- OCR + Gemini structured extraction over pending frames.

    frames WHERE extracted=0  ->  ocrmac (Apple Vision, on-device)
      OCR text >= 40 chars  -> text-only prompt
      OCR text  < 40 chars  -> ship the JPEG to gemini-2.5-flash vision
    batched 8 frames per call, native response_schema for guaranteed JSON
    -> extractions table, frames.extracted = 1
    -> sensitive rows are DELETED (row + jpeg), never stored

Idempotent: only touches extracted=0, and re-running replaces an extraction
row rather than duplicating it. Nothing is written unless the whole batch
came back.

    uv run extract.py                # one pass over the backlog
    uv run extract.py --no-llm       # OCR only, no Gemini calls at all
    uv run extract.py --watch 5      # poll forever
    uv run extract.py --dry-run      # OCR + prompt only, no API call
    uv run extract.py --reset        # mark everything unextracted again
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel

# --- config ---------------------------------------------------------------
MODEL = "gemini-2.5-flash"
BATCH_SIZE = 8
OCR_MIN_CHARS = 40          # below this we stop trusting OCR and use vision
MAX_OCR_CHARS = 2500        # per frame, keeps a batch prompt sane
KNOWN_SLUGS = 10            # how many recent task_thread slugs we show Gemini
MAX_RETRIES = 6

MEM_DIR = Path(os.environ.get("MEM_DIR", Path.home() / "mem"))
DB_PATH = Path(os.environ.get("MEM_DB", MEM_DIR / "mem.db"))

ACTIVITY = Literal["coding", "reading", "comms", "search", "media", "admin", "idle"]


class FrameExtraction(BaseModel):
    frame_index: int
    summary: str
    task_thread: str
    entities: List[str]
    resume_hint: str
    activity_type: ACTIVITY
    sensitive: bool


SYSTEM_INSTRUCTION = """\
You label screen recordings of one developer's workday. You are given a batch of
frames: app name, window title, capture time, and the on-screen text (OCR) or the
screenshot itself. Return one object per frame, in the same order, with the
frame_index you were given.

Rules:
- summary: ONE past-tense sentence naming the specific thing on screen. Say
  "Traced the CORS preflight 401 to require_auth in auth.py" not "Worked on code".
  Never say "the user"; the subject is implied.
- task_thread: a stable kebab-case slug for the ongoing piece of work, e.g.
  "cors-preflight-fix", "dashboard-timeline-ui". THIS IS THE MOST IMPORTANT
  FIELD. You are shown the recently used slugs. If a frame belongs to work you
  have already seen, you MUST reuse that exact slug, character for character.
  Invent a new slug only when the work is genuinely unrelated to every known
  slug - a different bug, a different project, a different topic. A one-off
  detour (a Slack ping, a reddit tab) inside ongoing work still gets its own
  honest slug; the sessionizer absorbs short blips, so do not force them into
  the coding slug. Slug drift shatters sessions: when in doubt, REUSE.
- entities: concrete strings visible on screen - filenames, function names,
  error messages, URLs, ticket ids, package names. Copy them verbatim. Empty
  list if there are none. No generic words.
- resume_hint: the literal next action someone would take to pick this back up,
  with file and line when visible: "Open dashboard.tsx:59 and convert f.ts to
  local time". Not a summary, an instruction.
- activity_type: one of coding, reading, comms, search, media, admin, idle.
- sensitive: true ONLY for banking or financial account screens, passwords,
  credentials, private/DM conversations, health or legal personal records.
  Public team chat about work is NOT sensitive. Sensitive frames are deleted,
  so do not flag ordinary work.
"""


# --- the capture seam -----------------------------------------------------
# `frames` is written by the capture side (screencapture/). We only ever read it
# and flip its extracted flag. Column names are resolved at runtime so a rename
# on their side doesn't break the merge.
FRAME_COLUMNS = {
    "id":           ["id", "frame_id", "rowid"],
    "ts":           ["ts", "timestamp", "captured_at", "capture_time", "time", "epoch"],
    "app":          ["app", "app_name", "application", "process"],
    "window_title": ["window_title", "title", "window", "window_name"],
    "image_path":   ["image_path", "path", "img_path", "file_path", "image", "screenshot"],
    "extracted":    ["extracted", "processed", "is_extracted", "done"],
}
_schema = None


def frames_schema(con):
    """Map our names onto the capture table's actual columns. Cached."""
    global _schema
    if _schema is not None:
        return _schema
    cols = [r[1] for r in con.execute("PRAGMA table_info(frames)")]
    if not cols:
        sys.exit(f"no `frames` table in {DB_PATH} - is the capture side pointed here?")
    lower = {c.lower(): c for c in cols}
    out = {}
    for want, candidates in FRAME_COLUMNS.items():
        out[want] = next((lower[c] for c in candidates if c in lower), None)
    missing = [k for k in ("ts", "image_path") if not out[k]]
    if missing:
        sys.exit(f"`frames` has no column for {missing}; saw {cols}. "
                 "Add it to FRAME_COLUMNS in extract.py.")
    out["id"] = out["id"] or "rowid"
    renamed = {k: v for k, v in out.items() if v and v != k}
    if renamed:
        print(f"[schema] frames columns mapped: {renamed}", file=sys.stderr)
    if not out["extracted"]:
        print("[schema] frames has no `extracted` column - using the extractions "
              "table itself as the processed marker", file=sys.stderr)
    _schema = out
    return out


def to_epoch(v):
    """Capture may store ts as int, float, digit-string or ISO 8601."""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.replace(".", "", 1).isdigit():
        n = float(s)
        return int(n / 1000) if n > 1e11 else int(n)   # tolerate milliseconds
    from datetime import datetime
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def resolve_image(path):
    """Absolute, relative-to-MEM_DIR, or bare filename in the frames dir."""
    if not path:
        return ""
    p = Path(path).expanduser()
    if p.exists():
        return str(p)
    for base in (MEM_DIR, MEM_DIR / "frames", DB_PATH.parent, DB_PATH.parent / "frames"):
        q = base / p.name
        if q.exists():
            return str(q)
    return str(p)


# --- db -------------------------------------------------------------------
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def ensure_schema(con):
    """Additive only. `frames` belongs to the capture side; we never alter it."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS extractions (
            frame_id      INTEGER PRIMARY KEY,
            ts            INTEGER NOT NULL,
            app           TEXT,
            window_title  TEXT,
            image_path    TEXT,
            summary       TEXT,
            task_thread   TEXT,
            entities      TEXT,          -- json list
            resume_hint   TEXT,
            activity_type TEXT,
            sensitive     INTEGER DEFAULT 0,
            ocr_text      TEXT,          -- raw Apple Vision text, kept for embedding
            ocr_chars     INTEGER,
            used_vision   INTEGER DEFAULT 0,
            extracted_at  INTEGER
        )
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(extractions)")}
    if "ocr_text" not in cols:            # migrate DBs made before ocr_text existed
        con.execute("ALTER TABLE extractions ADD COLUMN ocr_text TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ext_ts ON extractions(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ext_thread ON extractions(task_thread)")
    con.commit()


def pending_frames(con, limit):
    s = frames_schema(con)
    cols = (f"f.{s['id']} AS id, f.{s['ts']} AS ts, "
            f"{('f.' + s['app']) if s['app'] else 'NULL'} AS app, "
            f"{('f.' + s['window_title']) if s['window_title'] else 'NULL'} AS window_title, "
            f"f.{s['image_path']} AS image_path")
    if s["extracted"]:
        sql = (f"SELECT {cols} FROM frames f WHERE f.{s['extracted']} = 0 "
               f"ORDER BY f.{s['ts']} LIMIT ?")
    else:  # no flag on their table: a frame is done once it has an extraction
        sql = (f"SELECT {cols} FROM frames f WHERE NOT EXISTS "
               f"(SELECT 1 FROM extractions e WHERE e.frame_id = f.{s['id']}) "
               f"ORDER BY f.{s['ts']} LIMIT ?")
    rows = []
    for r in con.execute(sql, (limit,)).fetchall():
        d = dict(r)
        d["ts"] = to_epoch(d["ts"])
        d["image_path"] = resolve_image(d["image_path"])
        rows.append(d)
    return rows


def mark_extracted(con, frame_id):
    s = frames_schema(con)
    if s["extracted"]:
        con.execute(f"UPDATE frames SET {s['extracted']} = 1 WHERE {s['id']} = ?",
                    (frame_id,))


def delete_frame(con, frame_id):
    s = frames_schema(con)
    con.execute(f"DELETE FROM frames WHERE {s['id']} = ?", (frame_id,))


def recent_slugs(con, n=KNOWN_SLUGS):
    rows = con.execute(
        "SELECT task_thread FROM extractions WHERE task_thread IS NOT NULL "
        "ORDER BY ts DESC LIMIT 300"
    ).fetchall()
    seen, out = set(), []
    for r in rows:
        s = r["task_thread"]
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= n:
            break
    return out


# --- ocr ------------------------------------------------------------------
_OCR_WARNED = False


def ocr_image(path: str) -> str:
    """Apple Vision via ocrmac. Returns '' on any failure -> caller uses vision."""
    global _OCR_WARNED
    if not path or not Path(path).exists():
        return ""
    try:
        from ocrmac import ocrmac
    except Exception:
        if not _OCR_WARNED:
            print("[ocr] ocrmac unavailable (not macOS?) - every frame goes to vision",
                  file=sys.stderr)
            _OCR_WARNED = True
        return ""
    try:
        results = ocrmac.OCR(path, language_preference=["en-US"]).recognize()
        return "\n".join(r[0] for r in results if r and r[0]).strip()
    except Exception as e:
        print(f"[ocr] failed on {path}: {e}", file=sys.stderr)
        return ""


# --- gemini ---------------------------------------------------------------
_client = None


def client():
    global _client
    if _client is None:
        from google import genai
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            sys.exit("GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=key)
    return _client


# A per-DAY quota 429 never clears inside a backoff window - retrying it just
# burns ~90s and still fails. Per-minute rate limits do clear, so those we retry.
_DAILY_QUOTA = ("PERDAY", "REQUESTSPERDAY", "GENERATEREQUESTSPERDAYPERPROJECTPERMODEL",
                "FREE_TIER_REQUESTS", "FREE TIER")


def _retryable(err: Exception) -> bool:
    m = f"{type(err).__name__} {err}".upper()
    if "429" in m and any(t in m for t in _DAILY_QUOTA):
        print("[gemini] daily quota exhausted - not retrying (it resets at "
              "midnight Pacific; use a billed key, or `uv run stub_extract.py` "
              "for the offline path)", file=sys.stderr)
        return False
    return any(t in m for t in (
        "429", "RESOURCE_EXHAUSTED", "RATE", "QUOTA",
        "500", "503", "INTERNAL", "UNAVAILABLE", "DEADLINE", "TIMEOUT",
    ))


def with_backoff(fn):
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except Exception as e:
            if attempt == MAX_RETRIES - 1 or not _retryable(e):
                raise
            wait = min(2 ** (attempt + 1), 60) + random.uniform(0, 1.5)
            print(f"[gemini] {type(e).__name__}: retrying in {wait:.1f}s "
                  f"({attempt + 1}/{MAX_RETRIES - 1})", file=sys.stderr)
            time.sleep(wait)


def build_batch(rows, known):
    """-> (parts, meta) where meta[i] = dict(frame_id, ocr_chars, used_vision)."""
    from google.genai import types

    header = [
        "KNOWN task_thread SLUGS (most recent first). Reuse these exactly when "
        "the work matches; only invent a slug for genuinely new work:",
        ("  " + ", ".join(known)) if known else "  (none yet - this is the first batch)",
        "",
        f"{len(rows)} FRAMES FOLLOW. Return exactly {len(rows)} objects, "
        "one per frame, using the frame_index shown.",
        "",
    ]
    parts, meta = [], []
    image_parts = []

    for i, r in enumerate(rows):
        text = ocr_image(r["image_path"])
        used_vision = len(text) < OCR_MIN_CHARS
        meta.append({"frame_id": r["id"], "ocr_chars": len(text),
                     "ocr_text": text[:MAX_OCR_CHARS], "used_vision": 0})

        when = time.strftime("%a %H:%M", time.localtime(r["ts"]))
        block = [
            f"--- frame_index: {i}",
            f"time: {when}",
            f"app: {r['app']}",
            f"window_title: {r['window_title']}",
        ]
        if used_vision and r["image_path"] and Path(r["image_path"]).exists():
            meta[i]["used_vision"] = 1
            block.append("screen_text: (OCR came back empty - read the attached "
                         f"screenshot labelled FRAME {i})")
            image_parts.append(types.Part.from_text(text=f"SCREENSHOT FOR FRAME {i}:"))
            image_parts.append(types.Part.from_bytes(
                data=Path(r["image_path"]).read_bytes(), mime_type="image/jpeg"))
        else:
            block.append("screen_text:")
            block.append(text[:MAX_OCR_CHARS] if text else "(no text on screen)")
        block.append("")
        header.append("\n".join(block))

    parts.append(types.Part.from_text(text="\n".join(header)))
    parts.extend(image_parts)
    return parts, meta


USAGE = {"calls": 0, "prompt": 0, "output": 0, "thoughts": 0, "total": 0}


def note_usage(resp):
    """Accumulate real token counts so a run can be costed honestly."""
    u = getattr(resp, "usage_metadata", None)
    if not u:
        return
    USAGE["calls"] += 1
    USAGE["prompt"] += getattr(u, "prompt_token_count", 0) or 0
    USAGE["output"] += getattr(u, "candidates_token_count", 0) or 0
    USAGE["thoughts"] += getattr(u, "thoughts_token_count", 0) or 0
    USAGE["total"] += getattr(u, "total_token_count", 0) or 0


def extract_batch(rows, known):
    from google.genai import types

    parts, meta = build_batch(rows, known)
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.1,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        response_mime_type="application/json",
        response_schema=list[FrameExtraction],
    )
    resp = with_backoff(lambda: client().models.generate_content(
        model=MODEL, contents=parts, config=cfg))
    note_usage(resp)

    items = resp.parsed
    if not items:  # schema mode should always populate .parsed; belt and braces
        items = [FrameExtraction(**d) for d in json.loads(resp.text)]
    return items, meta


# --- write-back -----------------------------------------------------------
def slugify(s: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in (s or ""))
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:64] or "misc"


def fallback(row, index) -> FrameExtraction:
    """Model skipped a frame. Store something honest and move on - never loop."""
    return FrameExtraction(
        frame_index=index,
        summary=f"Had {row['app']} open on '{row['window_title']}'.",
        task_thread=slugify(row["app"]),
        entities=[],
        resume_hint=f"Reopen {row['app']}.",
        activity_type="idle",
        sensitive=False,
    )


def write_batch(con, rows, items, meta):
    by_index = {}
    for it in items:
        if 0 <= it.frame_index < len(rows):
            by_index[it.frame_index] = it

    now = int(time.time())
    kept = deleted = filled = 0

    for i, row in enumerate(rows):
        it = by_index.get(i)
        if it is None:
            it = fallback(row, i)
            filled += 1

        if it.sensitive:
            con.execute("DELETE FROM extractions WHERE frame_id = ?", (row["id"],))
            delete_frame(con, row["id"])
            p = Path(row["image_path"] or "")
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
            deleted += 1
            print(f"  [sensitive] dropped frame {row['id']} ({row['app']})")
            continue

        con.execute("""
            INSERT OR REPLACE INTO extractions
              (frame_id, ts, app, window_title, image_path, summary, task_thread,
               entities, resume_hint, activity_type, sensitive, ocr_text,
               ocr_chars, used_vision, extracted_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)
        """, (row["id"], row["ts"], row["app"], row["window_title"], row["image_path"],
              it.summary.strip(), slugify(it.task_thread),
              json.dumps([e.strip() for e in it.entities if e and e.strip()]),
              it.resume_hint.strip(), it.activity_type, meta[i].get("ocr_text", ""),
              meta[i]["ocr_chars"], meta[i]["used_vision"], now))
        mark_extracted(con, row["id"])
        kept += 1

    con.commit()
    return kept, deleted, filled


# --- ocr-only pass (no Gemini at all) -------------------------------------
# Deleting is irreversible and the --no-llm path has no judgement, only keywords,
# so the bar for deleting is higher here than in the Gemini path.
#
# Measured on 382 real captures: matching any of these words on its own deleted
# 107 frames (28%), including 23/23 VS Code frames and 71/131 Terminal frames.
# Only 19 held anything resembling a credential; the rest merely *mentioned* the
# words - a file tree containing `api.env`, a README, a terminal showing this
# project's own brief. So the words are split in two:
#
#   STRONG  phrases that essentially only occur on a genuinely sensitive screen
#   WEAK    words that occur constantly in ordinary dev work, and therefore only
#           count when an actual secret-shaped value sits next to them
SENSITIVE_STRONG = (
    "account balance", "available balance", "routing number",
    "chase online", "bank of america", "wells fargo",
    "1password", "keychain access", "last pass", "lastpass",
    "verification code", "one-time passcode", "social security number",
)

SENSITIVE_WEAK = (
    "password", "passphrase", "api key", "secret key", "private key",
    "access token", "credit card", "account number", "two-factor", "ssn",
    "paypal", "keychain",
)

# a credential-shaped value: `key = <16+ non-space chars>`, a long opaque token,
# a PEM block, or a card/SSN number
# Unambiguous on their own - these shapes do not occur in prose.
SECRET_SELF_EVIDENT = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b(?:sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9]{16,}\b",
    r"\bAIza[A-Za-z0-9_\-]{30,}\b",
    r"\b\d{3}-\d{2}-\d{4}\b",                                   # SSN
    r"\b(?:\d[ -]?){15,16}\b",                                   # card number
)

# A generic `name = value` assignment. Ambiguous alone (a docs page shows these
# too), so it only counts alongside one of the WEAK words.
SECRET_ASSIGNMENT = (
    r"(?:password|passwd|pwd|secret|token|api[_\- ]?key|apikey|access[_\- ]?key)"
    r"\s*[:=]\s*[\"\']?[^\s\"\']{12,}",
)

SECRET_SHAPES = SECRET_SELF_EVIDENT + SECRET_ASSIGNMENT

# Kept as the union so doctor.py and anything else importing it still works.
SENSITIVE_HINTS = SENSITIVE_STRONG + SENSITIVE_WEAK

# "api key" must also match "API_KEY" and "api-key": separators vary.
_word = lambda hs: re.compile(
    r"\b(?:" + "|".join(re.escape(h).replace(r"\ ", r"[\s_-]+") for h in hs) + r")\b",
    re.IGNORECASE)

# Matched on WORD BOUNDARIES, not as bare substrings. A plain `in` test made
# "ssn" match "className", so every React/JSX screen was flagged sensitive and
# --no-llm deleted the row *and unlinked the JPEG* - silent, unrecoverable data
# loss on ordinary frontend work.
_STRONG_RE = _word(SENSITIVE_STRONG)
_WEAK_RE = _word(SENSITIVE_WEAK)
_SENSITIVE_RE = _word(SENSITIVE_HINTS)          # any mention; used by doctor/tests
_SECRET_SELF_RE = re.compile("|".join(SECRET_SELF_EVIDENT), re.IGNORECASE)
_SECRET_RE = re.compile("|".join(SECRET_SHAPES), re.IGNORECASE)


def looks_sensitive(text, title):
    """True only when the screen is worth DELETING over.

    A strong phrase is enough on its own. A weak word needs a secret-shaped
    value on the same screen - otherwise every README that says "API key" and
    every sidebar listing `api.env` would be destroyed.
    """
    hay = f"{text}\n{title}"
    if _STRONG_RE.search(hay) or _SECRET_SELF_RE.search(hay):
        return True
    return bool(_WEAK_RE.search(hay) and _SECRET_RE.search(hay))


APP_ACTIVITY = {
    "code": "coding", "visual studio code": "coding", "xcode": "coding",
    "terminal": "coding", "iterm": "coding", "iterm2": "coding",
    "slack": "comms", "discord": "comms", "messages": "comms", "mail": "comms",
    "zoom": "comms", "notion": "admin", "linear": "admin", "figma": "admin",
    "preview": "reading", "books": "reading", "spotify": "media",
}


def no_llm_pass(con, limit):
    """OCR only: store the screen text, coarse app-derived thread, no Gemini.

    Enough for the embedding layer (text + application + timestamp); the
    Gemini fields stay empty until extract.py runs without --no-llm.

    Commits per frame, deliberately. The first INSERT opens a write
    transaction, and a single commit at the end would hold it across every
    remaining ocr_image() call - ~600ms each on real screenshots. Clearing a
    93-frame backlog held the write lock for ~56s, past indexer.py's 30s
    timeout, so the indexer died with "database is locked" and run_all.py tore
    the whole pipeline down with it. Same rule as the Gemini path: never hold a
    write transaction open across slow work.
    """
    rows = pending_frames(con, limit)
    if not rows:
        return 0
    now, kept, dropped = int(time.time()), 0, 0
    for r in rows:
        text = ocr_image(r["image_path"])
        if looks_sensitive(text, r["window_title"] or ""):
            con.execute("DELETE FROM extractions WHERE frame_id = ?", (r["id"],))
            delete_frame(con, r["id"])
            p = Path(r["image_path"] or "")
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
            dropped += 1
            con.commit()
            print(f"  [sensitive] dropped frame {r['id']} ({r['app']})")
            continue
        app = (r["app"] or "unknown").strip()
        con.execute("""
            INSERT OR REPLACE INTO extractions
              (frame_id, ts, app, window_title, image_path, summary, task_thread,
               entities, resume_hint, activity_type, sensitive, ocr_text,
               ocr_chars, used_vision, extracted_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,0,?)
        """, (r["id"], r["ts"], app, r["window_title"], r["image_path"],
              (r["window_title"] or app), slugify(app), "[]", "",
              APP_ACTIVITY.get(app.lower(), "admin"), text[:MAX_OCR_CHARS],
              len(text), now))
        mark_extracted(con, r["id"])
        kept += 1
        con.commit()
    print(f"[ocr-only] {kept} frames stored, {dropped} sensitive dropped, no API calls")
    return kept + dropped


# --- passes ---------------------------------------------------------------
def one_pass(con, limit, dry_run=False):
    rows = pending_frames(con, limit)
    if not rows:
        return 0
    total_kept = total_del = 0
    t0 = time.time()

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        known = recent_slugs(con)
        span = f"{batch[0]['id']}..{batch[-1]['id']}"
        print(f"[batch] {len(batch)} frames ({span}) | known slugs: "
              f"{', '.join(known) if known else 'none'}")

        if dry_run:
            parts, meta = build_batch(batch, known)
            print(parts[0].text)
            print(f"  -> {sum(m['used_vision'] for m in meta)} of {len(batch)} "
                  f"frames would go to vision; {len(parts) - 1} extra parts attached")
            continue

        items, meta = extract_batch(batch, known)
        kept, deleted, filled = write_batch(con, batch, items, meta)
        total_kept += kept
        total_del += deleted
        vis = sum(m["used_vision"] for m in meta)
        print(f"  -> {kept} extracted ({vis} via vision), {deleted} sensitive dropped"
              + (f", {filled} filled by fallback" if filled else ""))
        for r in con.execute(
                "SELECT task_thread, summary FROM extractions WHERE frame_id IN "
                f"({','.join('?' * len(batch))}) ORDER BY ts",
                [b["id"] for b in batch]).fetchall():
            print(f"     {r['task_thread']:<28} {r['summary'][:80]}")

    if USAGE["calls"]:
        print(f"[usage] {USAGE['calls']} {MODEL} calls, "
              f"{USAGE['prompt']} prompt + {USAGE['output']} output "
              f"(+{USAGE['thoughts']} thinking) = {USAGE['total']} tokens "
              f"in {time.time() - t0:.1f}s wall")
    return total_kept + total_del


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000, help="max frames per pass")
    ap.add_argument("--watch", type=float, default=0,
                    help="seconds between passes; 0 = single pass and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the prompt, print it, call nothing")
    ap.add_argument("--no-llm", action="store_true", default=True,
                    help="(default) OCR only: store screen text, no API calls")
    ap.add_argument("--llm", dest="no_llm", action="store_false",
                    help="opt in to Gemini semantic extraction (needs GEMINI_API_KEY)")
    ap.add_argument("--reset", action="store_true",
                    help="wipe extractions and set frames.extracted = 0")
    args = ap.parse_args()

    con = connect()
    ensure_schema(con)

    if args.reset:
        con.execute("DELETE FROM extractions")
        sch = frames_schema(con)
        if sch["extracted"]:
            con.execute(f"UPDATE frames SET {sch['extracted']} = 0")
        con.commit()
        print("reset: all frames pending again")
        return

    passer = (lambda: no_llm_pass(con, args.limit)) if args.no_llm else \
             (lambda: one_pass(con, args.limit, args.dry_run))

    if not args.watch:
        n = passer()
        left = len(pending_frames(con, 10 ** 9))
        print(f"done: {n} frames handled, {left} still pending")
        return

    print(f"watching {DB_PATH} every {args.watch}s (ctrl-c to stop)")
    try:
        while True:
            if passer() == 0:
                time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        con.close()


if __name__ == "__main__":
    main()
