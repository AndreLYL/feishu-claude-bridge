from unittest.mock import MagicMock
from bridge import CardManager


def _mock_feishu():
    feishu = MagicMock()
    feishu.send_card.return_value = "msg_001"
    feishu.update_card.return_value = True
    return feishu


def test_first_content_creates_card():
    feishu = _mock_feishu()
    cm = CardManager(feishu)
    cm.send_or_update({"card": {"header": {}, "elements": []}})
    feishu.send_card.assert_called_once()
    assert cm._active_card_id == "msg_001"


def test_second_content_patches_card():
    feishu = _mock_feishu()
    cm = CardManager(feishu)
    cm.send_or_update({"card": {"header": {}, "elements": [{"tag": "markdown", "content": "v1"}]}})
    cm.send_or_update({"card": {"header": {}, "elements": [{"tag": "markdown", "content": "v2"}]}})
    feishu.send_card.assert_called_once()
    feishu.update_card.assert_called_once_with(
        "msg_001",
        {"header": {}, "elements": [{"tag": "markdown", "content": "v2"}]}
    )


def test_finalize_resets_state():
    feishu = _mock_feishu()
    cm = CardManager(feishu)
    cm.send_or_update({"card": {"header": {}, "elements": []}})
    cm.finalize()
    assert cm._active_card_id is None
    cm.send_or_update({"card": {"header": {}, "elements": []}})
    assert feishu.send_card.call_count == 2


def test_send_standalone_always_creates():
    feishu = _mock_feishu()
    cm = CardManager(feishu)
    cm.send_or_update({"card": {"header": {}, "elements": []}})
    cm.send_standalone({"card": {"header": {}, "elements": [{"tag": "markdown", "content": "tool"}]}})
    assert feishu.send_card.call_count == 2
