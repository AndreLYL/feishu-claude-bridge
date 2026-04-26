import json

# Feishu interactive card message limit is ~30000 chars.
# Truncate long messages to stay safe.
MAX_TEXT_LENGTH = 4000
TRUNCATION_NOTICE = "\n\n---\n*（输出过长，已截断。发 /screenshot 查看完整终端）*"


def format_assistant_reply(text_blocks) -> dict:
    """Convert assistant text blocks to a Feishu card message."""
    combined = "\n\n".join(text_blocks)

    if len(combined) > MAX_TEXT_LENGTH:
        combined = combined[:MAX_TEXT_LENGTH] + TRUNCATION_NOTICE

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "Claude Code"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": combined,
                }
            ],
        },
    }
    return card


def format_permission_request(tool_name: str, tool_input_summary: str, request_id: str) -> dict:
    """Create an interactive card with Allow/Deny buttons for permission."""
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "Permission Request"},
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**Tool:** `{tool_name}`\n**Input:** {tool_input_summary[:500]}",
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "Allow"},
                            "type": "primary",
                            "value": json.dumps({"action": "allow", "id": request_id}),
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "Deny"},
                            "type": "danger",
                            "value": json.dumps({"action": "deny", "id": request_id}),
                        },
                    ],
                },
            ],
        },
    }
    return card


def format_status_notification(message: str, color: str = "green") -> dict:
    """Simple status card (session ended, error, etc.)."""
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "Bridge Status"},
                "template": color,
            },
            "elements": [
                {"tag": "markdown", "content": message},
            ],
        },
    }
    return card


def format_tool_use_notification(tools) -> dict:
    """Create a purple notification card for tool usage.

    Args:
        tools: List of dicts with keys 'name' and 'input_summary'

    Returns:
        Feishu interactive card message dict
    """
    from typing import Dict, List
    from collections import Counter

    # Count tool uses by name
    tool_counts = Counter(tool["name"] for tool in tools)

    # Build markdown content
    lines = []
    lines.append("**Tools Used:**\n")

    # Track which tools we've already displayed
    seen_tools = set()

    for tool in tools:
        tool_name = tool["name"]

        # Skip if we've already shown this tool
        if tool_name in seen_tools:
            continue
        seen_tools.add(tool_name)

        count = tool_counts[tool_name]
        input_summary = tool["input_summary"][:200]  # Truncate to 200 chars

        # Format with count if > 1
        if count > 1:
            lines.append(f"- `{tool_name}` × {count}")
        else:
            lines.append(f"- `{tool_name}`: {input_summary}")

    content = "\n".join(lines)

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "Tools Running"},
                "template": "purple",
            },
            "elements": [
                {"tag": "markdown", "content": content},
            ],
        },
    }
    return card
