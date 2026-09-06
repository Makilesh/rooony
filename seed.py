#!/usr/bin/env python3
"""
seed.py -- fabricate 30 realistic frame rows + matching placeholder JPEGs.

Lets the whole extract -> sessionize -> embed -> memory chain be tested before
the real capture loop lands. Idempotent: every run wipes the rows/files it made
last time (tracked in ~/mem/.seed_manifest.json) and writes a fresh set.

    uv run seed.py                 # 30 frames ending ~now
    uv run seed.py --keep          # append instead of replacing the last seed
    uv run seed.py --clean         # delete the seeded rows/files and exit
"""
import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MEM_DIR = Path(os.environ.get("MEM_DIR", Path.home() / "mem"))
DB_PATH = Path(os.environ.get("MEM_DB", MEM_DIR / "mem.db"))
FRAMES_DIR = Path(os.environ.get("MEM_FRAMES", MEM_DIR / "frames"))
MANIFEST = MEM_DIR / ".seed_manifest.json"

W, H = 1400, 900
JPEG_QUALITY = 60

FONT_CANDIDATES = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Supplemental/Andale Mono.ttf",
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

THEMES = {
    # name        bg          fg          bar         bar_fg      accent
    "editor":  ("#1e1e1e", "#d4d4d4", "#323233", "#cccccc", "#4ec9b0"),
    "terminal":("#0c0c0c", "#e8e8e8", "#2b2b2b", "#dddddd", "#7ee787"),
    "browser": ("#ffffff", "#202124", "#dee1e6", "#202124", "#1a73e8"),
    "chat":    ("#f8f8f8", "#1d1c1d", "#3f0e40", "#ffffff", "#1264a3"),
}


def load_font(size):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


