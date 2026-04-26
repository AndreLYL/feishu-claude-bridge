import subprocess
import shlex
from typing import Optional


class TmuxController:
    def __init__(self, session: str, window: Optional[str] = None):
        self.session = session
        self.window = window
        self._target = f"{session}:{window}" if window else session

    def is_alive(self) -> bool:
        """Check if the tmux session exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session],
            capture_output=True,
        )
        return result.returncode == 0

    def send_text(self, text: str) -> None:
        """Send text input followed by Enter to the tmux pane."""
        subprocess.run(
            ["tmux", "send-keys", "-t", self._target, "--", text, "Enter"],
            check=True,
        )

    def send_key(self, key: str) -> None:
        """Send a special key (e.g. 'Escape', 'C-c') without Enter."""
        subprocess.run(
            ["tmux", "send-keys", "-t", self._target, key],
            check=True,
        )

    def capture_pane(self, lines: int = 80) -> str:
        """Capture visible pane content as text."""
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", self._target, "-p", "-S", f"-{lines}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
