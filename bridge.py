#!/usr/bin/env python3
"""Feishu ↔ Claude Code Session Bridge.

Bridges multiple Claude Code tmux sessions to Feishu for mobile remote control.

Usage:
    python bridge.py --tmux-session claude
    python bridge.py --tmux-session claude --tmux-window 0
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from feishu_client import FeishuClient
from formatter import (
    format_assistant_reply,
    format_heartbeat,
    format_permission_request,
    format_selection_menu,
    format_session_info,
    format_session_list,
    format_status_notification,
    format_thinking_notification,
    format_tool_use_notification,
)
from hook_server import resolve_permission, start_hook_server
from session_manager import SessionManager
from session_monitor import SessionMonitor, find_latest_session, find_session_for_cwd
from tmux_controller import TmuxController

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("bridge")

# State constants
STATE_IDLE = "idle"
STATE_WAITING_SELECTION = "waiting_selection"
STATE_WAITING_PERMISSION = "waiting_permission"


@dataclass
class SessionState:
    """Per-session state for managing UI interactions."""
    session_id: str
    state: str = STATE_IDLE  # "idle" | "waiting_permission" | "waiting_selection"
    pending_permission: Optional[dict] = None
    pending_menu: Optional[dict] = None


class CardManager:
    """Manages the active Feishu card for the current assistant turn.

    First send creates a card; subsequent sends PATCH-update it in place.
    finalize() ends the turn and resets state.
    """

    def __init__(self, feishu):
        self.feishu = feishu
        self._active_card_id: Optional[str] = None

    def send_or_update(self, card: dict) -> Optional[str]:
        """Send a new card or PATCH the active one. Returns message_id."""
        if self._active_card_id is None:
            msg_id = self.feishu.send_card(card)
            if msg_id:
                self._active_card_id = msg_id
            return msg_id
        else:
            self.feishu.update_card(self._active_card_id, card.get("card", card))
            return self._active_card_id

    def send_standalone(self, card: dict) -> Optional[str]:
        """Always create a new card (e.g., tool notifications outside the main turn)."""
        return self.feishu.send_card(card)

    def finalize(self):
        """End the current turn. Next send_or_update will create a new card."""
        self._active_card_id = None


class Bridge:
    def __init__(self, tmux_session: str, tmux_window: Optional[str] = None,
                 session_file: Optional[str] = None, exclude_sessions: Optional[List[str]] = None,
                 max_sessions: int = 5, session_store_path: Path = None):
        # Feishu client
        self.feishu = FeishuClient(
            app_id=os.environ["FEISHU_APP_ID"],
            app_secret=os.environ["FEISHU_APP_SECRET"],
            allowed_chat_id=os.environ["ALLOWED_CHAT_ID"],
            on_message=self._handle_feishu_message,
            on_card_action=self._handle_card_action,
            on_image=self._handle_feishu_image,
        )

        # Multi-session support
        if session_store_path is None:
            session_store_path = Path.home() / ".feishu-claude-bridge" / "sessions.json"

        self.session_manager = SessionManager(
            tmux_session=tmux_session,
            store_path=session_store_path,
            max_sessions=max_sessions,
        )

        # Per-session state and resources
        self.session_states: Dict[str, SessionState] = {}
        self.session_controllers: Dict[str, TmuxController] = {}
        self.session_monitors: Dict[str, SessionMonitor] = {}
        self.card_managers: Dict[str, CardManager] = {}
        self.active_session_id: Optional[str] = None

        # Backward compatibility: if session_file or tmux_window provided, create initial session
        if session_file or tmux_window:
            self._init_legacy_mode(tmux_session, tmux_window, session_file, exclude_sessions)

        self.hook_port = int(os.environ.get("HOOK_SERVER_PORT", "19280"))
        self._menu_lock = threading.Lock()

    def _init_legacy_mode(self, tmux_session: str, tmux_window: Optional[str],
                          session_file: Optional[str], exclude_sessions: Optional[List[str]]):
        """Legacy mode: auto-create single session from existing tmux window."""
        # Find active session JSONL
        if session_file:
            jsonl_path = Path(session_file)
        else:
            # Auto-detect from tmux pane's working directory
            tmux = TmuxController(tmux_session, tmux_window)
            cwd = tmux.get_pane_cwd()
            if cwd:
                logger.info(f"Detected tmux cwd: {cwd}")
                jsonl_path = find_session_for_cwd(cwd, exclude_session_ids=exclude_sessions)
            else:
                jsonl_path = find_latest_session()

        if not jsonl_path or not jsonl_path.exists():
            logger.error("No active Claude Code session found")
            sys.exit(1)

        logger.info(f"Monitoring session: {jsonl_path.name}")

        # Extract session_id from JSONL filename (UUID.jsonl -> UUID)
        session_id = jsonl_path.stem

        # Create session in manager (import existing tmux window)
        # Note: This is a workaround for legacy mode. Real implementation would need
        # SessionManager.import_session() method.
        # For now, we'll create resources manually
        self.active_session_id = session_id
        self.session_states[session_id] = SessionState(session_id=session_id)
        self.session_controllers[session_id] = TmuxController(tmux_session, tmux_window)
        self.card_managers[session_id] = CardManager(self.feishu)

        # Start monitor for this session
        monitor = SessionMonitor(
            jsonl_path=jsonl_path,
            session_id=session_id,
            on_text_message=self._handle_text_message,
            on_tool_use=self._handle_tool_use,
            on_thinking=self._handle_thinking,
            on_heartbeat=self._handle_heartbeat,
            on_turn_end=self._handle_turn_end,
        )
        self.session_monitors[session_id] = monitor

    def start(self):
        # Start hook server
        start_hook_server(self.hook_port, self)

        # Start all session monitors
        for monitor in self.session_monitors.values():
            monitor.start()

        # Notify user
        self.feishu.send_card(
            format_status_notification("Bridge connected. Send /new to create a session or send messages to control Claude Code.")
        )

        logger.info("Bridge started. Ctrl+C to stop.")

        # Start Feishu WebSocket (blocking)
        self.feishu.start()

    def send_permission_card(self, tool_name: str, tool_input: str, request_id: str):
        """Called by hook_server when a permission request arrives."""
        try:
            # TODO: Get session_id from hook_server
            # For now, assume active session
            session_id = self.active_session_id
            if not session_id:
                logger.warning("Permission request received but no active session")
                return

            with self._menu_lock:
                state = self.session_states.get(session_id)
                if state:
                    if state.state == STATE_WAITING_SELECTION:
                        tmux = self.session_controllers.get(session_id)
                        if tmux:
                            tmux.send_key("Escape")
                        state.pending_menu = None
                    state.state = STATE_WAITING_PERMISSION
                    state.pending_permission = {"request_id": request_id}

            card = format_permission_request(tool_name, tool_input, request_id)
            self.feishu.send_card(card)
        except Exception as e:
            logger.error(f"Error sending permission card: {e}", exc_info=True)

    def _handle_feishu_message(self, text: str):
        """Handle text message from Feishu."""
        try:
            logger.info(f"Feishu → bridge: {text[:80]}")

            # Command routing — check commands first
            text_stripped = text.strip()

            # Session management commands
            if text_stripped.startswith("/new"):
                name = text_stripped[4:].strip() or None
                return self._cmd_new(name)
            elif text_stripped == "/list":
                return self._cmd_list()
            elif text_stripped.startswith("/switch"):
                try:
                    num = int(text_stripped[7:].strip())
                    return self._cmd_switch(num)
                except (ValueError, IndexError):
                    self.feishu.send_card(
                        format_status_notification("Invalid command. Usage: /switch <number>", "red")
                    )
                    return
            elif text_stripped.startswith("/delete"):
                try:
                    num = int(text_stripped[7:].strip())
                    return self._cmd_delete(num)
                except (ValueError, IndexError):
                    self.feishu.send_card(
                        format_status_notification("Invalid command. Usage: /delete <number>", "red")
                    )
                    return
            elif text_stripped.startswith("/rename"):
                try:
                    parts = text_stripped[7:].strip().split(" ", 1)
                    num = int(parts[0])
                    name = parts[1] if len(parts) > 1 else ""
                    return self._cmd_rename(num, name)
                except (ValueError, IndexError):
                    self.feishu.send_card(
                        format_status_notification("Invalid command. Usage: /rename <number> <name>", "red")
                    )
                    return
            elif text_stripped == "/current":
                return self._cmd_current()

            # Bridge control commands (act on active session)
            elif text_stripped == "/esc":
                return self._cmd_esc()
            elif text_stripped == "/screenshot":
                return self._cmd_screenshot()

            # Default: route to active session
            if not self.active_session_id:
                self.feishu.send_card(
                    format_status_notification("No active session. Use /new to create one.", "yellow")
                )
                return

            session_state = self.session_states.get(self.active_session_id)
            if not session_state:
                logger.error(f"Active session {self.active_session_id} has no state")
                return

            # Finalize previous card before new user message
            card_manager = self.card_managers.get(self.active_session_id)
            if card_manager:
                card_manager.finalize()

            with self._menu_lock:
                # Permission reply
                if session_state.state == STATE_WAITING_PERMISSION:
                    if text_stripped.lower() in ("y", "yes"):
                        logger.info("Permission granted via text")
                        resolve_permission(self.active_session_id, "allow")
                        session_state.state = STATE_IDLE
                        session_state.pending_permission = None
                        return
                    elif text_stripped.lower() in ("n", "no"):
                        logger.info("Permission denied via text")
                        resolve_permission(self.active_session_id, "deny")
                        session_state.state = STATE_IDLE
                        session_state.pending_permission = None
                        return

                # Menu selection
                if session_state.state == STATE_WAITING_SELECTION:
                    if text_stripped.isdigit():
                        idx = int(text_stripped)
                        menu = session_state.pending_menu
                        if menu and 0 <= idx < len(menu.get("options", [])):
                            logger.info(f"Selecting menu option {idx}: {menu['options'][idx]}")
                            tmux = self.session_controllers.get(self.active_session_id)
                            if tmux:
                                tmux.select_option(idx)
                            session_state.state = STATE_IDLE
                            session_state.pending_menu = None
                            return
                        else:
                            logger.warning(f"Invalid selection {idx}")

                    logger.info("Non-numeric input during menu state, resetting and sending as text")
                    session_state.state = STATE_IDLE
                    session_state.pending_menu = None
                    tmux = self.session_controllers.get(self.active_session_id)
                    if tmux:
                        tmux.send_key("Escape")
                        time.sleep(0.3)
                        tmux.send_text(text_stripped)
                    return

            # Plain text → send to active session's tmux
            self._ensure_claude_running(self.active_session_id)
            tmux = self.session_controllers.get(self.active_session_id)
            if tmux:
                tmux.send_text(text_stripped)
        except Exception as e:
            logger.error(f"Error handling Feishu message: {e}", exc_info=True)

    def _cmd_new(self, name: Optional[str]):
        """Create a new session."""
        try:
            session_id, session = self.session_manager.create_session(name)

            # Initialize session resources
            self.session_states[session_id] = SessionState(session_id=session_id)
            self.session_controllers[session_id] = TmuxController(
                self.session_manager.tmux_session,
                session["tmux_window"]
            )
            self.card_managers[session_id] = CardManager(self.feishu)

            # Start monitor for this session
            # TODO: Find JSONL file for this session
            # For now, we'll create a placeholder monitor that doesn't monitor anything
            # Real implementation would need to discover the JSONL path

            # Set as active
            self.active_session_id = session_id

            self.feishu.send_card(
                format_status_notification(
                    f"Session created: {session['name']} (#{session_id})\n"
                    f"Window: {session['tmux_window']}"
                )
            )
        except Exception as e:
            logger.error(f"Error creating session: {e}", exc_info=True)
            self.feishu.send_card(
                format_status_notification(f"Error creating session: {e}", "red")
            )

    def _cmd_list(self):
        """List all sessions."""
        try:
            sessions, active_id = self.session_manager.list_sessions()
            card = format_session_list(sessions, active_id)
            self.feishu.send_card(card)
        except Exception as e:
            logger.error(f"Error listing sessions: {e}", exc_info=True)
            self.feishu.send_card(
                format_status_notification(f"Error listing sessions: {e}", "red")
            )

    def _cmd_switch(self, display_number: int):
        """Switch active session."""
        try:
            session = self.session_manager.switch_session(display_number)
            self.active_session_id = session["id"]
            self.feishu.send_card(
                format_status_notification(f"Switched to session: {session['name']}")
            )
        except Exception as e:
            logger.error(f"Error switching session: {e}", exc_info=True)
            self.feishu.send_card(
                format_status_notification(f"Error: {e}", "red")
            )

    def _cmd_delete(self, display_number: int):
        """Delete a session."""
        try:
            session_id = self.session_manager.resolve_display_number(display_number)
            deleted_name = self.session_manager.delete_session(display_number)

            # Clean up resources
            self.session_states.pop(session_id, None)
            self.session_controllers.pop(session_id, None)
            self.card_managers.pop(session_id, None)
            monitor = self.session_monitors.pop(session_id, None)
            if monitor:
                monitor.stop()

            self.feishu.send_card(
                format_status_notification(f"Deleted session: {deleted_name}")
            )
        except Exception as e:
            logger.error(f"Error deleting session: {e}", exc_info=True)
            self.feishu.send_card(
                format_status_notification(f"Error: {e}", "red")
            )

    def _cmd_rename(self, display_number: int, new_name: str):
        """Rename a session."""
        try:
            session = self.session_manager.rename_session(display_number, new_name)
            self.feishu.send_card(
                format_status_notification(f"Renamed to: {session['name']}")
            )
        except Exception as e:
            logger.error(f"Error renaming session: {e}", exc_info=True)
            self.feishu.send_card(
                format_status_notification(f"Error: {e}", "red")
            )

    def _cmd_current(self):
        """Show current session info."""
        try:
            session = self.session_manager.get_active_session()
            if not session:
                self.feishu.send_card(
                    format_status_notification("No active session", "yellow")
                )
                return
            card = format_session_info(session)
            self.feishu.send_card(card)
        except Exception as e:
            logger.error(f"Error showing current session: {e}", exc_info=True)
            self.feishu.send_card(
                format_status_notification(f"Error: {e}", "red")
            )

    def _cmd_esc(self):
        """Send Escape key to active session."""
        if not self.active_session_id:
            self.feishu.send_card(
                format_status_notification("No active session", "yellow")
            )
            return

        session_state = self.session_states.get(self.active_session_id)
        if session_state:
            with self._menu_lock:
                session_state.state = STATE_IDLE
                session_state.pending_menu = None

        tmux = self.session_controllers.get(self.active_session_id)
        if tmux:
            tmux.send_key("Escape")

    def _cmd_screenshot(self):
        """Capture screenshot from active session."""
        if not self.active_session_id:
            self.feishu.send_card(
                format_status_notification("No active session", "yellow")
            )
            return

        tmux = self.session_controllers.get(self.active_session_id)
        if tmux:
            screenshot = tmux.capture_pane(50)
            self.feishu.send_text(f"```\n{screenshot}\n```")


    def _handle_feishu_image(self, image_path: str):
        """Handle image message from Feishu — download and send path to Claude."""
        try:
            logger.info(f"Feishu image → active session: {image_path}")

            if not self.active_session_id:
                self.feishu.send_card(
                    format_status_notification("No active session", "yellow")
                )
                return

            card_manager = self.card_managers.get(self.active_session_id)
            if card_manager:
                card_manager.finalize()

            self._ensure_claude_running(self.active_session_id)
            tmux = self.session_controllers.get(self.active_session_id)
            if tmux:
                tmux.send_text(f"请看这张图片：{image_path}")
        except Exception as e:
            logger.error(f"Error handling image: {e}", exc_info=True)

    def _handle_card_action(self, value: dict):
        """Handle Feishu card button click (Allow/Deny)."""
        try:
            action = value.get("action", "")
            request_id = value.get("id", "")
            if request_id and action in ("allow", "deny"):
                logger.info(f"Permission {action}: {request_id}")
                # TODO: Map request_id to session_id
                # For now, use active session
                if self.active_session_id:
                    resolve_permission(self.active_session_id, action)
        except Exception as e:
            logger.error(f"Error handling card action: {e}", exc_info=True)

    def _handle_text_message(self, session_id: str, text_blocks: List[str]):
        """Handle new assistant text message from JSONL monitor."""
        try:
            # Only push to Feishu if this is the active session
            if session_id != self.active_session_id:
                logger.debug(f"Background session {session_id} output suppressed")
                return

            # Get per-session resources
            card_manager = self.card_managers.get(session_id)
            if not card_manager:
                logger.error(f"No card manager for session {session_id}")
                return

            # Accumulate text in session state
            # TODO: Move accumulated_text to SessionState
            if not hasattr(self, '_accumulated_text'):
                self._accumulated_text = {}
            if session_id not in self._accumulated_text:
                self._accumulated_text[session_id] = []

            self._accumulated_text[session_id].extend(text_blocks)
            card = format_assistant_reply(self._accumulated_text[session_id])
            card_manager.send_or_update(card)
            # Do NOT finalize here — wait for on_turn_end (next human message)
            self._schedule_menu_detection(session_id)
        except Exception as e:
            logger.error(f"Error handling text message: {e}", exc_info=True)

    def _handle_tool_use(self, session_id: str, tools: List[Dict[str, str]]):
        """Handle tool use notification from JSONL monitor."""
        try:
            # Only push to Feishu if this is the active session
            if session_id != self.active_session_id:
                logger.debug(f"Background session {session_id} tool use suppressed")
                return

            card_manager = self.card_managers.get(session_id)
            if not card_manager:
                return

            card = format_tool_use_notification(tools)
            card_manager.send_standalone(card)
            # Tool use may trigger permission menus (Edit/Bash/Write confirmation)
            self._schedule_menu_detection(session_id)
        except Exception as e:
            logger.error(f"Error handling tool use: {e}", exc_info=True)

    def _handle_thinking(self, session_id: str, thinking_text: str):
        """Handle thinking block from JSONL."""
        try:
            # Only push to Feishu if this is the active session
            if session_id != self.active_session_id:
                return

            card_manager = self.card_managers.get(session_id)
            if not card_manager:
                return

            card = format_thinking_notification(thinking_text)
            card_manager.send_or_update(card)
        except Exception as e:
            logger.error(f"Error handling thinking: {e}", exc_info=True)

    def _handle_heartbeat(self, session_id: str):
        """Handle heartbeat — file changed but no sendable content."""
        try:
            # Only push to Feishu if this is the active session
            if session_id != self.active_session_id:
                return

            # Get per-session heartbeat start time
            if not hasattr(self, '_heartbeat_starts'):
                self._heartbeat_starts = {}

            if session_id not in self._heartbeat_starts:
                self._heartbeat_starts[session_id] = time.time()

            elapsed = int(time.time() - self._heartbeat_starts[session_id])
            card = format_heartbeat(elapsed)

            card_manager = self.card_managers.get(session_id)
            if card_manager:
                card_manager.send_or_update(card)
        except Exception as e:
            logger.error(f"Error handling heartbeat: {e}", exc_info=True)

    def _handle_turn_end(self, session_id: str):
        """Handle turn boundary — human message appeared, finalize the card."""
        try:
            card_manager = self.card_managers.get(session_id)
            if card_manager:
                card_manager.finalize()

            # Clear session-specific state
            if hasattr(self, '_heartbeat_starts'):
                self._heartbeat_starts.pop(session_id, None)
            if hasattr(self, '_accumulated_text'):
                self._accumulated_text.pop(session_id, None)
        except Exception as e:
            logger.error(f"Error handling turn end: {e}", exc_info=True)

    def _ensure_claude_running(self, session_id: str):
        """Check if Claude Code is running in tmux; if not, auto-resume."""
        tmux = self.session_controllers.get(session_id)
        if not tmux:
            return

        if tmux.is_claude_running():
            return

        logger.warning(f"Claude Code not running in session {session_id} — auto-resuming")
        self.feishu.send_card(
            format_status_notification("Claude Code exited, auto-resuming...", "yellow")
        )
        tmux.restart_claude(resume=True)
        # Wait for Claude to start up
        for _ in range(15):
            time.sleep(2)
            if tmux.is_claude_running():
                logger.info(f"Claude Code resumed successfully in session {session_id}")
                self.feishu.send_card(
                    format_status_notification("Claude Code resumed. Ready.")
                )
                return
        logger.error(f"Claude Code failed to resume in session {session_id} after 30s")
        self.feishu.send_card(
            format_status_notification("Claude Code failed to resume. Check manually.", "red")
        )

    def _schedule_menu_detection(self, session_id: str):
        """Start background thread to detect selection menus.
        Only one detection thread runs at a time per session to prevent duplicate cards."""
        # Get per-session menu detection state
        if not hasattr(self, '_menu_detect_active'):
            self._menu_detect_active = {}

        session_state = self.session_states.get(session_id)
        if not session_state:
            return

        with self._menu_lock:
            if self._menu_detect_active.get(session_id) or session_state.state == STATE_WAITING_SELECTION:
                return
            self._menu_detect_active[session_id] = True

        def detect_menu():
            try:
                time.sleep(2.0)
                for attempt in range(5):
                    tmux = self.session_controllers.get(session_id)
                    if not tmux:
                        break

                    menu_data = tmux.detect_selection_menu()
                    if menu_data and menu_data.get("is_menu"):
                        logger.info(f"Menu detected in session {session_id}: {len(menu_data['options'])} options")
                        with self._menu_lock:
                            if session_state:
                                session_state.state = STATE_WAITING_SELECTION
                                session_state.pending_menu = menu_data

                        # Only show menu if this is the active session
                        if session_id == self.active_session_id:
                            card = format_selection_menu(menu_data["options"])
                            self.feishu.send_card(card)
                        break
                    if attempt < 4:
                        time.sleep(2.0)
            except Exception as e:
                logger.error(f"Error in menu detection: {e}", exc_info=True)
            finally:
                with self._menu_lock:
                    if hasattr(self, '_menu_detect_active'):
                        self._menu_detect_active[session_id] = False

        thread = threading.Thread(target=detect_menu, daemon=True)
        thread.start()


def main():
    parser = argparse.ArgumentParser(description="Feishu ↔ Claude Code Bridge")
    parser.add_argument("--tmux-session", required=True, help="tmux session name")
    parser.add_argument("--tmux-window", default=None, help="tmux window index/name (legacy mode)")
    parser.add_argument("--session-file", default=None, help="Path to Claude Code session JSONL file (legacy mode)")
    parser.add_argument("--exclude-session", action="append", default=[], help="Session UUID(s) to exclude from auto-detect (legacy mode, repeatable)")
    parser.add_argument("--max-sessions", type=int, default=None, help="Maximum number of concurrent sessions (default: 5, can also set via MAX_SESSIONS env var)")
    parser.add_argument("--session-store-path", default=None, help="Path to sessions.json file (default: ~/.feishu-claude-bridge/sessions.json, can also set via SESSION_STORE_PATH env var)")
    args = parser.parse_args()

    # Read config from args/env vars with defaults
    max_sessions = args.max_sessions or int(os.environ.get("MAX_SESSIONS", "5"))
    session_store_path = args.session_store_path or os.environ.get(
        "SESSION_STORE_PATH",
        str(Path.home() / ".feishu-claude-bridge" / "sessions.json")
    )

    bridge = Bridge(
        args.tmux_session,
        args.tmux_window,
        args.session_file,
        args.exclude_session,
        max_sessions=max_sessions,
        session_store_path=Path(session_store_path)
    )

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        # Stop all session monitors
        for monitor in bridge.session_monitors.values():
            monitor.stop()
        bridge.feishu.send_card(
            format_status_notification("Bridge disconnected.", "red")
        )
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    bridge.start()


if __name__ == "__main__":
    main()
