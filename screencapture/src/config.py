"""
CP3: config loader + validation.

    from config import load_config
    cfg = load_config()  # dict with interval_seconds, output_dir, capture_app_name
"""
import json
import sys
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

DEFAULTS = {
    "interval_seconds": 60,
    "output_dir": "~/mem/frames",
    "capture_app_name": True,
}

MIN_INTERVAL = 10
MAX_INTERVAL = 300


class ConfigError(Exception):
    """Raised when config.json contains an invalid value."""


def load_config(path=DEFAULT_CONFIG_PATH) -> dict:
    """Load config from `path`, applying defaults and validating.

    If the file doesn't exist, writes it with DEFAULTS and returns DEFAULTS.
    Raises ConfigError (readable message) if interval_seconds is out of
    [10, 300]. `output_dir` is returned with `~` expanded.
    """
    path = Path(path)

    if not path.exists():
        path.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
        cfg = dict(DEFAULTS)
    else:
        with path.open() as f:
            raw = json.load(f)
        cfg = {**DEFAULTS, **raw}

    interval = cfg["interval_seconds"]
    if not isinstance(interval, int) or not (MIN_INTERVAL <= interval <= MAX_INTERVAL):
        raise ConfigError(
            f"interval_seconds must be an integer between {MIN_INTERVAL} and "
            f"{MAX_INTERVAL} inclusive, got: {interval!r}"
        )

    cfg["output_dir"] = str(Path(cfg["output_dir"]).expanduser())

    return cfg


if __name__ == "__main__":
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(cfg, indent=2))
