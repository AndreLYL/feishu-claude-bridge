"""Tests for SessionMonitor session_id passing."""
import json
import tempfile
import time
from pathlib import Path
from typing import List, Dict

import pytest

from session_monitor import SessionMonitor


def test_on_text_message_receives_session_id():
    """Test that on_text_message callback receives (session_id, text_blocks)."""
    # Create a temp JSONL file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        jsonl_path = Path(f.name)

    session_id = "test-session-123"
    received_calls = []

    def callback(sid: str, text_blocks: List[str]):
        received_calls.append((sid, text_blocks))

    monitor = SessionMonitor(
        jsonl_path=jsonl_path,
        session_id=session_id,
        on_text_message=callback,
        on_tool_use=lambda sid, tools: None,
        poll_interval=0.1,
    )

    # Write an assistant message with text
    with open(jsonl_path, "a") as f:
        entry = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "World"},
                ]
            },
        }
        f.write(json.dumps(entry) + "\n")

    thread = monitor.start()
    time.sleep(0.3)  # Wait for polling
    monitor.stop()
    thread.join(timeout=1.0)

    # Cleanup
    jsonl_path.unlink()

    # Assert
    assert len(received_calls) == 1
    assert received_calls[0][0] == session_id
    assert received_calls[0][1] == ["Hello", "World"]


def test_on_tool_use_receives_session_id():
    """Test that on_tool_use callback receives (session_id, tool_uses)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        jsonl_path = Path(f.name)

    session_id = "test-session-456"
    received_calls = []

    def callback(sid: str, tool_uses: List[Dict[str, str]]):
        received_calls.append((sid, tool_uses))

    monitor = SessionMonitor(
        jsonl_path=jsonl_path,
        session_id=session_id,
        on_text_message=lambda sid, text: None,
        on_tool_use=callback,
        poll_interval=0.1,
    )

    # Write an assistant message with tool_use
    with open(jsonl_path, "a") as f:
        entry = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.txt"},
                    }
                ]
            },
        }
        f.write(json.dumps(entry) + "\n")

    thread = monitor.start()
    time.sleep(0.3)
    monitor.stop()
    thread.join(timeout=1.0)

    jsonl_path.unlink()

    assert len(received_calls) == 1
    assert received_calls[0][0] == session_id
    assert len(received_calls[0][1]) == 1
    assert received_calls[0][1][0]["name"] == "Read"


def test_on_thinking_receives_session_id():
    """Test that on_thinking callback receives (session_id, thinking_text)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        jsonl_path = Path(f.name)

    session_id = "test-session-789"
    received_calls = []

    def callback(sid: str, thinking: str):
        received_calls.append((sid, thinking))

    monitor = SessionMonitor(
        jsonl_path=jsonl_path,
        session_id=session_id,
        on_text_message=lambda sid, text: None,
        on_tool_use=lambda sid, tools: None,
        on_thinking=callback,
        poll_interval=0.1,
    )

    # Write an assistant message with thinking
    with open(jsonl_path, "a") as f:
        entry = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Let me think about this..."}
                ]
            },
        }
        f.write(json.dumps(entry) + "\n")

    thread = monitor.start()
    time.sleep(0.3)
    monitor.stop()
    thread.join(timeout=1.0)

    jsonl_path.unlink()

    assert len(received_calls) == 1
    assert received_calls[0][0] == session_id
    assert "think about this" in received_calls[0][1]


def test_on_turn_end_receives_session_id():
    """Test that on_turn_end callback receives (session_id,)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        jsonl_path = Path(f.name)

    session_id = "test-session-abc"
    received_calls = []

    def callback(sid: str):
        received_calls.append((sid,))

    monitor = SessionMonitor(
        jsonl_path=jsonl_path,
        session_id=session_id,
        on_text_message=lambda sid, text: None,
        on_tool_use=lambda sid, tools: None,
        on_turn_end=callback,
        poll_interval=0.1,
    )

    # Write a user/human message to trigger turn_end
    with open(jsonl_path, "a") as f:
        entry = {"type": "human", "message": {"content": "test"}}
        f.write(json.dumps(entry) + "\n")

    thread = monitor.start()
    time.sleep(0.3)
    monitor.stop()
    thread.join(timeout=1.0)

    jsonl_path.unlink()

    assert len(received_calls) == 1
    assert received_calls[0][0] == session_id


def test_on_heartbeat_receives_session_id():
    """Test that on_heartbeat callback receives (session_id,)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        jsonl_path = Path(f.name)

    session_id = "test-session-hb"
    received_calls = []

    def callback(sid: str):
        received_calls.append((sid,))

    monitor = SessionMonitor(
        jsonl_path=jsonl_path,
        session_id=session_id,
        on_text_message=lambda sid, text: None,
        on_tool_use=lambda sid, tools: None,
        on_heartbeat=callback,
        poll_interval=0.1,
    )

    # Write an assistant message with NO sendable content (should trigger heartbeat)
    with open(jsonl_path, "a") as f:
        entry = {
            "type": "assistant",
            "message": {"content": []},  # Empty content
        }
        f.write(json.dumps(entry) + "\n")

    thread = monitor.start()
    time.sleep(0.3)
    monitor.stop()
    thread.join(timeout=1.0)

    jsonl_path.unlink()

    assert len(received_calls) == 1
    assert received_calls[0][0] == session_id


def test_stop_method_works():
    """Test that stop() method terminates the polling thread."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        jsonl_path = Path(f.name)

    monitor = SessionMonitor(
        jsonl_path=jsonl_path,
        session_id="test-stop",
        on_text_message=lambda sid, text: None,
        on_tool_use=lambda sid, tools: None,
        poll_interval=0.1,
    )

    thread = monitor.start()
    assert thread.is_alive()

    monitor.stop()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    jsonl_path.unlink()
