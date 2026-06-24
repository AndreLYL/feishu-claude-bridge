from cloudbridge import events
import dataclasses
import pytest


def test_events_carry_session_and_are_immutable():
    """Test that events carry session and are frozen."""
    e = events.TextDelta(session="main", text="hi")
    assert e.session == "main"
    assert e.text == "hi"
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.text = "x"


def test_turn_result_fields():
    """Test that TurnResult has required fields."""
    r = events.TurnResult(
        session="main",
        usage={"input_tokens": 5},
        total_cost_usd=0.01,
        duration_ms=1200,
        num_turns=2,
    )
    assert r.total_cost_usd == 0.01
    assert r.duration_ms == 1200
    assert r.num_turns == 2
    assert r.usage["input_tokens"] == 5


def test_text_delta_immutability():
    """Test that TextDelta is truly immutable."""
    e = events.TextDelta(session="s1", text="hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.session = "s2"


def test_turn_started():
    """Test TurnStarted event."""
    e = events.TurnStarted(session="s1", user_text="What is AI?")
    assert e.session == "s1"
    assert e.user_text == "What is AI?"


def test_text_done():
    """Test TextDone event."""
    e = events.TextDone(session="s1", full_text="AI is artificial intelligence.")
    assert e.session == "s1"
    assert e.full_text == "AI is artificial intelligence."


def test_thinking():
    """Test Thinking event."""
    e = events.Thinking(session="s1", text="Let me think...")
    assert e.session == "s1"
    assert e.text == "Let me think..."


def test_tool_use():
    """Test ToolUse event."""
    e = events.ToolUse(session="s1", name="get_weather", input={"city": "NYC"})
    assert e.session == "s1"
    assert e.name == "get_weather"
    assert e.input == {"city": "NYC"}


def test_tool_result():
    """Test ToolResult event."""
    e = events.ToolResult(
        session="s1",
        tool_use_id="tool-123",
        content="Weather: sunny",
        is_error=False,
    )
    assert e.session == "s1"
    assert e.tool_use_id == "tool-123"
    assert e.content == "Weather: sunny"
    assert e.is_error is False


def test_permission_request():
    """Test PermissionRequest event."""
    e = events.PermissionRequest(
        session="s1",
        request_id="req-123",
        tool_name="exec",
        input={"cmd": "ls"},
        description="Run ls command",
    )
    assert e.session == "s1"
    assert e.request_id == "req-123"
    assert e.tool_name == "exec"
    assert e.input == {"cmd": "ls"}
    assert e.description == "Run ls command"


def test_turn_cancelled():
    """Test TurnCancelled event."""
    e = events.TurnCancelled(session="s1")
    assert e.session == "s1"


def test_session_recovered():
    """Test SessionRecovered event."""
    e = events.SessionRecovered(session="s1")
    assert e.session == "s1"


def test_session_crashed():
    """Test SessionCrashed event."""
    e = events.SessionCrashed(session="s1", restarts=3)
    assert e.session == "s1"
    assert e.restarts == 3
