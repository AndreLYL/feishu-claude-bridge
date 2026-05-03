import json
import logging
import time
import threading
from pathlib import Path
from typing import Callable, Optional, List, Dict

logger = logging.getLogger("bridge.monitor")

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


def cwd_to_project_slug(cwd: str) -> str:
    """Convert an absolute path to Claude Code's project slug format.

    Claude Code replaces '/' and '.' with '-' in the path.
    e.g. /Users/john/project → -Users-john-project
    """
    return cwd.replace("/", "-").replace(".", "-")


def find_session_for_cwd(cwd: str, exclude_session_ids: Optional[List[str]] = None) -> Optional[Path]:
    """Find the latest session JSONL for a given working directory.

    Args:
        cwd: Absolute path of the working directory.
        exclude_session_ids: List of session UUIDs to exclude (e.g. the bridge operator's own session).
    """
    slug = cwd_to_project_slug(cwd)
    project_dir = CLAUDE_PROJECTS_DIR / slug
    if not project_dir.exists():
        return None

    exclude = set(exclude_session_ids or [])
    latest = None  # type: Optional[Path]
    latest_mtime = 0.0

    for f in project_dir.glob("*.jsonl"):
        if f.name == "history.jsonl":
            continue
        stem = f.stem  # UUID without .jsonl
        if stem in exclude:
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
        on_thinking: Optional[Callable[[str], None]] = None,
        on_heartbeat: Optional[Callable[[], None]] = None,
        on_turn_end: Optional[Callable[[], None]] = None,
        poll_interval: float = 1.0,
        stale_threshold: float = 60.0,
    ):
        self.jsonl_path = jsonl_path
        self.on_text_message = on_text_message
        self.on_tool_use = on_tool_use
        self.on_thinking = on_thinking
        self.on_heartbeat = on_heartbeat
        self.on_turn_end = on_turn_end
        self.poll_interval = poll_interval
        self._stale_threshold = stale_threshold
        self._project_dir = jsonl_path.parent
        self._offset = 0
        self._last_data_time = time.time()

        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        """Start polling in a background thread."""
        # Seek to a safe position: if the file was recently modified (within 30s),
        # there may be unread responses from before a crash/restart — read from
        # beginning to catch them. Otherwise seek to end for NEW messages only.
        if self.jsonl_path.exists():
            age = time.time() - self.jsonl_path.stat().st_mtime
            if age < 30:
                self._offset = 0
                logger.info("JSONL recently modified — reading from beginning to catch pending replies")
            else:
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
                self._check_stale_and_reposition()
            except Exception as e:
                logger.error(f"Monitor error: {e}")
            self._stop.wait(self.poll_interval)

    def _check_new_entries(self):
        if not self.jsonl_path.exists():
            return

        size = self.jsonl_path.stat().st_size
        if size <= self._offset:
            return

        self._last_data_time = time.time()

        with open(self.jsonl_path, "r") as f:
            f.seek(self._offset)
            new_data = f.read()
            self._offset = f.tell()

        had_sendable = False

        for line in new_data.strip().split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") in ("human", "user"):
                if self.on_turn_end:
                    self.on_turn_end()
                continue

            if entry.get("type") == "assistant":
                content = entry.get("message", {}).get("content", [])

                # Extract thinking blocks
                thinking_blocks = [
                    c.get("thinking", "") for c in content
                    if c.get("type") == "thinking" and c.get("thinking")
                ]
                if thinking_blocks and self.on_thinking:
                    combined_thinking = "\n".join(thinking_blocks)
                    self.on_thinking(combined_thinking)
                    had_sendable = True

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
                        input_parts = []
                        for key in list(input_data.keys())[:3]:
                            value = str(input_data[key])
                            if len(value) > 100:
                                value = value[:97] + "..."
                            input_parts.append(f"{key}={value}")
                        input_summary = ", ".join(input_parts) if input_parts else "(no input)"
                        tool_uses.append({"name": name, "input_summary": input_summary})

                if tool_uses:
                    self.on_tool_use(tool_uses)
                    had_sendable = True

                if text_blocks:
                    self.on_text_message(text_blocks)
                    had_sendable = True

        if not had_sendable and self.on_heartbeat:
            self._last_data_time = time.time()  # file changed — session is active
            self.on_heartbeat()

    def _check_stale_and_reposition(self):
        """If current JSONL is stale, try to find a newer one in the same project dir."""
        elapsed = time.time() - self._last_data_time
        if elapsed < self._stale_threshold:
            return

        if not self._project_dir.exists():
            return

        # Find the most recently modified JSONL in the project dir
        latest = None
        latest_mtime = 0.0

        for f in self._project_dir.glob("*.jsonl"):
            if f.name == "history.jsonl":
                continue
            mtime = f.stat().st_mtime
            if mtime > latest_mtime:
                latest = f
                latest_mtime = mtime

        if latest and latest != self.jsonl_path:
            logger.info(f"Auto-switched to {latest.name}")
            self.jsonl_path = latest
            self._offset = 0  # seek to beginning — process any responses already written
            self._last_data_time = time.time()
