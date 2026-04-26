import json
import os
import time
import threading
from pathlib import Path
from typing import Callable, Optional, List, Dict

# Claude Code stores sessions at:
# ~/.claude/projects/{project-slug}/{session-uuid}.jsonl
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def find_latest_session(project_slug: Optional[str] = None) -> Optional[Path]:
    """Find the most recently modified JSONL session file."""
    if project_slug:
        search_dirs = [CLAUDE_PROJECTS_DIR / project_slug]
    else:
        search_dirs = [d for d in CLAUDE_PROJECTS_DIR.iterdir() if d.is_dir()]

    latest: Optional[Path] = None
    latest_mtime = 0.0

    for d in search_dirs:
        for f in d.glob("*.jsonl"):
            if f.name == "history.jsonl":
                continue
            mtime = f.stat().st_mtime
            if mtime > latest_mtime:
                latest = f
                latest_mtime = mtime

    return latest


class SessionMonitor:
    """Poll a JSONL file for new assistant messages."""

    def __init__(
        self,
        jsonl_path: Path,
        on_text_message: Callable[[List[str]], None],
        on_tool_use: Callable[[List[Dict[str, str]]], None],
        poll_interval: float = 2.0,
    ):
        self.jsonl_path = jsonl_path
        self.on_text_message = on_text_message
        self.on_tool_use = on_tool_use
        self.poll_interval = poll_interval
        self._offset = 0
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        """Start polling in a background thread."""
        # Seek to end of file so we only get NEW messages
        if self.jsonl_path.exists():
            self._offset = self.jsonl_path.stat().st_size

        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop.set()

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self._check_new_entries()
            except Exception as e:
                print(f"[monitor] error: {e}")
            self._stop.wait(self.poll_interval)

    def _check_new_entries(self):
        if not self.jsonl_path.exists():
            return

        size = self.jsonl_path.stat().st_size
        if size <= self._offset:
            return

        with open(self.jsonl_path, "r") as f:
            f.seek(self._offset)
            new_data = f.read()
            self._offset = f.tell()

        for line in new_data.strip().split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") == "assistant":
                content = entry.get("message", {}).get("content", [])

                # Extract text blocks
                text_blocks = [
                    c["text"] for c in content if c.get("type") == "text" and c.get("text")
                ]

                # Extract tool_use blocks
                tool_uses = []
                for c in content:
                    if c.get("type") == "tool_use":
                        name = c.get("name", "unknown")
                        input_data = c.get("input", {})

                        # Build input_summary from first 3 keys, truncate each to 100 chars
                        input_parts = []
                        for key in list(input_data.keys())[:3]:
                            value = str(input_data[key])
                            if len(value) > 100:
                                value = value[:97] + "..."
                            input_parts.append(f"{key}={value}")

                        input_summary = ", ".join(input_parts) if input_parts else "(no input)"

                        tool_uses.append({
                            "name": name,
                            "input_summary": input_summary
                        })

                # Fire callbacks: tool_use BEFORE text (shows activity first, then result)
                if tool_uses:
                    self.on_tool_use(tool_uses)

                if text_blocks:
                    self.on_text_message(text_blocks)
