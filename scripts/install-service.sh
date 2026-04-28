#!/bin/bash
set -euo pipefail

PLIST_NAME="com.feishu-claude-bridge.plist"
SRC="$(cd "$(dirname "$0")/.." && pwd)/${PLIST_NAME}"
DEST="$HOME/Library/LaunchAgents/${PLIST_NAME}"
LOG_DIR="$(cd "$(dirname "$0")/.." && pwd)/logs"

mkdir -p "$LOG_DIR"

# Make wrapper executable
chmod +x "$(cd "$(dirname "$0")" && pwd)/bridge-wrapper.sh"

# Copy plist
cp "$SRC" "$DEST"

# Load (will start immediately due to KeepAlive)
launchctl load "$DEST"

echo "Installed and started: $PLIST_NAME"
echo "Logs: $LOG_DIR/"
echo "Stop: launchctl unload $DEST"
