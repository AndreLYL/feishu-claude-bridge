"""Tests for StreamJsonDriver — offline, driven by fakeclaude.py fixture replay."""
import json
import os
import sys
import asyncio
from pathlib import Path

import pytest
from cloudbridge import events
from cloudbridge.stream_json_driver import StreamJsonDriver

FIXTURES = Path(__file__).parent / "fixtures"


def _driver(fixture_path, session_id="S1"):
    argv = [sys.executable, "scripts/fakeclaude.py"]
    env = {**os.environ, "FAKE_FIXTURE": str(fixture_path)}
    return StreamJsonDriver(
        name="main",
        argv=argv,
        cwd=str(Path(__file__).parent.parent.parent),  # repo root
        session_id=session_id,
        env=env,
    )


async def _collect_until_result(driver, timeout=5):
    """Collect events until TurnResult, then return them."""
    got = []

    async def collect():
        async for e in driver.events():
            got.append(e)
            if isinstance(e, events.TurnResult):
                return

    await asyncio.wait_for(collect(), timeout=timeout)
    return got


# ---------------------------------------------------------------------------
# Test 1: turn_text_with_partial.jsonl — deltas, TextDone, TurnResult
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_turn_with_partial_yields_delta_done_result():
    """Loading turn_text_with_partial.jsonl produces TurnStarted, at least one
    TextDelta, a TextDone, and exactly one TurnResult with cost > 0 and num_turns >= 1."""
    fixture = FIXTURES / "turn_text_with_partial.jsonl"
    driver = _driver(fixture, session_id="3cc05ae4-8333-4777-a510-1eced83f7858")
    await driver.start()
    try:
        await driver.send("Reply with exactly one word: Hello")
        got = await _collect_until_result(driver)
    finally:
        await driver.close()

    turn_started = [e for e in got if isinstance(e, events.TurnStarted)]
    deltas = [e for e in got if isinstance(e, events.TextDelta)]
    done = [e for e in got if isinstance(e, events.TextDone)]
    results = [e for e in got if isinstance(e, events.TurnResult)]

    assert len(turn_started) >= 1, "Expected at least one TurnStarted"
    assert len(deltas) >= 1, f"Expected at least one TextDelta, got {got}"
    assert len(done) >= 1, f"Expected at least one TextDone, got {got}"
    assert len(results) == 1, f"Expected exactly one TurnResult, got {results}"
    assert results[0].total_cost_usd > 0, "Expected total_cost_usd > 0"
    assert results[0].num_turns >= 1, "Expected num_turns >= 1"


# ---------------------------------------------------------------------------
# Test 2: turn_text.jsonl — no partials, TextDone and TurnResult
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_turn_no_partial_yields_done_and_result():
    """Loading turn_text.jsonl (no stream_event deltas) produces a TextDone
    and exactly one TurnResult."""
    fixture = FIXTURES / "turn_text.jsonl"
    driver = _driver(fixture, session_id="d424d07a-c2b1-4f82-91e1-5854cb25fb74")
    await driver.start()
    try:
        await driver.send("Reply with exactly one word: Hello")
        got = await _collect_until_result(driver)
    finally:
        await driver.close()

    done = [e for e in got if isinstance(e, events.TextDone)]
    results = [e for e in got if isinstance(e, events.TurnResult)]

    assert len(done) >= 1, f"Expected at least one TextDone, got {got}"
    assert len(results) == 1, f"Expected exactly one TurnResult, got {results}"


# ---------------------------------------------------------------------------
# Test 3: C3 guard — compact result is ignored; only terminal result emits TurnResult
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compact_result_is_ignored_as_c3_guard(tmp_path):
    """A result with subtype 'compact' must NOT emit TurnResult.
    Only the terminal 'success' result should emit exactly one TurnResult."""
    c3_fixture = tmp_path / "c3.jsonl"
    lines = [
        {"type": "system", "subtype": "init", "session_id": "C3"},
        {"type": "result", "subtype": "compact", "session_id": "C3",
         "usage": {}, "total_cost_usd": 0.0, "duration_ms": 0, "num_turns": 0},
        {"type": "assistant", "session_id": "C3",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "after compact"}]}},
        {"type": "result", "subtype": "success", "session_id": "C3",
         "is_error": False, "duration_ms": 100, "num_turns": 1,
         "total_cost_usd": 0.001, "usage": {"input_tokens": 5, "output_tokens": 3}},
    ]
    c3_fixture.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    driver = _driver(c3_fixture, session_id="C3")
    await driver.start()
    try:
        await driver.send("trigger")
        got = await _collect_until_result(driver)
    finally:
        await driver.close()

    results = [e for e in got if isinstance(e, events.TurnResult)]
    assert len(results) == 1, (
        f"Expected exactly ONE TurnResult (compact ignored), got {len(results)}: {results}"
    )


# ---------------------------------------------------------------------------
# Test 4: permission.jsonl — PermissionRequest emitted with correct fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_permission_request_emitted_from_fixture():
    """Loading permission.jsonl emits a PermissionRequest with the correct
    request_id, tool_name='Write', and input containing 'file_path'."""
    fixture = FIXTURES / "permission.jsonl"
    driver = _driver(fixture, session_id="427590fa-fc31-4ecb-b689-a38e58bf7de3")
    await driver.start()
    try:
        await driver.send("write hi to /tmp/spike_test.txt")
        got = []

        async def collect_until_permission():
            async for e in driver.events():
                got.append(e)
                if isinstance(e, events.PermissionRequest):
                    return

        await asyncio.wait_for(collect_until_permission(), timeout=5)
    finally:
        await driver.close()

    perm = [e for e in got if isinstance(e, events.PermissionRequest)]
    assert len(perm) >= 1, f"Expected at least one PermissionRequest, got {got}"
    p = perm[0]
    assert p.request_id == "3199fb0f-25c5-41b1-87ab-dcefd1e4bfeb", (
        f"Unexpected request_id: {p.request_id}"
    )
    assert p.tool_name == "Write", f"Expected tool_name='Write', got {p.tool_name!r}"
    assert "file_path" in p.input, f"Expected 'file_path' in input, got {p.input}"
