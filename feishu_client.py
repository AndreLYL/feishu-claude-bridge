import json
import logging
from typing import Callable, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
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
        card_handler = lark.CardActionHandler.builder("", "").register(self._handle_card).build()

        # Build client with WebSocket mode
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

        self.ws_client = (
            lark.ws.Client(app_id, app_secret, event_handler=event_handler)
        )

    def start(self):
        """Start WebSocket connection (blocking)."""
        logger.info("Connecting to Feishu WebSocket...")
        self.ws_client.start()

    def send_card(self, card: dict) -> Optional[str]:
        """Send an interactive card message. Returns message_id on success, None on failure."""
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
            return None
        return response.data.message_id

    def update_card(self, message_id: str, card: dict) -> bool:
        """PATCH update an already-sent card. Returns True on success."""
        body = PatchMessageRequestBody.builder() \
            .content(json.dumps(card)) \
            .build()

        request = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()

        response = self.client.im.v1.message.patch(request)
        if not response.success():
            logger.error(f"Card update failed: {response.code} {response.msg}")
            return False
        return True

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
