"""End-to-end integration test: FakeClaude + FakeFeishu, no real claude / real Feishu.

Wire-up:
  - scripts/fakeclaude.py plays back a fixture on each user message written to stdin.
  - FakeFeishu records send_card / update_card calls.
  - Engine + FeishuGateway assembled via cloudbridge.app.build.
  - StreamJsonDriver points at fakeclaude subprocess.
  - gateway.on_inbound() triggers one full request-reply round-trip.
  - Assertions verify TextDelta rendered, TurnResult received, placeholder card sent.
"""

import asyncio
import os
import sys
import pytest

from cloudbridge import events
from cloudbridge.engine import Engine
from cloudbridge.gateway import FeishuGateway
from cloudbridge.health import HealthModel
from cloudbridge.inbound_filter import InboundFilter
from cloudbridge.stream_json_driver import StreamJsonDriver


# ---------------------------------------------------------------------------
# FakeFeishu stub
# ---------------------------------------------------------------------------

class FakeFeishu:
    """Records send_card / update_card calls; returns synthetic message ids."""

    def __init__(self):
        self.sends = []
        self.updates = []

    def send_card(self, card):
        self.sends.append(card)
        return f"fake-mid-{len(self.sends)}"

    def update_card(self, mid, card):
        self.updates.append((mid, card))
        return True


# ---------------------------------------------------------------------------
# End-to-end test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_message_to_rendered_reply_end_to_end(tmp_path):
    """Full round-trip: user text → FakeClaude → Engine → FeishuGateway."""
    # Use the real recorded fixture (turn_text.jsonl contains a TurnStarted-
    # triggering isReplay user line, a TextDone assistant line, and a result).
    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "turn_text.jsonl"
    )
    # Resolve project root so relative paths work regardless of test invocation cwd.
    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    fakeclaude = os.path.join(_project_root, "scripts", "fakeclaude.py")

    env = {**os.environ, "FAKE_FIXTURE": fixture}
    argv = [sys.executable, fakeclaude]

    loop = asyncio.get_running_loop()
    fk = FakeFeishu()

    # Assemble via app.build; start_ts=0.0 so the filter passes all messages.
    from cloudbridge import app
    eng, gw, health = app.build(
        loop, fk, cwd=_project_root, start_ts=0.0
    )

    # Collect all Engine events in a flat list for assertions.
    rendered: list = []
    eng.on_event = lambda e: (rendered.append(e), asyncio.create_task(gw.render(e)))[1]  # type: ignore[func-returns-value]

    drv = StreamJsonDriver(
        "main", argv, cwd=_project_root, session_id="S", env=env
    )
    await drv.start()
    eng.add_session("main", drv, active=True)
    await eng.start()

    # Trigger one inbound message (create_time_ms=10_000 > watermark=0).
    gw.on_inbound("m1", create_time_ms=10_000, text="hi")

    # Wait up to ~2.5 s for TurnResult.
    for _ in range(50):
        await asyncio.sleep(0.05)
        if any(isinstance(e, events.TurnResult) for e in rendered):
            break

    # Tear down gracefully.
    await eng.stop()
    await drv.close(grace_stop=0.2, grace_term=0.5)
    await gw.aclose()

    # Assertions -----------------------------------------------------------------
    # 1. A TextDone (assistant reply) was received.
    assert any(isinstance(e, events.TextDone) for e in rendered), (
        f"Expected TextDone in events; got: {[type(e).__name__ for e in rendered]}"
    )

    # 2. A TurnResult closed the turn.
    assert any(isinstance(e, events.TurnResult) for e in rendered), (
        f"Expected TurnResult in events; got: {[type(e).__name__ for e in rendered]}"
    )

    # 3. FeishuGateway sent at least one placeholder card (TurnStarted path).
    assert fk.sends, (
        "Expected FakeFeishu.send_card to have been called at least once"
    )
