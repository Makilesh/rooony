"""
CP1/CP2: capture module with active-app detection wired into the filename.

    from capture import take_screenshot
    path = take_screenshot("~/mem/frames")
"""
import subprocess
import time
from pathlib import Path

from active_app import get_active_app_name


class CaptureError(Exception):
    """Raised when screencapture fails to produce an image."""


def take_screenshot(output_dir, capture_app_name: bool = True) -> Path:
    """Capture the main display and return the saved path.

    Filename is `{epoch}_{app}.jpg` when the frontmost app is detected and
    `capture_app_name` is true, otherwise `{epoch}.jpg`. Creates `output_dir`
    if it doesn't exist. Raises CaptureError if the underlying `screencapture`
    call fails or produces no file.
    """
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    epoch = int(time.time())
    app_name = get_active_app_name() if capture_app_name else None
    filename = f"{epoch}_{app_name}.jpg" if app_name else f"{epoch}.jpg"
    path = output_dir / filename

    result = subprocess.run(
        ["screencapture", "-x", "-t", "jpg", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CaptureError(f"screencapture failed: {result.stderr.strip()}")
    if not path.exists():
        raise CaptureError("screencapture reported success but no file was written")

    return path


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "~/mem/frames"
    saved = take_screenshot(target)
    print(f"Saved: {saved} ({saved.stat().st_size} bytes)")
