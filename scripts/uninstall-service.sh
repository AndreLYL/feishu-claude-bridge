#!/bin/bash
set -euo pipefail

PLIST_NAME="com.feishu-claude-bridge.plist"
DEST="$HOME/Library/LaunchAgents/${PLIST_NAME}"

if [ -f "$DEST" ]; then
    launchctl unload "$DEST" 2>/dev/null || true
    rm "$DEST"
    echo "Uninstalled: $PLIST_NAME"
else
    echo "Not installed: $DEST not found"
fi
