import json
from unittest.mock import MagicMock, patch
from feishu_client import FeishuClient


def _make_client():
    """Create a FeishuClient with mocked internals."""
    with patch("lark_oapi.Client.builder") as mock_builder:
        mock_client_instance = MagicMock()
        mock_builder.return_value.app_id.return_value.app_secret.return_value.build.return_value = mock_client_instance
        with patch("lark_oapi.ws.Client"):
            client = FeishuClient(
                app_id="test_id",
                app_secret="test_secret",
                allowed_chat_id="test_chat",
                on_message=lambda *a: None,
                on_card_action=lambda v: None,
            )
    return client


def test_send_card_returns_message_id():
    client = _make_client()
    mock_response = MagicMock()
    mock_response.success.return_value = True
    mock_response.data.message_id = "msg_abc123"
    client.client.im.v1.message.create.return_value = mock_response

    card = {
        "card": {
            "header": {"title": {"tag": "plain_text", "content": "Test"}, "template": "blue"},
            "elements": [{"tag": "markdown", "content": "hello"}],
        }
    }
    result = client.send_card(card)
    assert result == "msg_abc123"


def test_send_card_returns_none_on_failure():
    client = _make_client()
    mock_response = MagicMock()
    mock_response.success.return_value = False
    mock_response.code = 99999
    mock_response.msg = "error"
    client.client.im.v1.message.create.return_value = mock_response

    card = {
        "card": {
            "header": {"title": {"tag": "plain_text", "content": "Test"}, "template": "blue"},
            "elements": [{"tag": "markdown", "content": "hello"}],
        }
    }
    result = client.send_card(card)
    assert result is None


def test_update_card_success():
    client = _make_client()
    mock_response = MagicMock()
    mock_response.success.return_value = True
    client.client.im.v1.message.patch.return_value = mock_response

    card_content = {
        "header": {"title": {"tag": "plain_text", "content": "Updated"}, "template": "blue"},
        "elements": [{"tag": "markdown", "content": "new content"}],
    }
    result = client.update_card("msg_abc123", card_content)
    assert result is True
    client.client.im.v1.message.patch.assert_called_once()


def test_update_card_failure():
    client = _make_client()
    mock_response = MagicMock()
    mock_response.success.return_value = False
    mock_response.code = 99999
    mock_response.msg = "error"
    client.client.im.v1.message.patch.return_value = mock_response

    result = client.update_card("msg_abc123", {})
    assert result is False
