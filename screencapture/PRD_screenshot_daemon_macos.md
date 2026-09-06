# PRD — Background Screenshot Capture Daemon (macOS, v1 Core)

**Owner:** You
**Builder:** Claude Code
**Platform:** macOS only (v1)
**Language:** Python 3.10+
**Status:** Ready to build

---

## 1. Objective

Build the first core function of the app: a process that runs continuously in the
background and captures a screenshot at a user-configurable interval (10 seconds to
5 minutes). Each screenshot is saved to a folder, named with the current Unix epoch
time. When possible, the name of the frontmost (active) application is appended.

This checkpoint delivers **only the capture engine + background runner** — no UI, no
upload, no analysis. Those come later.

---

## 2. Scope

**In scope (v1):**
- Screenshot capture on a fixed, configurable cadence (10s–300s).
- Save to a configurable output folder as PNG.
- Filename = `{epoch}.png`, or `{epoch}_{appname}.png` when the active app is known.
- Detect the frontmost application name.
- Run forever in the background, auto-start at login, restart on crash.
- Basic logging and graceful shutdown.

**Out of scope (v1) — do NOT build:**
- Any GUI / settings window (config is a file for now).
- Capturing the active *window title* (needs Accessibility permission — deferred).
- Multi-monitor stitching (v1 captures the **main display** only).
- Uploading, syncing, OCR, redaction, or any processing of images.
- Windows/Linux support.
- Storage retention/cleanup (optional stretch in CP6, otherwise deferred).

---

## 3. Key technical decisions (with rationale)

| Decision | Choice | Why |
|---|---|---|
| Capture method | Native `screencapture -x -t png <path>` via `subprocess` | Rock-solid, built into macOS, handles Retina, zero image dependencies. `mss` is a fine alternative if we later need in-memory frames, but for "grab and write to disk" the native tool is the most reliable for a long-running daemon. |
| Active-app detection | `NSWorkspace.frontmostApplication().localizedName()` via `pyobjc-framework-Cocoa` | Gives the app name without needing Accessibility permission. (Window *title* would need Accessibility — out of scope.) |
| Background runner | `launchd` LaunchAgent (per-user) | The macOS-native way to run at login, keep alive, and restart on crash. More reliable than a hand-rolled daemon. |
| Config | JSON file on disk | Simple, no UI needed yet. |
| Timestamp | Integer Unix epoch **seconds** | Min interval is 10s, so seconds never collide. |

