import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from session_manager import SessionManager


class TestSessionManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "sessions.json"
        self.tmux_session = "test-tmux"
        self.work_dir = "."

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("session_manager.TmuxController")
    def test_create_session_normal(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True
        mock_tmux.start_claude.return_value = None

        manager = SessionManager(self.tmux_session, self.store_path, max_sessions=5, work_dir=self.work_dir)
        session_id, session = manager.create_session()

        self.assertEqual(session_id, "s1")
        self.assertEqual(session["name"], "session-1")
        self.assertEqual(session["tmux_window"], "claude-1")
        self.assertEqual(session["state"], "running")
        self.assertIn("created_at", session)
        self.assertIn("updated_at", session)

        mock_tmux.create_window.assert_called_once_with("claude-1")
        mock_tmux.start_claude.assert_called_once_with("claude-1", resume_id=None)

        # Check persistence
        self.assertTrue(self.store_path.exists())
        with open(self.store_path) as f:
            data = json.load(f)
        self.assertEqual(data["active_session"], "s1")
        self.assertEqual(data["counter"], 1)

    @patch("session_manager.TmuxController")
    def test_create_session_with_name(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        manager = SessionManager(self.tmux_session, self.store_path)
        session_id, session = manager.create_session(name="custom")

        self.assertEqual(session["name"], "custom")

    @patch("session_manager.TmuxController")
    def test_create_session_at_max_limit(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        manager = SessionManager(self.tmux_session, self.store_path, max_sessions=2)
        manager.create_session()
        manager.create_session()

        with self.assertRaises(ValueError) as ctx:
            manager.create_session()
        self.assertIn("maximum", str(ctx.exception).lower())

    @patch("session_manager.TmuxController")
    def test_switch_session_valid(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        manager = SessionManager(self.tmux_session, self.store_path)
        manager.create_session(name="first")
        manager.create_session(name="second")

        # Switch to display number 1 (first session by creation time)
        session = manager.switch_session(1)
        self.assertEqual(session["name"], "first")
        self.assertEqual(manager.get_active_session()["name"], "first")

    @patch("session_manager.TmuxController")
    def test_switch_session_invalid_number(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        manager = SessionManager(self.tmux_session, self.store_path)
        manager.create_session()

        with self.assertRaises(ValueError) as ctx:
            manager.switch_session(5)
        self.assertIn("invalid", str(ctx.exception).lower())

    @patch("session_manager.TmuxController")
    def test_delete_session_valid(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True
        mock_tmux.kill_window.return_value = True

        manager = SessionManager(self.tmux_session, self.store_path)
        manager.create_session(name="first")
        manager.create_session(name="second")  # This becomes active

        # Delete display number 1 (first session)
        deleted_name = manager.delete_session(1)
        self.assertEqual(deleted_name, "first")

        sessions, active = manager.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["name"], "second")

        mock_tmux.kill_window.assert_called_once_with("claude-1")

    @patch("session_manager.TmuxController")
    def test_delete_session_cannot_delete_active(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        manager = SessionManager(self.tmux_session, self.store_path)
        manager.create_session(name="first")
        manager.create_session(name="second")  # Active

        # Trying to delete display number 2 (active)
        with self.assertRaises(ValueError) as ctx:
            manager.delete_session(2)
        self.assertIn("active", str(ctx.exception).lower())

    @patch("session_manager.TmuxController")
    def test_delete_session_invalid_number(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        manager = SessionManager(self.tmux_session, self.store_path)
        manager.create_session()

        with self.assertRaises(ValueError) as ctx:
            manager.delete_session(10)
        self.assertIn("invalid", str(ctx.exception).lower())

    @patch("session_manager.TmuxController")
    def test_rename_session(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        manager = SessionManager(self.tmux_session, self.store_path)
        manager.create_session(name="old-name")

        session = manager.rename_session(1, "new-name")
        self.assertEqual(session["name"], "new-name")

        sessions, _ = manager.list_sessions()
        self.assertEqual(sessions[0]["name"], "new-name")

    @patch("session_manager.TmuxController")
    def test_list_sessions_sorted_by_created_at(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        manager = SessionManager(self.tmux_session, self.store_path)
        manager.create_session(name="first")
        manager.create_session(name="second")
        manager.create_session(name="third")

        sessions, active_id = manager.list_sessions()
        self.assertEqual(len(sessions), 3)
        self.assertEqual(sessions[0]["name"], "first")
        self.assertEqual(sessions[1]["name"], "second")
        self.assertEqual(sessions[2]["name"], "third")
        self.assertEqual(active_id, "s3")

    @patch("session_manager.TmuxController")
    def test_resolve_display_number(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        manager = SessionManager(self.tmux_session, self.store_path)
        manager.create_session(name="first")
        manager.create_session(name="second")

        self.assertEqual(manager.resolve_display_number(1), "s1")
        self.assertEqual(manager.resolve_display_number(2), "s2")

    @patch("session_manager.TmuxController")
    def test_recover_marks_missing_windows_as_stopped(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.window_exists.side_effect = lambda name: name != "claude-2"

        # Manually create sessions.json with two sessions
        data = {
            "sessions": {
                "s1": {
                    "id": "s1",
                    "name": "first",
                    "tmux_window": "claude-1",
                    "state": "running",
                    "created_at": "2026-05-03T22:00:00+08:00",
                    "updated_at": "2026-05-03T22:00:00+08:00",
                },
                "s2": {
                    "id": "s2",
                    "name": "second",
                    "tmux_window": "claude-2",
                    "state": "running",
                    "created_at": "2026-05-03T22:01:00+08:00",
                    "updated_at": "2026-05-03T22:01:00+08:00",
                },
            },
            "active_session": "s2",
            "counter": 2,
        }
        with open(self.store_path, "w") as f:
            json.dump(data, f)

        manager = SessionManager(self.tmux_session, self.store_path)
        manager.recover()

        sessions, _ = manager.list_sessions()
        self.assertEqual(sessions[0]["state"], "running")
        self.assertEqual(sessions[1]["state"], "stopped")

    @patch("session_manager.TmuxController")
    def test_persistence_create_reload_intact(self, MockTmux):
        mock_tmux = MockTmux.return_value
        mock_tmux.create_window.return_value = True

        manager1 = SessionManager(self.tmux_session, self.store_path)
        manager1.create_session(name="test-session")

        # Create a new manager instance and load
        manager2 = SessionManager(self.tmux_session, self.store_path)
        sessions, active = manager2.list_sessions()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["name"], "test-session")
        self.assertEqual(active, "s1")


if __name__ == "__main__":
    unittest.main()
