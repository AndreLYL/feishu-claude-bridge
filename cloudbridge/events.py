"""Internal event model for feishu-claude-bridge.

This module defines frozen dataclasses for events that flow between the engine
and rendering layers, providing a clean decoupling boundary.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class _Event:
    """Base event class carrying session identifier."""

    session: str


@dataclass(frozen=True)
class TurnStarted(_Event):
    """Event emitted when a turn starts with user text."""

    user_text: str


@dataclass(frozen=True)
class TextDelta(_Event):
    """Event emitted for each incremental text token."""

    text: str


@dataclass(frozen=True)
class TextDone(_Event):
    """Event emitted when all text for a turn is complete."""

    full_text: str


@dataclass(frozen=True)
class Thinking(_Event):
    """Event emitted for model thinking/reasoning text."""

    text: str


@dataclass(frozen=True)
class ToolUse(_Event):
    """Event emitted when a tool is invoked."""

    name: str
    input: dict


@dataclass(frozen=True)
class ToolResult(_Event):
    """Event emitted with the result of a tool invocation."""

    tool_use_id: str
    content: str
    is_error: bool


@dataclass(frozen=True)
class PermissionRequest(_Event):
    """Event emitted when a tool requires user permission."""

    request_id: str
    tool_name: str
    input: dict
    description: str


@dataclass(frozen=True)
class TurnResult(_Event):
    """Event emitted at the end of a turn with aggregated metrics."""

    usage: dict = field(default_factory=dict)
    total_cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0


@dataclass(frozen=True)
class TurnCancelled(_Event):
    """Event emitted when a turn is cancelled by user."""

    pass


@dataclass(frozen=True)
class SessionRecovered(_Event):
    """Event emitted when a session is recovered from an error."""

    pass


@dataclass(frozen=True)
class SessionCrashed(_Event):
    """Event emitted when a session crashes."""

    restarts: int