**Dependencies:** only `pyobjc-framework-Cocoa` (do NOT install the full `pyobjc` meta-package — it's huge and unnecessary). Everything else is stdlib.

---

## 4. The critical gotcha — read before CP0

macOS blocks screen capture behind the **Screen Recording** permission
(System Settings → Privacy & Security → Screen Recording). Two things to know:

1. Without it, screenshots contain **only the wallpaper + menu bar**, not window
   contents. The capture "succeeds" but is useless — so we must visually verify.
2. The permission is granted to the **binary that runs the code**, not to "the script."
   - Running from Terminal → *Terminal* (or iTerm) needs the permission.
   - Running under launchd → the **python interpreter** at that exact path needs it.
   These are different TCC identities. This is why **CP5 must be tested separately**
   from CP4 — a build that works in the foreground can silently produce blank images
   under launchd until permission is granted to the daemon's interpreter.

Because of this, **CP0 is a permission smoke test** — we prove capture works before
writing any real code.

---

## 5. Configuration schema

`config.json`:
```json
{
  "interval_seconds": 60,
  "output_dir": "~/screenshot-daemon/captures",
  "capture_app_name": true
}
```
Rules:
- `interval_seconds`: integer, **must be 10–300 inclusive**. Reject anything outside
  with a clear error and exit.
- `output_dir`: expand `~`; create the folder if it doesn't exist.
- `capture_app_name`: if `false`, always use `{epoch}.png`.

---

## 6. Filename specification

- App detected + `capture_app_name` true → `{epoch}_{app}.png`
  (e.g. `1725609600_Google-Chrome.png`)
- App unknown, or `capture_app_name` false → `{epoch}.png`
- **App-name sanitization** (app names contain spaces / punctuation):
  ```python
  import re
  def sanitize(name: str) -> str:
      return re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-') or 'unknown'
  ```
- Always `.png`, always in `output_dir`.

---

## 7. Build plan — checkpoint by checkpoint

> Each checkpoint is independently verifiable. **Do not start the next checkpoint
> until the current one's acceptance criteria pass.** Commit at each checkpoint.

### CP0 — Scaffold + permission smoke test *(de-risk first)*
**Goal:** Prove screenshot capture and app detection actually work on this machine
before building anything.
**Tasks:**
- Create project: `venv`, `requirements.txt` (`pyobjc-framework-Cocoa`), `README.md`,
  a `src/` folder, `.gitignore` (ignore `captures/`, `venv/`, `*.log`).
- Write `smoke_test.py` that: takes ONE screenshot via
  `screencapture -x -t png /tmp/_smoke.png`, prints the frontmost app name, and prints
  the saved path + file size.
**Acceptance:**
- Running it produces a **non-blank** PNG (I open it and see my actual windows).
- The correct active app name is printed.
- If the image is blank, the README explains exactly how to grant Screen Recording
  permission to the terminal being used, and the script prints a warning pointing there.

### CP1 — Single-capture module
**Goal:** `capture.py` with `take_screenshot(output_dir) -> Path`.
**Tasks:** create `output_dir` if missing; capture main display to `{epoch}.png`;
return the path; raise a clear error if `screencapture` fails.
**Acceptance:** calling it once writes exactly one `{epoch}.png`; function returns the
correct path; missing folder is created automatically.

### CP2 — Active-app detection + filename
**Goal:** `active_app.py` returns the sanitized frontmost app name; wire it into the
filename.
**Tasks:** use `NSWorkspace.sharedWorkspace().frontmostApplication().localizedName()`;
sanitize; on any failure return `None`. Update capture to produce `{epoch}_{app}.png`,
falling back to `{epoch}.png` when app is `None` or `capture_app_name` is false.
**Acceptance:** filenames include the app name; switching the active app before a
capture changes the name in the filename; if detection returns `None`, filename is
epoch-only (no crash).

### CP3 — Config loader + validation
**Goal:** `config.py` loads `config.json`, applies defaults, validates.
**Tasks:** expand `~` in `output_dir`; enforce `interval_seconds` in [10, 300] with a
descriptive error; provide defaults if the file is missing (and write a default file).
**Acceptance:** an interval of `5` or `600` is rejected with a readable message and
non-zero exit; a valid config loads and is reflected in behavior; toggling
`capture_app_name` works.

### CP4 — Continuous loop (foreground)
**Goal:** `main.py` runs the capture on the configured cadence, in the foreground.
**Tasks:**
- Loop: capture → sleep. Use **drift-corrected** timing (target next-run timestamp,
  not `sleep(interval)`), so cadence stays accurate.
- Structured logging to stdout + a rotating log file (one line per capture: timestamp,
  app, filename, ms taken).
- If a single capture fails, log the error and **continue** — never crash the loop.
- Handle `SIGINT`/`SIGTERM` for graceful shutdown (finish/skip current, log "stopped").
**Acceptance:** running `python main.py` captures at the correct interval for several
cycles; Ctrl-C exits cleanly with a "stopped" log line; a forced single-capture failure
is logged but the loop keeps going.

### CP5 — Run forever in the background (launchd)
**Goal:** a per-user LaunchAgent that runs at login, restarts on crash, logs to a file.
**Tasks:**
- Generate `com.<you>.screenshotdaemon.plist` with `RunAtLoad=true`, `KeepAlive=true`,
  absolute paths to the venv python and `main.py`, and `StandardOutPath`/`StandardErrorPath`.
- `install.sh` (copy plist to `~/Library/LaunchAgents/`, `launchctl load`) and
  `uninstall.sh` (`launchctl unload`, remove plist).
- README section: **grant Screen Recording permission to the venv's python binary**
  (this is a *different* TCC identity than Terminal — see §4). Include how to verify.
**Acceptance:**
- After install, screenshots appear automatically with no terminal open.
- Survives logout/login and a reboot.
- Killing the process → launchd restarts it within seconds.
- Images are **non-blank** once permission is granted to the daemon's python.
- `uninstall.sh` fully stops it (no more captures).

### CP6 — *(Optional stretch)* resilience & retention
- Optional `max_files` or `max_age_days` cleanup of old captures.
- A `--status` command that prints last capture time + count.
- Only build if CP0–CP5 are solid.

---

## 8. Non-functional requirements
- **Reliability:** a failed capture must never kill the loop.
- **Performance:** each capture cycle should add negligible CPU; capture itself is
  a quick subprocess call.
- **Timing accuracy:** actual cadence should stay within ~1s of the configured interval
  over time (drift-corrected sleep).
- **Observability:** every capture and every error is logged with a timestamp.
- **Clean teardown:** install/uninstall are reversible; no orphaned processes.

---

## 9. Definition of done (v1)
Configure `interval_seconds` (10–300), run the installer once, and thereafter — with no
terminal open, across reboots — a folder fills with non-blank PNGs named `{epoch}.png`
or `{epoch}_{app}.png` at the configured cadence, with a readable log of activity, and a
one-command uninstall.

---

## 10. Kick-off prompt to paste into Claude Code

> Build a macOS background screenshot daemon in Python 3.10+, following the attached
> PRD. Work **checkpoint by checkpoint (CP0 → CP5)**; at the end of each checkpoint,
> stop and show me how to verify its acceptance criteria before continuing. Start with
> **CP0**: scaffold the project (venv, `requirements.txt` with only
> `pyobjc-framework-Cocoa`, `.gitignore`, `README.md`, `src/`) and write a
> `smoke_test.py` that takes one screenshot via `screencapture -x -t png` and prints the
> frontmost app name from `NSWorkspace`. The single biggest risk is the macOS Screen
> Recording permission — surface a clear warning and README instructions if the captured
> image looks blank. Do not build the loop or the launchd agent yet. Confirm CP0 works,
> then proceed.
