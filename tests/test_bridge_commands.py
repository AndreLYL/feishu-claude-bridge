"""Tests for Bridge multi-session command routing."""
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bridge import Bridge, SessionState
from formatter import (
    format_session_list,
    format_session_info,
    format_status_notification,
)


class TestBridgeCommands(unittest.TestCase):
    """Test Bridge command routing for multi-session support."""

    def setUp(self):
        """Set up test fixtures."""
        # Mock environment variables
        env_vars = {
            "FEISHU_APP_ID": "test_app_id",
            "FEISHU_APP_SECRET": "test_app_secret",
            "ALLOWED_CHAT_ID": "test_chat_id",
            "HOOK_SERVER_PORT": "19280",
        }

        with patch.dict("os.environ", env_vars):
            with patch("bridge.TmuxController"), \
                 patch("bridge.FeishuClient"), \
                 patch("bridge.start_hook_server"), \
                 patch("bridge.SessionManager") as mock_sm, \
                 patch("bridge.find_session_for_cwd"):

                # Mock SessionManager
                self.mock_session_manager = Mock()
                mock_sm.return_value = self.mock_session_manager

                # Create Bridge instance
                self.bridge = Bridge(
                    tmux_session="test_session",
                    tmux_window=None,
                    session_file=None,
                    exclude_sessions=None
                )

                # Mock FeishuClient methods
                self.bridge.feishu.send_card = MagicMock()
                self.bridge.feishu.send_text = MagicMock()

    def test_cmd_new_creates_session(self):
        """Test /new creates a new session and responds with confirmation."""
        # Setup: SessionManager.create_session returns new session
        self.mock_session_manager.create_session.return_value = (
            "s1",
            {
                "id": "s1",
                "name": "my-session",
                "tmux_window": "claude-1",
                "state": "running",
                "created_at": "2024-01-01T00:00:00Z",
            }
        )

        # Act: Call _handle_feishu_message with /new
        self.bridge._handle_feishu_message("/new my-session")

        # Assert: SessionManager.create_session was called
        self.mock_session_manager.create_session.assert_called_once_with("my-session")

        # Assert: Feishu card was sent
        self.bridge.feishu.send_card.assert_called_once()
        call_args = self.bridge.feishu.send_card.call_args[0][0]
        self.assertEqual(call_args["msg_type"], "interactive")
        self.assertIn("created", call_args["card"]["elements"][0]["content"].lower())

    def test_cmd_new_without_name_uses_default(self):
        """Test /new without name uses default session name."""
        self.mock_session_manager.create_session.return_value = (
            "s1",
            {
                "id": "s1",
                "name": "session-1",
                "tmux_window": "claude-1",
                "state": "running",
                "created_at": "2024-01-01T00:00:00Z",
            }
        )

        self.bridge._handle_feishu_message("/new")

        self.mock_session_manager.create_session.assert_called_once_with(None)

    def test_cmd_list_returns_session_list(self):
        """Test /list returns session list card."""
        # Setup: SessionManager.list_sessions returns sessions
        sessions = [
            {
                "id": "s1",
                "name": "session-1",
                "state": "running",
                "created_at": "2024-01-01T00:00:00Z",
            },
            {
                "id": "s2",
                "name": "session-2",
                "state": "running",
                "created_at": "2024-01-01T01:00:00Z",
            }
        ]
        self.mock_session_manager.list_sessions.return_value = (sessions, "s1")

        # Act
        self.bridge._handle_feishu_message("/list")

        # Assert: Feishu card was sent with session list
        self.bridge.feishu.send_card.assert_called_once()
        call_args = self.bridge.feishu.send_card.call_args[0][0]
        self.assertEqual(call_args["msg_type"], "interactive")
        # Should contain session names
        content = call_args["card"]["elements"][0]["content"]
        self.assertIn("session-1", content)
        self.assertIn("session-2", content)

    def test_cmd_switch_changes_active_session(self):
        """Test /switch changes active session."""
        # Setup
        switched_session = {
            "id": "s2",
            "name": "session-2",
            "tmux_window": "claude-2",
            "state": "running",
            "created_at": "2024-01-01T01:00:00Z",
        }
        self.mock_session_manager.switch_session.return_value = switched_session

        # Act
        self.bridge._handle_feishu_message("/switch 2")

        # Assert: SessionManager.switch_session was called
        self.mock_session_manager.switch_session.assert_called_once_with(2)

        # Assert: Confirmation card was sent
        self.bridge.feishu.send_card.assert_called_once()

    def test_cmd_delete_removes_non_active_session(self):
        """Test /delete removes a non-active session."""
        # Setup
        self.mock_session_manager.delete_session.return_value = "session-2"

        # Act
        self.bridge._handle_feishu_message("/delete 2")

        # Assert: SessionManager.delete_session was called
        self.mock_session_manager.delete_session.assert_called_once_with(2)

        # Assert: Confirmation card was sent
        self.bridge.feishu.send_card.assert_called_once()

    def test_cmd_delete_active_session_fails(self):
        """Test /delete on active session raises error."""
        # Setup: delete_session raises ValueError for active session
        self.mock_session_manager.delete_session.side_effect = ValueError("Cannot delete active session")

        # Act
        self.bridge._handle_feishu_message("/delete 1")

        # Assert: Error card was sent
        self.bridge.feishu.send_card.assert_called_once()
        call_args = self.bridge.feishu.send_card.call_args[0][0]
        # Check for error in content
        content = call_args["card"]["elements"][0]["content"]
        self.assertIn("error", content.lower())

    def test_cmd_rename_changes_session_name(self):
        """Test /rename changes session name."""
        # Setup
        renamed_session = {
            "id": "s1",
            "name": "new-name",
            "tmux_window": "claude-1",
            "state": "running",
            "created_at": "2024-01-01T00:00:00Z",
        }
        self.mock_session_manager.rename_session.return_value = renamed_session

        # Act
        self.bridge._handle_feishu_message("/rename 1 new-name")

        # Assert: SessionManager.rename_session was called
        self.mock_session_manager.rename_session.assert_called_once_with(1, "new-name")

        # Assert: Confirmation card was sent
        self.bridge.feishu.send_card.assert_called_once()

    def test_cmd_current_shows_active_session_info(self):
        """Test /current shows active session info."""
        # Setup
        active_session = {
            "id": "s1",
            "name": "session-1",
            "tmux_window": "claude-1",
            "state": "running",
            "created_at": "2024-01-01T00:00:00Z",
        }
        self.mock_session_manager.get_active_session.return_value = active_session

        # Act
        self.bridge._handle_feishu_message("/current")

        # Assert: Feishu card was sent with session info
        self.bridge.feishu.send_card.assert_called_once()
        call_args = self.bridge.feishu.send_card.call_args[0][0]
        self.assertEqual(call_args["msg_type"], "interactive")
        # Should contain session name
        title = call_args["card"]["header"]["title"]["content"]
        self.assertIn("session-1", title)

    def test_plain_text_goes_to_active_session(self):
        """Test plain text message goes to active session's tmux window."""
        # Setup: active session exists
        active_session = {
            "id": "s1",
            "name": "session-1",
            "tmux_window": "claude-1",
            "state": "running",
        }
        self.mock_session_manager.get_active_session.return_value = active_session
        self.bridge.active_session_id = "s1"

        # Mock TmuxController for active session
        mock_tmux = Mock()
        mock_tmux.is_claude_running.return_value = True
        self.bridge.session_controllers = {"s1": mock_tmux}

        # Mock SessionState for active session
        session_state = SessionState(session_id="s1", state="idle")
        self.bridge.session_states = {"s1": session_state}

        # Mock CardManager
        mock_card_manager = Mock()
        self.bridge.card_managers = {"s1": mock_card_manager}

        # Act
        self.bridge._handle_feishu_message("Hello Claude")

        # Assert: Text was sent to active session's tmux
        mock_tmux.send_text.assert_called_once_with("Hello Claude")

    def test_permission_reply_resolves_via_hook_server(self):
        """Test permission reply (y/n) resolves via hook_server."""
        # Setup: Bridge is in WAITING_PERMISSION state
        session_state = SessionState(
            session_id="s1",
            state="waiting_permission",
            pending_permission={"request_id": "req_123"}
        )
        self.bridge.session_states = {"s1": session_state}
        self.bridge.active_session_id = "s1"

        with patch("bridge.resolve_permission") as mock_resolve:
            # Act: Send "y" (yes)
            self.bridge._handle_feishu_message("y")

            # Assert: resolve_permission was called with "allow"
            mock_resolve.assert_called_once_with("s1", "allow")

            # Assert: State was reset to idle
            self.assertEqual(self.bridge.session_states["s1"].state, "idle")

    def test_esc_command_works_on_active_session(self):
        """Test /esc command sends Escape key to active session."""
        # Setup: active session exists
        active_session = {
            "id": "s1",
            "name": "session-1",
            "tmux_window": "claude-1",
            "state": "running",
        }
        self.mock_session_manager.get_active_session.return_value = active_session
        self.bridge.active_session_id = "s1"

        # Mock TmuxController for active session
        mock_tmux = Mock()
        self.bridge.session_controllers = {"s1": mock_tmux}

        # Act
        self.bridge._handle_feishu_message("/esc")

        # Assert: Escape key was sent
        mock_tmux.send_key.assert_called_once_with("Escape")

    def test_screenshot_command_captures_active_session(self):
        """Test /screenshot captures active session's pane."""
        # Setup: active session exists
        active_session = {
            "id": "s1",
            "name": "session-1",
            "tmux_window": "claude-1",
            "state": "running",
        }
        self.mock_session_manager.get_active_session.return_value = active_session
        self.bridge.active_session_id = "s1"

        # Mock TmuxController for active session
        mock_tmux = Mock()
        mock_tmux.capture_pane.return_value = "Captured screen content"
        self.bridge.session_controllers = {"s1": mock_tmux}

        # Act
        self.bridge._handle_feishu_message("/screenshot")

        # Assert: Pane was captured
        mock_tmux.capture_pane.assert_called_once_with(50)

        # Assert: Screenshot was sent via Feishu
        self.bridge.feishu.send_text.assert_called_once()
        call_args = self.bridge.feishu.send_text.call_args[0][0]
        self.assertIn("Captured screen content", call_args)


if __name__ == "__main__":
    unittest.main()
