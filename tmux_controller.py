import subprocess
import re
from typing import Optional


def parse_selection_menu(pane_text: str) -> Optional[dict]:
    """Parse a TUI selection menu from captured pane text.
    Detects both AskUserQuestion menus ("Enter to select") and
    permission/edit menus ("Esc to cancel").
    Returns None if no menu detected, or dict with options and selected_index."""
    # Detect menu markers — Claude Code uses specific formatted lines at the bottom.
    # Real menu markers are standalone lines like:
    #   "Enter to select · ↑/↓ to navigate · Esc to cancel"
    #   "  Esc to cancel · Tab to amend"
    #   "  Esc to cancel"
    # They may or may not start with whitespace depending on the menu type.
    # AskUserQuestion menus render the footer at column 0 (no indent).
    # Only check the last 15 lines to avoid false positives from assistant text.
    lines = pane_text.split("\n")
    tail_lines = lines[-15:]
    has_menu = False
    # Check for structural TUI indicators: separator lines (───) or
    # numbered options with selection markers (❯/>) to distinguish
    # real menus from assistant prose that mentions menu keywords.
    has_tui_structure = (
        any("───" in tl for tl in tail_lines) or
        any(re.match(r'^\s*[>❯]\s*\d+\.', tl.strip()) for tl in tail_lines)
    )
    for tl in tail_lines:
        stripped = tl.strip()
        # Match exact menu footer patterns:
        # - Line that IS "Esc to cancel" (optionally with · Tab to amend)
        # - Line containing "Enter to select" with · separator
        # Must co-occur with TUI structural elements to avoid false positives.
        if re.match(r'^(Esc to cancel|Enter to select)', stripped) and has_tui_structure:
            has_menu = True
            break
    if not has_menu:
        return None

    # Only parse options from the tail section where the menu was detected
    options = []
    selected_index = 0

    for line in tail_lines:
        stripped = line.strip()
        # Match both formats:
        #   AskUserQuestion: "> 1. Option text" or "  1. Option text"
        #   Permission menu: "❯ 1. Yes" or "  2. No"
        match = re.match(r'^([>❯]?\s*)\d+\.\s+(.+)', stripped)
        if match:
            prefix = match.group(1)
            text = match.group(2).strip()
            # Skip marker lines that happen to match
            skip_texts = ["Enter to select", "Esc to cancel", "Tab to amend"]
            if text and not any(s in text for s in skip_texts):
                if ">" in prefix or "❯" in prefix:
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

    def is_claude_running(self) -> bool:
        """Check if Claude Code CLI is running in the tmux pane.
        Checks child processes of the pane's shell for 'claude' or 'node'."""
        # Get pane PID (the shell process)
        result = subprocess.run(
            ["tmux", "display-message", "-t", self._target, "-p", "#{pane_pid}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
        pane_pid = result.stdout.strip()
        # Check child processes for claude
        children = subprocess.run(
            ["pgrep", "-P", pane_pid],
            capture_output=True,
            text=True,
        )
        if children.returncode != 0:
            return False
        for child_pid in children.stdout.strip().split("\n"):
            if not child_pid:
                continue
            proc = subprocess.run(
                ["ps", "-o", "comm=", "-p", child_pid],
                capture_output=True,
                text=True,
            )
            cmd = proc.stdout.strip().lower()
            if "claude" in cmd or "node" in cmd:
                return True
        return False

    def restart_claude(self, resume: bool = True) -> None:
        """Start Claude Code in the tmux pane.
        If resume=True, resumes the previous session."""
        cmd = "claude --resume" if resume else "claude"
        subprocess.run(
            ["tmux", "send-keys", "-t", self._target, "--", cmd, "Enter"],
            check=True,
        )

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

    def create_window(self, name: str) -> bool:
        """Create a new tmux window with the specified name.
        Returns True on success, False on failure."""
        result = subprocess.run(
            ["tmux", "new-window", "-t", f"{self.session}:{name}", "-n", name],
            capture_output=True,
        )
        return result.returncode == 0

    def kill_window(self, name: str) -> bool:
        """Kill a tmux window with the specified name.
        Returns True on success, False on failure."""
        result = subprocess.run(
            ["tmux", "kill-window", "-t", f"{self.session}:{name}"],
            capture_output=True,
        )
        return result.returncode == 0

    def window_exists(self, name: str) -> bool:
        """Check if a tmux window with the specified name exists.
        Returns True if the window exists, False otherwise."""
        result = subprocess.run(
            ["tmux", "list-windows", "-t", self.session, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        window_names = result.stdout.strip().split("\n")
        return name in window_names

    def start_claude(self, window_name: str, resume_id: Optional[str] = None) -> None:
        """Start Claude Code CLI in a specific window.
        If resume_id is provided, resumes that specific session."""
        if resume_id:
            cmd = f"claude --resume {resume_id}"
        else:
            cmd = "claude"
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{self.session}:{window_name}", "--", cmd, "Enter"],
            check=True,
        )
