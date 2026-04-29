#!/bin/bash
set -euo pipefail

PLIST_NAME="com.feishu-claude-bridge.plist"
BRIDGE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents/${PLIST_NAME}"
LOG_DIR="${BRIDGE_DIR}/logs"
WRAPPER="${BRIDGE_DIR}/scripts/bridge-wrapper.sh"

mkdir -p "$LOG_DIR"

# Make wrapper executable
chmod +x "$WRAPPER"

# Generate plist with correct paths
cat > "$DEST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.feishu-claude-bridge</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${WRAPPER}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${BRIDGE_DIR}</string>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/bridge.stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/bridge.stderr.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
PLIST

# Load (will start immediately due to KeepAlive)
launchctl load "$DEST"

echo "Installed and started: $PLIST_NAME"
echo "Logs: $LOG_DIR/"
echo "Stop: launchctl unload $DEST"
