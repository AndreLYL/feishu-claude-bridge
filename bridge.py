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
from typing import Optional

from dotenv import load_dotenv

from feishu_client import FeishuClient
from formatter import (
    format_assistant_reply,
    format_permission_request,
    format_status_notification,
)
from hook_server import resolve_permission, start_hook_server
from session_monitor import SessionMonitor, find_latest_session
from tmux_controller import TmuxController

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("bridge")


class Bridge:
    def __init__(self, tmux_session: str, tmux_window: Optional[str] = None):
        self.tmux = TmuxController(tmux_session, tmux_window)

        # Feishu client
        self.feishu = FeishuClient(
            app_id=os.environ["FEISHU_APP_ID"],
            app_secret=os.environ["FEISHU_APP_SECRET"],
            allowed_chat_id=os.environ["ALLOWED_CHAT_ID"],
            on_message=self._handle_feishu_message,
            on_card_action=self._handle_card_action,
        )

        # Find active session JSONL
        jsonl_path = find_latest_session()
        if not jsonl_path:
            logger.error("No active Claude Code session found")
            sys.exit(1)
        logger.info(f"Monitoring session: {jsonl_path.name}")

        # Session monitor
        self.monitor = SessionMonitor(
            jsonl_path=jsonl_path,
            on_assistant_message=self._handle_assistant_message,
        )

        self.hook_port = int(os.environ.get("HOOK_SERVER_PORT", "19280"))

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
        card = format_permission_request(tool_name, tool_input, request_id)
        self.feishu.send_card(card)

    def _handle_feishu_message(self, text: str):
        """Handle text message from Feishu."""
        logger.info(f"Feishu → tmux: {text[:80]}")

        # Handle bridge commands
        if text == "/esc":
            self.tmux.send_key("Escape")
            return
        if text == "/screenshot":
            screenshot = self.tmux.capture_pane(50)
            self.feishu.send_text(f"```\n{screenshot}\n```")
            return

        # Forward everything else to tmux as text input
        self.tmux.send_text(text)

    def _handle_card_action(self, value: dict):
        """Handle Feishu card button click (Allow/Deny)."""
        action = value.get("action", "")
        request_id = value.get("id", "")
        if request_id and action in ("allow", "deny"):
            logger.info(f"Permission {action}: {request_id}")
            resolve_permission(request_id, action)

    def _handle_assistant_message(self, text_blocks: list[str]):
        """Handle new assistant message from JSONL monitor."""
        card = format_assistant_reply(text_blocks)
        self.feishu.send_card(card)


def main():
    parser = argparse.ArgumentParser(description="Feishu ↔ Claude Code Bridge")
    parser.add_argument("--tmux-session", required=True, help="tmux session name")
    parser.add_argument("--tmux-window", default=None, help="tmux window index/name")
    args = parser.parse_args()

    bridge = Bridge(args.tmux_session, args.tmux_window)

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
