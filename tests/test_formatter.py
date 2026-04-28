import pytest
from formatter import (
    format_tool_use_notification,
    format_selection_menu,
    format_thinking_notification,
    format_heartbeat,
    format_permission_request,
)


def test_single_tool_use():
    tools = [{"name": "Read", "input_summary": "file_path: /tmp/foo.py"}]
    card = format_tool_use_notification(tools)
    assert card["card"]["header"]["template"] == "purple"
    assert "Read" in card["card"]["elements"][0]["content"]


def test_multiple_tool_uses_merged():
    tools = [
        {"name": "Read", "input_summary": "file_path: /a.py"},
        {"name": "Read", "input_summary": "file_path: /b.py"},
        {"name": "Grep", "input_summary": "pattern: foo"},
    ]
    card = format_tool_use_notification(tools)
    content = card["card"]["elements"][0]["content"]
    assert "Read" in content
    assert "2" in content  # count indicator
    assert "Grep" in content


def test_input_summary_truncated():
    tools = [{"name": "Read", "input_summary": "x" * 300}]
    card = format_tool_use_notification(tools)
    content = card["card"]["elements"][0]["content"]
    assert len(content) < 400


def test_selection_menu_basic():
    options = [
        "一个 orchestrator 同时控制多个 Claude Code 实例",
        "手动开多个终端窗口各跑一个 Claude Code",
        "扩展 Feishu Bridge 支持多 session",
    ]
    card = format_selection_menu(options)
    assert card["card"]["header"]["template"] == "yellow"
    content = card["card"]["elements"][0]["content"]
    assert "1." in content
    assert "2." in content
    assert "3." in content
    assert "回复数字选择" in content


def test_selection_menu_empty():
    card = format_selection_menu([])
    content = card["card"]["elements"][0]["content"]
    assert "回复数字选择" in content


def test_format_thinking_truncates_at_200():
    text = "x" * 300
    card = format_thinking_notification(text)
    content = card["card"]["elements"][0]["content"]
    assert len(content) <= 210
    assert card["card"]["header"]["template"] == "grey"
    assert "Thinking" in card["card"]["header"]["title"]["content"]


def test_format_thinking_short_text():
    card = format_thinking_notification("analyzing the code")
    content = card["card"]["elements"][0]["content"]
    assert content == "analyzing the code"


def test_format_heartbeat():
    card = format_heartbeat(12)
    content = card["card"]["elements"][0]["content"]
    assert "12s" in content
    assert card["card"]["header"]["template"] == "grey"


def test_format_permission_request_no_buttons():
    card = format_permission_request("Bash", "command=git push", "req-123")
    elements = card["card"]["elements"]
    for el in elements:
        assert el["tag"] != "action", "Permission card should not have buttons in V2"
    content = elements[0]["content"]
    assert "y" in content.lower()
    assert "Bash" in content
