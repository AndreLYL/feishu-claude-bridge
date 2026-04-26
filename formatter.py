import json

# Feishu interactive card message limit is ~30000 chars.
# Truncate long messages to stay safe.
MAX_TEXT_LENGTH = 4000
TRUNCATION_NOTICE = "\n\n---\n*（输出过长，已截断。发 /screenshot 查看完整终端）*"


def format_assistant_reply(text_blocks: list[str]) -> dict:
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
