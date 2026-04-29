import subprocess
import shlex
import re
from typing import Optional


def parse_selection_menu(pane_text: str) -> Optional[dict]:
    """Parse a TUI selection menu from captured pane text.
    Returns None if no menu detected, or dict with options and selected_index."""
    if "Enter to select" not in pane_text:
        return None

    lines = pane_text.split("\n")
    options = []
    selected_index = 0

    for line in lines:
        stripped = line.strip()
        match = re.match(r'^(>?\s*)\d+\.\s+(.+)', stripped)
        if match:
            prefix = match.group(1)
            text = match.group(2).strip()
            if text and "Enter to select" not in text:
                if ">" in prefix:
                    selected_index = len(options)
                options.append(text)

    if not options:
        return None

    return {"is_menu": True, "options": options, "selected_index": selected_index}


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

    def detect_selection_menu(self) -> Optional[dict]:
        """Detect if a TUI selection menu is currently displayed.
        Returns menu data or None."""
        pane_text = self.capture_pane(50)
        return parse_selection_menu(pane_text)

    def get_pane_cwd(self) -> Optional[str]:
        """Get the current working directory of the tmux pane."""
        result = subprocess.run(
            ["tmux", "display-message", "-t", self._target, "-p", "#{pane_current_path}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None

    def select_option(self, target_index: int, current_index: int = 0) -> None:
        """Navigate to and select a menu option by index.
        Resets to top, moves down target_index times, confirms with Enter."""
        # Reset to top
        for _ in range(20):
            self.send_key("Up")
        # Move down to target
        for _ in range(target_index):
            self.send_key("Down")
        # Confirm
        self.send_key("Enter")
