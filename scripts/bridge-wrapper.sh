#!/bin/bash
# Wrapper script for launchd — loads .env and starts bridge
set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BRIDGE_DIR"

# Load .env
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

exec "${BRIDGE_DIR}/.venv/bin/python" bridge.py --tmux-session "${CLAUDE_BRIDGE_TMUX_SESSION:-claude}"
