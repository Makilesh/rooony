# Screenshot Daemon (macOS)

A background process that captures a screenshot on a configurable interval
(10s–300s) and saves it with the frontmost app's name in the filename.

Final output format (matches the shared `~/mem/frames` convention used by
the rest of the project):

```
~/mem/frames/{unix_ts}_{app_name}.jpg
```

`unix_ts` is the integer Unix epoch timestamp (seconds) at capture time.

Status: **CP0 — scaffold + permission smoke test.** No loop or launchd agent
yet.

---

## Setup

```bash
cd screencapture
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## CP0: run the smoke test

```bash
venv/bin/python src/smoke_test.py
```

This takes ONE screenshot to `/tmp/_smoke.png`, prints the frontmost app
name, and prints the saved path + file size.

**You must open the file and look at it:**

```bash
open /tmp/_smoke.png
```

If you see your actual desktop/windows → permission is working, CP0 passes.
If you see only the wallpaper and menu bar (no windows, no dock) → the
Screen Recording permission has not been granted to the terminal/app
running the script. The script also prints a size-based warning as a
heuristic, but only opening the image is a real confirmation.

---

## Screen Recording permission

macOS requires the **Screen Recording** permission
(System Settings → Privacy & Security → Screen Recording) before
`screencapture` can capture window contents. Without it, screenshots
silently "succeed" but only contain the wallpaper + menu bar.

**Important:** the permission is granted to the specific binary that runs
the code, not to "the script" in the abstract:

- Running `smoke_test.py` from **Terminal.app** → grant permission to
  *Terminal*.
- Running it from **iTerm** → grant permission to *iTerm*.
- Running it from an IDE's integrated terminal (VS Code, etc.) → grant
  permission to that IDE.
- Later, when running under `launchd` (CP5), the **python interpreter
  binary itself** (e.g. `screencapture/venv/bin/python3.x`) needs the
  permission — this is a *different* identity than your terminal app, and
  must be granted separately. Don't be surprised if CP0–CP4 work fine in
  the foreground but CP5 needs its own permission grant.

### How to grant it

1. Open **System Settings → Privacy & Security → Screen Recording**.
2. Click the **+** button (you may need to unlock with your password/Touch ID
   first).
3. Add the app that's running the script (e.g. `Terminal`, `iTerm`, or your
   IDE). If it's already in the list, make sure its toggle is **on**.
4. **Quit and reopen** that app completely (not just the window) — macOS
   caches the permission at process-launch time, so a running process won't
   pick up a newly granted permission until it restarts.
5. Re-run `venv/bin/python src/smoke_test.py` and `open /tmp/_smoke.png`
   again.

If the app you need isn't listed under the **+** picker, run the smoke test
once first (it will trigger macOS to prompt you, or at least register the
app as a candidate), then check the list again.

---

## Project layout

```
screencapture/
├── venv/              # local virtualenv (gitignored)
├── requirements.txt   # only pyobjc-framework-Cocoa
├── .gitignore
├── README.md
└── src/
    └── smoke_test.py  # CP0: one-shot capture + frontmost app print
```
