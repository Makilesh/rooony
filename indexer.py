#!/usr/bin/env python3
"""
indexer.py -- the bridge between screencapture/ and the extraction layer.

The capture daemon (screencapture/src/main.py) only writes JPEGs:

    ~/mem/frames/{epoch}_{App-Name}.jpg      (or {epoch}.jpg)

The extraction layer reads rows out of the SQLite table `frames`. Nothing was
creating those rows. This does: it scans the capture directory and inserts one
row per new JPEG, at extracted=0, for extract.py to pick up.

  ts            parsed from the filename (file mtime if the name doesn't parse)
  app           the app name from the filename, dashes turned back into spaces
  window_title  NULL - the capture side detects the app but not the window title
  image_path    absolute path to the JPEG
  dhash         8x8 difference hash, so near-identical screens are detectable
  dup_count     how many consecutive frames share this dhash (1 = new content)

Idempotent: image_path is unique, re-runs insert nothing.

    uv run indexer.py                # index whatever is new and exit
    uv run indexer.py --watch 5      # keep indexing as the daemon captures
    uv run indexer.py --prune        # drop rows whose JPEG is gone (retention)
    uv run indexer.py --stats
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from PIL import Image

MEM_DIR = Path(os.environ.get("MEM_DIR", Path.home() / "mem"))
DB_PATH = Path(os.environ.get("MEM_DB", MEM_DIR / "mem.db"))
CAPTURE_CONFIG = Path(__file__).resolve().parent / "screencapture" / "config.json"
SETTLE_S = 2                       # ignore files still being written
NAME_RE = re.compile(r"^(\d{9,13})(?:_(.+?))?\.(jpe?g|png)$", re.I)


def frames_dir():
    """Wherever the capture daemon is actually writing."""
    if os.environ.get("MEM_FRAMES"):
        return Path(os.environ["MEM_FRAMES"]).expanduser()
    if CAPTURE_CONFIG.exists():                    # read it, don't import it:
        try:                                       # load_config() writes the file
            cfg = json.loads(CAPTURE_CONFIG.read_text())
            if cfg.get("output_dir"):
                return Path(cfg["output_dir"]).expanduser()
        except Exception as e:
            print(f"[indexer] ignoring unreadable {CAPTURE_CONFIG}: {e}", file=sys.stderr)
    return MEM_DIR / "frames"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            app TEXT,
            window_title TEXT,
            image_path TEXT,
            dhash TEXT,
            dup_count INTEGER DEFAULT 1,
            extracted INTEGER DEFAULT 0
        )
    """)
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_frames_path ON frames(image_path)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_frames_extracted ON frames(extracted)")
    con.commit()
    return con


def parse_name(path: Path):
    """-> (ts, app). Falls back to mtime / no app for unexpected filenames."""
    m = NAME_RE.match(path.name)
    if not m:
        return int(path.stat().st_mtime), None
    ts = int(m.group(1))
    if ts > 1e11:                                  # milliseconds
        ts //= 1000
    app = m.group(2)
    # capture sanitises "Google Chrome" -> "Google-Chrome"; put the spaces back
    return ts, (app.replace("-", " ").strip() if app else None)


def dhash(path: Path, size: int = 8) -> str:
    try:
        img = Image.open(path).convert("L").resize((size + 1, size), Image.LANCZOS)
    except Exception:
        return ""
    px = list(img.tobytes())          # row-major grayscale bytes
    bits = 0
    for r in range(size):
        row = px[r * (size + 1):(r + 1) * (size + 1)]
        for c in range(size):
            bits = (bits << 1) | (1 if row[c] < row[c + 1] else 0)
    return f"{bits:016x}"


def index_once(con, directory: Path, verbose=True):
    if not directory.is_dir():
        print(f"[indexer] no capture directory at {directory} - is the daemon running?")
        return 0

    known = {r["image_path"] for r in con.execute("SELECT image_path FROM frames")}
    now = time.time()
    files = []
    for p in directory.iterdir():
        if not p.is_file() or p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        if str(p) in known:
            continue
        try:
            if now - p.stat().st_mtime < SETTLE_S:  # still being written
                continue
        except OSError:
            continue
        files.append(p)
    if not files:
        return 0

    rows = sorted(((*parse_name(p), p) for p in files), key=lambda t: t[0])

    last = con.execute("SELECT dhash, dup_count FROM frames ORDER BY ts DESC "
                       "LIMIT 1").fetchone()
    prev_hash = last["dhash"] if last else None
    prev_dup = last["dup_count"] if last else 0

    inserted = 0
    for ts, app, path in rows:
        h = dhash(path)
        dup = prev_dup + 1 if (h and h == prev_hash) else 1
        cur = con.execute(
            "INSERT OR IGNORE INTO frames (ts, app, window_title, image_path,"
            " dhash, dup_count, extracted) VALUES (?,?,NULL,?,?,?,0)",
            (ts, app, str(path), h, dup))
        if cur.rowcount:
            inserted += 1
            prev_hash, prev_dup = h, dup
    con.commit()

    if verbose and inserted:
        span = f"{time.strftime('%H:%M', time.localtime(rows[0][0]))}-" \
               f"{time.strftime('%H:%M', time.localtime(rows[-1][0]))}"
        apps = sorted({a for _, a, _ in rows if a})
        print(f"[indexer] +{inserted} frames ({span}) "
              f"apps: {', '.join(apps[:6]) or 'unknown'}")
    return inserted


def prune(con):
    """Retention deleted some JPEGs. Drop rows we never got to extract."""
    gone = [r["id"] for r in con.execute("SELECT id, image_path FROM frames")
            if not Path(r["image_path"]).exists()]
    if not gone:
        return 0
    keep = {r[0] for r in con.execute(
        f"SELECT frame_id FROM extractions WHERE frame_id IN "
        f"({','.join('?' * len(gone))})", gone)} if table_exists(con, "extractions") else set()
    drop = [i for i in gone if i not in keep]
    con.executemany("DELETE FROM frames WHERE id=?", [(i,) for i in drop])
    con.commit()
    print(f"[indexer] pruned {len(drop)} rows with missing files "
          f"({len(gone) - len(drop)} kept - already extracted)")
    return len(drop)


def table_exists(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone() is not None


def stats(con, directory):
    n_files = len([p for p in directory.iterdir()
                   if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")]) \
        if directory.is_dir() else 0
    total = con.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    pending = con.execute("SELECT COUNT(*) FROM frames WHERE extracted=0").fetchone()[0]
    dupes = con.execute("SELECT COUNT(*) FROM frames WHERE dup_count > 1").fetchone()[0]
    print(f"capture dir : {directory}  ({n_files} images)")
    print(f"db          : {DB_PATH}")
    print(f"frames rows : {total}  pending extraction: {pending}  near-duplicates: {dupes}")
    if n_files > total:
        print(f"  -> {n_files - total} images not indexed yet; run `uv run indexer.py`")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=float, default=0,
                    help="seconds between scans; 0 = one pass and exit")
    ap.add_argument("--prune", action="store_true",
                    help="drop rows whose JPEG no longer exists")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    d = frames_dir()
    con = connect()

    if args.stats:
        stats(con, d)
        return
    if args.prune:
        prune(con)

    if not args.watch:
        n = index_once(con, d)
        print(f"indexed {n} new frames from {d}")
        stats(con, d)
        return

    print(f"watching {d} every {args.watch}s (ctrl-c to stop)")
    try:
        while True:
            index_once(con, d)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        con.close()


if __name__ == "__main__":
    main()