# (minutes_from_start, app, window_title, theme, [screen lines])
# Story: CORS bug in VS Code -> a Slack blip -> Reddit/YouTube/banking
# detour -> 12 min idle gap -> back to the CORS fix -> a comms block.
SCRIPT = [
    # --- coding thread: the CORS bug -------------------------------------
    (0, "Code", "server.py - mem-api - Visual Studio Code", "editor", [
        "server.py                                        mem-api",
        "",
        "  1  from flask import Flask, jsonify",
        "  2  from flask_cors import CORS",
        "  3",
        "  4  app = Flask(__name__)",
        "  5  CORS(app)",
        "  6",
        "  7  @app.route('/api/frames')",
        "  8  def frames():",
        "  9      return jsonify(load_frames())",
        " 10",
        " 11  if __name__ == '__main__':",
        " 12      app.run(port=8000)",
        "",
        "PROBLEMS 1   frontend cannot reach /api/frames",
    ]),
    (4, "Google Chrome", "localhost:5173/dashboard - Console - Chrome", "browser", [
        "DevTools - Console                     localhost:5173/dashboard",
        "",
        "Access to fetch at 'http://localhost:8000/api/frames'",
        "from origin 'http://localhost:5173' has been blocked by",
        "CORS policy: No 'Access-Control-Allow-Origin' header is",
        "present on the requested resource.",
        "",
        "GET http://localhost:8000/api/frames net::ERR_FAILED",
        "dashboard.tsx:42  Uncaught (in promise) TypeError: Failed to fetch",
    ]),
    (8, "Google Chrome", "flask cors preflight 401 - Google Search", "browser", [
        "Google                flask cors preflight 401 no access-control-allow-origin",
        "",
        "Flask-CORS: OPTIONS request returns 401 before CORS headers",
        "  stackoverflow.com/questions/25594893",
        "",
        "Handling CORS preflight with an auth decorator - Flask docs",
        "  flask-cors.readthedocs.io/en/latest/#using-cors-with-cookies",
        "",
        "Why does my preflight fail but curl succeed?",
    ]),
    (12, "Google Chrome", "Flask-CORS preflight blocked by auth - Stack Overflow", "browser", [
        "Stack Overflow  -  Flask-CORS preflight blocked by auth decorator",
        "",
        "Accepted answer (312 votes):",
        "Your @require_auth decorator runs on the OPTIONS preflight too.",
        "The browser sends OPTIONS without the Authorization header, so",
        "the request 401s before Flask-CORS can attach the headers.",
        "",
        "Exempt OPTIONS in the decorator, or set",
        "CORS(app, supports_credentials=True) and skip auth for OPTIONS.",
    ]),
    (16, "Code", "auth.py - mem-api - Visual Studio Code", "editor", [
        "auth.py                                          mem-api",
        "",
        " 18  def require_auth(fn):",
        " 19      @wraps(fn)",
        " 20      def wrapper(*args, **kwargs):",
        " 21          if request.method == 'OPTIONS':",
        " 22              return fn(*args, **kwargs)   # <- preflight escape",
        " 23          token = request.headers.get('Authorization')",
        " 24          if not token:",
        " 25              return jsonify(error='unauthorized'), 401",
        " 26          return fn(*args, **kwargs)",
        "",
        "auth.py line 21 edited - not saved",
    ]),
    (20, "Slack", "Slack | #eng-platform | rooony", "chat", [
        "#eng-platform",
        "",
        "priya  10:41 AM",
        "  heads up, staging redeploy at noon",
        "",
        "you  10:42 AM",
        "  ack. fighting a CORS preflight thing on mem-api, will be quick",
    ]),
    (24, "Terminal", "curl - mem-api - bash", "terminal", [
        "$ curl -i -X OPTIONS http://localhost:8000/api/frames \\",
        "    -H 'Origin: http://localhost:5173' \\",
        "    -H 'Access-Control-Request-Method: GET'",
        "",
        "HTTP/1.1 401 UNAUTHORIZED",
        "Content-Type: application/json",
        "Server: Werkzeug/3.0.1 Python/3.11.9",
        "",
        "{\"error\": \"unauthorized\"}",
    ]),
    (28, "Code", "auth.py - mem-api - Visual Studio Code", "editor", [
        "auth.py                                          mem-api",
        "",
        " 18  def require_auth(fn):",
        " 19      @wraps(fn)",
        " 20      def wrapper(*args, **kwargs):",
        " 21          if request.method == 'OPTIONS':",
        " 22              return ('', 204)",
        " 23          token = request.headers.get('Authorization')",
        "",
        "TERMINAL  * Detected change in auth.py, reloading",
        " * Restarting with stat",
    ]),
    (32, "Terminal", "curl - mem-api - bash", "terminal", [
        "$ curl -i -X OPTIONS http://localhost:8000/api/frames \\",
        "    -H 'Origin: http://localhost:5173'",
        "",
        "HTTP/1.1 204 NO CONTENT",
        "Access-Control-Allow-Origin: http://localhost:5173",
        "Access-Control-Allow-Headers: Authorization, Content-Type",
        "",
        "preflight passes now - check the real GET from the browser",
    ]),
    (36, "Google Chrome", "localhost:5173/dashboard - Console - Chrome", "browser", [
        "DevTools - Console                     localhost:5173/dashboard",
        "",
        "Access to XMLHttpRequest at 'http://localhost:8000/api/frames'",
        "has been blocked by CORS policy: The value of the",
        "'Access-Control-Allow-Credentials' header in the response is ''",
        "which must be 'true' when the request's credentials mode is 'include'.",
        "",
        "dashboard.tsx:42  still failing on the credentialed GET",
    ]),
    # --- distraction ------------------------------------------------------
    (40, "Google Chrome", "r/webdev - Reddit", "browser", [
        "r/webdev                                          Hot",
        "",
        "1.2k  My manager asked me to 'just turn CORS off'",
        "      412 comments",
        "",
        "876   What is your least favourite part of frontend work?",
        "",
        "540   Show reddit: I rebuilt my portfolio in 4 hours",
    ]),
    (44, "Google Chrome", "r/webdev - comments - Reddit", "browser", [
        "My manager asked me to 'just turn CORS off'  -  412 comments",
        "",
        "u/nullpointer_dev  3h",
        "  proxy the API through vite and the whole class of bug disappears",
        "",
        "u/http_teapot  2h",
        "  CORS is a browser thing. curl will always lie to you.",
    ]),
    (48, "Google Chrome", "Kurzgesagt - YouTube", "browser", [
        "YouTube",
        "",
        "Now playing: What Happens If You Never Take A Break?",
        "Kurzgesagt - In a Nutshell    2.1M views    12:04",
        "",
        "Up next: The Egg - A Short Story",
        "         Why Cities Are Built The Way They Are",
    ]),
    (52, "Google Chrome", "YouTube - watch", "browser", [
        "YouTube",
        "",
        "Now playing: How I Structure My Side Projects",
        "8:12 / 14:30",
        "",
        "Comments 1,204",
        "  'the part about scoping at 6:40 changed how I work'",
    ]),
    (56, "Google Chrome", "Chase Online - Account Summary", "browser", [
        "Chase Online          Account Summary",
        "",
        "TOTAL CHECKING (...4417)      Available balance   $3,182.44",
        "SAPPHIRE PREFERRED (...9021)  Current balance     $1,047.19",
        "",
        "Recent activity",
        "  09/03  BLUE BOTTLE COFFEE           -$6.75",
        "  09/02  PAYROLL DIRECT DEP        +$2,910.00",
        "",
        "Pay card  |  Transfer money  |  Statements",
    ]),
    # --- 12 minute idle gap here (session boundary) -----------------------
    # --- return to coding -------------------------------------------------
    (72, "Code", "server.py - mem-api - Visual Studio Code", "editor", [
        "server.py                                        mem-api",
        "",
        "  4  app = Flask(__name__)",
        "  5  CORS(app, supports_credentials=True,",
        "  6       origins=['http://localhost:5173'])",
        "  7",
        "  8  @app.route('/api/frames')",
        "  9  @require_auth",
        " 10  def frames():",
        " 11      return jsonify(load_frames())",
        "",
        "server.py line 5 - added supports_credentials",
    ]),
    (76, "Terminal", "flask run - mem-api - bash", "terminal", [
        "$ flask --app server run --port 8000",
        " * Serving Flask app 'server'",
        " * Debug mode: on",
        " * Running on http://127.0.0.1:8000",
        " * Restarting with stat",
        " * Debugger PIN: 118-402-771",
    ]),
    (80, "Google Chrome", "localhost:5173/dashboard - Console - Chrome", "browser", [
        "DevTools - Network                     localhost:5173/dashboard",
        "",
        "Name              Status   Type    Size     Time",
        "frames            200      xhr     41.2 kB  38 ms",
        "OPTIONS frames    204      preflight  0 B    4 ms",
        "",
        "Console: no errors",
    ]),
    (84, "Code", "test_cors.py - mem-api - Visual Studio Code", "editor", [
        "test_cors.py                                     mem-api",
        "",
        "  1  def test_preflight_returns_204(client):",
        "  2      r = client.options('/api/frames',",
        "  3          headers={'Origin': 'http://localhost:5173'})",
        "  4      assert r.status_code == 204",
        "  5      assert r.headers['Access-Control-Allow-Origin'] == \\",
        "  6          'http://localhost:5173'",
        "",
        "  8  def test_credentialed_get(client):   # TODO write this",
    ]),
    (88, "Terminal", "pytest - mem-api - bash", "terminal", [
        "$ pytest tests/test_cors.py -q",
        "",
        "..                                                   [100%]",
        "2 passed in 0.41s",
        "",
        "$ git add -A && git commit -m 'fix: exempt OPTIONS from auth'",
        "[main 8f2c1ad] fix: exempt OPTIONS from auth",
        " 3 files changed, 24 insertions(+), 5 deletions(-)",
    ]),
    (92, "Code", "dashboard.tsx - mem-web - Visual Studio Code", "editor", [
        "dashboard.tsx                                    mem-web",
        "",
        " 40  const res = await fetch(API + '/api/frames', {",
        " 41    credentials: 'include',",
        " 42    headers: { Authorization: `Bearer ${token}` },",
        " 43  })",
        " 44  const frames = await res.json()",
        "",
        "dashboard.tsx line 44 - render the timeline next",
    ]),
    (96, "Google Chrome", "localhost:5173/dashboard - Chrome", "browser", [
        "mem dashboard                          localhost:5173/dashboard",
        "",
        "Timeline  -  41 frames loaded",
        "10:02  Visual Studio Code   server.py",
        "10:06  Google Chrome        Console",
        "10:10  Google Chrome        Google Search",
        "",
        "frames render, timestamps are UTC and should be local",
    ]),
    (100, "Code", "dashboard.tsx - mem-web - Visual Studio Code", "editor", [
        "dashboard.tsx                                    mem-web",
        "",
        " 58  <span className='ts'>",
        " 59    {new Date(f.ts * 1000).toLocaleTimeString()}",
        " 60  </span>",
        "",
        "dashboard.tsx line 59 - convert epoch to local time",
    ]),
    # --- comms block ------------------------------------------------------
    (104, "Slack", "Slack | #eng-platform | rooony", "chat", [
        "#eng-platform",
        "",
        "you  11:46 AM",
        "  CORS is fixed - the auth decorator was 401ing the OPTIONS",
        "  preflight before flask-cors could add the headers",
        "",
        "priya  11:47 AM",
        "  ohhh. that explains the staging report too",
    ]),
    (108, "Slack", "Slack | #eng-platform | rooony", "chat", [
        "#eng-platform",
        "",
        "priya  11:49 AM",
        "  can you put that on ENG-412? qa hit the same thing tuesday",
        "",
        "you  11:50 AM",
        "  yep, writing it up now",
    ]),
    (112, "Google Chrome", "ENG-412 CORS preflight fails on credentialed requests - Linear", "browser", [
        "Linear   ENG-412   CORS preflight fails on credentialed requests",
        "",
        "Status: In Progress      Assignee: you      Cycle 14",
        "",
        "Root cause: require_auth in auth.py ran on OPTIONS and returned",
        "401 before Flask-CORS attached Access-Control-Allow-Origin.",
        "Fix: short-circuit OPTIONS in the decorator + supports_credentials.",
        "",
        "Add comment...",
    ]),
    (116, "Slack", "Slack | #design-sync | rooony", "chat", [
        "#design-sync",
        "",
        "marcus  11:55 AM",
        "  timeline mock v3 is in figma, the hover state changed",
        "",
        "you  11:56 AM",
        "  looking after standup notes",
    ]),
    (120, "Code", "standup.md - notes - Visual Studio Code", "editor", [
        "standup.md                                        notes",
        "",
        "## Thu",
        "- fixed ENG-412 (CORS preflight 401 via require_auth)",
        "- dashboard timeline renders, timestamps still UTC",
        "- next: local time formatting in dashboard.tsx:59, then keyframes",
        "",
        "blockers: none",
    ]),
    (124, "Slack", "Slack | #eng-platform | rooony", "chat", [
        "#eng-platform",
        "",
        "you  12:03 PM",
        "  ENG-412 updated with the root cause + the pytest that covers it",
        "",
        "priya  12:04 PM",
        "  thank you. moving it to review",
    ]),
    (128, "Slack", "Slack | #eng-platform | rooony", "chat", [
        "#eng-platform",
        "",
        "priya  12:06 PM",
        "  after lunch can you look at the keyframe picker?",
        "",
        "you  12:07 PM",
        "  yes - dashboard.tsx:59 first, then keyframes",
    ]),
]


