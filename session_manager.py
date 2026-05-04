import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tmux_controller import TmuxController

logger = logging.getLogger("bridge.session_manager")


class SessionManager:
    """Manages multiple Claude Code sessions with persistent state.

    Each session runs in its own tmux window. State is persisted to sessions.json.
    """

    def __init__(self, tmux_session: str, store_path: Path, max_sessions: int = 5, work_dir: str = "."):
        self.tmux_session = tmux_session
        self.store_path = store_path
        self.max_sessions = max_sessions
        self.work_dir = work_dir

        self.tmux = TmuxController(tmux_session)

        # State
        self.sessions: Dict[str, dict] = {}
        self.active_session: Optional[str] = None
        self.counter = 0

        # Load existing state if available
        self._load()

    def create_session(self, name: Optional[str] = None) -> Tuple[str, dict]:
        """Create new session. Returns (session_id, session_dict). Raises if at max."""
        if len(self.sessions) >= self.max_sessions:
            raise ValueError(f"Maximum session limit ({self.max_sessions}) reached")

        # Increment counter
        self.counter += 1
        session_id = f"s{self.counter}"

        # Default name
        if name is None:
            name = f"session-{self.counter}"

        # Create tmux window
        tmux_window = f"claude-{self.counter}"
        if not self.tmux.create_window(tmux_window):
            raise RuntimeError(f"Failed to create tmux window: {tmux_window}")

        # Start claude in that window
        self.tmux.start_claude(tmux_window, resume_id=None)

        # Create session metadata
        now = datetime.now(timezone.utc).isoformat()
        session = {
            "id": session_id,
            "name": name,
            "tmux_window": tmux_window,
            "state": "running",
            "created_at": now,
            "updated_at": now,
        }

        # Store and set as active
        self.sessions[session_id] = session
        self.active_session = session_id

        # Persist
        self._persist()

        logger.info(f"Created session {session_id} ({name}) in window {tmux_window}")
        return session_id, session

    def switch_session(self, display_number: int) -> dict:
        """Switch active session by display number (1-based). Returns session dict."""
        session_id = self.resolve_display_number(display_number)

        if session_id not in self.sessions:
            raise ValueError(f"Invalid display number: {display_number}")

        self.active_session = session_id
        self.sessions[session_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

        self._persist()

        logger.info(f"Switched to session {session_id} ({self.sessions[session_id]['name']})")
        return self.sessions[session_id]

    def delete_session(self, display_number: int) -> str:
        """Delete session by display number. Returns deleted session name. Raises if active."""
        session_id = self.resolve_display_number(display_number)

        if session_id not in self.sessions:
            raise ValueError(f"Invalid display number: {display_number}")

        if session_id == self.active_session:
            raise ValueError("Cannot delete active session")

        session = self.sessions[session_id]
        tmux_window = session["tmux_window"]

        # Kill tmux window
        if not self.tmux.kill_window(tmux_window):
            logger.warning(f"Failed to kill tmux window {tmux_window}")

        # Remove from sessions
        deleted_name = session["name"]
        del self.sessions[session_id]

        self._persist()

        logger.info(f"Deleted session {session_id} ({deleted_name})")
        return deleted_name

    def rename_session(self, display_number: int, new_name: str) -> dict:
        """Rename session. Returns updated session dict."""
        session_id = self.resolve_display_number(display_number)

        if session_id not in self.sessions:
            raise ValueError(f"Invalid display number: {display_number}")

        self.sessions[session_id]["name"] = new_name
        self.sessions[session_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

        self._persist()

        logger.info(f"Renamed session {session_id} to {new_name}")
        return self.sessions[session_id]

    def get_active_session(self) -> Optional[dict]:
        """Return active session metadata."""
        if self.active_session is None:
            return None
        return self.sessions.get(self.active_session)

    def list_sessions(self) -> Tuple[List[dict], str]:
        """Return (sorted sessions list, active_session_id).

        Sessions are sorted by created_at ascending.
        """
        sorted_sessions = sorted(
            self.sessions.values(),
            key=lambda s: s["created_at"]
        )
        return sorted_sessions, self.active_session

    def resolve_display_number(self, display_number: int) -> str:
        """Convert 1-based display number to internal session_id."""
        sorted_sessions, _ = self.list_sessions()

        if display_number < 1 or display_number > len(sorted_sessions):
            raise ValueError(f"Invalid display number: {display_number}")

        # 1-based to 0-based index
        return sorted_sessions[display_number - 1]["id"]

    def recover(self) -> None:
        """On startup: read sessions.json, verify tmux windows exist.

        For each session: check window_exists
        Window gone -> mark state = "stopped"
        """
        for session_id, session in self.sessions.items():
            tmux_window = session["tmux_window"]
            if not self.tmux.window_exists(tmux_window):
                logger.warning(f"Session {session_id} window {tmux_window} not found, marking as stopped")
                session["state"] = "stopped"

        self._persist()

    def migrate(self) -> None:
        """First run migration: if no sessions.json but tmux has claude running, import as s1.

        This is a placeholder for future implementation if needed.
        """
        pass

    def _persist(self) -> None:
        """Save current state to sessions.json."""
        data = {
            "sessions": self.sessions,
            "active_session": self.active_session,
            "counter": self.counter,
        }

        # Ensure parent directory exists
        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.store_path, "w") as f:
            json.dump(data, f, indent=2)

    def _load(self) -> None:
        """Load state from sessions.json."""
        if not self.store_path.exists():
            logger.info(f"No existing sessions.json at {self.store_path}")
            return

        try:
            with open(self.store_path, "r") as f:
                data = json.load(f)

            self.sessions = data.get("sessions", {})
            self.active_session = data.get("active_session")
            self.counter = data.get("counter", 0)

            logger.info(f"Loaded {len(self.sessions)} sessions from {self.store_path}")
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load sessions.json: {e}")
