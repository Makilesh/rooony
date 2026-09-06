"""
CP6 (optional stretch): retention cleanup for captured files.

    from retention import enforce_retention
    enforce_retention(output_dir, max_files=500, max_age_days=7)
"""
import time
from pathlib import Path


def enforce_retention(output_dir, max_files=None, max_age_days=None) -> int:
    """Delete old captures in `output_dir` per the given limits.

    `max_files`: keep only the N most recently modified files, delete the rest.
    `max_age_days`: delete any file older than this many days.
    Either limit may be None to disable it. Returns the number of files deleted.
    """
    output_dir = Path(output_dir).expanduser()
    if not output_dir.is_dir():
        return 0

    files = sorted(
        (p for p in output_dir.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    to_delete = set()

    if max_age_days is not None:
        cutoff = time.time() - (max_age_days * 86400)
        to_delete.update(p for p in files if p.stat().st_mtime < cutoff)

    if max_files is not None and len(files) > max_files:
        to_delete.update(files[max_files:])

    for p in to_delete:
        p.unlink(missing_ok=True)

    return len(to_delete)
