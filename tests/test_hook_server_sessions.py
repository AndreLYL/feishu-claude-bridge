"""Tests for session-aware hook server functionality."""
import json
import time
import threading
import urllib.request
import urllib.error
from typing import Optional
from http.server import HTTPServer
from unittest.mock import Mock

import pytest

from hook_server import HookHandler, _ReusableHTTPServer


class TestHookServerSessions:
    """Test multi-session permission handling."""

    @pytest.fixture
    def hook_server(self):
        """Start hook server in background thread for testing."""
        # Mock bridge reference
        bridge_ref = Mock()
        HookHandler.bridge_ref = bridge_ref

        # Start server
        server = _ReusableHTTPServer(("127.0.0.1", 0), HookHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        yield port, bridge_ref

        # Cleanup
        server.shutdown()

    def _post_json(self, port: int, path: str, data: dict) -> dict:
        """Helper to POST JSON and get JSON response."""
        url = f"http://127.0.0.1:{port}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())

    def test_post_permission_with_session_id(self, hook_server):
        """Test that POST /permission-request stores session_id correctly."""
        port, bridge_ref = hook_server

        response = self._post_json(
            port,
            "/permission-request",
            {
                "session_id": "session-1",
                "request_id": "req-1",
                "tool_name": "Bash",
                "tool_input": "ls -la",
            },
        )

        assert response["status"] == "pending"

        # Bridge should have been called with request_id
        bridge_ref.send_permission_card.assert_called_once()
        call_args = bridge_ref.send_permission_card.call_args[0]
        assert call_args[0] == "Bash"
        assert call_args[2] == "req-1"

    def test_multiple_sessions_simultaneous_permissions(self, hook_server):
        """Test multiple sessions can have pending permissions at once."""
        port, bridge_ref = hook_server

        # Session 1 requests permission
        response1 = self._post_json(
            port,
            "/permission-request",
            {
                "session_id": "session-1",
                "request_id": "req-1",
                "tool_name": "Bash",
                "tool_input": "rm -rf /",
            },
        )
        assert response1["status"] == "pending"

        # Session 2 requests permission
        response2 = self._post_json(
            port,
            "/permission-request",
            {
                "session_id": "session-2",
                "request_id": "req-2",
                "tool_name": "Edit",
                "tool_input": "sensitive.txt",
            },
        )
        assert response2["status"] == "pending"

        # Both sessions should be tracked
        assert bridge_ref.send_permission_card.call_count == 2

    def test_resolve_permission_resolves_correct_session(self, hook_server):
        """Test that resolving one session doesn't affect others."""
        port, bridge_ref = hook_server

        # Create two pending permissions
        self._post_json(
            port,
            "/permission-request",
            {
                "session_id": "session-1",
                "request_id": "req-1",
                "tool_name": "Bash",
                "tool_input": "ls",
            },
        )

        self._post_json(
            port,
            "/permission-request",
            {
                "session_id": "session-2",
                "request_id": "req-2",
                "tool_name": "Edit",
                "tool_input": "file.txt",
            },
        )

        # Import resolve_permission function
        from hook_server import resolve_permission

        # Resolve session-1
        resolve_permission("session-1", "allow")

        # Poll session-1 should return resolved
        response1 = self._post_json(
            port,
            "/permission-poll",
            {"request_id": "req-1"},
        )
        assert response1["status"] == "resolved"
        assert response1["action"] == "allow"

        # Poll session-2 should still be pending
        response2 = self._post_json(
            port,
            "/permission-poll",
            {"request_id": "req-2"},
        )
        assert response2["status"] == "pending"

    def test_poll_session_waits_and_returns_decision(self, hook_server):
        """Test that polling eventually returns decision when resolved (via polling loop)."""
        port, bridge_ref = hook_server

        # Create pending permission
        self._post_json(
            port,
            "/permission-request",
            {
                "session_id": "session-1",
                "request_id": "req-1",
                "tool_name": "Bash",
                "tool_input": "echo test",
            },
        )

        # Import resolve_permission
        from hook_server import resolve_permission

        # Poll immediately - should be pending
        response = self._post_json(
            port,
            "/permission-poll",
            {"request_id": "req-1"},
        )
        assert response["status"] == "pending"

        # Resolve the permission
        resolve_permission("session-1", "deny")

        # Poll again - should now be resolved
        response = self._post_json(
            port,
            "/permission-poll",
            {"request_id": "req-1"},
        )
        assert response["status"] == "resolved"
        assert response["action"] == "deny"

    def test_backward_compatibility_missing_session_id(self, hook_server):
        """Test that missing session_id uses 'default' as fallback."""
        port, bridge_ref = hook_server

        # POST without session_id
        response = self._post_json(
            port,
            "/permission-request",
            {
                "request_id": "req-1",
                "tool_name": "Bash",
                "tool_input": "pwd",
            },
        )

        assert response["status"] == "pending"

        # Should still work with default session
        from hook_server import resolve_permission
        resolve_permission("default", "allow")

        poll_response = self._post_json(
            port,
            "/permission-poll",
            {"request_id": "req-1"},
        )
        assert poll_response["status"] == "resolved"
        assert poll_response["action"] == "allow"

    def test_get_pending_permissions(self, hook_server):
        """Test get_pending_permissions returns all pending permissions."""
        port, bridge_ref = hook_server

        # Create multiple pending permissions
        self._post_json(
            port,
            "/permission-request",
            {
                "session_id": "session-1",
                "request_id": "req-1",
                "tool_name": "Bash",
                "tool_input": "ls",
            },
        )

        self._post_json(
            port,
            "/permission-request",
            {
                "session_id": "session-2",
                "request_id": "req-2",
                "tool_name": "Edit",
                "tool_input": "file.txt",
            },
        )

        # Import and call get_pending_permissions
        from hook_server import get_pending_permissions
        pending = get_pending_permissions()

        assert "session-1" in pending
        assert "session-2" in pending
        assert pending["session-1"]["tool_name"] == "Bash"
        assert pending["session-2"]["tool_name"] == "Edit"
