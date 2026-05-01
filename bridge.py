#!/usr/bin/env python3
"""Feishu ↔ Claude Code Session Bridge.

Bridges an active Claude Code tmux session to Feishu for mobile remote control.

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
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from feishu_client import FeishuClient
from formatter import (
    format_assistant_reply,
    format_heartbeat,
    format_permission_request,
    format_selection_menu,
    format_status_notification,
    format_thinking_notification,
    format_tool_use_notification,
)
from hook_server import resolve_permission, start_hook_server
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
                 session_file: Optional[str] = None, exclude_sessions: Optional[List[str]] = None):
        self.tmux = TmuxController(tmux_session, tmux_window)

        # Feishu client
        self.feishu = FeishuClient(
            app_id=os.environ["FEISHU_APP_ID"],
            app_secret=os.environ["FEISHU_APP_SECRET"],
            allowed_chat_id=os.environ["ALLOWED_CHAT_ID"],
            on_message=self._handle_feishu_message,
            on_card_action=self._handle_card_action,
            on_image=self._handle_feishu_image,
        )

        # Find active session JSONL
        if session_file:
            jsonl_path = Path(session_file)
        else:
            # Auto-detect from tmux pane's working directory
            cwd = self.tmux.get_pane_cwd()
            if cwd:
                logger.info(f"Detected tmux cwd: {cwd}")
                jsonl_path = find_session_for_cwd(cwd, exclude_session_ids=exclude_sessions)
            else:
                jsonl_path = find_latest_session()
        if not jsonl_path or not jsonl_path.exists():
            logger.error("No active Claude Code session found")
            sys.exit(1)
        logger.info(f"Monitoring session: {jsonl_path.name}")

        # Card manager for active turn lifecycle
        self.card_manager = CardManager(self.feishu)

        # Session monitor
        self.monitor = SessionMonitor(
            jsonl_path=jsonl_path,
            on_text_message=self._handle_text_message,
            on_tool_use=self._handle_tool_use,
            on_thinking=self._handle_thinking,
            on_heartbeat=self._handle_heartbeat,
            on_turn_end=self._handle_turn_end,
        )

        self.hook_port = int(os.environ.get("HOOK_SERVER_PORT", "19280"))

        # State machine
        self._state = STATE_IDLE
        self._menu_options = []
        self._menu_lock = threading.Lock()
        self._pending_permission_id: Optional[str] = None
        self._heartbeat_start: Optional[float] = None
        self._accumulated_text: List[str] = []

    def start(self):
        # Verify tmux is alive
        if not self.tmux.is_alive():
            logger.error(f"tmux session not found")
            sys.exit(1)

        # Start hook server
        start_hook_server(self.hook_port, self)

        # Start session monitor
        self.monitor.start()

        # Notify user
        self.feishu.send_card(
            format_status_notification("Bridge connected. Send messages here to control Claude Code.")
        )

        logger.info("Bridge started. Ctrl+C to stop.")

        # Start Feishu WebSocket (blocking)
        self.feishu.start()

    def send_permission_card(self, tool_name: str, tool_input: str, request_id: str):
        """Called by hook_server when a permission request arrives."""
        try:
            with self._menu_lock:
                if self._state == STATE_WAITING_SELECTION:
                    self.tmux.send_key("Escape")
                    self._menu_options = []
                self._state = STATE_WAITING_PERMISSION
                self._pending_permission_id = request_id
            card = format_permission_request(tool_name, tool_input, request_id)
            self.feishu.send_card(card)
        except Exception as e:
            logger.error(f"Error sending permission card: {e}", exc_info=True)

    def _handle_feishu_message(self, text: str):
        """Handle text message from Feishu."""
        try:
            logger.info(f"Feishu → tmux: {text[:80]}")

            # Finalize previous card before new user message
            self.card_manager.finalize()
            self._accumulated_text.clear()

            with self._menu_lock:
                # Permission reply
                if self._state == STATE_WAITING_PERMISSION:
                    if text.lower() in ("y", "yes"):
                        logger.info("Permission granted via text")
                        resolve_permission(self._pending_permission_id, "allow")
                        self._state = STATE_IDLE
                        self._pending_permission_id = None
                        return
                    elif text.lower() in ("n", "no"):
                        logger.info("Permission denied via text")
                        resolve_permission(self._pending_permission_id, "deny")
                        self._state = STATE_IDLE
                        self._pending_permission_id = None
                        return

                # Menu selection
                if self._state == STATE_WAITING_SELECTION:
                    if text.isdigit():
                        idx = int(text)
                        if 0 <= idx < len(self._menu_options):
                            logger.info(f"Selecting menu option {idx}: {self._menu_options[idx]}")
                            self.tmux.select_option(idx)
                            self._state = STATE_IDLE
                            self._menu_options = []
                            return
                        else:
                            logger.warning(f"Invalid selection {idx}, valid range: 0-{len(self._menu_options)-1}")

                    logger.info("Non-numeric input during menu state, resetting and sending as text")
                    self._state = STATE_IDLE
                    self._menu_options = []
                    self.tmux.send_key("Escape")
                    time.sleep(0.3)
                    self.tmux.send_text(text)
                    return

            # Bridge commands
            if text == "/esc":
                with self._menu_lock:
                    self._state = STATE_IDLE
                    self._menu_options = []
                self.tmux.send_key("Escape")
                return
            if text == "/screenshot":
                screenshot = self.tmux.capture_pane(50)
                self.feishu.send_text(f"```\n{screenshot}\n```")
                return

            self._ensure_claude_running()
            self.tmux.send_text(text)
        except Exception as e:
            logger.error(f"Error handling Feishu message: {e}", exc_info=True)

    def _handle_feishu_image(self, image_path: str):
        """Handle image message from Feishu — download and send path to Claude."""
        try:
            logger.info(f"Feishu image → tmux: {image_path}")
            self.card_manager.finalize()
            self._accumulated_text.clear()
            self._ensure_claude_running()
            self.tmux.send_text(f"请看这张图片：{image_path}")
        except Exception as e:
            logger.error(f"Error handling image: {e}", exc_info=True)

    def _handle_card_action(self, value: dict):
        """Handle Feishu card button click (Allow/Deny)."""
        try:
            action = value.get("action", "")
            request_id = value.get("id", "")
            if request_id and action in ("allow", "deny"):
                logger.info(f"Permission {action}: {request_id}")
                resolve_permission(request_id, action)
        except Exception as e:
            logger.error(f"Error handling card action: {e}", exc_info=True)

    def _handle_text_message(self, text_blocks: List[str]):
        """Handle new assistant text message from JSONL monitor."""
        try:
            self._heartbeat_start = None
            self._accumulated_text.extend(text_blocks)
            card = format_assistant_reply(self._accumulated_text)
            self.card_manager.send_or_update(card)
            # Do NOT finalize here — wait for on_turn_end (next human message)
            self._schedule_menu_detection()
        except Exception as e:
            logger.error(f"Error handling text message: {e}", exc_info=True)

    def _handle_tool_use(self, tools: List[Dict[str, str]]):
        """Handle tool use notification from JSONL monitor."""
        try:
            card = format_tool_use_notification(tools)
            self.card_manager.send_standalone(card)
            # Tool use may trigger permission menus (Edit/Bash/Write confirmation)
            self._schedule_menu_detection()
        except Exception as e:
            logger.error(f"Error handling tool use: {e}", exc_info=True)

    def _handle_thinking(self, thinking_text: str):
        """Handle thinking block from JSONL."""
        try:
            card = format_thinking_notification(thinking_text)
            self.card_manager.send_or_update(card)
        except Exception as e:
            logger.error(f"Error handling thinking: {e}", exc_info=True)

    def _handle_heartbeat(self):
        """Handle heartbeat — file changed but no sendable content."""
        try:
            if self._heartbeat_start is None:
                self._heartbeat_start = time.time()
            elapsed = int(time.time() - self._heartbeat_start)
            card = format_heartbeat(elapsed)
            self.card_manager.send_or_update(card)
        except Exception as e:
            logger.error(f"Error handling heartbeat: {e}", exc_info=True)

    def _handle_turn_end(self):
        """Handle turn boundary — human message appeared, finalize the card."""
        try:
            self.card_manager.finalize()
            self._heartbeat_start = None
            self._accumulated_text.clear()
        except Exception as e:
            logger.error(f"Error handling turn end: {e}", exc_info=True)

    def _ensure_claude_running(self):
        """Check if Claude Code is running in tmux; if not, auto-resume."""
        if self.tmux.is_claude_running():
            return
        logger.warning("Claude Code not running — auto-resuming")
        self.feishu.send_card(
            format_status_notification("Claude Code exited, auto-resuming...", "yellow")
        )
        self.tmux.restart_claude(resume=True)
        # Wait for Claude to start up
        for _ in range(15):
            time.sleep(2)
            if self.tmux.is_claude_running():
                logger.info("Claude Code resumed successfully")
                self.feishu.send_card(
                    format_status_notification("Claude Code resumed. Ready.")
                )
                return
        logger.error("Claude Code failed to resume after 30s")
        self.feishu.send_card(
            format_status_notification("Claude Code failed to resume. Check manually.", "red")
        )

    def _schedule_menu_detection(self):
        """Start background thread to detect selection menus."""
        def detect_menu():
            try:
                time.sleep(2.0)
                for attempt in range(5):
                    menu_data = self.tmux.detect_selection_menu()
                    if menu_data and menu_data.get("is_menu"):
                        logger.info(f"Menu detected: {len(menu_data['options'])} options")
                        with self._menu_lock:
                            self._state = STATE_WAITING_SELECTION
                            self._menu_options = menu_data["options"]
                        card = format_selection_menu(menu_data["options"])
                        self.feishu.send_card(card)
                        break
                    if attempt < 4:
                        time.sleep(2.0)
            except Exception as e:
                logger.error(f"Error in menu detection: {e}", exc_info=True)

        thread = threading.Thread(target=detect_menu, daemon=True)
        thread.start()


def main():
    parser = argparse.ArgumentParser(description="Feishu ↔ Claude Code Bridge")
    parser.add_argument("--tmux-session", required=True, help="tmux session name")
    parser.add_argument("--tmux-window", default=None, help="tmux window index/name")
    parser.add_argument("--session-file", default=None, help="Path to Claude Code session JSONL file")
    parser.add_argument("--exclude-session", action="append", default=[], help="Session UUID(s) to exclude from auto-detect (repeatable)")
    args = parser.parse_args()

    bridge = Bridge(args.tmux_session, args.tmux_window, args.session_file, args.exclude_session)

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        bridge.monitor.stop()
        bridge.feishu.send_card(
            format_status_notification("Bridge disconnected.", "red")
        )
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    bridge.start()


if __name__ == "__main__":
    main()
