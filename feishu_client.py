import json
import logging
from typing import Callable

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)

logger = logging.getLogger("bridge.feishu")


class FeishuClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        allowed_chat_id: str,
        on_message: Callable[[str], None],
        on_card_action: Callable[[dict], None],
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.allowed_chat_id = allowed_chat_id
        self.on_message = on_message
        self.on_card_action = on_card_action

        # Build event handler
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message)
            .build()
        )

        # Card action handler
        card_handler = lark.CardActionHandler.builder("").register(self._handle_card).build()

        # Build client with WebSocket mode
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

        self.ws_client = (
            lark.ws.Client(app_id, app_secret, event_handler=event_handler, card_handler=card_handler)
        )

    def start(self):
        """Start WebSocket connection (blocking)."""
        logger.info("Connecting to Feishu WebSocket...")
        self.ws_client.start()

    def send_card(self, card: dict) -> None:
        """Send an interactive card message to the allowed chat."""
        body = CreateMessageRequestBody.builder() \
            .msg_type("interactive") \
            .receive_id(self.allowed_chat_id) \
            .content(json.dumps(card["card"])) \
            .build()

        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()

        response = self.client.im.v1.message.create(request)
        if not response.success():
            logger.error(f"Send failed: {response.code} {response.msg}")

    def send_text(self, text: str) -> None:
        """Send a plain text message."""
        content = json.dumps({"text": text})
        body = CreateMessageRequestBody.builder() \
            .msg_type("text") \
            .receive_id(self.allowed_chat_id) \
            .content(content) \
            .build()

        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()

        response = self.client.im.v1.message.create(request)
        if not response.success():
            logger.error(f"Send failed: {response.code} {response.msg}")

    def _handle_message(self, data: P2ImMessageReceiveV1) -> None:
        """Handle incoming message event."""
        event = data.event
        msg = event.message

        # Security: only accept messages from allowed chat
        if msg.chat_id != self.allowed_chat_id:
            logger.warning(f"Rejected message from chat {msg.chat_id}")
            return

        # Extract text content
        if msg.message_type == "text":
            content = json.loads(msg.content)
            text = content.get("text", "").strip()
            if text:
                self.on_message(text)

    def _handle_card(self, data) -> None:
        """Handle card action (button clicks)."""
        action = data.event.action
        value = json.loads(action.value) if isinstance(action.value, str) else action.value
        self.on_card_action(value)
