#!/usr/bin/env python3
"""
stub_extract.py -- DEV ONLY. Fills `extractions` with hand-written labels for
the 30 rows seed.py makes, so sessionize/embed/memory can be tested (or demoed)
with zero Gemini calls. Delete this file once extract.py has run for real.

    uv run seed.py && uv run stub_extract.py
"""
import json
import os
import sqlite3
import time
from pathlib import Path

MEM_DIR = Path(os.environ.get("MEM_DIR", Path.home() / "mem"))
DB_PATH = Path(os.environ.get("MEM_DB", MEM_DIR / "mem.db"))

CORS = "cors-preflight-fix"
BREAK = "reddit-youtube-break"
DASH = "dashboard-timeline-ui"
COMMS = "team-comms"

# one entry per seed.py SCRIPT frame, in ts order
# (slug, activity, summary, resume_hint, entities, sensitive)
LABELS = [
    (CORS, "coding", "Opened server.py in mem-api with a bare CORS(app) call and no frontend access.",
     "Open server.py:5 and widen the CORS config", ["server.py", "mem-api", "flask_cors", "/api/frames"], 0),
    (CORS, "coding", "Hit 'No Access-Control-Allow-Origin header' fetching /api/frames from localhost:5173.",
     "Reproduce the failing GET from dashboard.tsx:42", ["dashboard.tsx:42", "Access-Control-Allow-Origin", "http://localhost:5173", "net::ERR_FAILED"], 0),
    (CORS, "search", "Searched for why a Flask CORS preflight returns 401 with no CORS headers.",
     "Open the Stack Overflow answer on preflight and auth decorators", ["flask-cors", "OPTIONS", "401"], 0),
    (CORS, "reading", "Read the Stack Overflow answer explaining that require_auth 401s the OPTIONS preflight before Flask-CORS runs.",
     "Exempt OPTIONS inside require_auth in auth.py", ["require_auth", "auth.py", "supports_credentials", "OPTIONS"], 0),
    (CORS, "coding", "Edited require_auth in auth.py to let OPTIONS through before the token check.",
     "Save auth.py:21 and re-run the OPTIONS curl", ["auth.py:21", "require_auth", "Authorization"], 0),
    (COMMS, "comms", "Told priya in #eng-platform about the CORS preflight bug ahead of the noon staging redeploy.",
     "Reply in #eng-platform once the fix lands", ["#eng-platform", "priya", "staging"], 0),
    (CORS, "coding", "Ran an OPTIONS curl against /api/frames and still got HTTP 401 UNAUTHORIZED.",
     "Return 204 from the OPTIONS branch in auth.py:22", ["curl", "/api/frames", "401 UNAUTHORIZED", "Werkzeug"], 0),
    (CORS, "coding", "Changed the OPTIONS branch in auth.py to return an empty 204 and let Flask reload.",
     "Re-run the OPTIONS curl to confirm 204", ["auth.py:22", "204", "require_auth"], 0),
    (CORS, "coding", "Confirmed the preflight now returns 204 with Access-Control-Allow-Origin set.",
     "Retry the credentialed GET from the browser", ["204 NO CONTENT", "Access-Control-Allow-Origin", "Access-Control-Allow-Headers"], 0),
    (CORS, "coding", "Found the credentialed GET still blocked because Access-Control-Allow-Credentials was empty.",
     "Set supports_credentials=True in server.py:5", ["Access-Control-Allow-Credentials", "dashboard.tsx:42", "XMLHttpRequest"], 0),
    (BREAK, "reading", "Scrolled r/webdev instead of finishing the credentials fix.",
     "Close the reddit tab and go back to server.py", ["r/webdev"], 0),
    (BREAK, "reading", "Read a r/webdev thread about proxying an API through Vite to dodge CORS.",
     "Close the reddit tab and go back to server.py", ["r/webdev", "vite"], 0),
    (BREAK, "media", "Watched a Kurzgesagt video about taking breaks.",
     "Close YouTube and reopen mem-api", ["YouTube", "Kurzgesagt"], 0),
    (BREAK, "media", "Watched a video on structuring side projects.",
     "Close YouTube and reopen mem-api", ["YouTube"], 0),
    ("banking", "admin", "Checked bank balances.", "n/a", [], 1),
    (CORS, "coding", "Added supports_credentials=True and an explicit localhost:5173 origin to the CORS config in server.py.",
     "Restart flask and re-check the browser network tab", ["server.py:5", "supports_credentials", "http://localhost:5173"], 0),
    (CORS, "coding", "Restarted the flask dev server on port 8000 with the new CORS config.",
     "Reload localhost:5173/dashboard and watch the network tab", ["flask", "port 8000", "server.py"], 0),
    (CORS, "coding", "Saw /api/frames return 200 and the preflight 204 with no console errors.",
     "Write a pytest that locks the preflight behaviour in", ["/api/frames", "204", "200"], 0),
    (CORS, "coding", "Wrote test_preflight_returns_204 in test_cors.py and stubbed the credentialed GET test.",
     "Write test_credentialed_get in test_cors.py:8", ["test_cors.py", "test_preflight_returns_204", "test_credentialed_get"], 0),
    (CORS, "coding", "Ran the CORS tests green and committed 'fix: exempt OPTIONS from auth'.",
     "Push the branch and update ENG-412", ["pytest", "test_cors.py", "8f2c1ad"], 0),
    (DASH, "coding", "Wired dashboard.tsx to fetch /api/frames with credentials and a bearer token.",
     "Render the timeline from the frames array in dashboard.tsx:44", ["dashboard.tsx:44", "credentials: 'include'", "Authorization"], 0),
    (DASH, "coding", "Loaded 41 frames into the dashboard timeline but the timestamps rendered in UTC.",
     "Convert f.ts to local time in dashboard.tsx", ["localhost:5173/dashboard", "timeline", "UTC"], 0),
    (DASH, "coding", "Started converting the epoch ts to local time with toLocaleTimeString in dashboard.tsx.",
     "Finish dashboard.tsx:59 and reload the timeline", ["dashboard.tsx:59", "toLocaleTimeString", "f.ts"], 0),
    (COMMS, "comms", "Told #eng-platform the CORS fix was the auth decorator 401ing the OPTIONS preflight.",
     "Answer priya about the staging report", ["#eng-platform", "priya", "OPTIONS"], 0),
    (COMMS, "comms", "Agreed with priya to write the root cause up on ENG-412 after QA hit it Tuesday.",
     "Write the root cause into ENG-412", ["ENG-412", "priya", "QA"], 0),
    ("eng-412-writeup", "admin", "Wrote the require_auth root cause and the fix into Linear ticket ENG-412.",
     "Move ENG-412 to review", ["ENG-412", "require_auth", "auth.py", "Linear"], 0),
    (COMMS, "comms", "Saw marcus post timeline mock v3 in #design-sync with a changed hover state.",
     "Open the v3 timeline mock in Figma", ["#design-sync", "marcus", "Figma"], 0),
    ("standup-notes", "admin", "Wrote standup notes covering ENG-412, the timeline render, and the UTC timestamp gap.",
     "Do the local time formatting at dashboard.tsx:59, then keyframes", ["standup.md", "ENG-412", "dashboard.tsx:59"], 0),
    (COMMS, "comms", "Posted in #eng-platform that ENG-412 had the root cause and a covering pytest.",
     "Wait for priya to move ENG-412 to review", ["ENG-412", "#eng-platform", "pytest"], 0),
    (COMMS, "comms", "Agreed with priya to pick up the keyframe picker after lunch.",
     "Finish dashboard.tsx:59, then start the keyframe picker", ["dashboard.tsx:59", "keyframe picker", "priya"], 0),
]


