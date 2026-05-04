"""Tests for session list and info formatters."""

import pytest
from formatter import format_session_list, format_session_info


class TestFormatSessionList:
    """Tests for format_session_list function."""

    def test_single_session_active(self):
        """Test single session marked as active."""
        sessions = [
            {
                "id": "session-1",
                "name": "main",
                "state": "idle",
                "created_at": "2026-05-04T10:00:00",
            }
        ]

        card = format_session_list(sessions, "session-1")

        assert card["msg_type"] == "interactive"
        assert card["card"]["header"]["title"]["content"] == "📋 Sessions (1)"
        assert card["card"]["header"]["template"] == "blue"

        content = card["card"]["elements"][0]["content"]
        assert "▶ 1. 📌 main · idle · 2026-05-04T10:00:00" in content
        assert "/switch <n> 切换 · /delete <n> 删除" in content

    def test_multiple_sessions_with_active(self):
        """Test multiple sessions with one active."""
        sessions = [
            {
                "id": "session-1",
                "name": "main",
                "state": "idle",
                "created_at": "2026-05-04T10:00:00",
            },
            {
                "id": "session-2",
                "name": "feature-x",
                "state": "busy",
                "created_at": "2026-05-04T11:00:00",
            },
            {
                "id": "session-3",
                "name": "debug",
                "state": "idle",
                "created_at": "2026-05-04T09:00:00",
            }
        ]

        card = format_session_list(sessions, "session-2")

        assert card["msg_type"] == "interactive"
        assert card["card"]["header"]["title"]["content"] == "📋 Sessions (3)"

        content = card["card"]["elements"][0]["content"]
        # session-3 should be first (oldest created_at)
        # session-1 should be second
        # session-2 should be third (newest) and marked active
        assert "◻ 1. 📌 debug · idle · 2026-05-04T09:00:00" in content
        assert "◻ 2. 📌 main · idle · 2026-05-04T10:00:00" in content
        assert "▶ 3. 📌 feature-x · busy · 2026-05-04T11:00:00" in content

    def test_empty_session_list(self):
        """Test empty session list."""
        card = format_session_list([], None)

        assert card["msg_type"] == "interactive"
        assert card["card"]["header"]["title"]["content"] == "📋 Sessions (0)"

        content = card["card"]["elements"][0]["content"]
        assert "No active sessions" in content or content == "/switch <n> 切换 · /delete <n> 删除"

    def test_no_active_session(self):
        """Test session list with no active session."""
        sessions = [
            {
                "id": "session-1",
                "name": "main",
                "state": "idle",
                "created_at": "2026-05-04T10:00:00",
            },
            {
                "id": "session-2",
                "name": "feature-x",
                "state": "idle",
                "created_at": "2026-05-04T11:00:00",
            }
        ]

        card = format_session_list(sessions, None)

        content = card["card"]["elements"][0]["content"]
        # Both should have inactive marker
        assert "◻ 1. 📌 main" in content
        assert "◻ 2. 📌 feature-x" in content
        assert "▶" not in content


class TestFormatSessionInfo:
    """Tests for format_session_info function."""

    def test_full_session_info(self):
        """Test session info with all fields."""
        session = {
            "id": "session-abc123",
            "name": "main",
            "state": "idle",
            "created_at": "2026-05-04T10:00:00",
            "tmux_window": "claude:1",
            "jsonl_path": "/path/to/session.jsonl",
        }

        card = format_session_info(session)

        assert card["msg_type"] == "interactive"
        assert card["card"]["header"]["title"]["content"] == "📌 Session: main"
        assert card["card"]["header"]["template"] == "blue"

        content = card["card"]["elements"][0]["content"]
        assert "ID: session-abc123" in content
        assert "State: idle" in content
        assert "Window: claude:1" in content
        assert "Created: 2026-05-04T10:00:00" in content

    def test_minimal_session_info(self):
        """Test session info with only required fields."""
        session = {
            "id": "session-xyz",
            "name": "test",
            "state": "busy",
            "created_at": "2026-05-04T12:00:00",
        }

        card = format_session_info(session)

        assert card["msg_type"] == "interactive"
        assert card["card"]["header"]["title"]["content"] == "📌 Session: test"

        content = card["card"]["elements"][0]["content"]
        assert "ID: session-xyz" in content
        assert "State: busy" in content
        assert "Created: 2026-05-04T12:00:00" in content

    def test_session_info_card_structure(self):
        """Test that session info card follows standard structure."""
        session = {
            "id": "session-1",
            "name": "main",
            "state": "idle",
            "created_at": "2026-05-04T10:00:00",
        }

        card = format_session_info(session)

        # Verify standard Feishu card structure
        assert "msg_type" in card
        assert "card" in card
        assert "header" in card["card"]
        assert "elements" in card["card"]
        assert "title" in card["card"]["header"]
        assert "template" in card["card"]["header"]
        assert len(card["card"]["elements"]) > 0
        assert card["card"]["elements"][0]["tag"] == "markdown"
