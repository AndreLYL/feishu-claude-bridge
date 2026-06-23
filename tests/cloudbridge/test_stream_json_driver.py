"""Tests for StreamJsonDriver — offline, driven by fakeclaude.py fixture replay."""
import json
import os
import sys
import asyncio
import signal
import types
from pathlib import Path

import pytest
from cloudbridge import events
from cloudbridge.stream_json_driver import StreamJsonDriver

FIXTURES = Path(__file__).parent / "fixtures"

_REPO_ROOT = str(Path(__file__).parent.parent.parent)


def _fixture(tmp_path: Path, lines: list) -> Path:
    """Write a JSONL fixture file and return its path."""
    fix = tmp_path / "fixture.jsonl"
    fix.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return fix


def _argv(fixture_path: Path):
    """Return (argv, env) for fakeclaude.py pointing at fixture_path."""
    argv = [sys.executable, "scripts/fakeclaude.py"]
    env = {**os.environ, "FAKE_FIXTURE": str(fixture_path)}
    return argv, env


def _driver(fixture_path, session_id="S1"):
    argv = [sys.executable, "scripts/fakeclaude.py"]
    env = {**os.environ, "FAKE_FIXTURE": str(fixture_path)}
    return StreamJsonDriver(
        name="main",
        argv=argv,
        cwd=_REPO_ROOT,
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


# ---------------------------------------------------------------------------
# Test 5: answer_permission writes correct control_response JSON to stdin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_answer_permission_writes_control_response_to_stdin():
    """Unit test: verify answer_permission writes the correct control_response
    JSON (with request_id and decision) to stdin without needing a real subprocess."""
    # Construct a StreamJsonDriver instance (no subprocess start)
    driver = StreamJsonDriver(
        name="test",
        argv=["dummy"],
        cwd="/tmp",
        session_id="test-sid",
    )

    # Stub stdin with a recorder
    written_lines = []

    async def stub_drain():
        """No-op async drain."""
        pass

    class StubStdin:
        def write(self, data: bytes) -> None:
            """Record written bytes as decoded string."""
            written_lines.append(data.decode("utf-8"))

        async def drain(self) -> None:
            """Async drain (no-op)."""
            await stub_drain()

    # Replace the process mock with a stub stdin
    driver._proc = types.SimpleNamespace(stdin=StubStdin())

    # Test 1: answer_permission with allow=True
    await driver.answer_permission("req-123", allow=True)
    assert len(written_lines) == 1, f"Expected 1 write, got {len(written_lines)}"
    line = written_lines[0].strip()
    obj = json.loads(line)
    assert obj["type"] == "control_response"
    assert obj["request_id"] == "req-123"
    assert obj["decision"] == "allow"

    # Test 2: answer_permission with allow=False
    written_lines.clear()
    await driver.answer_permission("req-456", allow=False)
    assert len(written_lines) == 1, f"Expected 1 write, got {len(written_lines)}"
    line = written_lines[0].strip()
    obj = json.loads(line)
    assert obj["type"] == "control_response"
    assert obj["request_id"] == "req-456"
    assert obj["decision"] == "deny"


# ---------------------------------------------------------------------------
# Test 6: close() kills the entire process group
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_terminates_process_group(tmp_path):
    """close() must kill the subprocess (and its process group) completely."""
    fix = _fixture(tmp_path, [
        {"type": "system", "subtype": "init", "session_id": "S4"},
        {"type": "result", "subtype": "success", "result": "x",
         "session_id": "S4", "usage": {}, "cost_usd": 0.0},
    ])
    argv, env = _argv(fix)
    d = StreamJsonDriver("main", argv, cwd=_REPO_ROOT, session_id="S4", env=env)
    await d.start()
    pid = d._proc.pid
    await d.close(grace_stop=0.2, grace_term=0.5)
    # The process must be gone
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# ---------------------------------------------------------------------------
# Test 7: supervise() detects a restart storm and marks driver.failed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_storm_marks_failed(tmp_path):
    """supervise() must detect a restart storm and mark driver.failed=True.

    We use an empty fixture so fakeclaude has no output to replay.  To make
    each subprocess instance exit quickly (simulating a crash), supervise() is
    wrapped in a task and we close each subprocess's stdin after a brief delay
    via a helper coroutine that triggers EOF on fakeclaude's stdin.
    """
    fix = tmp_path / "empty.jsonl"
    fix.write_text("")
    argv, env = _argv(fix)
    d = StreamJsonDriver("main", argv, cwd=_REPO_ROOT, session_id="S5", env=env)
    crashes: list = []
    await d.start()

    async def _drain_stdin():
        """Close each subprocess stdin quickly so fakeclaude exits (EOF crash)."""
        while not d.failed:
            proc = d._proc
            if proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            await asyncio.sleep(0.02)

    drain_task = asyncio.create_task(_drain_stdin())
    try:
        await asyncio.wait_for(
            d.supervise(
                on_event=lambda e: crashes.append(e),
                max_restarts=3,
                window_s=60,
                backoff_base=0.01,
                _test_max_loops=5,
            ),
            timeout=10.0,
        )
    finally:
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

    assert d.failed is True, "Expected driver.failed to be True after restart storm"
    assert any(isinstance(e, events.SessionCrashed) for e in crashes), (
        f"Expected a SessionCrashed event, got: {crashes}"
    )
