#!/usr/bin/env bash
# CP5: fully stop and remove the screenshot daemon LaunchAgent.
set -euo pipefail

LABEL="com.thryambak.screenshotdaemon"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -f "$PLIST_DEST" ]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    echo "Uninstalled LaunchAgent: $LABEL"
else
    echo "No LaunchAgent installed at $PLIST_DEST (nothing to do)."
fi

echo "Verify no more captures are happening:"
echo "  launchctl list | grep $LABEL   # should print nothing"
