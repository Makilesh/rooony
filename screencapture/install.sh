#!/usr/bin/env bash
# CP5: install the screenshot daemon as a per-user launchd LaunchAgent.
set -euo pipefail

LABEL="com.thryambak.screenshotdaemon"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_PATH="$SCRIPT_DIR/venv/bin/python3"
MAIN_PY_PATH="$SCRIPT_DIR/src/main.py"
TEMPLATE_PATH="$SCRIPT_DIR/launchd/com.screenshotdaemon.plist.template"

LOG_DIR="$SCRIPT_DIR/logs"
STDOUT_LOG="$LOG_DIR/launchd-stdout.log"
STDERR_LOG="$LOG_DIR/launchd-stderr.log"

PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$PYTHON_PATH" ]; then
    echo "ERROR: venv python not found at $PYTHON_PATH" >&2
    echo "Run: python3 -m venv venv && venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

sed \
    -e "s|__LABEL__|$LABEL|g" \
    -e "s|__PYTHON_PATH__|$PYTHON_PATH|g" \
    -e "s|__MAIN_PY_PATH__|$MAIN_PY_PATH|g" \
    -e "s|__STDOUT_LOG__|$STDOUT_LOG|g" \
    -e "s|__STDERR_LOG__|$STDERR_LOG|g" \
    "$TEMPLATE_PATH" > "$PLIST_DEST"

# Unload first in case it's already installed (e.g. re-running after a change).
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"

echo "Installed and loaded LaunchAgent: $LABEL"
echo "  plist:  $PLIST_DEST"
echo "  python: $PYTHON_PATH"
echo "  stdout: $STDOUT_LOG"
echo "  stderr: $STDERR_LOG"
echo
echo "IMPORTANT: grant Screen Recording permission to the venv's python binary"
echo "(a different TCC identity than Terminal). See README.md for how."
echo
echo "Verify it's running:"
echo "  launchctl list | grep $LABEL"
