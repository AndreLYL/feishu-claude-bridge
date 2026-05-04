"""Tests for TmuxController window management methods."""
import subprocess
from unittest.mock import Mock, patch
import pytest

from tmux_controller import TmuxController


class TestWindowManagement:
    """Test window creation, deletion, and existence checks."""

    def test_create_window_success(self):
        """Test creating a new tmux window."""
        controller = TmuxController(session="test-session")

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            result = controller.create_window("chat-123")

            assert result is True
            mock_run.assert_called_once_with(
                ["tmux", "new-window", "-t", "test-session:chat-123", "-n", "chat-123"],
                capture_output=True,
            )

    def test_create_window_failure(self):
        """Test handling of window creation failure."""
        controller = TmuxController(session="test-session")

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=1)

            result = controller.create_window("chat-123")

            assert result is False

    def test_kill_window_success(self):
        """Test killing an existing tmux window."""
        controller = TmuxController(session="test-session")

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            result = controller.kill_window("chat-123")

            assert result is True
            mock_run.assert_called_once_with(
                ["tmux", "kill-window", "-t", "test-session:chat-123"],
                capture_output=True,
            )

    def test_kill_window_failure(self):
        """Test handling of window kill failure."""
        controller = TmuxController(session="test-session")

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=1)

            result = controller.kill_window("chat-123")

            assert result is False

    def test_window_exists_true(self):
        """Test checking if a window exists (positive case)."""
        controller = TmuxController(session="test-session")

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="window-1\nchat-123\nwindow-3\n"
            )

            result = controller.window_exists("chat-123")

            assert result is True
            mock_run.assert_called_once_with(
                ["tmux", "list-windows", "-t", "test-session", "-F", "#{window_name}"],
                capture_output=True,
                text=True,
            )

    def test_window_exists_false(self):
        """Test checking if a window exists (negative case)."""
        controller = TmuxController(session="test-session")

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="window-1\nwindow-2\nwindow-3\n"
            )

            result = controller.window_exists("chat-123")

            assert result is False

    def test_window_exists_command_failure(self):
        """Test handling of list-windows command failure."""
        controller = TmuxController(session="test-session")

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout="")

            result = controller.window_exists("chat-123")

            assert result is False


class TestStartClaude:
    """Test starting Claude in a specific window."""

    def test_start_claude_without_resume_id(self):
        """Test starting Claude without a resume ID."""
        controller = TmuxController(session="test-session")

        with patch('subprocess.run') as mock_run:
            controller.start_claude("chat-123")

            mock_run.assert_called_once_with(
                ["tmux", "send-keys", "-t", "test-session:chat-123", "--", "claude", "Enter"],
                check=True,
            )

    def test_start_claude_with_resume_id(self):
        """Test starting Claude with a resume ID."""
        controller = TmuxController(session="test-session")

        with patch('subprocess.run') as mock_run:
            controller.start_claude("chat-123", resume_id="abc123")

            mock_run.assert_called_once_with(
                ["tmux", "send-keys", "-t", "test-session:chat-123", "--", "claude --resume abc123", "Enter"],
                check=True,
            )

    def test_start_claude_with_none_resume_id(self):
        """Test starting Claude with explicit None resume ID."""
        controller = TmuxController(session="test-session")

        with patch('subprocess.run') as mock_run:
            controller.start_claude("chat-123", resume_id=None)

            mock_run.assert_called_once_with(
                ["tmux", "send-keys", "-t", "test-session:chat-123", "--", "claude", "Enter"],
                check=True,
            )
