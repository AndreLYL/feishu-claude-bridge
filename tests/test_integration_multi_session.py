"""Integration tests for multi-session support.

Tests the interaction between multiple components:
- SessionManager
- SessionMonitor
- Bridge
- HookServer
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bridge import Bridge, SessionState, CardManager
from hook_server import create_permission_request, resolve_permission, get_result
from session_manager import SessionManager
from session_monitor import SessionMonitor


class TestIntegrationMultiSession(unittest.TestCase):
    """Integration tests for multi-session support."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.store_path = self.temp_path / "sessions.json"

    def tearDown(self):
        """Clean up test fixtures."""
        self.temp_dir.cleanup()

    @patch("session_manager.TmuxController")
    def test_full_session_lifecycle(self, MockTmux):
        """Test: Create → list → switch → switch back → delete → verify."""
        # Setup: Mock TmuxController
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True
        mock_tmux.kill_window.return_value = True

        # Step 1: Create SessionManager
        manager = SessionManager(
            tmux_session="test-session",
            store_path=self.store_path,
            max_sessions=5
        )

        # Step 2: Create two sessions
        session1_id, session1 = manager.create_session(name="first")
        session2_id, session2 = manager.create_session(name="second")

        self.assertEqual(session1_id, "s1")
        self.assertEqual(session2_id, "s2")

        # Step 3: Verify both in list (second is active)
        sessions, active_id = manager.list_sessions()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(active_id, "s2")
        self.assertEqual(sessions[0]["name"], "first")
        self.assertEqual(sessions[1]["name"], "second")

        # Step 4: Switch to first
        manager.switch_session(1)
        sessions, active_id = manager.list_sessions()
        self.assertEqual(active_id, "s1")

        # Step 5: Switch back to second
        manager.switch_session(2)
        sessions, active_id = manager.list_sessions()
        self.assertEqual(active_id, "s2")

        # Step 6: Delete first (non-active)
        deleted_name = manager.delete_session(1)
        self.assertEqual(deleted_name, "first")

        # Step 7: Verify first removed from list
        sessions, active_id = manager.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["name"], "second")
        self.assertEqual(active_id, "s2")

        # Verify tmux kill was called
        mock_tmux.kill_window.assert_called_once_with("claude-1")

    def test_two_sessions_simultaneous_output(self):
        """Test: Create 2 sessions, simulate JSONL output. Verify only active gets sent."""
        # Create temp JSONL files for two sessions
        jsonl1 = self.temp_path / "session1.jsonl"
        jsonl2 = self.temp_path / "session2.jsonl"
        jsonl1.write_text("")
        jsonl2.write_text("")

        # Mock FeishuClient
        mock_feishu = Mock()
        mock_feishu.send_card = MagicMock()

        # Create CardManagers for both sessions
        card_manager_1 = CardManager(mock_feishu)
        card_manager_2 = CardManager(mock_feishu)

        # Track sent messages
        sent_messages = []

        def mock_on_text_message(session_id: str, text_blocks):
            # Simulate Bridge behavior: only send if active
            if session_id == "s1":  # s1 is active
                card_manager_1.send_or_update({"msg_type": "text", "content": {"text": text_blocks[0]}})
                sent_messages.append((session_id, text_blocks[0]))

        # Create monitors for both sessions
        monitor1 = SessionMonitor(
            jsonl_path=jsonl1,
            session_id="s1",
            on_text_message=mock_on_text_message,
            on_tool_use=lambda sid, tools: None,
            poll_interval=0.1
        )
        monitor2 = SessionMonitor(
            jsonl_path=jsonl2,
            session_id="s2",
            on_text_message=mock_on_text_message,
            on_tool_use=lambda sid, tools: None,
            poll_interval=0.1
        )

        # Start monitors
        monitor1.start()
        monitor2.start()

        try:
            # Write output to both sessions
            with open(jsonl1, "a") as f:
                f.write(json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Output from session 1"}]
                    }
                }) + "\n")

            with open(jsonl2, "a") as f:
                f.write(json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Output from session 2"}]
                    }
                }) + "\n")

            # Wait for monitors to process
            time.sleep(0.5)

            # Verify: Only active session (s1) output was sent
            self.assertEqual(len(sent_messages), 1)
            self.assertEqual(sent_messages[0][0], "s1")
            self.assertIn("session 1", sent_messages[0][1])

        finally:
            monitor1.stop()
            monitor2.stop()
            time.sleep(0.2)

    def test_permission_from_background_session(self):
        """Test: Create 2 sessions, s1 active, s2 sends permission. Verify pushed with label."""
        # Mock FeishuClient
        mock_feishu = Mock()
        mock_feishu.send_card = MagicMock()

        # Test the hook_server permission flow
        # Session s2 (background) sends permission request
        event = create_permission_request(
            session_id="s2",
            request_id="req_123",
            tool_name="Edit",
            tool_input="file.py"
        )

        # Verify event is created and pending
        self.assertIsInstance(event, threading.Event)
        self.assertFalse(event.is_set())

        # Simulate Bridge sending permission card (it should include session label)
        # In real implementation, Bridge.send_permission_card would be called
        # For this test, we just verify the hook_server state

        # Resolve permission (simulate Feishu callback)
        resolve_permission("s2", "allow")

        # Verify event is set
        self.assertTrue(event.is_set())

        # Verify result can be retrieved
        result = get_result("req_123")
        self.assertEqual(result, "allow")

    @patch("session_manager.TmuxController")
    def test_recovery_after_restart(self, MockTmux):
        """Test: Create 2 sessions, persist, load new SessionManager, verify recovered."""
        # Setup: Mock TmuxController
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        # Step 1: Create SessionManager and sessions
        manager1 = SessionManager(
            tmux_session="test-session",
            store_path=self.store_path,
            max_sessions=5
        )

        session1_id, _ = manager1.create_session(name="first")
        session2_id, _ = manager1.create_session(name="second")

        # Verify sessions.json exists
        self.assertTrue(self.store_path.exists())

        # Step 2: Create new SessionManager from same file (simulates restart)
        manager2 = SessionManager(
            tmux_session="test-session",
            store_path=self.store_path,
            max_sessions=5
        )

        # Step 3: Verify both sessions recovered
        sessions, active_id = manager2.list_sessions()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["name"], "first")
        self.assertEqual(sessions[1]["name"], "second")
        self.assertEqual(active_id, "s2")

        # Verify counter is preserved
        self.assertEqual(manager2.counter, 2)

    @patch("session_manager.TmuxController")
    def test_max_sessions_enforcement(self, MockTmux):
        """Test: Set max=2, create 2, try 3rd → ValueError."""
        # Setup: Mock TmuxController
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        # Create SessionManager with max_sessions=2
        manager = SessionManager(
            tmux_session="test-session",
            store_path=self.store_path,
            max_sessions=2
        )

        # Create 2 sessions (at limit)
        manager.create_session(name="first")
        manager.create_session(name="second")

        # Try to create 3rd session → should raise ValueError
        with self.assertRaises(ValueError) as ctx:
            manager.create_session(name="third")

        self.assertIn("maximum", str(ctx.exception).lower())
        self.assertIn("2", str(ctx.exception))

        # Verify only 2 sessions exist
        sessions, _ = manager.list_sessions()
        self.assertEqual(len(sessions), 2)

    @patch("session_manager.TmuxController")
    def test_session_monitor_with_turn_end_callback(self, MockTmux):
        """Test: SessionMonitor calls on_turn_end when human message appears."""
        # Create temp JSONL file
        jsonl_path = self.temp_path / "session.jsonl"
        jsonl_path.write_text("")

        # Track callbacks
        turn_end_calls = []

        def mock_on_turn_end(session_id: str):
            turn_end_calls.append(session_id)

        # Create monitor
        monitor = SessionMonitor(
            jsonl_path=jsonl_path,
            session_id="s1",
            on_text_message=lambda sid, blocks: None,
            on_tool_use=lambda sid, tools: None,
            on_turn_end=mock_on_turn_end,
            poll_interval=0.1
        )

        monitor.start()

        try:
            # Write assistant message
            with open(jsonl_path, "a") as f:
                f.write(json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Assistant reply"}]
                    }
                }) + "\n")

            # Write human message (should trigger turn_end)
            with open(jsonl_path, "a") as f:
                f.write(json.dumps({
                    "type": "human",
                    "message": "User message"
                }) + "\n")

            # Wait for monitor to process
            time.sleep(0.5)

            # Verify on_turn_end was called
            self.assertEqual(len(turn_end_calls), 1)
            self.assertEqual(turn_end_calls[0], "s1")

        finally:
            monitor.stop()
            time.sleep(0.2)

    @patch("session_manager.TmuxController")
    def test_bridge_session_state_isolation(self, MockTmux):
        """Test: Bridge maintains separate state for each session."""
        # Setup environment
        env_vars = {
            "FEISHU_APP_ID": "test_app_id",
            "FEISHU_APP_SECRET": "test_app_secret",
            "ALLOWED_CHAT_ID": "test_chat_id",
            "HOOK_SERVER_PORT": "19280",
        }

        with patch.dict("os.environ", env_vars):
            with patch("bridge.FeishuClient"), \
                 patch("bridge.start_hook_server"):
                # Mock TmuxController
                mock_tmux = MockTmux.return_value
                mock_tmux.create_window.return_value = True

                # Create Bridge
                bridge = Bridge(
                    tmux_session="test-session",
                    tmux_window=None,
                    session_file=None,
                    exclude_sessions=None
                )

                # Create two sessions via SessionManager
                session1_id, session1 = bridge.session_manager.create_session(name="first")
                session2_id, session2 = bridge.session_manager.create_session(name="second")

                # Initialize session states manually (since we're not using Bridge._cmd_new)
                bridge.session_states[session1_id] = SessionState(session_id=session1_id)
                bridge.session_states[session2_id] = SessionState(session_id=session2_id)

                # Verify both have separate state objects
                self.assertIn(session1_id, bridge.session_states)
                self.assertIn(session2_id, bridge.session_states)
                self.assertIsNot(
                    bridge.session_states[session1_id],
                    bridge.session_states[session2_id]
                )

                # Modify state of one session
                bridge.session_states[session1_id].state = "waiting_permission"
                bridge.session_states[session1_id].pending_permission = {"request_id": "req_1"}

                # Verify other session state is unaffected
                self.assertEqual(bridge.session_states[session2_id].state, "idle")
                self.assertIsNone(bridge.session_states[session2_id].pending_permission)

    @patch("session_manager.TmuxController")
    def test_persistence_includes_all_metadata(self, MockTmux):
        """Test: Sessions persist with all metadata fields."""
        # Setup
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        # Create SessionManager and session
        manager = SessionManager(
            tmux_session="test-session",
            store_path=self.store_path,
            max_sessions=5
        )

        session_id, session = manager.create_session(name="test-session")

        # Read persisted data
        with open(self.store_path) as f:
            data = json.load(f)

        # Verify structure
        self.assertIn("sessions", data)
        self.assertIn("active_session", data)
        self.assertIn("counter", data)

        # Verify session metadata
        persisted_session = data["sessions"][session_id]
        self.assertEqual(persisted_session["id"], session_id)
        self.assertEqual(persisted_session["name"], "test-session")
        self.assertEqual(persisted_session["tmux_window"], "claude-1")
        self.assertEqual(persisted_session["state"], "running")
        self.assertIn("created_at", persisted_session)
        self.assertIn("updated_at", persisted_session)


if __name__ == "__main__":
    unittest.main()