def render_frame(path, app, window_title, theme, lines):
    bg, fg, bar, bar_fg, accent = THEMES[theme]
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    title_font = load_font(26)
    body_font = load_font(24)

    d.rectangle([0, 0, W, 64], fill=bar)
    d.text((28, 18), f"{app}  -  {window_title}", font=title_font, fill=bar_fg)
    d.line([(0, 64), (W, 64)], fill=accent, width=3)

    y = 110
    for i, line in enumerate(lines):
        color = accent if i == 0 else fg
        d.text((44, y), line, font=body_font, fill=color)
        y += 34
        if y > H - 60:
            break

    img.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True)


def connect():
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    # Teammate owns this table; create it only so seeding works standalone.
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
    con.execute("CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_frames_extracted ON frames(extracted)")
    con.commit()
    return con


def read_manifest():
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text())
        except Exception:
            return {}
    return {}


def clean(con, manifest):
    ts_list = manifest.get("ts", [])
    if not ts_list:
        return 0
    cur = con.executemany("DELETE FROM frames WHERE ts = ?", [(t,) for t in ts_list])
    con.commit()
    removed = 0
    for t in ts_list:
        p = FRAMES_DIR / f"{t}.jpg"
        if p.exists():
            p.unlink()
            removed += 1
    # extractions may not exist yet (extract.py creates it)
    try:
        con.executemany("DELETE FROM extractions WHERE ts = ?", [(t,) for t in ts_list])
        con.commit()
    except sqlite3.OperationalError:
        pass
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes-ago", type=int, default=132,
                    help="when the first fabricated frame happened (default: 132)")
    ap.add_argument("--keep", action="store_true",
                    help="do not delete the previous seed before writing")
    ap.add_argument("--clean", action="store_true",
                    help="delete the previous seed and exit")
    args = ap.parse_args()

    con = connect()
    manifest = read_manifest()

    if args.clean:
        n = clean(con, manifest)
        MANIFEST.unlink(missing_ok=True)
        print(f"removed {len(manifest.get('ts', []))} rows, {n} jpegs")
        return

    if not args.keep and manifest.get("ts"):
        clean(con, manifest)
        print(f"replaced previous seed ({len(manifest['ts'])} frames)")

    start = int(time.time()) - args.minutes_ago * 60
    created = []
    prev_key = None
    dup_run = 1

    for offset, app, title, theme, lines in SCRIPT:
        ts = start + offset * 60
        path = FRAMES_DIR / f"{ts}.jpg"
        render_frame(path, app, title, theme, lines)

        key = f"{app}|{title}"
        dhash = hashlib.md5(key.encode()).hexdigest()[:16]
        dup_run = dup_run + 1 if key == prev_key else 1
        prev_key = key

        con.execute(
            "INSERT INTO frames (ts, app, window_title, image_path, dhash, dup_count, extracted)"
            " VALUES (?,?,?,?,?,?,0)",
            (ts, app, title, str(path), dhash, dup_run),
        )
        created.append(ts)

    con.commit()
    MANIFEST.write_text(json.dumps({"ts": created, "written_at": int(time.time())}))

    span = (created[-1] - created[0]) / 60.0
    print(f"seeded {len(created)} frames spanning {span:.0f} min")
    print(f"  db     {DB_PATH}")
    print(f"  frames {FRAMES_DIR}/{{ts}}.jpg  ({W}px, q{JPEG_QUALITY})")
    print(f"  pending extraction: "
          f"{con.execute('SELECT COUNT(*) FROM frames WHERE extracted=0').fetchone()[0]}")
    con.close()


if __name__ == "__main__":
    main()
