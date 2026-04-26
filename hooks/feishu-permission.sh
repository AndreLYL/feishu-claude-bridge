#!/usr/bin/env bash
# Claude Code stop hook: sends permission requests to the bridge's hook server.
# Reads tool info from stdin (JSON), posts to hook server, polls for result.
#
# Exit codes:
#   0 = allow (tool proceeds)
#   2 = deny (tool blocked, error fed back to Claude)

HOOK_PORT="${FEISHU_BRIDGE_HOOK_PORT:-19280}"
HOOK_URL="http://127.0.0.1:${HOOK_PORT}"
POLL_INTERVAL=2
TIMEOUT=300  # 5 minutes max wait

# Read tool info from stdin
TOOL_JSON=$(cat)
TOOL_NAME=$(echo "$TOOL_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name','unknown'))" 2>/dev/null)
TOOL_INPUT=$(echo "$TOOL_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('tool_input',{}))[:500])" 2>/dev/null)
REQUEST_ID="perm-$$-$(date +%s)"

# Post permission request
curl -s -X POST "${HOOK_URL}/permission-request" \
  -H "Content-Type: application/json" \
  -d "{\"request_id\": \"${REQUEST_ID}\", \"tool_name\": \"${TOOL_NAME}\", \"tool_input\": \"${TOOL_INPUT}\"}" > /dev/null 2>&1

# If hook server is not running, allow by default (bridge not active)
if [ $? -ne 0 ]; then
  exit 0
fi

# Poll for result
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  RESPONSE=$(curl -s -X POST "${HOOK_URL}/permission-poll" \
    -H "Content-Type: application/json" \
    -d "{\"request_id\": \"${REQUEST_ID}\"}" 2>/dev/null)

  STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
  ACTION=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('action',''))" 2>/dev/null)

  if [ "$STATUS" = "resolved" ]; then
    if [ "$ACTION" = "allow" ]; then
      exit 0
    else
      echo "Denied via Feishu" >&2
      exit 2
    fi
  fi

  sleep $POLL_INTERVAL
  ELAPSED=$((ELAPSED + POLL_INTERVAL))
done

# Timeout: allow by default to avoid blocking
exit 0
