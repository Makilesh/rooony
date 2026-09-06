#!/usr/bin/env python3
"""
run_all.py -- the whole thing, one command, ctrl-c stops everything.

    capture daemon   screencapture/src/main.py   JPEGs -> ~/mem/frames
    indexer          indexer.py --watch          JPEGs -> frames rows
    extractor        extract.py --watch          frames -> extractions
    roller           every --roll seconds: sessionize.py + embed.py

    uv run run_all.py                 # everything
    uv run run_all.py --no-capture    # capture already running (or launchd)
    uv run run_all.py --no-llm        # OCR only, zero Gemini calls
"""
import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAPTURE = ROOT / "screencapture" / "src" / "main.py"
CAPTURE_VENV = ROOT / "screencapture" / "venv" / "bin" / "python"

procs = []
stop = threading.Event()


def spawn(name, argv, **kw):
    print(f"[run_all] starting {name}: {' '.join(str(a) for a in argv)}")
    p = subprocess.Popen(argv, **kw)
    procs.append((name, p))
    return p


def roller(interval, no_llm):
    """Close finished sessions and index them, so memory.py stays current."""
    while not stop.wait(interval):
        for script, extra in (("sessionize.py", ["--flush"] + (["--no-llm"] if no_llm else [])),
                              ("embed.py", [])):
            r = subprocess.run([sys.executable, str(ROOT / script), *extra],
                               capture_output=True, text=True)
            tail = (r.stdout or r.stderr).strip().splitlines()
            if tail:
                print(f"[roll] {script}: {tail[-1]}")


def shutdown(signum=None, frame=None):
    if stop.is_set():
        return
    stop.set()
    print("\n[run_all] stopping...")
    for name, p in procs:
        if p.poll() is None:
            p.terminate()
    deadline = time.time() + 8
    for name, p in procs:
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            print(f"[run_all] {name} did not stop, killing")
            p.kill()
    print("[run_all] stopped")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-capture", action="store_true",
                    help="don't start the capture daemon (launchd or another shell has it)")
    ap.add_argument("--no-llm", action="store_true", default=True,
                    help="(default) OCR only; no API calls anywhere")
    ap.add_argument("--llm", dest="no_llm", action="store_false",
                    help="opt in to Gemini extraction + summaries")
    ap.add_argument("--index-every", type=float, default=5)
    ap.add_argument("--extract-every", type=float, default=5)
    ap.add_argument("--roll", type=float, default=120,
                    help="seconds between sessionize+embed passes; 0 disables")
    args = ap.parse_args()

    if not args.no_llm and not os.environ.get("GEMINI_API_KEY"):
        sys.exit("--llm needs GEMINI_API_KEY; drop --llm to run fully local")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if not args.no_capture:
        if not CAPTURE.exists():
            sys.exit(f"no capture daemon at {CAPTURE} - merge screencapture/ in, "
                     "or pass --no-capture")
        # the daemon needs pyobjc; prefer its own venv if the install script made one
        py = str(CAPTURE_VENV) if CAPTURE_VENV.exists() else sys.executable
        spawn("capture", [py, str(CAPTURE)])

    spawn("indexer", [sys.executable, str(ROOT / "indexer.py"),
                      "--watch", str(args.index_every)])
    spawn("extract", [sys.executable, str(ROOT / "extract.py"),
                      "--watch", str(args.extract_every)]
                     + (["--no-llm"] if args.no_llm else []))

    if args.roll:
        threading.Thread(target=roller, args=(args.roll, args.no_llm),
                         daemon=True).start()

    print("[run_all] up. ctrl-c to stop. "
          "ask it things with: uv run memory.py recall \"...\"")
    try:
        while not stop.is_set():
            for name, p in procs:
                if p.poll() is not None:
                    print(f"[run_all] {name} exited with {p.returncode}")
                    shutdown()
                    return 1
            time.sleep(1)
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
