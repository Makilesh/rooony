"""
CP4: continuous capture loop (foreground).
CP6 (optional stretch): retention cleanup + --status.

    venv/bin/python src/main.py
    venv/bin/python src/main.py --status
"""
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from capture import CaptureError, take_screenshot
from config import ConfigError, load_config
from retention import enforce_retention

LOG_PATH = Path(__file__).resolve().parent.parent / "daemon.log"

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("screenshot_daemon")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


def run(cfg: dict, logger: logging.Logger) -> None:
    interval = cfg["interval_seconds"]
    output_dir = cfg["output_dir"]
    capture_app_name = cfg["capture_app_name"]
    max_files = cfg["max_files"]
    max_age_days = cfg["max_age_days"]

    next_run = time.monotonic()

    while not _shutdown_requested:
        start = time.monotonic()
        try:
            path = take_screenshot(output_dir, capture_app_name=capture_app_name)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.info("captured file=%s took_ms=%d", path.name, elapsed_ms)
            if max_files is not None or max_age_days is not None:
                deleted = enforce_retention(output_dir, max_files=max_files, max_age_days=max_age_days)
                if deleted:
                    logger.info("retention: deleted %d old file(s)", deleted)
        except CaptureError as e:
            logger.error("capture failed: %s", e)

        next_run += interval
        sleep_for = next_run - time.monotonic()
        # Sleep in short slices so a SIGINT/SIGTERM during a long sleep is
        # noticed promptly instead of waiting out the full interval.
        while sleep_for > 0 and not _shutdown_requested:
            time.sleep(min(sleep_for, 1))
            sleep_for = next_run - time.monotonic()

        if _shutdown_requested:
            break

        # If a capture (or system sleep) took longer than the interval,
        # don't try to "catch up" with back-to-back captures — resync.
        if next_run < time.monotonic():
            next_run = time.monotonic()

    logger.info("stopped")


def print_status(cfg: dict) -> None:
    output_dir = Path(cfg["output_dir"])
    files = [p for p in output_dir.iterdir() if p.is_file()] if output_dir.is_dir() else []

    print(f"output_dir: {output_dir}")
    print(f"total captures: {len(files)}")

    if files:
        latest = max(files, key=lambda p: p.stat().st_mtime)
        age_s = int(time.time() - latest.stat().st_mtime)
        print(f"last capture: {latest.name} ({age_s}s ago)")
    else:
        print("last capture: none")


def main() -> None:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if "--status" in sys.argv[1:]:
        print_status(cfg)
        return

    logger = setup_logging()

    logger.info(
        "starting: interval=%ds output_dir=%s capture_app_name=%s",
        cfg["interval_seconds"], cfg["output_dir"], cfg["capture_app_name"],
    )

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    run(cfg, logger)


if __name__ == "__main__":
    main()
