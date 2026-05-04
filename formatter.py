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


def format_thinking_notification(thinking_text: str) -> dict:
    """Grey card showing truncated thinking content."""
    if len(thinking_text) > 200:
        thinking_text = thinking_text[:200] + "..."

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "Thinking..."},
                "template": "grey",
            },
            "elements": [
                {"tag": "markdown", "content": thinking_text},
            ],
        },
    }


def format_heartbeat(elapsed_seconds: int) -> dict:
    """Grey card showing working status with elapsed time."""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "Working..."},
                "template": "grey",
            },
            "elements": [
                {"tag": "markdown", "content": f"Claude is working... ({elapsed_seconds}s)"},
            ],
        },
    }


def format_permission_request(tool_name: str, tool_input_summary: str, request_id: str) -> dict:
    """Permission request card — text-based reply (no buttons, WebSocket doesn't support card callbacks)."""
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
                    "content": (
                        f"**Tool:** `{tool_name}`\n"
                        f"**Input:** {tool_input_summary[:500]}\n\n"
                        f"回复 **y** 允许，**n** 拒绝"
                    ),
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


def format_selection_menu(options) -> dict:
    """Create a yellow selection menu card with numbered options.

    Args:
        options: List of option strings to display

    Returns:
        Feishu interactive card message dict with yellow header
    """
    # Build numbered list
    lines = []
    for idx, option in enumerate(options, start=1):
        lines.append(f"{idx}. {option}")

    # Add instructions footer
    if lines:
        content = "\n".join(lines) + "\n\n---\n回复数字选择，发 /esc 取消"
    else:
        content = "回复数字选择，发 /esc 取消"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "Select Option"},
                "template": "yellow",
            },
            "elements": [
                {"tag": "markdown", "content": content},
            ],
        },
    }
    return card


def format_session_list(sessions, active_id: str) -> dict:
    """Create a blue card showing session list with active marker.

    Args:
        sessions: List of session dicts (each has: id, name, state, created_at)
        active_id: ID of the currently active session

    Returns:
        Feishu interactive card message dict
    """
    # Sort sessions by created_at (oldest first)
    sorted_sessions = sorted(sessions, key=lambda s: s["created_at"])

    # Build session list
    lines = []
    for idx, session in enumerate(sorted_sessions, start=1):
        marker = "▶" if session["id"] == active_id else "◻"
        # Format date as MM-DD HH:MM
        created = session["created_at"]
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(created)
            dt = dt.astimezone()  # convert to local timezone
            created = dt.strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            pass
        lines.append(
            f"{marker} {idx}. 📌 {session['name']} · {session['state']} · {created}"
        )

    # Build content
    if lines:
        content = "\n".join(lines) + "\n\n---\n/switch <n> 切换 · /delete <n> 删除"
    else:
        content = "No active sessions\n\n---\n/switch <n> 切换 · /delete <n> 删除"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📋 Sessions ({len(sessions)})"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": content},
            ],
        },
    }
    return card


def format_session_info(session: dict) -> dict:
    """Create a blue card showing detailed session information.

    Args:
        session: Session dict (id, name, state, created_at, tmux_window, jsonl_path)

    Returns:
        Feishu interactive card message dict
    """
    # Build content lines
    lines = [
        f"ID: {session['id']} | State: {session['state']}",
    ]

    # Add optional fields if present
    if "tmux_window" in session:
        lines.append(f"Window: {session['tmux_window']}")

    # Format date to local time
    created = session['created_at']
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(created)
        dt = dt.astimezone()
        created = dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        pass
    lines.append(f"Created: {created}")

    content = "\n".join(lines)

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📌 Session: {session['name']}"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": content},
            ],
        },
    }
    return card
