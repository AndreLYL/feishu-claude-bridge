#!/bin/bash
# Wrapper script for launchd — loads .env and starts bridge
set -euo pipefail

cd /Users/yinglong.li/feishu-claude-bridge

# Load .env
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

exec /Users/yinglong.li/feishu-claude-bridge/.venv/bin/python bridge.py --tmux-session claude
