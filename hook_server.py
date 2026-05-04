import json
import threading
import logging
from typing import Dict, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger("bridge.hook")

# Session-aware permission storage
# Maps session_id -> permission data
_pending_permissions: Dict[str, dict] = {}
# Maps session_id -> threading.Event (for blocking poll)
_permission_events: Dict[str, threading.Event] = {}
# Maps session_id -> decision ("allow" | "deny")
_decisions: Dict[str, str] = {}
# Maps request_id -> session_id (for backward compat)
_request_to_session: Dict[str, str] = {}
_lock = threading.Lock()


def create_permission_request(session_id: str, request_id: str, tool_name: str, tool_input: str) -> threading.Event:
    """Register a pending permission request. Returns an Event that will be set when resolved."""
    event = threading.Event()
    with _lock:
        _pending_permissions[session_id] = {
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
        _permission_events[session_id] = event
        _decisions.pop(session_id, None)
        _request_to_session[request_id] = session_id
    return event


def resolve_permission(session_id: str, action: str) -> None:
    """Resolve a pending permission request by session_id (called from Feishu card callback)."""
    with _lock:
        _decisions[session_id] = action
        _pending_permissions.pop(session_id, None)
        event = _permission_events.pop(session_id, None)
    if event:
        event.set()


def get_result(request_id: str) -> Optional[str]:
    """Get the result of a resolved request by request_id (for backward compat)."""
    with _lock:
        session_id = _request_to_session.get(request_id)
        if session_id and session_id in _decisions:
            decision = _decisions.pop(session_id)
            _request_to_session.pop(request_id, None)
            return decision
    return None


def get_pending_permissions() -> Dict[str, dict]:
    """Get all pending permissions (for Feishu dashboard)."""
    with _lock:
        return _pending_permissions.copy()


class HookHandler(BaseHTTPRequestHandler):
    """HTTP handler for the stop hook script to communicate with the bridge."""

    # Will be set by start_hook_server
    bridge_ref = None

    def do_POST(self):
        if self.path == "/permission-request":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))

            # Extract session_id (fallback to "default" for backward compat)
            session_id = body.get("session_id", "default")
            request_id = body["request_id"]
            tool_name = body["tool_name"]
            tool_input = body.get("tool_input", "")

            # Create pending request
            event = create_permission_request(session_id, request_id, tool_name, tool_input)

            # Push Feishu card (bridge_ref is set at startup)
            if self.bridge_ref:
                self.bridge_ref.send_permission_card(tool_name, tool_input, request_id)

            self._respond(200, {"status": "pending"})

        elif self.path == "/permission-poll":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            request_id = body["request_id"]

            result = get_result(request_id)
            if result:
                self._respond(200, {"status": "resolved", "action": result})
            else:
                self._respond(200, {"status": "pending"})

        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        logger.debug(format % args)


class _ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


def start_hook_server(port: int, bridge_ref) -> threading.Thread:
    """Start the hook HTTP server in a background thread."""
    HookHandler.bridge_ref = bridge_ref
    server = _ReusableHTTPServer(("127.0.0.1", port), HookHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info(f"Hook server listening on 127.0.0.1:{port}")
    return t
