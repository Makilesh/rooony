"""
CP0 smoke test: prove that screenshot capture and frontmost-app detection
actually work on this machine before any real code gets written.

Run with the venv's python:
    venv/bin/python src/smoke_test.py
"""
import subprocess
import sys
from pathlib import Path

SMOKE_PATH = Path("/tmp/_smoke.png")

# A screenshot of just the wallpaper/menu bar (no windows) compresses far
# smaller than one with real window content. This is a heuristic, not proof —
# the acceptance criterion is a human opening the PNG and looking at it.
BLANK_SIZE_THRESHOLD_BYTES = 50_000


def take_screenshot(path: Path) -> None:
    result = subprocess.run(
        ["screencapture", "-x", "-t", "png", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: screencapture failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


def get_frontmost_app_name() -> str | None:
    try:
        from AppKit import NSWorkspace
    except ImportError:
        print(
            "ERROR: pyobjc-framework-Cocoa is not installed in this interpreter. "
            "Run: venv/bin/pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None
    return app.localizedName()


def main() -> None:
    take_screenshot(SMOKE_PATH)

    if not SMOKE_PATH.exists():
        print("ERROR: screencapture reported success but no file was written.", file=sys.stderr)
        sys.exit(1)

    size = SMOKE_PATH.stat().st_size
    app_name = get_frontmost_app_name()

    print(f"Frontmost app: {app_name!r}")
    print(f"Saved screenshot to: {SMOKE_PATH}")
    print(f"File size: {size} bytes")

    if size < BLANK_SIZE_THRESHOLD_BYTES:
        print()
        print("=" * 70)
        print("WARNING: The screenshot file is suspiciously small.")
        print("This usually means macOS Screen Recording permission has NOT")
        print("been granted to the terminal/app running this script, so the")
        print("capture only contains the wallpaper + menu bar (no windows).")
        print()
        print("Open the file yourself to confirm:")
        print(f"    open {SMOKE_PATH}")
        print()
        print("See README.md -> 'Screen Recording permission' for how to fix.")
        print("=" * 70)
    else:
        print()
        print("Looks OK by size heuristic — but still open the file and")
        print("confirm you can see your actual windows, not just wallpaper:")
        print(f"    open {SMOKE_PATH}")


if __name__ == "__main__":
    main()
