import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

from session_monitor import SessionMonitor


def _write_jsonl(path: Path, entries: list):
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def test_thinking_callback_fires():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
        tmp = Path(f.name)

    thinking_received = []
    monitor = SessionMonitor(
        jsonl_path=tmp,
        session_id="test-session-1",
        on_text_message=lambda sid, t: None,
        on_tool_use=lambda sid, t: None,
        on_thinking=lambda sid, text: thinking_received.append((sid, text)),
    )
    monitor._offset = 0

    entry = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "Let me analyze this step by step..."}
            ]
        }
    }
    _write_jsonl(tmp, [entry])
    monitor._check_new_entries()

    assert len(thinking_received) == 1
    assert thinking_received[0][0] == "test-session-1"
    assert "analyze" in thinking_received[0][1]
    tmp.unlink()


def test_heartbeat_callback_fires_on_non_text_entries():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
        tmp = Path(f.name)

    heartbeat_fired = []
    monitor = SessionMonitor(
        jsonl_path=tmp,
        session_id="test-session-2",
        on_text_message=lambda sid, t: None,
        on_tool_use=lambda sid, t: None,
        on_heartbeat=lambda sid: heartbeat_fired.append(sid),
    )
    monitor._offset = 0

    entry = {"type": "tool_result", "content": "some result"}
    _write_jsonl(tmp, [entry])
    monitor._check_new_entries()

    assert len(heartbeat_fired) == 1
    assert heartbeat_fired[0] == "test-session-2"
    tmp.unlink()


def test_stale_session_reposition(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    old_file = project_dir / "old-session.jsonl"
    new_file = project_dir / "new-session.jsonl"

    old_file.write_text('{"type":"user"}\n')
    time.sleep(0.1)
    new_file.write_text('{"type":"user"}\n')

    monitor = SessionMonitor(
        jsonl_path=old_file,
        session_id="test-session-3",
        on_text_message=lambda sid, t: None,
        on_tool_use=lambda sid, t: None,
    )
    monitor._offset = old_file.stat().st_size
    monitor._stale_threshold = 0  # Force immediate stale detection
    monitor._last_data_time = 0  # Force stale

    monitor._check_stale_and_reposition()

    assert monitor.jsonl_path == new_file


def test_text_callback_still_works():
    """Verify existing text callback behavior is preserved."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
        tmp = Path(f.name)

    text_received = []
    monitor = SessionMonitor(
        jsonl_path=tmp,
        session_id="test-session-4",
        on_text_message=lambda sid, t: text_received.append((sid, t)),
        on_tool_use=lambda sid, t: None,
    )
    monitor._offset = 0

    entry = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Hello world"}
            ]
        }
    }
    _write_jsonl(tmp, [entry])
    monitor._check_new_entries()

    assert len(text_received) == 1
    assert text_received[0][0] == "test-session-4"
    assert text_received[0][1] == ["Hello world"]
    tmp.unlink()
