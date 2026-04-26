import json
import threading
import logging
from typing import Dict, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger("bridge.hook")

# Stores pending permission requests: {request_id: threading.Event}
_pending: Dict[str, threading.Event] = {}
_results: Dict[str, str] = {}  # {request_id: "allow" | "deny"}
_lock = threading.Lock()


def create_permission_request(request_id: str) -> threading.Event:
    """Register a pending permission request. Returns an Event that will be set when resolved."""
    event = threading.Event()
    with _lock:
        _pending[request_id] = event
        _results.pop(request_id, None)
    return event


def resolve_permission(request_id: str, action: str) -> None:
    """Resolve a pending permission request (called from Feishu card callback)."""
    with _lock:
        _results[request_id] = action
        event = _pending.pop(request_id, None)
    if event:
        event.set()


def get_result(request_id: str) -> Optional[str]:
    """Get the result of a resolved request."""
    with _lock:
        return _results.pop(request_id, None)


class HookHandler(BaseHTTPRequestHandler):
    """HTTP handler for the stop hook script to communicate with the bridge."""

    # Will be set by start_hook_server
    bridge_ref = None

    def do_POST(self):
        if self.path == "/permission-request":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))

            request_id = body["request_id"]
            tool_name = body["tool_name"]
            tool_input = body.get("tool_input", "")

            # Create pending request
            event = create_permission_request(request_id)

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


def start_hook_server(port: int, bridge_ref) -> threading.Thread:
    """Start the hook HTTP server in a background thread."""
    HookHandler.bridge_ref = bridge_ref
    server = HTTPServer(("127.0.0.1", port), HookHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info(f"Hook server listening on 127.0.0.1:{port}")
    return t
