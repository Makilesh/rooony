#!/usr/bin/env python3
"""
doctor.py -- check the capture side and the extraction side actually line up.

Run this first after merging screencapture/ in, and any time the chain looks
wrong. It never writes to `frames`.

    uv run doctor.py                 # offline checks only
    uv run doctor.py --live          # also spend ~2 API calls to prove the keys work

Exit code 0 = everything needed to run is in place, 1 = something is broken.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

MEM_DIR = Path(os.environ.get("MEM_DIR", Path.home() / "mem"))
DB_PATH = Path(os.environ.get("MEM_DB", MEM_DIR / "mem.db"))

FAILED = []
GREEN, YELLOW, RED, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def ok(msg, detail=""):
    print(f"{GREEN}PASS{OFF} {msg}" + (f"  {detail}" if detail else ""))


def warn(msg, detail=""):
    print(f"{YELLOW}WARN{OFF} {msg}" + (f"  {detail}" if detail else ""))


def bad(msg, fix=""):
    FAILED.append(msg)
    print(f"{RED}FAIL{OFF} {msg}" + (f"\n     fix: {fix}" if fix else ""))


def section(name):
    print(f"\n--- {name} ---")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="make one real Gemini call and one real embedding call")
    args = ap.parse_args()

    section("environment")
    print(f"     MEM_DIR={MEM_DIR}  MEM_DB={DB_PATH}")
    if os.environ.get("GEMINI_API_KEY"):
        ok("GEMINI_API_KEY set")
    else:
        warn("GEMINI_API_KEY not set",
             "extract/sessionize/recall fall back or exit; --no-llm still works")
    print(f"     VECTOR_BACKEND={os.environ.get('VECTOR_BACKEND', 'sqlite')}")

    section("dependencies")
    for mod, why in [("google.genai", "Gemini + embeddings"), ("PIL", "seed.py"),
                     ("numpy", "cosine"), ("pydantic", "response_schema")]:
        try:
            __import__(mod)
            ok(f"{mod} importable", why)
        except Exception as e:
            bad(f"{mod} missing ({why})", f"uv pip install google-genai pillow numpy pydantic  [{e}]")
    try:
        from ocrmac import ocrmac  # noqa: F401
        ok("ocrmac importable", "Apple Vision OCR available")
        HAVE_OCR = True
    except Exception as e:
        HAVE_OCR = False
        warn("ocrmac unavailable", f"macOS only; every frame would go to Gemini vision [{e}]")

    section("database")
    if not DB_PATH.exists():
        bad(f"no database at {DB_PATH}",
            "point MEM_DB at the capture side's db, or run `uv run seed.py`")
        print_summary()
        return
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    (ok if mode.lower() == "wal" else warn)(f"journal_mode={mode}",
                                            "" if mode.lower() == "wal" else
                                            "capture + extract both write; WAL avoids lock storms")
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    print(f"     tables: {', '.join(sorted(tables)) or '(none)'}")
    if "frames" not in tables:
        bad("no `frames` table", "the capture side has not created it in this db yet")
        print_summary()
        return

    section("capture seam (frames)")
    sys.path.insert(0, str(Path(__file__).parent))
    import extract
    try:
        sch = extract.frames_schema(con)
    except SystemExit as e:
        bad(f"could not map the frames schema: {e}")
        print_summary()
        return
    print("     column map: " + json.dumps(sch))
    for k in ("id", "ts", "app", "window_title", "image_path"):
        (ok if sch[k] else warn)(f"{k} -> {sch[k]}")
    if not sch["extracted"]:
        warn("no `extracted` column",
             "falling back to `extractions` as the processed marker (still idempotent)")

    total = con.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    ok(f"{total} rows in frames")
    if total == 0:
        warn("frames is empty", "start the capture loop, or run `uv run seed.py`")

    row = con.execute(f"SELECT * FROM frames ORDER BY {sch['ts']} DESC LIMIT 1").fetchone()
    if row:
        raw = row[sch["ts"]]
        try:
            ts = extract.to_epoch(raw)
            age = (time.time() - ts) / 60
            ok(f"newest ts parses", f"{raw!r} -> {datetime.fromtimestamp(ts)} ({age:.0f} min ago)")
            if not (1e9 < ts < 4e9):
                bad(f"ts {ts} is not a plausible epoch second",
                    "check the capture side's timestamp units")
        except Exception as e:
            bad(f"cannot parse ts {raw!r}: {e}",
                "add the format to to_epoch() in extract.py")

        missing = 0
        sample = con.execute(f"SELECT {sch['image_path']} p FROM frames "
                             f"ORDER BY {sch['ts']} DESC LIMIT 10").fetchall()
        for s in sample:
            if not Path(extract.resolve_image(s["p"])).exists():
                missing += 1
        if missing:
            bad(f"{missing}/{len(sample)} recent image files not found on disk",
                f"first path: {sample[0]['p']!r} - check MEM_DIR or the capture side's path")
        else:
            p = Path(extract.resolve_image(sample[0]["p"]))
            ok("recent image files exist", f"{p} ({p.stat().st_size // 1024} kB)")

    try:
        extract.ensure_schema(con)
        pending = len(extract.pending_frames(con, 10 ** 9))
        ok(f"{pending} frames pending extraction")
    except Exception as e:
        bad(f"pending query failed: {e}")

    section("pipeline state")
    for t in ("extractions", "sessions", "threads", "entities", "vectors"):
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } else None
        if n is None:
            warn(f"{t}: table not created yet",
                 {"extractions": "run extract.py", "sessions": "run sessionize.py",
                  "threads": "run sessionize.py", "entities": "run sessionize.py",
                  "vectors": "run embed.py"}[t])
        else:
            ok(f"{t}: {n} rows")

    if "extractions" in tables:
        n = con.execute("SELECT COUNT(*) FROM extractions WHERE sensitive=1").fetchone()[0]
        (ok if n == 0 else bad)(f"{n} sensitive rows stored"
                                + ("" if n == 0 else " - they should be deleted, not kept"))
        slugs = con.execute("SELECT task_thread, COUNT(*) c FROM extractions "
                            "GROUP BY 1 ORDER BY c DESC LIMIT 12").fetchall()
        if slugs:
            print("     slugs: " + ", ".join(f"{r['task_thread']}({r['c']})" for r in slugs))
            if len(slugs) > 10:
                warn("many distinct slugs", "possible slug drift - check the known-slug prompt")

    section("vector layer")
    try:
        import embed
        import vectorstore
        n = vectorstore.count()
        ok(f"backend {vectorstore.BACKEND} reachable", f"{n} vectors")
        if vectorstore.BACKEND == "sqlite" and n:
            dims = con.execute("SELECT DISTINCT dim FROM vectors").fetchall()
            d = [r[0] for r in dims]
            (ok if d == [embed.EMBED_DIM] else bad)(
                f"vector dims {d} (expected [{embed.EMBED_DIM}])")
    except Exception as e:
        warn(f"vector layer unavailable: {type(e).__name__}: {e}",
             "memory.py degrades to SQLite LIKE; SQLite stays correct")

    if HAVE_OCR and row:
        section("ocr")
        p = extract.resolve_image(row[sch["image_path"]])
        t0 = time.time()
        text = extract.ocr_image(p)
        dt = time.time() - t0
        if len(text) >= 40:
            ok(f"OCR returned {len(text)} chars in {dt:.2f}s",
               repr(text.strip().splitlines()[0][:70]))
        else:
            warn(f"OCR returned only {len(text)} chars",
                 "frames like this get sent to Gemini vision instead")

    if args.live:
        section("live api")
        try:
            import embed
            t0 = time.time()
            v = embed.embed_query("cors preflight")
            ok(f"embedding call ok", f"{len(v)}d in {time.time() - t0:.2f}s")
            if abs(sum(x * x for x in v) ** 0.5 - 1.0) > 0.01:
                bad("query embedding is not unit length", "check normalization in embed.py")
        except Exception as e:
            bad(f"embedding call failed: {type(e).__name__}: {e}")
        try:
            from google import genai
            from google.genai import types
            t0 = time.time()
            c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            r = c.models.generate_content(
                model=extract.MODEL, contents="reply with the single word: ok",
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=0)))
            ok(f"{extract.MODEL} call ok", f"{r.text.strip()[:20]!r} in {time.time() - t0:.2f}s")
        except Exception as e:
            bad(f"gemini call failed: {type(e).__name__}: {e}")

    print_summary()


def print_summary():
    print()
    if FAILED:
        print(f"{RED}{len(FAILED)} check(s) failed:{OFF}")
        for f in FAILED:
            print(f"  - {f}")
        sys.exit(1)
    print(f"{GREEN}all checks passed{OFF}")


if __name__ == "__main__":
    main()
