import json
import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
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
        on_image: Optional[Callable[[str], None]] = None,
        image_dir: Optional[str] = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.allowed_chat_id = allowed_chat_id
        self.on_message = on_message
        self.on_card_action = on_card_action
        self.on_image = on_image
        self._image_dir = Path(image_dir or "/tmp/feishu-bridge-images")
        self._image_dir.mkdir(parents=True, exist_ok=True)
        self._seen_msg_ids: OrderedDict[str, None] = OrderedDict()
        self._seen_msg_ids_max = 200

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

        # Dedup: skip already-seen messages (WebSocket reconnect can re-deliver)
        msg_id = msg.message_id
        if msg_id in self._seen_msg_ids:
            logger.info(f"Skipping duplicate message: {msg_id}")
            return
        self._seen_msg_ids[msg_id] = None
        if len(self._seen_msg_ids) > self._seen_msg_ids_max:
            self._seen_msg_ids.popitem(last=False)

        # Extract text content
        if msg.message_type == "text":
            content = json.loads(msg.content)
            text = content.get("text", "").strip()
            if text:
                self.on_message(text)
        elif msg.message_type == "post":
            content = json.loads(msg.content)
            text = self._extract_post_text(content)
            if text:
                self.on_message(text)
        elif msg.message_type == "image" and self.on_image:
            content = json.loads(msg.content)
            image_key = content.get("image_key", "")
            if image_key:
                local_path = self._download_image(msg.message_id, image_key)
                if local_path:
                    self.on_image(local_path)

    @staticmethod
    def _extract_post_text(content: dict) -> str:
        """Extract plain text from Feishu post (rich text) message."""
        lines = []
        # Post format: {"zh_cn": {"title": "...", "content": [[{"tag":"text","text":"..."},...]]}}
        for locale_data in content.values():
            if not isinstance(locale_data, dict):
                continue
            title = locale_data.get("title", "")
            if title:
                lines.append(title)
            for para in locale_data.get("content", []):
                parts = []
                for node in para:
                    if node.get("tag") == "text":
                        parts.append(node.get("text", ""))
                    elif node.get("tag") == "a":
                        parts.append(node.get("text", "") or node.get("href", ""))
                if parts:
                    lines.append("".join(parts))
            break  # Only process first locale
        return "\n".join(lines).strip()

    def _download_image(self, message_id: str, image_key: str) -> Optional[str]:
        """Download image from Feishu and save locally. Returns local file path."""
        try:
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()
            response = self.client.im.v1.message_resource.get(request)
            if not response.success():
                logger.error(f"Image download failed: {response.code} {response.msg}")
                return None

            local_path = self._image_dir / f"{image_key}.png"
            with open(local_path, "wb") as f:
                f.write(response.file.read())
            logger.info(f"Image saved: {local_path}")
            return str(local_path)
        except Exception as e:
            logger.error(f"Image download error: {e}", exc_info=True)
            return None

    def _handle_card(self, data) -> None:
        """Handle card action (button clicks)."""
        action = data.event.action
        value = json.loads(action.value) if isinstance(action.value, str) else action.value
        self.on_card_action(value)
