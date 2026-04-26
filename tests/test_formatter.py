import pytest
from formatter import format_tool_use_notification


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