def main():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    import extract
    extract.ensure_schema(con)

    rows = con.execute("SELECT id, ts, app, window_title, image_path FROM frames "
                       "ORDER BY ts").fetchall()
    # Align by seed manifest ts, not by position: a sensitive frame deleted on a
    # previous run would otherwise shift every label after it.
    manifest = MEM_DIR / ".seed_manifest.json"
    if manifest.exists():
        order = {ts: i for i, ts in enumerate(json.loads(manifest.read_text())["ts"])}
        rows = [r for r in rows if r["ts"] in order]
        pairs = [(r, LABELS[order[r["ts"]]]) for r in rows if order[r["ts"]] < len(LABELS)]
    else:
        print("no seed manifest; falling back to positional alignment")
        pairs = list(zip(rows, LABELS))
    now = int(time.time())
    kept = dropped = 0
    for row, label in pairs:
        slug, activity, summary, hint, ents, sensitive = label
        if sensitive:
            con.execute("DELETE FROM frames WHERE id=?", (row["id"],))
            p = Path(row["image_path"] or "")
            if p.exists():
                p.unlink()
            dropped += 1
            continue
        con.execute("""INSERT OR REPLACE INTO extractions
            (frame_id, ts, app, window_title, image_path, summary, task_thread,
             entities, resume_hint, activity_type, sensitive, ocr_text,
             ocr_chars, used_vision, extracted_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,0,?)""",
            (row["id"], row["ts"], row["app"], row["window_title"], row["image_path"],
             summary, slug, json.dumps(ents), hint, activity,
             f"{row['window_title']}\n{summary}\n" + " ".join(ents),
             400, now))
        con.execute("UPDATE frames SET extracted=1 WHERE id=?", (row["id"],))
        kept += 1
    con.commit()
    print(f"stubbed {kept} extractions, dropped {dropped} sensitive (no API calls)")


if __name__ == "__main__":
    main()
